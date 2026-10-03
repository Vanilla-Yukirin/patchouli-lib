"""Small deterministic checks for lifespan-local metadata admission and retries."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from threading import Event
from typing import cast

import anyio
import pytest
from sqlalchemy import Table, create_engine, select
from sqlalchemy.exc import OperationalError

from patchouli_lib.database import immediate_transaction
from patchouli_lib.request_log import RequestLogRepository, RequestLogWrite
from patchouli_lib.request_log.models import RequestLogRecord
from patchouli_lib.request_log.writer import RequestLogWriter


def _busy(code: int = sqlite3.SQLITE_BUSY) -> OperationalError:
    original = sqlite3.OperationalError("synthetic private error")
    original.sqlite_errorcode = code
    return OperationalError("synthetic private statement", {"secret": "private"}, original)


def test_gate_waits_before_dispatching_another_thread() -> None:
    entered = Event()
    release = Event()
    second_entered = Event()

    def first() -> int:
        entered.set()
        assert release.wait(timeout=2)
        return 1

    def second() -> int:
        second_entered.set()
        return 2

    async def run() -> None:
        writer = RequestLogWriter()
        first_task = asyncio.create_task(writer.run(first))
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        second_task = asyncio.create_task(writer.run(second))
        await asyncio.sleep(0.02)
        assert not second_entered.is_set()
        release.set()
        assert await first_task == 1
        assert await second_task == 2
        await writer.close()
        with pytest.raises(RuntimeError, match="closed"):
            await writer.run(second)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("code", "expected_attempts"),
    [(sqlite3.SQLITE_BUSY, 3), (sqlite3.SQLITE_BUSY | (2 << 8), 3), (sqlite3.SQLITE_LOCKED, 1)],
)
def test_only_numeric_busy_codes_receive_finite_retries(code: int, expected_attempts: int) -> None:
    attempts = 0

    def fail() -> None:
        nonlocal attempts
        attempts += 1
        raise _busy(code)

    async def run() -> None:
        writer = RequestLogWriter()
        with pytest.raises(OperationalError):
            await writer.run(fail)
        await writer.close()

    asyncio.run(run())
    assert attempts == expected_attempts


def test_error_without_sqlite_code_is_not_retried() -> None:
    attempts = 0

    def fail() -> None:
        nonlocal attempts
        attempts += 1
        raise OperationalError("private", {}, sqlite3.OperationalError("database is locked"))

    async def run() -> None:
        writer = RequestLogWriter()
        with pytest.raises(OperationalError):
            await writer.run(fail)
        await writer.close()

    asyncio.run(run())
    assert attempts == 1


@pytest.mark.parametrize("lock_kind", ["writer", "reader"])
def test_real_busy_retries_whole_transaction_after_rollback(tmp_path: Path, lock_kind: str) -> None:
    # A short timeout belongs only to this independent synthetic engine. The
    # product's five-second timeout/configuration is deliberately unchanged.
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'busy.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 0.005},
    )
    cast(Table, RequestLogRecord.__table__).create(engine)
    entry = RequestLogWrite(
        request_id="req_" + "a" * 32,
        method="GET",
        route_template="/api/v1/synthetic",
        status_code=200,
        completion="completed",
        occurred_at=1,
        duration_us=1,
    )
    first_failure = Event()
    attempts = 0

    def write() -> None:
        nonlocal attempts
        attempts += 1
        try:
            with immediate_transaction(engine) as connection:
                RequestLogRepository(connection).add(entry)
        except OperationalError as error:
            assert isinstance(error.orig, sqlite3.OperationalError)
            assert error.orig.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            first_failure.set()
            raise

    async def run() -> None:
        writer = RequestLogWriter()
        with engine.connect() as blocker:
            blocker.exec_driver_sql("BEGIN IMMEDIATE" if lock_kind == "writer" else "BEGIN")
            if lock_kind == "reader":
                blocker.execute(select(RequestLogRecord.id)).all()
            task = asyncio.create_task(writer.run(write))
            assert await anyio.to_thread.run_sync(first_failure.wait, 2)
            blocker.rollback()
            await task
        await writer.close()

    try:
        asyncio.run(run())
        assert attempts == 2
        with engine.connect() as connection:
            # COMMIT BUSY must roll back its first insert: exactly one survives.
            assert connection.execute(select(RequestLogRecord.request_id)).scalars().all() == [
                entry.request_id
            ]
    finally:
        engine.dispose()


def test_direct_task_cancellation_and_shutdown_join_inflight_and_queued_writes() -> None:
    entered = Event()
    release = Event()
    finished: list[int] = []

    def first() -> None:
        entered.set()
        assert release.wait(timeout=2)
        finished.append(1)

    def second() -> None:
        finished.append(2)

    async def run() -> None:
        writer = RequestLogWriter()
        first_task = asyncio.create_task(writer.run(first))
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        second_task = asyncio.create_task(writer.run(second))
        await asyncio.sleep(0)
        first_task.cancel()
        second_task.cancel()
        await asyncio.sleep(0)
        first_task.cancel()  # Repeated cancellation still cannot abandon a write.
        closing = asyncio.create_task(writer.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert finished == []
        release.set()
        await closing
        for task in (first_task, second_task):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished == [1, 2]

    asyncio.run(run())


def test_anyio_cancel_scope_does_not_abandon_accepted_operation() -> None:
    finished = Event()

    async def run() -> None:
        writer = RequestLogWriter()
        with anyio.CancelScope() as scope:
            scope.cancel()
            await writer.run(finished.set)
        assert finished.is_set()
        await writer.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("code", "expected_attempts"), [(sqlite3.SQLITE_BUSY, 3), (sqlite3.SQLITE_LOCKED, 1)]
)
def test_cancellation_survives_final_operation_failure(code: int, expected_attempts: int) -> None:
    entered = Event()
    release = Event()
    attempts = 0

    def fail() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(timeout=2)
        raise _busy(code)

    async def run() -> None:
        writer = RequestLogWriter()
        task = asyncio.create_task(writer.run(fail))
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert attempts == expected_attempts
        await writer.close()

    asyncio.run(run())


def test_writer_rejects_cross_loop_reuse() -> None:
    async def create() -> RequestLogWriter:
        return RequestLogWriter()

    writer = asyncio.run(create())

    async def reuse() -> None:
        with pytest.raises(RuntimeError, match="different lifespan loop"):
            await writer.run(lambda: None)

    asyncio.run(reuse())
