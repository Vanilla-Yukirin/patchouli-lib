"""Library description migration and historical backup compatibility."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    create_backup,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    MASTER_LIFECYCLE_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.database import build_engine

from .conftest import APP_VERSION, _config
from .test_master_lifecycle import _delete_with_caller, _restore_with_master
from .test_service import identity

_LIBRARY_ID = "1" * 32


def _insert_library(path: Path, *, description: str | None = None) -> None:
    with closing(sqlite3.connect(path)) as connection:
        if description is None:
            connection.execute(
                "INSERT INTO libraries (id, name, created_at, updated_at) VALUES (?, ?, 1, 1)",
                (_LIBRARY_ID, "Synthetic Library"),
            )
        else:
            connection.execute(
                "INSERT INTO libraries (id, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 1)",
                (_LIBRARY_ID, "Synthetic Library", description),
            )
        connection.commit()


def _description(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return cast(
            str,
            connection.execute(
                "SELECT description FROM libraries WHERE id = ?", (_LIBRARY_ID,)
            ).fetchone()[0],
        )


def test_0019_library_upgrade_and_historical_bundle_stay_verifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "old.sqlite"
    config = _config(source, monkeypatch)
    command.upgrade(config, MASTER_LIFECYCLE_SCHEMA_REVISION)
    _insert_library(source)
    assert validate_database(source, schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION)

    bundle = tmp_path / "historical-bundle"
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
        schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity=artifact.identity,
        artifact_digest=artifact.digest,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    assert (
        verify_backup_bundle(
            bundle, app_version=APP_VERSION, schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION
        )
        == manifest
    )
    restored = tmp_path / "old-restored.sqlite"
    restore_backup(
        bundle,
        restored,
        app_version=APP_VERSION,
        schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION,
    )
    assert validate_database(restored, schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION)

    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    assert _description(source) == ""
    assert validate_database(source).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_new_description_survives_backup_and_prevents_lossy_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "described.sqlite"
    config = _config(source, monkeypatch)
    command.upgrade(config, "head")
    _insert_library(source, description="Synthetic knowledge space")
    engine = build_engine(f"sqlite:///{source.as_posix()}")
    try:
        bundle = tmp_path / "new-bundle"
        created = create_backup(
            engine,
            bundle,
            artifact_identity=identity(),
            app_version=APP_VERSION,
        )
        assert verify_backup_bundle(bundle, app_version=APP_VERSION) == created.manifest
        restored = tmp_path / "new-restored.sqlite"
        restore_backup(bundle, restored, app_version=APP_VERSION)
        assert _description(restored) == "Synthetic knowledge space"
        assert validate_database(restored).schema_revision == SUPPORTED_SCHEMA_REVISION
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="would be lost"):
        command.downgrade(config, MASTER_LIFECYCLE_SCHEMA_REVISION)
    assert validate_database(source).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_empty_description_can_downgrade_without_other_schema_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "empty.sqlite"
    config = _config(source, monkeypatch)
    command.upgrade(config, "head")
    _insert_library(source)
    command.downgrade(config, MASTER_LIFECYCLE_SCHEMA_REVISION)
    assert validate_database(source, schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION)
    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    assert _description(source) == ""
    assert validate_database(source).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_0019_master_restore_semantics_remain_valid(
    complete_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page_id, page_uid = _delete_with_caller(complete_engine)
    _restore_with_master(complete_engine, page_uid)
    source_name = complete_engine.url.database
    assert source_name is not None
    source = Path(source_name)
    command.downgrade(_config(source, monkeypatch), MASTER_LIFECYCLE_SCHEMA_REVISION)
    assert (
        validate_database(source, schema_revision=MASTER_LIFECYCLE_SCHEMA_REVISION).schema_revision
        == MASTER_LIFECYCLE_SCHEMA_REVISION
    )
