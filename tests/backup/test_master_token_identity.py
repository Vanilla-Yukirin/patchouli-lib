"""A verifier-only administrator identity survives a validated backup."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.backup import BackupDatabaseError, restore_backup, validate_database
from patchouli_lib.backup.manifest import SUPPORTED_SCHEMA_REVISION
from patchouli_lib.database import build_engine, immediate_transaction

from .conftest import APP_VERSION
from .test_service import _create

_MASTER_TOKEN = "synthetic-master-token-value-for-backup-round-trip"


def test_master_identity_backup_round_trip(complete_engine: Engine, tmp_path: Path) -> None:
    with immediate_transaction(complete_engine) as connection:
        original = MasterTokenRepository(connection).initialize_from_local_cli(
            _MASTER_TOKEN, now=100
        )
    bundle = _create(complete_engine, tmp_path / "master-identity-bundle")
    assert validate_database(bundle.database_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    assert _MASTER_TOKEN.encode("ascii") not in bundle.database_path.read_bytes()
    assert _MASTER_TOKEN.encode("ascii") not in bundle.manifest_path.read_bytes()

    restored = tmp_path / "master-identity-restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    engine = build_engine(f"sqlite:///{restored.as_posix()}")
    try:
        with engine.connect() as connection:
            repository = MasterTokenRepository(connection)
            assert repository.authenticate(_MASTER_TOKEN) == original
            assert repository.authenticate("wrong-master-token") is None
            assert repository.is_session_generation_current(
                original.identity_id, original.session_generation
            )
    finally:
        engine.dispose()


def test_backup_rejects_malformed_master_verifier(complete_engine: Engine, tmp_path: Path) -> None:
    with immediate_transaction(complete_engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER_TOKEN, now=100)
    database = _create(complete_engine, tmp_path / "bad-master-verifier").database_path
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE admin_master_identity SET token_verifier = ?",
            ("pbkdf2_sha256$invalid",),
        )
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)
