"""New backups retain unbounded Agent Tokens without weakening old formats."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.backup import (
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    BACKUP_FILENAME,
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    MANIFEST_FILENAME,
    OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)

from .conftest import APP_VERSION, _config
from .test_agent_token_values import _CREDENTIAL_ID, _seed_revealable_agent
from .test_service import _create


def test_backup_restores_null_expiry_token_value_and_existing_history(
    complete_engine: Engine, tmp_path: Path
) -> None:
    value = _seed_revealable_agent(complete_engine)
    with complete_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE auth_credentials SET expires_at = NULL, updated_at = 2000, "
            "last_used_at = 2000 WHERE id = ?",
            (_CREDENTIAL_ID,),
        )
    bundle = _create(complete_engine, tmp_path / "bundle")
    assert bundle.manifest.schema_revision == SUPPORTED_SCHEMA_REVISION
    assert verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION) == bundle.manifest
    restored = tmp_path / "restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    assert validate_database(restored).schema_revision == SUPPORTED_SCHEMA_REVISION
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute(
            "SELECT k.expires_at, k.last_used_at, v.token_value "
            "FROM auth_credentials k JOIN auth_agent_token_values v ON v.credential_id = k.id "
            "WHERE k.id = ?",
            (_CREDENTIAL_ID,),
        ).fetchone() == (None, 2000, value)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM revision_files").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM auth_audit_events").fetchone() == (1,)
    assert value.encode("ascii") not in bundle.manifest_path.read_bytes()


def test_backup_rejects_operator_with_null_expiry(complete_engine: Engine, tmp_path: Path) -> None:
    with complete_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE auth_credentials SET expires_at = NULL WHERE id = ?", ("c" * 32,)
        )
    with pytest.raises(BackupDatabaseError):
        _create(complete_engine, tmp_path / "invalid-operator")


@pytest.mark.parametrize(
    "revision",
    [LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION],
)
def test_old_bundle_keeps_explicit_verify_restore_support(
    complete_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    assert complete_engine.url.database is not None
    source = Path(complete_engine.url.database)
    command.downgrade(_config(source, monkeypatch), revision)
    bundle = tmp_path / "old-bundle"
    bundle.mkdir()
    database = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(database)) as target:
        origin.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        target.commit()
    data = database.read_bytes()
    manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at="2026-10-18T00:00:00.000000Z",
        app_version=APP_VERSION,
        schema_revision=revision,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode="delete",
        artifact_identity="synthetic/finite-expiry@immutable",
        artifact_digest="sha256:" + "1" * 64,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    assert (
        verify_backup_bundle(bundle, app_version=APP_VERSION, schema_revision=revision) == manifest
    )
    restored = tmp_path / "old-restored.sqlite"
    restore_backup(
        bundle,
        restored,
        app_version=APP_VERSION,
        schema_revision=revision,
    )
    assert validate_database(restored, schema_revision=revision).schema_revision == revision
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM auth_credentials WHERE expires_at IS NOT NULL"
        ).fetchone() == (3,)
