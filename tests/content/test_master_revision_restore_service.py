"""Historical restore reuses real master writes, complete snapshots and success receipts."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from time import time
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select

from patchouli_lib.admin.contracts import MasterCorrectOccurrenceInput, MasterDeletePageFormInput
from patchouli_lib.admin.file_set_receipts import MasterFileSetReceiptRow
from patchouli_lib.admin.file_set_service import (
    MasterFileSetConflictError,
    MasterFileSetNotFoundError,
    MasterFileSetResult,
    MasterFileSetService,
)
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.revision_restore_service import (
    MasterRevisionRestoreCommand,
    MasterRevisionRestoreService,
)
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSession, AdminSessionCodec, MasterAdminSession
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.backup import (
    BackupArtifactIdentity,
    create_backup,
    restore_backup,
    validate_database,
)
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.file_set_write_service import FileSetAppendCommand
from patchouli_lib.content.models import PageSource, Revision, RevisionFile
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput, PageRecord
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import CURRENT_SCHEMA_REVISION, immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.identifiers import MAX_REVISION_NUMBER
from patchouli_lib.retrieval.file_set_read import (
    FileSetReadPersistenceError,
    read_verified_revision_snapshot,
)
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.search.query_v2 import SearchQueryV2Wire
from patchouli_lib.search.service_v2 import search_pages_for_master

from .conftest import OPERATION_TIME
from .helpers import insert_page_graph, page_graph_values, seed_library_structure

_TOKEN = "synthetic revision restore master token material"
_CHANGED = (("current.md", b"# current-marker\n"), ("current.bin", b"\x02\x01"))
_SOURCE_FILES = {
    "markdown": (("content.md", b"# historical-marker\r\n"),),
    "mixed": (("notes.md", b"# historical-marker\n"), ("image.bin", b"\x00\xff\x81")),
    "binary": (("slides.bin", b"\x00\xff\x81\x02"),),
}


def _key(value: str = "synthetic-restore") -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(value))


def _page(engine: Engine, library_id: str, page_id: str) -> PageRecord:
    with engine.connect() as connection:
        page = ContentRepository(connection).get_page(library_id, page_id)
        assert page is not None
        return page


def _etag(page: PageRecord) -> str:
    return page_current_etag(
        page.page_uid,
        page.current_revision_id,
        page.current_revision_number,
        page.occurred_at,
        page.updated_at,
    )


def _counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                Revision,
                RevisionFile,
                PageSource,
                MasterAuditEvent,
                MasterFileSetReceiptRow,
            )
        )


@dataclass(frozen=True)
class Story:
    command: MasterRevisionRestoreCommand
    session: MasterAdminSession
    source_revision_id: str
    source_files: tuple[tuple[str, bytes], ...]

    def append(self, files: tuple[tuple[str, bytes], ...], etag: str) -> FileSetAppendCommand:
        return FileSetAppendCommand(
            library_id=self.command.library_id,
            section_id=self.command.section_id,
            page_id=self.command.page_id,
            expected_etag=etag,
            files=files,
            source=ArchiveSourceInput(kind="synthetic"),
            request_id="req_" + "1" * 32,
        )


def _story(engine: Engine, kind: str = "mixed") -> Story:
    library, section, book = seed_library_structure(engine)
    with immediate_transaction(engine) as connection:
        state = MasterTokenRepository(connection).initialize_from_local_cli(_TOKEN, now=1_000_000)
    session = MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic_restore_csrf",
        identity_id=state.identity_id,
        session_generation=state.session_generation,
    )
    writes = MasterFileSetService(engine, clock=lambda: OPERATION_TIME)
    source_files: tuple[tuple[str, bytes], ...]
    if kind == "legacy":
        graph = page_graph_values(library_id=library, section_id=section, book_id=book)
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, graph)
        first_page = _page(engine, library, graph[0].page_id)
        source_files = (("content.md", graph[1].content_md),)
    else:
        source_files = _SOURCE_FILES[kind]
        first = writes.create_page(
            FileSetCreateCommand(
                library_id=library,
                section_id=section,
                book_id=book,
                title="Synthetic restore document",
                occurred_at=OPERATION_TIME - 100,
                files=source_files,
                source=ArchiveSourceInput(kind="synthetic"),
                request_id="req_" + "1" * 32,
            ),
            _key("create"),
            master_session=session,
        )
        first_page = _page(engine, library, first.receipt.page_id)
    initial = Story(
        MasterRevisionRestoreCommand(
            library_id=library,
            section_id=section,
            book_id=book,
            page_id=first_page.page_id,
            source_revision_number=1,
            expected_etag=_etag(first_page),
        ),
        session,
        first_page.current_revision_id,
        source_files,
    )
    second = MasterFileSetService(engine, clock=lambda: OPERATION_TIME + 10).revise_page(
        initial.append(_CHANGED, _etag(first_page)), book, _key("change"), master_session=session
    )
    return replace(
        initial,
        command=initial.command.model_copy(update={"expected_etag": second.receipt.response_etag}),
    )


@pytest.mark.parametrize("kind", ["legacy", "markdown", "mixed", "binary"])
def test_restore_exact_complete_history_creates_a_new_revision(
    content_engine: Engine, kind: str
) -> None:
    story = _story(content_engine, kind)
    result = MasterRevisionRestoreService(
        content_engine, clock=lambda: OPERATION_TIME + 20
    ).restore_revision(story.command, _key(), master_session=story.session)
    assert result.receipt.changed == 1 and not result.replayed
    assert result.receipt.revision_number == 3
    assert result.receipt.revision_id != story.source_revision_id
    assert tuple((entry.name, entry.content) for entry in result.manifest.files) == tuple(
        sorted(story.source_files)
    )
    with content_engine.connect() as connection:
        page = ContentRepository(connection).get_page(
            story.command.library_id, story.command.page_id
        )
        assert page is not None and page.current_revision_number == 3
        for revision_id in (story.source_revision_id, result.receipt.revision_id):
            snapshot = read_verified_revision_snapshot(connection, page, revision_id)
            assert tuple((entry.name, entry.content) for entry in snapshot.manifest.files) == tuple(
                sorted(story.source_files)
            )
        source = connection.execute(
            select(PageSource.kind, PageSource.locator, PageSource.captured_at).where(
                PageSource.source_id == result.receipt.source_id
            )
        ).one()
        assert tuple(source) == ("revision_restore", story.source_revision_id, None)
        assert (
            connection.scalar(
                select(MasterAuditEvent.action).where(
                    MasterAuditEvent.id == result.receipt.master_audit_event_id
                )
            )
            == "content.page.file_set.revise"
        )


def test_noop_and_same_key_different_source_even_with_identical_bytes(
    content_engine: Engine,
) -> None:
    story = _story(content_engine)
    service = MasterRevisionRestoreService(content_engine, clock=lambda: OPERATION_TIME + 20)
    restored = service.restore_revision(
        story.command, _key("restore-old"), master_session=story.session
    )
    command = story.command.model_copy(update={"expected_etag": restored.receipt.response_etag})
    before = _counts(content_engine)
    noop = service.restore_revision(command, _key("noop"), master_session=story.session)
    assert noop.receipt.changed == 0 and not noop.replayed
    assert noop.receipt.source_id is None and noop.receipt.master_audit_event_id is None
    assert noop.receipt.revision_number == 3
    assert _counts(content_engine) == (*before[:-1], before[-1] + 1)
    replay = service.restore_revision(command, _key("noop"), master_session=story.session)
    assert replay.replayed and replay.receipt == noop.receipt
    with pytest.raises(MasterFileSetConflictError):
        service.restore_revision(
            command.model_copy(update={"source_revision_number": 3}),
            _key("noop"),
            master_session=story.session,
        )
    assert _counts(content_engine) == (*before[:-1], before[-1] + 1)


def test_old_etag_new_key_denied_but_original_replays_after_changes_and_delete(
    content_engine: Engine,
) -> None:
    story = _story(content_engine)
    service = MasterRevisionRestoreService(content_engine, clock=lambda: OPERATION_TIME + 20)
    original = service.restore_revision(story.command, _key(), master_session=story.session)
    with pytest.raises(FileSetPreconditionFailedError):
        service.restore_revision(story.command, _key("stale-new-key"), master_session=story.session)
    latest = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME + 30).revise_page(
        story.append(_CHANGED, original.receipt.response_etag),
        story.command.book_id,
        _key("later-change"),
        master_session=story.session,
    )
    replay = service.restore_revision(story.command, _key(), master_session=story.session)
    assert replay.replayed and replay.receipt == original.receipt
    actions = AdminActionService(content_engine, clock=lambda: OPERATION_TIME + 40)
    actions.correct_page_occurrence_as_master(
        story.command.library_id,
        story.command.section_id,
        story.command.book_id,
        story.command.page_id,
        MasterCorrectOccurrenceInput(
            occurred_at="2026-08-14T10:00:00.123456Z", expected_etag=latest.receipt.response_etag
        ),
        master_session=story.session,
    )
    current = _page(content_engine, story.command.library_id, story.command.page_id)
    replay = service.restore_revision(story.command, _key(), master_session=story.session)
    assert replay.replayed and replay.receipt == original.receipt
    AdminActionService(content_engine, clock=lambda: OPERATION_TIME + 50).delete_page_as_master(
        story.command.library_id,
        story.command.section_id,
        story.command.book_id,
        story.command.page_id,
        MasterDeletePageFormInput(expected_etag=_etag(current), confirm_delete="yes"),
        master_session=story.session,
    )
    before = _counts(content_engine)
    replay = service.restore_revision(story.command, _key(), master_session=story.session)
    assert replay.replayed and replay.receipt == original.receipt
    assert _counts(content_engine) == before
    current = _page(content_engine, story.command.library_id, story.command.page_id)
    assert current.deleted_at is not None and current.current_revision_number == 4
    with pytest.raises(MasterFileSetNotFoundError):
        service.restore_revision(
            story.command.model_copy(update={"expected_etag": _etag(current)}),
            _key("deleted-new-key"),
            master_session=story.session,
        )


def test_restore_rejects_bad_command_wrong_scope_and_unknown_source(content_engine: Engine) -> None:
    story = _story(content_engine)
    service = MasterRevisionRestoreService(content_engine)
    for number in (0, -1, MAX_REVISION_NUMBER + 1, True, "1"):
        with pytest.raises(ValidationError):
            MasterRevisionRestoreCommand.model_validate(
                {**story.command.model_dump(), "source_revision_number": number}
            )
    for invalid_fields in (
        {"expected_etag": None},
        {"expected_etag": 'W/"page-v2-' + "0" * 64 + '"'},
        {"page_id": "not/a/page"},
        {"extra": "unsupported"},
    ):
        with pytest.raises(ValidationError):
            MasterRevisionRestoreCommand.model_validate(
                {**story.command.model_dump(), **invalid_fields}
            )
    other_library, other_section, other_book = seed_library_structure(
        content_engine, prefix="4", label="Other"
    )
    before = _counts(content_engine)
    for change in (
        {"library_id": other_library},
        {"section_id": other_section},
        {"book_id": other_book},
        {"source_revision_number": 3},
    ):
        with pytest.raises(MasterFileSetNotFoundError):
            service.restore_revision(
                story.command.model_copy(update=change), _key(), master_session=story.session
            )
    for session in (
        replace(story.session, expires_at=int(time()) - 1),
        replace(story.session, session_generation=2),
        AdminSession(expires_at=int(time()) + 600, csrf_token="synthetic_legacy_csrf"),
    ):
        with pytest.raises(AuthenticationError):
            service.restore_revision(
                story.command, _key(), master_session=cast("MasterAdminSession", session)
            )
    assert _counts(content_engine) == before


@pytest.mark.parametrize("mutation", ["update", "rotate"])
def test_recheck_after_read_snapshot_closes_before_write(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    story = _story(content_engine)
    original = MasterFileSetService.revise_page

    def race(
        self: MasterFileSetService,
        command: FileSetAppendCommand,
        book_id: str,
        idempotency: ArchiveIdempotencyKey,
        *,
        master_session: MasterAdminSession,
    ) -> MasterFileSetResult:
        # The injection runs only after the source read has released its snapshot.
        if mutation == "rotate":
            with immediate_transaction(content_engine) as connection:
                assert (
                    MasterTokenRepository(connection).rotate(
                        _TOKEN, _TOKEN + "-rotated", now=OPERATION_TIME + 15
                    )
                    is not None
                )
        else:
            original(
                MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME + 15),
                story.append(
                    (("concurrent.md", b"concurrent change"),), story.command.expected_etag
                ),
                book_id,
                _key("concurrent"),
                master_session=master_session,
            )
        return original(self, command, book_id, idempotency, master_session=master_session)

    monkeypatch.setattr(MasterFileSetService, "revise_page", race)
    expected = AuthenticationError if mutation == "rotate" else FileSetPreconditionFailedError
    with pytest.raises(expected):
        MasterRevisionRestoreService(
            content_engine, clock=lambda: OPERATION_TIME + 20
        ).restore_revision(story.command, _key(), master_session=story.session)
    current = _page(content_engine, story.command.library_id, story.command.page_id)
    assert current.current_revision_number == (2 if mutation == "rotate" else 3)
    with content_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(MasterFileSetReceiptRow)
                .where(MasterFileSetReceiptRow.key_digest == _key().key_digest)
            )
            == 0
        )


@pytest.mark.parametrize("corruption", ["hash", "seal", "seal_guard"])
def test_invalid_source_bytes_or_seal_fail_before_any_new_write(
    content_engine: Engine, corruption: str
) -> None:
    story = _story(content_engine)
    database = content_engine.url.database
    assert database is not None
    # Deliberately corrupt only this synthetic database, bypassing immutable DDL.
    with closing(sqlite3.connect(database)) as connection:
        if corruption == "hash":
            connection.execute("DROP TRIGGER trg_revision_files_no_update")
            connection.execute(
                "UPDATE revision_files SET content_sha256 = ? WHERE revision_id = ?",
                (b"x" * 32, story.source_revision_id),
            )
        elif corruption == "seal":
            connection.execute("DROP TRIGGER trg_revision_file_seals_no_delete")
            connection.execute(
                "DELETE FROM revision_file_seals WHERE revision_id = ?",
                (story.source_revision_id,),
            )
        else:
            connection.execute("DROP TRIGGER trg_revision_file_seal_guards_no_delete")
            connection.execute(
                "DELETE FROM revision_file_seal_guards WHERE revision_id = ?",
                (story.source_revision_id,),
            )
        connection.commit()
    before = _counts(content_engine)
    with pytest.raises(FileSetReadPersistenceError):
        MasterRevisionRestoreService(content_engine).restore_revision(
            story.command, _key(), master_session=story.session
        )
    assert _counts(content_engine) == before
    assert (
        _page(
            content_engine, story.command.library_id, story.command.page_id
        ).current_revision_number
        == 2
    )


def test_audit_failure_rolls_back_restored_files_pointer_source_and_receipt(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(content_engine)
    before = _counts(content_engine)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic restore audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", fail)
    with pytest.raises(RuntimeError, match="synthetic restore audit failure"):
        MasterRevisionRestoreService(
            content_engine, clock=lambda: OPERATION_TIME + 20
        ).restore_revision(story.command, _key(), master_session=story.session)
    assert _counts(content_engine) == before
    assert (
        _etag(_page(content_engine, story.command.library_id, story.command.page_id))
        == story.command.expected_etag
    )


def test_restore_updates_current_search_and_survives_exact_0027_backup_roundtrip(
    content_engine: Engine, tmp_path: Path
) -> None:
    story = _story(content_engine)
    codec = AdminSessionCodec(b"synthetic restore session material 0001", ttl_seconds=600)
    cookie, session = codec.issue_master(
        story.session.identity_id, story.session.session_generation
    )
    rebuild_search_index(content_engine)

    def hits(word: str) -> tuple[str, ...]:
        return tuple(
            item.page_id
            for item in search_pages_for_master(
                content_engine, cookie, codec, SearchQueryV2Wire(keywords=[word]).to_query()
            ).items
        )

    assert hits("historical-marker") == ()
    assert hits("current-marker") == (story.command.page_id,)
    restored = MasterRevisionRestoreService(
        content_engine, clock=lambda: OPERATION_TIME + 20
    ).restore_revision(story.command, _key(), master_session=session)
    assert hits("historical-marker") == (story.command.page_id,)
    assert hits("current-marker") == ()
    bundle = create_backup(
        content_engine,
        tmp_path / "bundle",
        app_version="0.1.0a0",
        artifact_identity=BackupArtifactIdentity("synthetic/source", "sha256:" + "1" * 64),
    )
    validate_database(bundle.database_path)
    restored_db = restore_backup(
        bundle.bundle_path, tmp_path / "restored.sqlite", app_version="0.1.0a0"
    )
    validate_database(restored_db.destination_path)
    with closing(sqlite3.connect(restored_db.destination_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            CURRENT_SCHEMA_REVISION,
        )
        assert connection.execute("SELECT current_revision_number FROM pages").fetchone() == (3,)
        assert tuple(
            connection.execute(
                "SELECT filename, content_bytes FROM revision_files "
                "WHERE revision_id = ? ORDER BY filename",
                (restored.receipt.revision_id,),
            )
        ) == tuple(sorted(story.source_files))
