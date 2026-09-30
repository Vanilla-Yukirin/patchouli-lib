"""Disposable request metadata never carries user-supplied request contents."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from patchouli_lib.backup import validate_database
from patchouli_lib.backup.manifest import PAGE_TITLE_SCHEMA_REVISION, SUPPORTED_SCHEMA_REVISION
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.request_log import UNMATCHED_ROUTE, RequestLogRepository, RequestLogWrite

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, Path, Config]]:
    path = tmp_path / "requests.sqlite"
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    config = Config(str(_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        yield engine, path, config
    finally:
        engine.dispose()


def _entry(n: int, **overrides: object) -> RequestLogWrite:
    values: dict[str, object] = {
        "request_id": f"req_{n:032x}",
        "method": "GET",
        "route_template": "/api/v1/libraries/{library_id}",
        "status_code": 200,
        "completion": "completed",
        "occurred_at": 1,
        "duration_us": 42,
        "caller_id": "a" * 32,
        "home_library_id": "b" * 32,
        "credential_id": "c" * 32,
    }
    values.update(overrides)
    return RequestLogWrite(**values)  # type: ignore[arg-type]


def test_add_read_and_database_constraints(database: tuple[Engine, Path, Config]) -> None:
    engine, path, _ = database
    entry = _entry(1)
    with immediate_transaction(engine) as connection:
        repository = RequestLogRepository(connection)
        assert repository.add(entry) == 1
        assert repository.get(entry.request_id) == entry
        assert repository.get("req_" + "f" * 32) is None
    with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
        RequestLogRepository(connection).add(entry)
    with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
        connection.exec_driver_sql(
            "INSERT INTO api_request_log (request_id, method, route_template, "
            "status_code, completion, occurred_at, duration_us) "
            "VALUES (?, 'POST', '/api/v1/test', 200, 'completed', 1, 0)",
            ("req_" + "f" * 32,),
        )
        connection.exec_driver_sql(
            "UPDATE api_request_log SET route_template = '/api/v1/test?token=secret' "
            "WHERE request_id = ?",
            ("req_" + "f" * 32,),
        )
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": "client-request-id"},
        {"route_template": "https://example.invalid/api?q=secret"},
        {"route_template": "/api/v1/pages/secret?token=secret"},
        {"route_template": "/api/v1/pages/#fragment"},
        {"method": "TRACE"},
        {"status_code": None},
        {"status_code": 600},
        {"occurred_at": -1},
        {"duration_us": -1},
        {"caller_id": "not-an-opaque-id"},
        {"credential_id": "c" * 32, "caller_id": None},
    ],
)
def test_write_rejects_uncontrolled_or_invalid_metadata(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _entry(1, **overrides)


def test_unsupported_method_and_unmatched_route_are_fixed_markers(
    database: tuple[Engine, Path, Config],
) -> None:
    engine, path, _ = database
    entry = _entry(
        2,
        method="OTHER",
        route_template=UNMATCHED_ROUTE,
        status_code=405,
        caller_id=None,
        home_library_id=None,
        credential_id=None,
    )
    interrupted = _entry(
        3, status_code=None, completion="interrupted", route_template=UNMATCHED_ROUTE
    )
    with immediate_transaction(engine) as connection:
        repository = RequestLogRepository(connection)
        repository.add(entry)
        repository.add(interrupted)
        assert repository.get(entry.request_id) == entry
        assert repository.get(interrupted.request_id) == interrupted
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_retention_is_batched_strict_and_transactional(
    database: tuple[Engine, Path, Config],
) -> None:
    engine, path, _ = database
    with immediate_transaction(engine) as connection:
        repository = RequestLogRepository(connection)
        ids = [repository.add(_entry(n, occurred_at=n)) for n in range(1, 5)]
    with immediate_transaction(engine) as connection:
        repository = RequestLogRepository(connection)
        assert repository.delete_before(3, batch_size=1) == 1
        assert repository.get(_entry(1).request_id) is None
        assert repository.get(_entry(2).request_id) is not None
        assert repository.get(_entry(3).request_id) is not None
    with immediate_transaction(engine) as connection:
        repository = RequestLogRepository(connection)
        assert repository.delete_before(3) == 1
        assert repository.delete_before(3) == 0
        assert repository.add(_entry(5, occurred_at=5)) > max(ids)
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_actor_recent_index_matches_start_time_pagination(
    database: tuple[Engine, Path, Config],
) -> None:
    engine, _, _ = database
    with engine.connect() as connection:
        columns = connection.exec_driver_sql(
            "PRAGMA index_xinfo(ix_api_request_log_actor_recent)"
        ).all()
        assert [(row[2], row[3]) for row in columns if row[5]] == [
            ("home_library_id", 0),
            ("caller_id", 0),
            ("occurred_at", 0),
            ("id", 0),
        ]
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM api_request_log "
            "WHERE home_library_id = ? AND caller_id = ? "
            "AND (occurred_at, id) < (?, ?) "
            "ORDER BY occurred_at DESC, id DESC LIMIT 20",
            ("b" * 32, "a" * 32, 1_000_000, 100),
        ).all()
        assert any("USING COVERING INDEX ix_api_request_log_actor_recent" in row[3] for row in plan)
        assert not any("USE TEMP B-TREE FOR ORDER BY" in row[3] for row in plan)


def test_migration_downgrade_never_discards_records(
    database: tuple[Engine, Path, Config],
) -> None:
    engine, path, config = database
    with immediate_transaction(engine) as connection:
        RequestLogRepository(connection).add(_entry(1))
    with pytest.raises(RuntimeError, match="Cannot discard stored HTTP request records"):
        command.downgrade(config, PAGE_TITLE_SCHEMA_REVISION)
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION
    with immediate_transaction(engine) as connection:
        assert RequestLogRepository(connection).delete_before(2) == 1
    command.downgrade(config, PAGE_TITLE_SCHEMA_REVISION)
    assert validate_database(path, schema_revision=PAGE_TITLE_SCHEMA_REVISION)
    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    command.check(config)
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION
