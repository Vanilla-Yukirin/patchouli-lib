from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, inspect, text

from patchouli_lib.backup.manifest import SUPPORTED_SCHEMA_REVISION
from patchouli_lib.database import build_engine, immediate_transaction

from .conftest import (
    CALLER_A,
    LIBRARY_A,
    configure_database,
    request_for,
    response_for,
    seed_idempotency_prerequisites,
)


def test_migration_roundtrip_metadata_and_populated_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, config = configure_database(tmp_path / "migration.db", monkeypatch)
    # Generic success bodies exercise the original storage migration, not the
    # complete content graph required by later schema revisions.
    command.upgrade(config, "20260813_0005")

    engine = build_engine(database_url)
    seed_idempotency_prerequisites(engine)
    with immediate_transaction(engine) as connection:
        historical_table = Table("idempotency_records", MetaData(), autoload_with=connection)
        connection.execute(
            historical_table.insert(),
            {
                "library_id": LIBRARY_A,
                "caller_id": CALLER_A,
                **request_for().model_dump(),
                **response_for().model_dump(),
            },
        )
        assert connection.scalar(text("SELECT count(*) FROM idempotency_records")) == 1
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert first.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260813_0005"
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
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == SUPPORTED_SCHEMA_REVISION
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
