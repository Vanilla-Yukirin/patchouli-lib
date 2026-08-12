from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from patchouli_lib.database import build_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUTH_TABLES = {
    "auth_callers",
    "auth_credentials",
    "auth_section_grants",
    "auth_audit_events",
    "operator_bootstrap_markers",
}


def _configure(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def _assert_head(database_url: str) -> None:
    engine = build_engine(database_url)
    try:
        assert AUTH_TABLES.issubset(inspect(engine).get_table_names())
        with engine.connect() as first, engine.connect() as second:
            assert first.connection.driver_connection is not second.connection.driver_connection
            assert first.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert second.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert first.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert second.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                first.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "20260813_0004"
            )
    finally:
        engine.dispose()


def test_auth_migration_round_trip_and_metadata_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, config = _configure(tmp_path / "auth-migration.db", monkeypatch)

    command.upgrade(config, "head")
    _assert_head(database_url)
    command.check(config)

    engine = build_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO libraries "
                    "(id, name, created_at, updated_at) "
                    "VALUES (:id, :name, :created_at, :updated_at)"
                ),
                {
                    "id": "1" * 32,
                    "name": "Synthetic Migration Library",
                    "created_at": 1_000_000,
                    "updated_at": 1_000_000,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO auth_callers "
                    "(id, library_id, kind, name, description, policy_version, "
                    "created_at, updated_at, disabled_at) "
                    "VALUES (:id, :library_id, 'agent', :name, '', 1, "
                    ":created_at, :updated_at, NULL)"
                ),
                {
                    "id": "2" * 32,
                    "library_id": "1" * 32,
                    "name": "Synthetic Migration Agent",
                    "created_at": 2_000_000,
                    "updated_at": 2_000_000,
                },
            )
            credential_values = {
                "library_id": "1" * 32,
                "caller_id": "2" * 32,
                "token_version": 1,
                "verifier": b"v" * 32,
                "expires_at": 10_000_000,
                "created_at": 2_000_000,
                "updated_at": 3_000_000,
            }
            connection.execute(
                text(
                    "INSERT INTO auth_credentials "
                    "(id, library_id, caller_id, selector, token_version, verifier, "
                    "expires_at, created_at, updated_at, last_used_at, revoked_at, "
                    "rotated_at, rotated_to_credential_id) "
                    "VALUES (:id, :library_id, :caller_id, :selector, :token_version, "
                    ":verifier, :expires_at, :created_at, :updated_at, NULL, NULL, "
                    "NULL, NULL)"
                ),
                credential_values
                | {
                    "id": "4" * 32,
                    "selector": "B" * 22,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO auth_credentials "
                    "(id, library_id, caller_id, selector, token_version, verifier, "
                    "expires_at, created_at, updated_at, last_used_at, revoked_at, "
                    "rotated_at, rotated_to_credential_id) "
                    "VALUES (:id, :library_id, :caller_id, :selector, :token_version, "
                    ":verifier, :expires_at, :created_at, :updated_at, NULL, "
                    ":rotated_at, :rotated_at, :rotated_to_credential_id)"
                ),
                credential_values
                | {
                    "id": "3" * 32,
                    "selector": "A" * 22,
                    "rotated_at": 3_000_000,
                    "rotated_to_credential_id": "4" * 32,
                },
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260812_0002")
    engine = build_engine(database_url)
    try:
        assert AUTH_TABLES.isdisjoint(inspect(engine).get_table_names())
        assert {"libraries", "sections", "books"}.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    _assert_head(database_url)
    command.check(config)
