"""Expiry migration preserves credential references and rejects lossy rollback."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from backup.conftest import _config, seed_complete_database

from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.backup import validate_database
from patchouli_lib.backup.manifest import (
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION,
)


def _snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT GLOB 'sqlite_*' AND name != 'alembic_version'"
        ).fetchall()
        return {
            name: connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
            for (name,) in tables
        }


def _schema(path: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_schema "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
        ).fetchall()


@pytest.mark.parametrize("populated", [False, True])
def test_upgrade_and_finite_downgrade_preserve_exact_data_and_historical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: bool
) -> None:
    path = tmp_path / "expiry.sqlite"
    config = _config(path, monkeypatch)
    if populated:
        engine = seed_complete_database(path, monkeypatch)
        issued = generate_token()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE auth_credentials SET selector = ?, verifier = ? WHERE id = ?",
                (issued.selector, issued.verifier, "e" * 32),
            )
            connection.exec_driver_sql(
                "INSERT INTO auth_agent_token_values (credential_id, token_value) VALUES (?, ?)",
                ("e" * 32, issued.value),
            )
            connection.exec_driver_sql(
                "INSERT INTO auth_credential_library_policies "
                "(credential_id, caller_id, home_library_id, mode, created_at) "
                "VALUES (?, ?, ?, 'library_grants', 20)",
                ("e" * 32, "b" * 32, "1" * 32),
            )
            connection.exec_driver_sql(
                "INSERT INTO auth_credential_library_grants "
                "(credential_id, caller_id, home_library_id, "
                "target_library_id, action, created_at) "
                "VALUES (?, ?, ?, ?, 'read', 20)",
                ("e" * 32, "b" * 32, "1" * 32, "1" * 32),
            )
        engine.dispose()
        command.downgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
    else:
        command.upgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
    before = _snapshot(path)
    schema_before = _schema(path)
    validate_database(path, schema_revision=LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)

    command.upgrade(config, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(auth_credentials)")}
        assert columns["expires_at"][3] == 0
    assert (
        validate_database(
            path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION
        ).schema_revision
        == OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION
    )

    command.downgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
    assert _snapshot(path) == before
    assert _schema(path) == schema_before
    validate_database(path, schema_revision=LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
    command.upgrade(config, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    assert _snapshot(path) == before
    validate_database(path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)


def test_no_expiry_accepts_later_use_but_keeps_timestamp_constraints_and_blocks_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unbounded.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    command.downgrade(_config(path, monkeypatch), OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE auth_credentials SET expires_at = NULL, last_used_at = 2000, "
            "updated_at = 2000 WHERE id = ?",
            ("e" * 32,),
        )
        connection.commit()
        for assignments in (
            "last_used_at = 2001",
            "last_used_at = 19",
            "expires_at = 20",
            "expires_at = 2000",
            "revoked_at = 2001",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE auth_credentials SET {assignments} WHERE id = ?", ("e" * 32,)
                )
            connection.rollback()
    validate_database(path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    before = _snapshot(path)
    config = _config(path, monkeypatch)
    with pytest.raises(RuntimeError, match="without expiry"):
        command.downgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
    assert _snapshot(path) == before
    validate_database(path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
    # Revocation does not authorize silently discarding historical expiry state.
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE auth_credentials SET revoked_at = 2000 WHERE id = ?", ("e" * 32,)
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="without expiry"):
        command.downgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
    validate_database(path, schema_revision=OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION)
