from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import Engine

from patchouli_lib.database import DatabaseNotReadyError, build_engine, check_database


class ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class Connection:
    def __init__(self, fts5_enabled: int) -> None:
        self.fts5_enabled = fts5_enabled

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _statement: object) -> None:
        return None

    def exec_driver_sql(self, _statement: str) -> ScalarResult:
        return ScalarResult(self.fts5_enabled)


class Connectable:
    def __init__(self, fts5_enabled: int) -> None:
        self.fts5_enabled = fts5_enabled

    def connect(self) -> Connection:
        return Connection(self.fts5_enabled)


class BrokenConnectable:
    def connect(self) -> None:
        raise OSError("private connection detail")


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
