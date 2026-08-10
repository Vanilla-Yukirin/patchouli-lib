from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

import patchouli_lib.database as database_module
from patchouli_lib.database import (
    SQLITE_BUSY_TIMEOUT_MS,
    DatabaseNotReadyError,
    build_engine,
    check_database,
    immediate_transaction,
)


class ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class Connection:
    def __init__(self, fts5_enabled: int, foreign_keys_enabled: int) -> None:
        self.fts5_enabled = fts5_enabled
        self.foreign_keys_enabled = foreign_keys_enabled

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _statement: object) -> None:
        return None

    def exec_driver_sql(self, statement: str) -> ScalarResult:
        if statement == "PRAGMA foreign_keys":
            return ScalarResult(self.foreign_keys_enabled)
        return ScalarResult(self.fts5_enabled)


class Connectable:
    def __init__(self, fts5_enabled: int, foreign_keys_enabled: int = 1) -> None:
        self.fts5_enabled = fts5_enabled
        self.foreign_keys_enabled = foreign_keys_enabled

    def connect(self) -> Connection:
        return Connection(self.fts5_enabled, self.foreign_keys_enabled)


class BrokenConnectable:
    def connect(self) -> None:
        raise OSError("private connection detail")


class SimulatedCancellation(BaseException):
    pass


def test_in_memory_engine_is_ready() -> None:
    engine = build_engine("sqlite:///:memory:")
    try:
        check_database(engine)
    finally:
        engine.dispose()


def test_database_failure_is_wrapped() -> None:
    engine = cast(Engine, BrokenConnectable())

    with pytest.raises(DatabaseNotReadyError, match="connectivity check failed"):
        check_database(engine)


def test_missing_fts5_is_not_ready() -> None:
    engine = cast(Engine, Connectable(fts5_enabled=0))

    with pytest.raises(DatabaseNotReadyError, match="without FTS5"):
        check_database(engine)


def test_disabled_foreign_keys_are_not_ready() -> None:
    engine = cast(Engine, Connectable(fts5_enabled=1, foreign_keys_enabled=0))

    with pytest.raises(DatabaseNotReadyError, match="foreign-key enforcement is disabled"):
        check_database(engine)


def _file_database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_file_engine_configures_every_connection(tmp_path: Path) -> None:
    engine = build_engine(_file_database_url(tmp_path / "configured.db"))
    try:
        with engine.connect() as first, engine.connect() as second:
            assert first.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert second.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert first.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == (
                SQLITE_BUSY_TIMEOUT_MS
            )
            assert second.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == (
                SQLITE_BUSY_TIMEOUT_MS
            )

        engine.dispose()
        with engine.connect() as replacement:
            assert replacement.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert replacement.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == (
                SQLITE_BUSY_TIMEOUT_MS
            )
    finally:
        engine.dispose()


def test_file_engine_rejects_orphan_foreign_keys(tmp_path: Path) -> None:
    engine = build_engine(_file_database_url(tmp_path / "foreign-keys.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE children ("
                "id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES parents(id)"
                ")"
            )

        with (
            pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
            engine.begin() as connection,
        ):
            connection.exec_driver_sql("INSERT INTO children (id, parent_id) VALUES (1, 999)")

        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM children")).scalar_one() == 0
    finally:
        engine.dispose()


def test_immediate_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    engine = build_engine(_file_database_url(tmp_path / "rollback.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE events (id INTEGER PRIMARY KEY)")

        with (
            pytest.raises(RuntimeError, match="abort unit of work"),
            immediate_transaction(engine) as connection,
        ):
            connection.exec_driver_sql("INSERT INTO events (id) VALUES (1)")
            raise RuntimeError("abort unit of work")

        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM events")).scalar_one() == 0
    finally:
        engine.dispose()


def test_immediate_transaction_rolls_back_and_reraises_base_exception(
    tmp_path: Path,
) -> None:
    engine = build_engine(_file_database_url(tmp_path / "base-exception.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE events (id INTEGER PRIMARY KEY)")

        with (
            pytest.raises(SimulatedCancellation),
            immediate_transaction(engine) as connection,
        ):
            connection.exec_driver_sql("INSERT INTO events (id) VALUES (1)")
            raise SimulatedCancellation

        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM events")).scalar_one() == 0
    finally:
        engine.dispose()


def test_immediate_transaction_respects_busy_timeout_during_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy_timeout_ms = 50
    monkeypatch.setattr(database_module, "SQLITE_BUSY_TIMEOUT_MS", busy_timeout_ms)
    engine = build_engine(_file_database_url(tmp_path / "contention.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE events (id INTEGER PRIMARY KEY)")

        with immediate_transaction(engine) as first:
            first.exec_driver_sql("INSERT INTO events (id) VALUES (1)")
            started_at = monotonic()
            with (
                pytest.raises(OperationalError, match="database is locked"),
                immediate_transaction(engine),
            ):
                pass
            elapsed_ms = (monotonic() - started_at) * 1_000

        assert elapsed_ms >= busy_timeout_ms * 0.5
        assert elapsed_ms < 1_000
        with immediate_transaction(engine) as second:
            second.exec_driver_sql("INSERT INTO events (id) VALUES (2)")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM events")).scalar_one() == 2
    finally:
        engine.dispose()
