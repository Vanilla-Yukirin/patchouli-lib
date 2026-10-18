"""A master reveal audit survives backup without retaining secret material."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, select

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.backup import restore_backup, validate_database, verify_backup_bundle
from patchouli_lib.backup.manifest import (
    ACTOR_HOME_SCHEMA_REVISION,
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.database import build_engine, immediate_transaction

from .conftest import APP_VERSION
from .test_service import _create

TOKEN = "synthetic master token for audited backup 0001"
IDENTITY = "a" * 32
EVENT = "b" * 32
TARGET = "c" * 32
FINGERPRINT = b"f" * 32


def test_master_audit_backup_round_trip(complete_engine: Engine, tmp_path: Path) -> None:
    with immediate_transaction(complete_engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: IDENTITY
        ).initialize_from_local_cli(TOKEN, now=1_000)
        MasterAuditRepository(connection).add_success(
            identity_id=IDENTITY,
            session_generation=1,
            session_fingerprint=FINGERPRINT,
            action="auth.agent_token.reveal",
            target_type="credential",
            target_id=TARGET,
            occurred_at=1_001,
            event_id=EVENT,
        )
    bundle = _create(complete_engine, tmp_path / "master-audit-bundle")
    assert validate_database(bundle.database_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    for path in (bundle.database_path, bundle.manifest_path):
        assert TOKEN.encode("ascii") not in path.read_bytes()
    restored = tmp_path / "master-audit-restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    engine = build_engine(f"sqlite:///{restored.as_posix()}")
    try:
        with engine.connect() as connection:
            row = connection.execute(select(MasterAuditEvent)).mappings().one()
            assert row["id"] == EVENT
            assert row["identity_id"] == IDENTITY
            assert row["session_generation"] == 1
            assert row["session_fingerprint"] == FINGERPRINT
            assert row["action"] == "auth.agent_token.reveal"
            assert row["target_type"] == "credential"
            assert row["target_id"] == TARGET
            assert row["occurred_at"] == 1_001
    finally:
        engine.dispose()


def test_0017_historical_bundle_remains_verifiable_and_restorable(
    complete_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_name = complete_engine.url.database
    assert source_name is not None
    source = Path(source_name)
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{source.as_posix()}")
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.downgrade(config, ACTOR_HOME_SCHEMA_REVISION)
    assert validate_database(source, schema_revision=ACTOR_HOME_SCHEMA_REVISION)

    bundle = tmp_path / "historical-0017-bundle"
    bundle.mkdir()
    database = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source)) as source_connection:
        journal_mode = source_connection.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(database)) as destination:
            source_connection.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
    data = database.read_bytes()
    manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at="2026-09-29T12:34:56.123456Z",
        app_version=APP_VERSION,
        schema_revision=ACTOR_HOME_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity="synthetic/master-audit@historical",
        artifact_digest="sha256:" + "1" * 64,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    assert (
        verify_backup_bundle(
            bundle,
            app_version=APP_VERSION,
            schema_revision=ACTOR_HOME_SCHEMA_REVISION,
        )
        == manifest
    )
    restored = tmp_path / "historical-0017-restored.sqlite"
    restore_backup(
        bundle,
        restored,
        app_version=APP_VERSION,
        schema_revision=ACTOR_HOME_SCHEMA_REVISION,
    )
    assert (
        validate_database(restored, schema_revision=ACTOR_HOME_SCHEMA_REVISION).schema_revision
        == ACTOR_HOME_SCHEMA_REVISION
    )
