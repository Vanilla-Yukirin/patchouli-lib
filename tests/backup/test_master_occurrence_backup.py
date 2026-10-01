"""Exact old backups and mixed actor occurrence history use real migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from time import time

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.admin.file_set_service import MasterFileSetService
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    BackupDatabaseError,
    BackupManifestError,
    BackupManifestV1,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
)
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    CorrectArchiveOccurrenceCommand,
    PageOccurrenceCorrectionCommand,
)
from patchouli_lib.content.service import ArchiveService, page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key

from .conftest import APP_VERSION, _config
from .test_page_lifecycle_validation import _TIME, _credential
from .test_service import _create, identity

_LIBRARY = "1" * 32
_SECTION = "2" * 32
_BOOK = "3" * 32
_MASTER_EVENT = "f" * 32
_FILES = (("notes.md", b"# Synthetic receipt\n"), ("image.bin", b"\x00\xff\x81"))


def _source(engine: Engine) -> Path:
    assert engine.url.database is not None
    return Path(engine.url.database)


def _session(engine: Engine) -> MasterAdminSession:
    with immediate_transaction(engine) as connection:
        state = MasterTokenRepository(connection).initialize_from_local_cli(
            "synthetic master occurrence test token", now=_TIME
        )
    return MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic-occurrence-csrf",
        identity_id=state.identity_id,
        session_generation=state.session_generation,
    )


def _create_file_set(engine: Engine, session: MasterAdminSession) -> str:
    result = MasterFileSetService(engine, clock=lambda: _TIME).create_page(
        FileSetCreateCommand(
            library_id=_LIBRARY,
            section_id=_SECTION,
            book_id=_BOOK,
            title="Synthetic receipt",
            occurred_at=_TIME - 100,
            files=_FILES,
            source=ArchiveSourceInput(kind="synthetic"),
            request_id="req_" + "3" * 32,
        ),
        ArchiveIdempotencyKey(key_digest=digest_idempotency_key("synthetic-receipt")),
        master_session=session,
    )
    return result.receipt.page_id


def _master_correct(
    engine: Engine, session: MasterAdminSession, page_id: str, *, at: int = _TIME + 10
) -> None:
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(_LIBRARY, page_id)
        assert page is not None
        corrected_at = max(at, page.updated_at + 1)
        MasterAuditRepository(connection).add_success(
            identity_id=session.identity_id,
            session_generation=session.session_generation,
            session_fingerprint=session.audit_fingerprint(),
            action="content.page.occurrence.correct",
            target_type="page",
            target_id=f"{_LIBRARY}:{page.page_uid.hex()}",
            occurred_at=corrected_at,
            event_id=_MASTER_EVENT,
        )
        updated, correction = repository.correct_occurrence(
            page,
            PageOccurrenceCorrectionCommand(
                library_id=_LIBRARY,
                page_uid=page.page_uid,
                old_occurred_at=page.occurred_at,
                new_occurred_at=page.occurred_at + 100,
                master_audit_event_id=_MASTER_EVENT,
                corrected_at=corrected_at,
            ),
        )
        assert updated.page_uid == page.page_uid and updated.page_id == page.page_id
        assert updated.current_revision_id == page.current_revision_id
        assert correction.actor_caller_id is None and correction.actor_home_library_id is None
        assert correction.master_audit_event_id == _MASTER_EVENT


def _agent_correct(engine: Engine, token: str, page_id: str, *, at: int) -> None:
    with immediate_transaction(engine) as connection:
        page = ContentRepository(connection).get_page(_LIBRARY, page_id)
        assert page is not None
        ArchiveService(connection, clock=lambda: at).correct_occurrence(
            token,
            CorrectArchiveOccurrenceCommand(
                library_id=_LIBRARY,
                section_id=_SECTION,
                page_id=page_id,
                expected_etag=page_current_etag(
                    page.page_uid,
                    page.current_revision_id,
                    page.current_revision_number,
                    page.occurred_at,
                    page.updated_at,
                ),
                occurred_at=page.occurred_at + 100,
                request_id="req_" + "4" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key(f"synthetic-agent-{at}")),
        )


def _historical_bundle(source: Path, bundle: Path) -> BackupManifestV1:
    """Construct an exact 0026 producer bundle; never reinterpret it as 0027."""

    bundle.mkdir()
    database = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source)) as original:
        journal_mode = original.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(database)) as artifact:
            original.backup(artifact)
            artifact.execute("PRAGMA journal_mode = DELETE")
            artifact.commit()
    payload = database.read_bytes()
    producer = identity()
    manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at="2026-10-01T00:00:00.123456Z",
        app_version=APP_VERSION,
        schema_revision=MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity=producer.identity,
        artifact_digest=producer.digest,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    return manifest


def _tamper(
    database: Path, trigger_name: str, statement: str, parameters: tuple[str | int, ...]
) -> None:
    # Only synthetic closed copies change, and the exact trigger SQL is restored
    # so acceptance exercises semantic checks rather than schema hash rejection.
    with closing(sqlite3.connect(database)) as connection, connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = ?", (trigger_name,)
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(statement, parameters)
        connection.execute(trigger[0])


def test_0026_receipt_and_agent_history_remain_exactly_restorable(
    complete_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(complete_engine)
    page_id = _create_file_set(complete_engine, session)
    token = _credential(complete_engine)
    _agent_correct(complete_engine, token, page_id, at=_TIME + 20)
    source = _source(complete_engine)
    command.downgrade(_config(source, monkeypatch), MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION)
    assert validate_database(source, schema_revision=MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION)
    with pytest.raises(BackupDatabaseError):
        validate_database(source)
    bundle = tmp_path / "old-26-bundle"
    manifest = _historical_bundle(source, bundle)
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=APP_VERSION)
    assert (
        verify_backup_bundle(
            bundle,
            app_version=APP_VERSION,
            schema_revision=MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        )
        == manifest
    )
    rejected = tmp_path / "default-rejected.sqlite"
    with pytest.raises(BackupManifestError):
        restore_backup(bundle, rejected, app_version=APP_VERSION)
    assert not rejected.exists()
    restored = restore_backup(
        bundle,
        tmp_path / "old-26-restored.sqlite",
        app_version=APP_VERSION,
        schema_revision=MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
    )
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
        )
        assert "master_audit_event_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(page_occurrence_corrections)")
        }
        assert connection.execute(
            "SELECT count(*) FROM admin_master_file_set_receipts"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT actor_caller_id, actor_home_library_id FROM page_occurrence_corrections"
        ).fetchone() == ("b" * 32, _LIBRARY)
    assert restored.destination_path.read_bytes() == (bundle / BACKUP_FILENAME).read_bytes()


@pytest.mark.parametrize(
    "revision", [MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION, SUPPORTED_SCHEMA_REVISION]
)
def test_receipt_semantics_are_enforced_in_both_revisions(
    complete_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    _create_file_set(complete_engine, _session(complete_engine))
    source = _source(complete_engine)
    if revision == MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION:
        command.downgrade(_config(source, monkeypatch), revision)
        bundle = tmp_path / "receipt-copy"
        manifest = _historical_bundle(source, bundle)
    else:
        created = _create(complete_engine, tmp_path / "receipt-copy")
        bundle, manifest = created.bundle_path, created.manifest
    database = bundle / BACKUP_FILENAME
    _tamper(
        database,
        "trg_master_file_set_receipts_no_update",
        "UPDATE admin_master_file_set_receipts SET response_etag = ?",
        ('"incorrect-frozen-etag"',),
    )
    payload = database.read_bytes()
    rebound = replace(manifest, byte_size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    (bundle / MANIFEST_FILENAME).write_bytes(rebound.canonical_bytes())
    with pytest.raises(BackupDatabaseError):
        verify_backup_bundle(bundle, app_version=APP_VERSION, schema_revision=revision)
    destination = tmp_path / "never-restored.sqlite"
    with pytest.raises(BackupDatabaseError):
        restore_backup(bundle, destination, app_version=APP_VERSION, schema_revision=revision)
    assert not destination.exists()


def test_0027_mixed_agent_master_history_and_receipt_round_trip(
    complete_engine: Engine, tmp_path: Path
) -> None:
    session = _session(complete_engine)
    page_id = _create_file_set(complete_engine, session)
    token = _credential(complete_engine)
    _agent_correct(complete_engine, token, page_id, at=_TIME + 20)
    _master_correct(complete_engine, session, page_id, at=_TIME + 30)
    _agent_correct(complete_engine, token, page_id, at=_TIME + 40)
    tables = (
        "pages",
        "revisions",
        "revision_files",
        "revision_file_sets",
        "revision_file_seals",
        "page_identifier_registry",
        "page_sources",
        "page_occurrence_corrections",
        "admin_master_audit_events",
        "admin_master_file_set_receipts",
    )
    with complete_engine.connect() as connection:
        before = {
            table: [
                tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}").all()
            ]
            for table in tables
        }
    bundle = _create(complete_engine, tmp_path / "mixed-27-bundle")
    assert bundle.manifest.schema_revision == SUPPORTED_SCHEMA_REVISION
    assert verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION) == bundle.manifest
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "mixed-27-restored.sqlite", app_version=APP_VERSION
    )
    assert validate_database(restored.destination_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        for table, values in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == values
        assert connection.execute(
            "SELECT sequence, actor_caller_id, actor_home_library_id, master_audit_event_id "
            "FROM page_occurrence_corrections ORDER BY sequence"
        ).fetchall() == [
            (1, "b" * 32, _LIBRARY, None),
            (2, None, None, _MASTER_EVENT),
            (3, "b" * 32, _LIBRARY, None),
        ]
        assert connection.execute(
            "SELECT count(*) FROM page_occurrence_correction_guards"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT files.filename, files.content_bytes FROM revision_files AS files "
            "JOIN pages AS page ON files.library_id = page.library_id "
            "AND files.revision_id = page.current_revision_id "
            "WHERE page.page_id = ? ORDER BY files.filename",
            (page_id,),
        ).fetchall() == sorted(_FILES)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "content.archive.restore"),
        ("target_type", "library"),
        ("target_id", f"{_LIBRARY}:{'0' * 32}"),
        ("occurred_at", _TIME + 11),
    ],
)
def test_master_occurrence_audit_mismatch_rejected(
    complete_engine: Engine, tmp_path: Path, field: str, value: str | int
) -> None:
    session = _session(complete_engine)
    page_id = _create_file_set(complete_engine, session)
    _master_correct(complete_engine, session, page_id)
    bundle = _create(complete_engine, tmp_path / "audit-corrupt-copy")
    _tamper(
        bundle.database_path,
        "trg_admin_master_audit_no_update",
        f"UPDATE admin_master_audit_events SET {field} = ? WHERE id = ?",
        (value, _MASTER_EVENT),
    )
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)


def test_orphan_master_occurrence_audit_rejected(complete_engine: Engine) -> None:
    session = _session(complete_engine)
    page_id = _create_file_set(complete_engine, session)
    with immediate_transaction(complete_engine) as connection:
        page = ContentRepository(connection).get_page(_LIBRARY, page_id)
        assert page is not None
        MasterAuditRepository(connection).add_success(
            identity_id=session.identity_id,
            session_generation=session.session_generation,
            session_fingerprint=session.audit_fingerprint(),
            action="content.page.occurrence.correct",
            target_type="page",
            target_id=f"{_LIBRARY}:{page.page_uid.hex()}",
            occurred_at=_TIME + 10,
            event_id=_MASTER_EVENT,
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(_source(complete_engine))
