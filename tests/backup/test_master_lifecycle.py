"""Synthetic master lifecycle and explicitly selected historical backups."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    BackupDatabaseError,
    BackupManifestError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    MASTER_AUDIT_SCHEMA_REVISION,
    SEARCH_INDEX_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction

from .conftest import APP_VERSION, _config
from .test_page_lifecycle_validation import _TIME, _credential, _lifecycle, _page
from .test_service import _append_binary_file_set, _create, identity

_LIBRARY_ID = "1" * 32
_SECTION_ID = "2" * 32
_MASTER_EVENT_ID = "8" * 32
_MASTER_IDENTITY_ID = "9" * 32
_MASTER_TIME = _TIME + 1
_DELETE_EVENT_ID = "7" * 32


def _delete_with_master(engine: Engine) -> bytes:
    page_id, _revision_id, _number, _kind, _occurrence, page_uid = _page(engine)
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(_LIBRARY_ID, page_id)
        assert page is not None
        MasterAuditRepository(connection).add_success(
            identity_id=_MASTER_IDENTITY_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.archive.delete",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=_TIME,
            event_id=_DELETE_EVENT_ID,
        )
        repository.transition_page_lifecycle(
            page,
            action="delete",
            actor_caller_id=None,
            actor_home_library_id=None,
            master_audit_event_id=_DELETE_EVENT_ID,
            request_id="req_" + "7" * 32,
            changed_at=_TIME,
        )
    return page_uid


def _delete_with_caller(engine: Engine) -> tuple[str, bytes]:
    token = _credential(engine)
    page_id, revision_id, number, _kind, occurrence, page_uid = _page(engine)
    _lifecycle(
        engine,
        token=token,
        page_id=page_id,
        etag=page_current_etag(page_uid, revision_id, number, occurrence, 2_000_000),
        action="delete",
        suffix="a",
        at=_TIME,
    )
    return page_id, page_uid


def _restore_with_master(engine: Engine, page_uid: bytes) -> None:
    with immediate_transaction(engine) as connection:
        row = connection.exec_driver_sql(
            "SELECT current_revision_number, occurred_at, deleted_at, updated_at "
            "FROM pages WHERE library_id = ? AND page_uid = ?",
            (_LIBRARY_ID, page_uid),
        ).one()
        assert row[2:] == (_TIME, _TIME)
        MasterAuditRepository(connection).add_success(
            identity_id=_MASTER_IDENTITY_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.archive.restore",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=_MASTER_TIME,
            event_id=_MASTER_EVENT_ID,
        )
        connection.exec_driver_sql(
            "INSERT INTO page_lifecycle_guards "
            "(library_id, page_uid, sequence, action, section_id, old_deleted_at, "
            "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
            "actor_caller_id, actor_home_library_id, master_audit_event_id, request_id) "
            "VALUES (?, ?, 2, 'restore', ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                _LIBRARY_ID,
                page_uid,
                _SECTION_ID,
                _TIME,
                _TIME,
                _MASTER_TIME,
                row[0],
                row[1],
                _MASTER_EVENT_ID,
                "req_" + "8" * 32,
            ),
        )
        connection.exec_driver_sql(
            "UPDATE pages SET deleted_at = NULL, updated_at = ? "
            "WHERE library_id = ? AND page_uid = ?",
            (_MASTER_TIME, _LIBRARY_ID, page_uid),
        )


def test_master_restore_bundle_round_trip_without_caller_replay(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _page_id, page_uid = _delete_with_caller(complete_engine)
    _restore_with_master(complete_engine, page_uid)
    with complete_engine.connect() as connection:
        event = connection.exec_driver_sql(
            "SELECT actor_caller_id, actor_home_library_id, master_audit_event_id "
            "FROM page_lifecycle_events WHERE sequence = 2"
        ).one()
        assert event == (None, None, _MASTER_EVENT_ID)
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM idempotency_records WHERE route_template LIKE '%restore%'"
            ).scalar_one()
            == 0
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM auth_audit_events WHERE action = 'content.archive.restore'"
            ).scalar_one()
            == 0
        )

    bundle = _create(complete_engine, tmp_path / "master-restore-bundle")
    assert verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION) == bundle.manifest
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "master-restore.sqlite", app_version=APP_VERSION
    )
    assert validate_database(restored.destination_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    with closing(sqlite3.connect(restored.destination_path)) as database:
        assert database.execute("SELECT deleted_at FROM pages").fetchone() == (None,)
        assert database.execute(
            "SELECT master_audit_event_id FROM page_lifecycle_events WHERE sequence = 2"
        ).fetchone() == (_MASTER_EVENT_ID,)


def test_backup_rejects_master_restore_audit_target_corruption(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _page_id, page_uid = _delete_with_caller(complete_engine)
    _restore_with_master(complete_engine, page_uid)
    bundle = _create(complete_engine, tmp_path / "master-corrupt-bundle")
    with closing(sqlite3.connect(bundle.database_path)) as database:
        trigger = database.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'trg_admin_master_audit_no_update'"
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        database.execute("DROP TRIGGER trg_admin_master_audit_no_update")
        database.execute(
            "UPDATE admin_master_audit_events SET target_id = ? WHERE id = ?",
            (f"{_LIBRARY_ID}:{'0' * 32}", _MASTER_EVENT_ID),
        )
        database.execute(trigger[0])
        database.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)


def test_backup_rejects_orphan_master_restore_audit(
    complete_engine: Engine, tmp_path: Path
) -> None:
    page_uid = _page(complete_engine)[5]
    with immediate_transaction(complete_engine) as connection:
        MasterAuditRepository(connection).add_success(
            identity_id=_MASTER_IDENTITY_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.archive.restore",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=_MASTER_TIME,
            event_id=_MASTER_EVENT_ID,
        )
    database_path = Path(complete_engine.url.database or "")
    with pytest.raises(BackupDatabaseError):
        validate_database(database_path)


def test_master_delete_binary_snapshot_round_trip_retains_all_content(
    complete_engine: Engine, tmp_path: Path
) -> None:
    revision_id, files = _append_binary_file_set(complete_engine)
    with complete_engine.connect() as connection:
        before = {
            table: connection.exec_driver_sql(f"SELECT * FROM {table} ORDER BY rowid").all()
            for table in (
                "revisions",
                "revision_files",
                "revision_file_sets",
                "revision_file_seals",
                "page_sources",
                "page_identifier_registry",
            )
        }
    page_uid = _delete_with_master(complete_engine)
    bundle = _create(complete_engine, tmp_path / "master-deleted-bundle")
    assert verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION) == bundle.manifest
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "master-deleted.sqlite", app_version=APP_VERSION
    )
    assert validate_database(restored.destination_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    with closing(sqlite3.connect(restored.destination_path)) as database:
        for table, rows in before.items():
            assert database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == [
                tuple(row) for row in rows
            ]
        assert database.execute(
            "SELECT current_revision_id, current_revision_number, deleted_at FROM pages"
        ).fetchone() == (revision_id, 2, _TIME)
        assert database.execute(
            "SELECT filename, content_bytes FROM revision_files WHERE revision_id = ? "
            "ORDER BY filename",
            (revision_id,),
        ).fetchall() == sorted(files)
        assert database.execute(
            "SELECT action, actor_caller_id, actor_home_library_id, master_audit_event_id "
            "FROM page_lifecycle_events"
        ).fetchone() == ("delete", None, None, _DELETE_EVENT_ID)

    _restore_with_master(complete_engine, page_uid)
    restored_bundle = _create(complete_engine, tmp_path / "master-restored-bundle")
    assert verify_backup_bundle(restored_bundle.bundle_path, app_version=APP_VERSION) == (
        restored_bundle.manifest
    )
    recovered = restore_backup(
        restored_bundle.bundle_path, tmp_path / "master-restored.sqlite", app_version=APP_VERSION
    )
    with closing(sqlite3.connect(recovered.destination_path)) as database:
        assert database.execute("SELECT deleted_at FROM pages").fetchone() == (None,)
        assert database.execute("SELECT count(*) FROM revisions").fetchone() == (2,)
        assert database.execute(
            "SELECT action, master_audit_event_id FROM page_lifecycle_events ORDER BY sequence"
        ).fetchall() == [("delete", _DELETE_EVENT_ID), ("restore", _MASTER_EVENT_ID)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action", "content.archive.restore"),
        ("target_type", "library"),
        ("target_id", f"{_LIBRARY_ID}:{'0' * 32}"),
        ("occurred_at", _TIME + 1),
    ],
)
def test_backup_rejects_master_delete_audit_corruption(
    complete_engine: Engine, tmp_path: Path, field: str, value: str | int
) -> None:
    _delete_with_master(complete_engine)
    bundle = _create(complete_engine, tmp_path / "master-delete-corrupt-bundle")
    with closing(sqlite3.connect(bundle.database_path)) as database:
        trigger = database.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'trg_admin_master_audit_no_update'"
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        database.execute("DROP TRIGGER trg_admin_master_audit_no_update")
        database.execute(
            f"UPDATE admin_master_audit_events SET {field} = ? WHERE id = ?",
            (value, _DELETE_EVENT_ID),
        )
        database.execute(trigger[0])
        database.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)


def test_backup_rejects_orphan_master_delete_audit(complete_engine: Engine) -> None:
    page_uid = _page(complete_engine)[5]
    with immediate_transaction(complete_engine) as connection:
        MasterAuditRepository(connection).add_success(
            identity_id=_MASTER_IDENTITY_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.archive.delete",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=_TIME,
            event_id=_DELETE_EVENT_ID,
        )
    database_path = Path(complete_engine.url.database or "")
    with pytest.raises(BackupDatabaseError):
        validate_database(database_path)


@pytest.mark.parametrize(
    "schema_revision", [MASTER_AUDIT_SCHEMA_REVISION, SEARCH_INDEX_SCHEMA_REVISION]
)
def test_historical_lifecycle_history_remains_explicitly_restorable(
    complete_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_revision: str
) -> None:
    _page_id, page_uid = _delete_with_caller(complete_engine)
    if schema_revision == SEARCH_INDEX_SCHEMA_REVISION:
        _restore_with_master(complete_engine, page_uid)
    source_name = complete_engine.url.database
    assert source_name is not None
    source = Path(source_name)
    command.downgrade(_config(source, monkeypatch), schema_revision)
    assert validate_database(source, schema_revision=schema_revision)
    with pytest.raises(BackupDatabaseError):
        validate_database(source)

    bundle = tmp_path / "historical-lifecycle-bundle"
    bundle.mkdir()
    database_path = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source)) as source_connection:
        journal_mode = source_connection.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(database_path)) as destination:
            source_connection.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
    data = database_path.read_bytes()
    artifact = identity()
    manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at="2026-09-30T12:34:56.123456Z",
        app_version=APP_VERSION,
        schema_revision=schema_revision,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity=artifact.identity,
        artifact_digest=artifact.digest,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=APP_VERSION)
    default_destination = tmp_path / "not-restored.sqlite"
    with pytest.raises(BackupManifestError):
        restore_backup(bundle, default_destination, app_version=APP_VERSION)
    assert not default_destination.exists()
    assert (
        verify_backup_bundle(bundle, app_version=APP_VERSION, schema_revision=schema_revision)
        == manifest
    )
    restored = restore_backup(
        bundle,
        tmp_path / "historical-lifecycle-restored.sqlite",
        app_version=APP_VERSION,
        schema_revision=schema_revision,
    )
    assert (
        validate_database(
            restored.destination_path, schema_revision=schema_revision
        ).schema_revision
        == schema_revision
    )
    assert restored.destination_path.read_bytes() == data
