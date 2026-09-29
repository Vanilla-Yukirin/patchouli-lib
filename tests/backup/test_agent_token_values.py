"""The backup artifact keeps exact Agent values without leaking them elsewhere."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, text

from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    AGENT_TOKEN_VALUES_SCHEMA_REVISION,
    LIBRARY_POLICY_SCHEMA_REVISION,
    MANIFEST_FILENAME,
    MASTER_IDENTITY_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.database import immediate_transaction

from .conftest import APP_VERSION, _config
from .test_service import _create

_CREDENTIAL_ID = "6" * 32


def _seed_revealable_agent(engine: Engine, *, expires_at: int = 1_000) -> str:
    issued = generate_token()
    with immediate_transaction(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO auth_credentials "
                "(id, library_id, caller_id, selector, token_version, verifier, "
                "expires_at, created_at, updated_at, last_used_at, revoked_at, "
                "rotated_at, rotated_to_credential_id) VALUES "
                "(:id, :library_id, :caller_id, :selector, :token_version, :verifier, "
                ":expires_at, 30, 30, NULL, NULL, NULL, NULL)"
            ),
            {
                "id": _CREDENTIAL_ID,
                "library_id": "1" * 32,
                "caller_id": "b" * 32,
                "selector": issued.selector,
                "token_version": issued.version,
                "verifier": issued.verifier,
                "expires_at": expires_at,
            },
        )
        connection.execute(
            text(
                "INSERT INTO auth_agent_token_values (credential_id, token_value) "
                "VALUES (:credential_id, :token_value)"
            ),
            {"credential_id": _CREDENTIAL_ID, "token_value": issued.value},
        )
    return issued.value


def test_backup_round_trips_revealable_agent_value(complete_engine: Engine, tmp_path: Path) -> None:
    value = _seed_revealable_agent(complete_engine)
    bundle = _create(complete_engine, tmp_path / "agent-value-bundle")
    assert validate_database(bundle.database_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    with closing(sqlite3.connect(bundle.database_path)) as connection:
        stored = connection.execute(
            "SELECT token_value FROM auth_agent_token_values WHERE credential_id = ?",
            (_CREDENTIAL_ID,),
        ).fetchone()
    assert stored == (value,)
    assert bundle.manifest_path.read_bytes().find(value.encode("ascii")) == -1
    restored = tmp_path / "agent-value-restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    with closing(sqlite3.connect(restored)) as connection:
        restored_value = connection.execute(
            "SELECT token_value FROM auth_agent_token_values WHERE credential_id = ?",
            (_CREDENTIAL_ID,),
        ).fetchone()
    assert restored_value == (value,)


def test_backup_rejects_raw_value_with_wrong_verifier(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _seed_revealable_agent(complete_engine)
    database = _create(complete_engine, tmp_path / "wrong-verifier").database_path
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE auth_credentials SET verifier = ? WHERE id = ?",
            (b"\x00" * 32, _CREDENTIAL_ID),
        )
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_expired_value_does_not_invalidate_historical_backup(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _seed_revealable_agent(complete_engine, expires_at=40)
    database = _create(complete_engine, tmp_path / "expired-value").database_path
    assert validate_database(database).schema_revision == SUPPORTED_SCHEMA_REVISION


@pytest.mark.parametrize(
    "schema_revision",
    (
        LIBRARY_POLICY_SCHEMA_REVISION,
        AGENT_TOKEN_VALUES_SCHEMA_REVISION,
        MASTER_IDENTITY_SCHEMA_REVISION,
    ),
)
def test_prior_bundle_remains_explicitly_verifiable_and_restorable(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_revision: str,
) -> None:
    source_database = complete_engine.url.database
    assert source_database is not None
    source_path = Path(source_database)
    command.downgrade(_config(source_path, monkeypatch), schema_revision)

    bundle = tmp_path / "library-policy-bundle"
    bundle.mkdir()
    database = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source_path)) as source:
        journal_mode = source.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(database)) as destination:
            source.backup(destination)
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
        schema_revision=schema_revision,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity="synthetic/library-policy@immutable",
        artifact_digest="sha256:" + "1" * 64,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())

    assert (
        verify_backup_bundle(
            bundle,
            app_version=APP_VERSION,
            schema_revision=schema_revision,
        )
        == manifest
    )
    restored = tmp_path / "library-policy-restored.sqlite"
    restore_backup(
        bundle,
        restored,
        app_version=APP_VERSION,
        schema_revision=schema_revision,
    )
    assert (
        validate_database(restored, schema_revision=schema_revision).schema_revision
        == schema_revision
    )
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("SELECT count(*) FROM auth_credentials").fetchone() == (3,)
