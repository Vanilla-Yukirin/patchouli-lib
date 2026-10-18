"""Actor activity index migration and historical backup compatibility."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    BackupDatabaseError,
    create_backup,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    LIBRARY_DESCRIPTION_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)

from .conftest import APP_VERSION, _config
from .test_service import identity

_INDEX = "ix_auth_audit_events_actor_recent"


def _database_path(engine: Engine) -> Path:
    name = engine.url.database
    assert name is not None
    return Path(name)


def _audit_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT * FROM auth_audit_events ORDER BY id").fetchall()


def _index_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index' AND name NOT GLOB 'sqlite_*'"
            )
        }


def test_existing_audit_rows_survive_index_upgrade_and_downgrade(
    complete_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database_path(complete_engine)
    config = _config(path, monkeypatch)
    command.downgrade(config, LIBRARY_DESCRIPTION_SCHEMA_REVISION)
    before = _audit_rows(path)
    assert before  # The fixture contains a real, fully related audit row.
    assert _INDEX not in _index_names(path)
    assert validate_database(path, schema_revision=LIBRARY_DESCRIPTION_SCHEMA_REVISION)

    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    assert _audit_rows(path) == before
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION
    command.check(config)
    with closing(sqlite3.connect(path)) as connection:
        columns = connection.execute(f"PRAGMA index_xinfo({_INDEX})").fetchall()
        assert [(row[2], row[3]) for row in columns if row[5]] == [
            ("actor_home_library_id", 0),
            ("actor_caller_id", 0),
            ("outcome", 0),
            ("occurred_at", 0),
            ("id", 0),
        ]
        home_library, caller = connection.execute(
            "SELECT actor_home_library_id, actor_caller_id FROM auth_audit_events LIMIT 1"
        ).fetchone()
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT event.id FROM auth_audit_events AS event "
            "JOIN auth_callers AS caller ON caller.library_id = event.actor_home_library_id "
            "AND caller.id = event.actor_caller_id "
            "WHERE event.actor_home_library_id = ? AND event.actor_caller_id = ? "
            "AND event.outcome = 'succeeded' "
            "AND event.action IN ('auth.credential.rotate', 'tag.create') "
            "ORDER BY event.occurred_at DESC, event.id DESC LIMIT 50",
            (home_library, caller),
        ).fetchall()
        assert any(
            f"USING INDEX {_INDEX}" in row[3]
            and "actor_home_library_id=? AND actor_caller_id=? AND outcome=?" in row[3]
            for row in plan
        )
        assert not any("USE TEMP B-TREE FOR ORDER BY" in row[3] for row in plan)

    command.downgrade(config, LIBRARY_DESCRIPTION_SCHEMA_REVISION)
    assert _INDEX not in _index_names(path)
    assert _audit_rows(path) == before
    assert validate_database(path, schema_revision=LIBRARY_DESCRIPTION_SCHEMA_REVISION)


def test_0020_and_0021_backups_verify_and_restore(
    complete_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database_path(complete_engine)
    config = _config(source, monkeypatch)
    command.downgrade(config, LIBRARY_DESCRIPTION_SCHEMA_REVISION)
    old_rows = _audit_rows(source)

    old_bundle = tmp_path / "old-bundle"
    old_bundle.mkdir()
    old_database = old_bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source)) as source_connection:
        journal_mode = source_connection.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(old_database)) as destination:
            source_connection.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
    data = old_database.read_bytes()
    artifact = identity()
    old_manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at="2026-09-30T12:34:56.123456Z",
        app_version=APP_VERSION,
        schema_revision=LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity=artifact.identity,
        artifact_digest=artifact.digest,
    )
    (old_bundle / MANIFEST_FILENAME).write_bytes(old_manifest.canonical_bytes())
    assert (
        verify_backup_bundle(
            old_bundle,
            app_version=APP_VERSION,
            schema_revision=LIBRARY_DESCRIPTION_SCHEMA_REVISION,
        )
        == old_manifest
    )
    old_restored = tmp_path / "old-restored.sqlite"
    restore_backup(
        old_bundle,
        old_restored,
        app_version=APP_VERSION,
        schema_revision=LIBRARY_DESCRIPTION_SCHEMA_REVISION,
    )
    assert _audit_rows(old_restored) == old_rows
    assert _INDEX not in _index_names(old_restored)

    # A TEXT-affinity column can still contain a BLOB; 0020 must retain its
    # existing semantic validation even after 0021 becomes the default.
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE libraries SET description = x'4142'")
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(source, schema_revision=LIBRARY_DESCRIPTION_SCHEMA_REVISION)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE libraries SET description = ''")
        connection.commit()

    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    new_bundle = tmp_path / "new-bundle"
    created = create_backup(
        complete_engine,
        new_bundle,
        artifact_identity=identity(),
        app_version=APP_VERSION,
    )
    assert verify_backup_bundle(new_bundle, app_version=APP_VERSION) == created.manifest
    new_restored = tmp_path / "new-restored.sqlite"
    restore_backup(new_bundle, new_restored, app_version=APP_VERSION)
    assert _audit_rows(new_restored) == old_rows
    assert _INDEX in _index_names(new_restored)
    assert validate_database(new_restored).schema_revision == SUPPORTED_SCHEMA_REVISION
