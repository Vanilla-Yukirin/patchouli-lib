"""Current request records and historical 0022 artifacts validate independently."""

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
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    PAGE_TITLE_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.request_log import RequestLogRepository, RequestLogWrite

from .conftest import APP_VERSION, _config
from .test_service import _create, identity


def _path(engine: Engine) -> Path:
    name = engine.url.database
    assert name is not None
    return Path(name)


def _add_request(engine: Engine) -> None:
    with immediate_transaction(engine) as connection:
        RequestLogRepository(connection).add(
            RequestLogWrite(
                request_id="req_" + "a" * 32,
                method="POST",
                route_template="/api/v1/sections/{section_id}/pages",
                status_code=201,
                completion="completed",
                occurred_at=123_456_789,
                duration_us=987,
                caller_id="b" * 32,
                home_library_id="1" * 32,
                credential_id="c" * 32,
            )
        )


def test_request_log_round_trips_in_current_backup(complete_engine: Engine, tmp_path: Path) -> None:
    _add_request(complete_engine)
    created = _create(complete_engine, tmp_path / "requests-bundle")
    assert created.manifest.schema_revision == SUPPORTED_SCHEMA_REVISION
    assert verify_backup_bundle(created.bundle_path, app_version=APP_VERSION) == created.manifest
    restored = restore_backup(
        created.bundle_path,
        tmp_path / "restored.sqlite",
        app_version=APP_VERSION,
    )
    assert validate_database(restored.destination_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        assert connection.execute(
            "SELECT request_id, route_template, status_code, duration_us FROM api_request_log"
        ).fetchall() == [
            (
                "req_" + "a" * 32,
                "/api/v1/sections/{section_id}/pages",
                201,
                987,
            )
        ]


def test_request_log_invalid_value_is_rejected_by_backup_validation(
    complete_engine: Engine,
) -> None:
    _add_request(complete_engine)
    path = _path(complete_engine)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE api_request_log SET route_template = '/private?token=secret'")
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(path)


def test_historical_0022_bundle_still_verifies_and_restores(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _path(complete_engine)
    config = _config(source, monkeypatch)
    command.downgrade(config, PAGE_TITLE_SCHEMA_REVISION)
    assert validate_database(source, schema_revision=PAGE_TITLE_SCHEMA_REVISION)

    bundle = tmp_path / "historical-0022"
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
        schema_revision=PAGE_TITLE_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity=artifact.identity,
        artifact_digest=artifact.digest,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    assert (
        verify_backup_bundle(
            bundle, app_version=APP_VERSION, schema_revision=PAGE_TITLE_SCHEMA_REVISION
        )
        == manifest
    )
    restored = restore_backup(
        bundle,
        tmp_path / "historical-restored.sqlite",
        app_version=APP_VERSION,
        schema_revision=PAGE_TITLE_SCHEMA_REVISION,
    )
    assert validate_database(restored.destination_path, schema_revision=PAGE_TITLE_SCHEMA_REVISION)
    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    assert validate_database(source).schema_revision == SUPPORTED_SCHEMA_REVISION
