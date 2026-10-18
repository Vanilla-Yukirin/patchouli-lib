"""Master writes use real migrated SQLite, not an impersonated Agent identity."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import time

import pytest
from alembic import command as alembic
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.contracts import MasterDeletePageFormInput, MasterUpdatePageTitleInput
from patchouli_lib.admin.file_set_receipt_validation import (
    MasterFileSetReceiptCorruptError,
    validate_master_file_set_receipt,
)
from patchouli_lib.admin.file_set_receipts import (
    MasterFileSetReceiptRepository,
    MasterFileSetReceiptRow,
)
from patchouli_lib.admin.file_set_service import (
    MasterFileSetConflictError,
    MasterFileSetNotFoundError,
    MasterFileSetService,
)
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.models import Caller, MasterAuditEvent
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.backup import (
    BackupArtifactIdentity,
    BackupDatabaseError,
    create_backup,
    restore_backup,
    validate_database,
)
from patchouli_lib.backup.manifest import (
    MASTER_PAGE_DELETE_SCHEMA_REVISION,
)
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.file_set_write_service import FileSetAppendCommand
from patchouli_lib.content.models import Page, PageSource, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key

from .conftest import OPERATION_TIME, alembic_config
from .helpers import insert_page_graph, page_graph_values, seed_library_structure

_TOKEN = "synthetic master token material 0001"
_FILES = (("notes.md", b"# Synthetic\n"), ("image.bin", b"\x00\xff\x81"))


def _key(value: str = "synthetic-master-create") -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(value))


def _setup(engine: Engine) -> tuple[FileSetCreateCommand, MasterAdminSession]:
    library, section, book = seed_library_structure(engine)
    with immediate_transaction(engine) as connection:
        state = MasterTokenRepository(connection).initialize_from_local_cli(
            _TOKEN, now=OPERATION_TIME
        )
    session = MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic_csrf_material",
        identity_id=state.identity_id,
        session_generation=state.session_generation,
    )
    return FileSetCreateCommand(
        library_id=library,
        section_id=section,
        book_id=book,
        title="Synthetic document",
        occurred_at=OPERATION_TIME - 10,
        files=_FILES,
        source=ArchiveSourceInput(kind="synthetic"),
        request_id="req_" + "1" * 32,
    ), session


def _append(
    create: FileSetCreateCommand,
    page_id: str,
    etag: str,
    files: tuple[tuple[str, bytes], ...] = (("content.md", b"# Changed\n"),),
) -> FileSetAppendCommand:
    return FileSetAppendCommand(
        library_id=create.library_id,
        section_id=create.section_id,
        page_id=page_id,
        expected_etag=etag,
        files=files,
        source=create.source,
        request_id="req_" + "2" * 32,
    )


def _counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                Page,
                Revision,
                PageSource,
                MasterAuditEvent,
                MasterFileSetReceiptRow,
                Caller,
            )
        )


def test_create_full_snapshot_and_order_independent_frozen_replay(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    service = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
    result = service.create_page(create, _key(), master_session=session)
    replay = service.create_page(
        create.model_copy(update={"files": tuple(reversed(_FILES))}), _key(), master_session=session
    )
    assert result.receipt == replay.receipt and not result.replayed and replay.replayed
    assert [(item.name, item.content) for item in replay.manifest.files] == sorted(_FILES)
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)
    for change in (
        {"title": "Different"},
        {"files": (("notes.md", b"changed"),)},
        {"occurred_at": None},
        {"book_id": "a" * 32},
    ):
        with pytest.raises(MasterFileSetConflictError):
            service.create_page(create.model_copy(update=change), _key(), master_session=session)
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)


def test_revision_replay_precedes_etag_and_noop_records_no_activity(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    service = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
    first = service.create_page(create, _key(), master_session=session)
    append = _append(create, first.receipt.page_id, first.receipt.response_etag)
    second = service.revise_page(append, create.book_id, _key("revision"), master_session=session)
    assert second.receipt.revision_number == 2 and second.receipt.changed == 1
    assert second.receipt.original_page_updated_at == OPERATION_TIME + 1
    assert (
        service.revise_page(
            append, create.book_id, _key("revision"), master_session=session
        ).receipt
        == second.receipt
    )
    with pytest.raises(FileSetPreconditionFailedError):
        service.revise_page(append, create.book_id, _key("stale"), master_session=session)
    noop = _append(create, second.receipt.page_id, second.receipt.response_etag)
    unchanged = service.revise_page(noop, create.book_id, _key("no-op"), master_session=session)
    assert unchanged.receipt.changed == 0 and not unchanged.replayed
    assert unchanged.receipt.source_id is None and unchanged.receipt.master_audit_event_id is None
    assert unchanged.receipt.response_etag == second.receipt.response_etag
    assert _counts(content_engine) == (1, 2, 2, 2, 3, 0)


def test_later_title_and_delete_do_not_rewrite_replayed_result(
    content_engine: Engine, tmp_path: Path
) -> None:
    create, session = _setup(content_engine)
    service = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
    original = service.create_page(create, _key(), master_session=session)
    actions = AdminActionService(content_engine, clock=lambda: OPERATION_TIME + 20)
    actions.update_page_title_as_master(
        create.library_id,
        create.section_id,
        create.book_id,
        original.receipt.page_id,
        MasterUpdatePageTitleInput(title="Renamed", expected_updated_at=OPERATION_TIME),
        master_session=session,
    )
    noop_command = _append(
        create,
        original.receipt.page_id,
        _current_etag(content_engine, create.library_id, original.receipt.page_id),
        _FILES,
    )
    noop = service.revise_page(
        noop_command, create.book_id, _key("after-title-noop"), master_session=session
    )
    assert noop.receipt.original_page_updated_at == OPERATION_TIME + 20
    assert noop.receipt.changed == 0
    actions.delete_page_as_master(
        create.library_id,
        create.section_id,
        create.book_id,
        original.receipt.page_id,
        MasterDeletePageFormInput(expected_etag=noop.receipt.response_etag, confirm_delete="yes"),
        master_session=session,
    )
    for command_, key_ in ((create, _key()),):
        assert (
            service.create_page(command_, key_, master_session=session).receipt == original.receipt
        )
    assert (
        service.revise_page(
            noop_command, create.book_id, _key("after-title-noop"), master_session=session
        ).receipt
        == noop.receipt
    )
    with pytest.raises(MasterFileSetNotFoundError):
        service.revise_page(noop_command, create.book_id, _key("new-write"), master_session=session)
    with content_engine.connect() as connection:
        assert connection.scalar(select(Page.deleted_at)) is not None
        assert connection.scalar(select(Page.title)) == "Renamed"
    bundle = create_backup(
        content_engine,
        tmp_path / "bundle",
        app_version="0.1.0a0",
        artifact_identity=BackupArtifactIdentity("synthetic/source", "sha256:" + "1" * 64),
    )
    validate_database(bundle.database_path)
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "restored.sqlite", app_version="0.1.0a0"
    )
    validate_database(restored.destination_path)
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM admin_master_file_set_receipts"
        ).fetchone() == (2,)
        assert connection.execute("SELECT deleted_at FROM pages").fetchone()[0] is not None


def _current_etag(engine: Engine, library_id: str, page_id: str) -> str:
    from patchouli_lib.content.service import page_current_etag

    with engine.connect() as connection:
        page = ContentRepository(connection).get_page(library_id, page_id)
        assert page is not None
        return page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )


def test_token_rotation_denies_old_cookie_but_new_generation_replays(
    content_engine: Engine,
) -> None:
    create, session = _setup(content_engine)
    service = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
    result = service.create_page(create, _key(), master_session=session)
    with immediate_transaction(content_engine) as connection:
        state = MasterTokenRepository(connection).rotate(
            _TOKEN, _TOKEN + "-new", now=OPERATION_TIME + 1
        )
        assert state is not None
    with pytest.raises(AuthenticationError):
        service.create_page(create, _key(), master_session=session)
    new = MasterAdminSession(
        expires_at=session.expires_at,
        csrf_token="new-synthetic-csrf",
        identity_id=state.identity_id,
        session_generation=state.session_generation,
    )
    assert service.create_page(create, _key(), master_session=new).receipt == result.receipt
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)


@pytest.mark.parametrize("failure", ["source", "audit", "receipt", "commit-index"])
def test_all_write_failures_roll_back_complete_graph(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    create, session = _setup(content_engine)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic failure")

    if failure == "source":
        monkeypatch.setattr(ContentRepository, "add_source", fail)
    elif failure == "audit":
        monkeypatch.setattr(MasterAuditRepository, "add_success", fail)
    elif failure == "receipt":
        monkeypatch.setattr(MasterFileSetReceiptRepository, "add", fail)
    else:
        monkeypatch.setattr("patchouli_lib.search.index_v2.flush_dirty_since", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME).create_page(
            create, _key(), master_session=session
        )
    assert _counts(content_engine) == (0, 0, 0, 0, 0, 0)


def test_legacy_markdown_equal_files_are_noop_and_can_be_replayed(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    values = page_graph_values(
        library_id=create.library_id, section_id=create.section_id, book_id=create.book_id
    )
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
    page = values[0]
    files = (("content.md", values[1].content_md),)
    append = _append(
        create, page.page_id, _current_etag(content_engine, page.library_id, page.page_id), files
    )
    service = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
    result = service.revise_page(append, create.book_id, _key(), master_session=session)
    assert result.receipt.changed == 0 and result.receipt.revision_number == 1
    assert (
        service.revise_page(append, create.book_id, _key(), master_session=session).receipt
        == result.receipt
    )
    assert _counts(content_engine) == (1, 1, 1, 0, 1, 0)


def test_receipts_are_immutable_and_prevent_destructive_downgrade(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME).create_page(
        create, _key(), master_session=session
    )
    for statement in (
        "UPDATE admin_master_file_set_receipts SET changed = changed",
        "DELETE FROM admin_master_file_set_receipts",
    ):
        with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
            connection.exec_driver_sql(statement)
    with pytest.raises(RuntimeError, match="Cannot discard"):
        alembic.downgrade(alembic_config(), MASTER_PAGE_DELETE_SCHEMA_REVISION)
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_sha256", b"z" * 32),
        ("page_id", "wrong-page"),
        ("original_occurred_at", 0),
        ("original_page_updated_at", OPERATION_TIME + 1),
        ("response_etag", '"page-v2-' + "0" * 64 + '"'),
        ("source_id", "0" * 32),
        ("identity_id", "0" * 32),
        ("operation_at", OPERATION_TIME + 1),
    ],
)
def test_frozen_results_reject_inconsistent_content_and_links(
    content_engine: Engine, field: str, value: object
) -> None:
    create, session = _setup(content_engine)
    receipt = (
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
        .create_page(create, _key(), master_session=session)
        .receipt
    )
    with content_engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        with pytest.raises(MasterFileSetReceiptCorruptError):
            validate_master_file_set_receipt(raw, receipt.model_copy(update={field: value}))


def test_backup_rejects_coordinated_time_and_etag_tampering(
    content_engine: Engine, tmp_path: Path
) -> None:
    from patchouli_lib.content.service import page_current_etag

    create, session = _setup(content_engine)
    receipt = (
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
        .create_page(create, _key(), master_session=session)
        .receipt
    )
    bundle = create_backup(
        content_engine,
        tmp_path / "bundle",
        app_version="0.1.0a0",
        artifact_identity=BackupArtifactIdentity("synthetic/source", "sha256:" + "1" * 64),
    )
    validate_database(bundle.database_path)
    # Preserve exact schema: this isolates semantic validation, not the earlier
    # schema-hash or foreign-key checks. Only this synthetic closed copy changes.
    with closing(sqlite3.connect(bundle.database_path)) as connection, connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'trg_master_file_set_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_master_file_set_receipts_no_update")
        connection.execute(
            "UPDATE admin_master_file_set_receipts SET original_occurred_at = ?, response_etag = ?",
            (
                0,
                page_current_etag(
                    receipt.page_uid,
                    receipt.revision_id,
                    receipt.revision_number,
                    0,
                    receipt.original_page_updated_at,
                ),
            ),
        )
        connection.execute(trigger)
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)


def test_success_links_cannot_be_claimed_twice(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    receipt = (
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
        .create_page(create, _key(), master_session=session)
        .receipt
    )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        MasterFileSetReceiptRepository(connection).add(
            receipt.model_copy(update={"key_digest": _key("second-claim").key_digest})
        )
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)


def test_backup_rejects_new_content_audit_without_receipt(
    content_engine: Engine, tmp_path: Path
) -> None:
    create, session = _setup(content_engine)
    receipt = (
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
        .create_page(create, _key(), master_session=session)
        .receipt
    )
    with immediate_transaction(content_engine) as connection:
        MasterAuditRepository(connection).add_success(
            identity_id=session.identity_id,
            session_generation=session.session_generation,
            session_fingerprint=session.audit_fingerprint(),
            action="content.page.file_set.revise",
            target_type="page",
            target_id=f"{receipt.library_id}:{receipt.page_uid.hex()}",
            occurred_at=OPERATION_TIME + 1,
            event_id="e" * 32,
        )
    with pytest.raises(BackupDatabaseError):
        create_backup(
            content_engine,
            tmp_path / "invalid-bundle",
            app_version="0.1.0a0",
            artifact_identity=BackupArtifactIdentity("synthetic/source", "sha256:" + "1" * 64),
        )
