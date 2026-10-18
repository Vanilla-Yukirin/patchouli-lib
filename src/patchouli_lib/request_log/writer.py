"""Lifespan-local admission for request metadata transactions.

This gate coordinates one application's logs and retention, not business
transactions or other server processes. It must never be shared across loops.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import suppress

import anyio
from sqlalchemy.exc import OperationalError

REQUEST_LOG_WRITER_STATE_ATTRIBUTE = "_request_log_writer"
_BUSY_RETRY_DELAYS = (0.025, 0.05)


def _is_sqlite_busy(error: OperationalError) -> bool:
    original = error.orig
    return (
        isinstance(original, sqlite3.OperationalError)
        and isinstance(code := getattr(original, "sqlite_errorcode", None), int)
        and code & 0xFF == sqlite3.SQLITE_BUSY
    )


async def _finish_owned_task[T](task: asyncio.Task[T]) -> T:
    """Do not abandon a thread/transaction even on direct Task.cancel()."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            # Fetch the original failure below, without inspecting its values.
            break
    if cancelled:
        # Joining failed work must not swallow shutdown cancellation: retention
        # would otherwise treat the failure as recoverable and sleep again.
        # Consume its result so no task exception is left unobserved.
        with suppress(Exception):
            task.result()
        raise asyncio.CancelledError
    return task.result()


class RequestLogWriter:
    """Await every accepted operation; serialize before taking thread/DB slots."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._gate = asyncio.Lock()
        self._pending: set[asyncio.Task[object]] = set()
        self._closed = False

    def _require_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Request log writer belongs to a different lifespan loop.")

    async def run[T](self, operation: Callable[[], T]) -> T:
        self._require_loop()
        if self._closed:
            raise RuntimeError("Request log writer is closed.")
        # The task is always awaited, including on cancellation. It is not a
        # background write queue; shielding also covers admission and backoff.
        with anyio.CancelScope(shield=True):
            task = asyncio.create_task(self._run_serialized(operation))
            self._pending.add(task)
            try:
                return await _finish_owned_task(task)
            finally:
                self._pending.discard(task)
        raise AssertionError("Unreachable request log cancellation state.")

    async def _run_serialized[T](self, operation: Callable[[], T]) -> T:
        async with self._gate:
            for attempt in range(len(_BUSY_RETRY_DELAYS) + 1):
                try:
                    return await anyio.to_thread.run_sync(operation, abandon_on_cancel=False)
                except OperationalError as error:
                    if not _is_sqlite_busy(error) or attempt == len(_BUSY_RETRY_DELAYS):
                        raise
                # The operation's transaction context has already rolled back
                # and closed its connection. Retry the complete operation only.
                await asyncio.sleep(_BUSY_RETRY_DELAYS[attempt])
        raise AssertionError("Unreachable request log retry state.")

    async def close(self) -> None:
        """Stop admission and drain accepted writes before disposing the engine."""
        self._require_loop()
        self._closed = True
        with anyio.CancelScope(shield=True):
            task = asyncio.create_task(self._drain(tuple(self._pending)))
            await _finish_owned_task(task)

    async def _drain(self, pending: tuple[asyncio.Task[object], ...]) -> None:
        # The callers own failure reporting; shutdown merely joins their work.
        await asyncio.gather(*pending, return_exceptions=True)
