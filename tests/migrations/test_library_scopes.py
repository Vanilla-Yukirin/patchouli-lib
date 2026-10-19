"""Library scopes preserve old grants and remain recoverable through backups."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from auth.test_optional_expiry_migration import _schema, _snapshot
from backup.conftest import APP_VERSION, _config, seed_complete_database
from backup.test_service import _create

from patchouli_lib.backup import (
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    LIBRARY_SCOPES_SCHEMA_REVISION,
    OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
)

POLICY = "auth_credential_library_policies"


def _add_policy(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"INSERT INTO {POLICY} "
        "(credential_id, caller_id, home_library_id, mode, created_at) "
        "VALUES (?, ?, ?, 'library_grants', 20)",
        ("e" * 32, "b" * 32, "1" * 32),
    )
    connection.execute(
        "INSERT INTO auth_credential_library_grants "
        "(credential_id, caller_id, home_library_id, target_library_id, action, created_at) "
        "VALUES (?, ?, ?, ?, 'read', 20)",
        ("e" * 32, "b" * 32, "1" * 32, "1" * 32),
    )


@pytest.mark.parametrize("populated", [False, True])
def test_scope_upgrade_preserves_data_and_exact_0031_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: bool
) -> None:
    path = tmp_path / "scopes.sqlite"
    config = _config(path, monkeypatch)
    if populated:
        engine = seed_complete_database(path, monkeypatch)
        engine.dispose()
        command.downgrade(config, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
        with closing(sqlite3.connect(path)) as connection:
            _add_policy(connection)
            connection.commit()
    else:
        command.upgrade(config, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    before = _snapshot(path)
    old_schema = _schema(path)
    validate_database(path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)

    command.upgrade(config, LIBRARY_SCOPES_SCHEMA_REVISION)
    after = _snapshot(path)
    assert [row[:-3] for row in after[POLICY]] == before[POLICY]
    assert all(row[-3:] == ("include", "include", 0) for row in after[POLICY])
    assert {k: v for k, v in after.items() if k != POLICY} == {
        k: v for k, v in before.items() if k != POLICY
    }
    assert (
        validate_database(path, schema_revision=LIBRARY_SCOPES_SCHEMA_REVISION).schema_revision
        == LIBRARY_SCOPES_SCHEMA_REVISION
    )

    command.downgrade(config, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    assert _snapshot(path) == before
    assert _schema(path) == old_schema
    validate_database(path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    command.upgrade(config, LIBRARY_SCOPES_SCHEMA_REVISION)
    validate_database(path, schema_revision=LIBRARY_SCOPES_SCHEMA_REVISION)


@pytest.mark.parametrize("action", ["read", "write"])
def test_exclude_scope_blocks_lossy_downgrade_without_data_or_schema_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    path = tmp_path / "exclude.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    command.downgrade(_config(path, monkeypatch), LIBRARY_SCOPES_SCHEMA_REVISION)
    with closing(sqlite3.connect(path)) as connection:
        _add_policy(connection)
        connection.execute(f"UPDATE {POLICY} SET {action}_mode = 'exclude', scope_revision = 1")
        connection.commit()
    before, schema = _snapshot(path), _schema(path)
    with pytest.raises(RuntimeError, match="exclude Library scopes"):
        command.downgrade(_config(path, monkeypatch), OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    assert _snapshot(path) == before
    assert _schema(path) == schema
    validate_database(path, schema_revision=LIBRARY_SCOPES_SCHEMA_REVISION)


def test_policy_scope_changes_enforce_integer_revisions_and_immutable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "constraints.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        _add_policy(connection)
        connection.commit()
        for assignment in (
            "read_mode = 'exclude'",
            "write_mode = 'exclude'",
            "scope_revision = 2",
            "scope_revision = -1",
            "scope_revision = 0.5",
            "scope_revision = 'bad'",
            "read_mode = 'bad', scope_revision = 1",
            "write_mode = 'bad', scope_revision = 1",
            "created_at = 21",
            "caller_id = 'wrong'",
            "credential_id = 'wrong'",
            "home_library_id = 'wrong'",
            "mode = 'wrong'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {POLICY} SET {assignment}")
            connection.rollback()
        connection.execute(f"UPDATE {POLICY} SET read_mode = 'exclude', scope_revision = 1")
        connection.execute(f"UPDATE {POLICY} SET scope_revision = 2")
        connection.execute(f"UPDATE {POLICY} SET scope_revision = 2")
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    validate_database(path)


def test_backup_restore_preserves_independent_modes_grants_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backup.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    with closing(sqlite3.connect(path)) as connection:
        _add_policy(connection)
        connection.execute(f"UPDATE {POLICY} SET read_mode = 'exclude', scope_revision = 1")
        connection.commit()
    try:
        bundle = _create(engine, tmp_path / "bundle")
    finally:
        engine.dispose()
    assert bundle.manifest.schema_revision == SUPPORTED_SCHEMA_REVISION
    verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION)
    restored = tmp_path / "restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    assert _snapshot(restored)[POLICY] == _snapshot(path)[POLICY]
    assert (
        _snapshot(restored)["auth_credential_library_grants"]
        == _snapshot(path)["auth_credential_library_grants"]
    )
    validate_database(restored)


@pytest.mark.parametrize("invalid", ["'invalid', 'include', 0", "'include', 'include', -1"])
def test_backup_rejects_corrupt_scope_rows_even_when_sqlite_checks_were_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    path = tmp_path / "invalid.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"INSERT INTO {POLICY} "
            "(credential_id, caller_id, home_library_id, mode, created_at, "
            "read_mode, write_mode, scope_revision) "
            f"VALUES (?, ?, ?, 'library_grants', 20, {invalid})",
            ("e" * 32, "b" * 32, "1" * 32),
        )
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(path)
