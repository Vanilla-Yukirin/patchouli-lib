from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.idempotency import IdempotencyRepository, IdempotencyService

from .conftest import (
    configure_database,
    request_for,
    response_for,
    seed_idempotency_prerequisites,
    validated_caller,
)


def test_migration_roundtrip_metadata_and_populated_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, config = configure_database(tmp_path / "migration.db", monkeypatch)
    command.upgrade(config, "head")
    command.check(config)

    engine = build_engine(database_url)
    seed_idempotency_prerequisites(engine)
    with immediate_transaction(engine) as connection:
        caller = validated_caller(connection)
        IdempotencyService(IdempotencyRepository(connection)).record_success(
            caller,
            request_for(),
            response_for(),
        )
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert first.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260813_0006"
        )
        assert set(
            first.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'idempotency_records'"
                )
            ).scalars()
        ) == {
            "trg_idempotency_records_immutable_update",
            "trg_idempotency_records_no_delete",
        }
    engine.dispose()

    command.downgrade(config, "20260813_0004")
    engine = build_engine(database_url)
    try:
        assert "idempotency_records" not in inspect(engine).get_table_names()
        assert {"libraries", "auth_callers", "pages"}.issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM auth_callers")).scalar_one() == 3
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260813_0006")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
