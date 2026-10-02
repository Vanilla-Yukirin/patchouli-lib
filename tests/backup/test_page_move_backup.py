"""Real mixed move history, old formats, and semantic corruption acceptance."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from content.page_move_helpers import _command, _target
from content.test_master_revision_restore_service import _etag, _key, _page
from sqlalchemy import Engine

from patchouli_lib.admin.contracts import (
    MasterCorrectOccurrenceInput,
    MasterDeletePageFormInput,
    MasterRestoreArchiveFormInput,
    MasterUpdatePageTitleInput,
)
from patchouli_lib.admin.page_move_service import MasterPageMoveService
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.content.file_set_write_service import (
    FileSetAppendCommand,
    FileSetWriteService,
    FileSetWriteSuccess,
)
from patchouli_lib.content.page_membership_history import (
    PageMembershipHistoryError,
    load_page_state_timeline,
)
from patchouli_lib.content.schemas import ArchiveSourceInput
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import canonical_utc_wire

from .conftest import APP_VERSION
from .test_master_occurrence_backup import (
    _agent_correct,
    _create_file_set,
    _session,
    _source,
    _tamper,
)
from .test_page_lifecycle_validation import _TIME, _credential, _lifecycle
from .test_service import _create


def test_mixed_history_backup_restore_and_exact_historical_paths(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    session = _session(complete_engine)
    token = _credential(complete_engine)
    # The fixture supplies a real frozen legacy Agent create response. Add Agent
    # lifecycle and occurrence receipts before moving that old Page cross-Section.
    with complete_engine.connect() as connection:
        legacy_id = connection.exec_driver_sql("SELECT page_id FROM pages").scalar_one()
    legacy = _page(complete_engine, "1" * 32, legacy_id)
    deleted = _lifecycle(
        complete_engine,
        token=token,
        page_id=legacy_id,
        etag=_etag(legacy),
        action="delete",
        suffix="a",
        at=_TIME,
    )
    _lifecycle(
        complete_engine,
        token=token,
        page_id=legacy_id,
        etag=deleted.response_etag,
        action="restore",
        suffix="b",
        at=_TIME + 1,
    )
    _agent_correct(complete_engine, token, legacy_id, at=_TIME + 2)
    legacy = _page(complete_engine, legacy.library_id, legacy_id)
    target = _target(complete_engine, legacy)
    moves = MasterPageMoveService(complete_engine, clock=lambda: _TIME + 3)
    moves.move_page(_command(legacy, target), _key("legacy-move"), master_session=session)

    page_id = _create_file_set(complete_engine, session)
    page = _page(complete_engine, legacy.library_id, page_id)
    files = (("binary.bin", b"\x00\xff\x81"), ("notes.md", b"# changed\n"))
    for key in ("agent-changed", "agent-noop"):
        page = _page(complete_engine, page.library_id, page_id)
        with immediate_transaction(complete_engine) as connection:
            result = FileSetWriteService(connection, clock=lambda: _TIME + 4).append_existing_page(
                token,
                FileSetAppendCommand(
                    library_id=page.library_id,
                    section_id=page.section_id,
                    page_id=page_id,
                    expected_etag=_etag(page),
                    files=files,
                    source=ArchiveSourceInput(kind="synthetic"),
                    request_id="req_" + "c" * 32,
                ),
                _key(key),
            )
            assert isinstance(result, FileSetWriteSuccess)
            assert result.changed == (key == "agent-changed")
    page = _page(complete_engine, page.library_id, page_id)
    original = page
    first = moves.move_page(_command(page, target), _key("first-move"), master_session=session)
    page = _page(complete_engine, page.library_id, page_id)
    noop = moves.move_page(_command(page, target), _key("noop-move"), master_session=session)
    assert noop.receipt.changed == 0
    actions = AdminActionService(complete_engine, clock=lambda: _TIME + 5)
    actions.update_page_title_as_master(
        page.library_id,
        page.section_id,
        page.book_id,
        page_id,
        MasterUpdatePageTitleInput(title="Later title", expected_updated_at=page.updated_at),
        master_session=session,
    )
    page = _page(complete_engine, page.library_id, page_id)
    actions.correct_page_occurrence_as_master(
        page.library_id,
        page.section_id,
        page.book_id,
        page_id,
        MasterCorrectOccurrenceInput(
            occurred_at=canonical_utc_wire(page.occurred_at + 100), expected_etag=_etag(page)
        ),
        master_session=session,
    )
    page = _page(complete_engine, page.library_id, page_id)
    actions.delete_page_as_master(
        page.library_id,
        page.section_id,
        page.book_id,
        page_id,
        MasterDeletePageFormInput(expected_etag=_etag(page), confirm_delete="yes"),
        master_session=session,
    )
    page = _page(complete_engine, page.library_id, page_id)
    actions.restore_archive_page_as_master(
        page.library_id,
        page.section_id,
        page_id,
        MasterRestoreArchiveFormInput(expected_etag=_etag(page)),
        master_session=session,
    )
    page = _page(complete_engine, page.library_id, page_id)
    moves.move_page(
        _command(page, (original.section_id, original.book_id)),
        _key("return"),
        master_session=session,
    )
    assert (
        moves.move_page(
            _command(original, target), _key("first-move"), master_session=session
        ).receipt
        == first.receipt
    )

    bundle = _create(complete_engine, tmp_path / "mixed-moves")
    assert verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION) == bundle.manifest
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "restored.sqlite", app_version=APP_VERSION
    )
    database = bundle.bundle_path / BACKUP_FILENAME
    assert restored.destination_path.read_bytes() == database.read_bytes()
    assert validate_database(restored.destination_path)
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        timeline = load_page_state_timeline(
            connection,
            schema_revision="20261001_0028",
            library_id=page.library_id,
            page_uid=page.page_uid,
        )
        assert timeline.exact_state(original.updated_at).section_id == original.section_id
        assert timeline.exact_state(first.receipt.result_updated_at).section_id == target[0]
        assert timeline.current.section_id == original.section_id
        assert connection.execute("SELECT count(*) FROM page_move_events").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM admin_master_move_receipts").fetchone() == (
            4,
        )
        # An ETag from the target interval cannot prove that the original path was
        # current then, even though both paths exist in the complete history.
        with pytest.raises(PageMembershipHistoryError):
            timeline.match_active_etag(
                revision_id=first.receipt.revision_id,
                revision_number=first.receipt.revision_number,
                etag=first.receipt.response_etag,
                section_id=original.section_id,
            )
        assert connection.execute(
            "SELECT content_bytes FROM revision_files WHERE filename = 'binary.bin'"
        ).fetchone() == (b"\x00\xff\x81",)
    # Pairing a legitimate old no-op ETag with a different visited Section must
    # still fail. Historical path membership alone is not a sufficient proof.
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        rows = connection.execute("SELECT rowid, response_body FROM idempotency_records").fetchall()
        rowid, body = next(
            (rid, json.loads(body))
            for rid, body in rows
            if json.loads(body).get("changed") is False
        )
    body["section_id"] = target[0]
    payload_hex = json.dumps(body).encode().hex()
    _tamper(
        restored.destination_path,
        "trg_idempotency_records_immutable_update",
        f"UPDATE idempotency_records SET response_body = X'{payload_hex}' WHERE rowid = {rowid}",
        (),
    )
    with pytest.raises(BackupDatabaseError):
        validate_database(restored.destination_path)


@pytest.mark.parametrize("tamper", ["path", "clock", "receipt", "orphan"])
def test_semantic_move_corruption_rejected_with_original_trigger_sql(
    complete_engine: Engine,
    tmp_path: Path,
    tamper: str,
) -> None:
    session = _session(complete_engine)
    page_id = _create_file_set(complete_engine, session)
    page = _page(complete_engine, "1" * 32, page_id)
    moved = MasterPageMoveService(complete_engine, clock=lambda: _TIME + 10).move_page(
        _command(page, _target(complete_engine, page)),
        _key("move"),
        master_session=session,
    )
    database = _create(complete_engine, tmp_path / "closed-copy").bundle_path / BACKUP_FILENAME
    if tamper == "receipt":
        _tamper(
            database,
            "trg_master_move_receipts_no_delete",
            "DELETE FROM admin_master_move_receipts",
            (),
        )
    elif tamper == "orphan":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "INSERT INTO admin_master_audit_events SELECT ?, identity_id, session_generation, "
                "session_fingerprint, action, target_type, target_id, occurred_at "
                "FROM admin_master_audit_events WHERE id = ?",
                ("e" * 32, moved.receipt.master_audit_event_id),
            )
    else:
        statement = "UPDATE page_move_events SET old_updated_at = old_updated_at - 1"
        if tamper == "path":
            # Both paths and FKs remain valid; swapping them breaks temporal proof.
            statement = (
                "UPDATE page_move_events SET old_section_id = "
                "new_section_id, old_book_id = new_book_id, "
                "new_section_id = old_section_id, new_book_id = old_book_id"
            )
            _tamper(database, "trg_page_move_events_no_update", statement, ())
        else:
            _tamper(database, "trg_page_move_events_no_update", statement, ())
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_orphan_audit_is_detected_by_backup_and_blocks_downgrade(
    complete_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic import command

    from patchouli_lib.admin.master_audit import MasterAuditRepository

    from .conftest import _config

    session = _session(complete_engine)
    with immediate_transaction(complete_engine) as connection:
        MasterAuditRepository(connection).add_success(
            identity_id=session.identity_id,
            session_generation=session.session_generation,
            session_fingerprint=session.audit_fingerprint(),
            action="content.page.move",
            target_type="page",
            target_id="1" * 32 + ":" + "2" * 32,
            occurred_at=_TIME,
            event_id="e" * 32,
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(_source(complete_engine))
    with pytest.raises(RuntimeError, match="Cannot discard Page move audit"):
        command.downgrade(_config(_source(complete_engine), monkeypatch), "20261001_0027")
