"""Best-effort API request metadata capture and bounded online retention."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from time import monotonic_ns, time_ns

import anyio
from sqlalchemy import Engine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from patchouli_lib.api.request_ids import REQUEST_ID_STATE_ATTRIBUTE, generate_request_id
from patchouli_lib.database import immediate_transaction
from patchouli_lib.request_log import UNMATCHED_ROUTE, RequestLogRepository, RequestLogWrite
from patchouli_lib.request_log.identity import (
    RequestIdentity,
    begin_request_identity,
    end_request_identity,
)
from patchouli_lib.request_log.writer import (
    REQUEST_LOG_WRITER_STATE_ATTRIBUTE,
    RequestLogWriter,
)

_LOGGER = logging.getLogger(__name__)
_API_PREFIX = "/api"
_RETENTION_DAYS = 30
_MICROSECONDS_PER_DAY = 86_400_000_000
_RETENTION_INTERVAL_SECONDS = 15 * 60
_BACKLOG_RETRY_SECONDS = 1
_FAILURE_WARNING_INTERVAL_NS = 60 * 1_000_000_000
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _is_api_request(scope: Scope) -> bool:
    path = scope.get("path", "")
    return isinstance(path, str) and (path == _API_PREFIX or path.startswith(f"{_API_PREFIX}/"))


def _route_template(scope: Scope) -> str:
    """Use only the router's declared template, never the incoming URL."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return UNMATCHED_ROUTE


def _method(scope: Scope) -> str:
    value = scope.get("method")
    return value if isinstance(value, str) and value in _KNOWN_METHODS else "OTHER"


def _write_request(engine: Engine, entry: RequestLogWrite) -> None:
    with immediate_transaction(engine) as connection:
        RequestLogRepository(connection).add(entry)


def cleanup_request_logs_once(
    engine: Engine,
    *,
    now_utc_us: int | None = None,
    batch_size: int = 1_000,
) -> int:
    """Delete at most one expired batch; never run this for each request."""
    now = time_ns() // 1_000 if now_utc_us is None else now_utc_us
    cutoff = max(0, now - _RETENTION_DAYS * _MICROSECONDS_PER_DAY)
    with immediate_transaction(engine) as connection:
        return RequestLogRepository(connection).delete_before(cutoff, batch_size=batch_size)


class _BoundedWarning:
    def __init__(self) -> None:
        self._last_ns = -_FAILURE_WARNING_INTERVAL_NS

    def warn(self, text: str) -> None:
        now = monotonic_ns()
        if now - self._last_ns >= _FAILURE_WARNING_INTERVAL_NS:
            self._last_ns = now
            # Never include a database exception: it may carry bound values.
            _LOGGER.warning("%s", text)


async def run_request_log_retention(engine: Engine, writer: RequestLogWriter) -> None:
    """Prune on startup and periodically, one short transaction at a time."""
    warning = _BoundedWarning()
    while True:
        try:
            removed = await writer.run(partial(cleanup_request_logs_once, engine))
        except Exception:
            warning.warn("API request retention cleanup unavailable; will retry.")
            await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)
        else:
            await asyncio.sleep(
                _BACKLOG_RETRY_SECONDS if removed == 1_000 else _RETENTION_INTERVAL_SECONDS
            )


class RequestLogMiddleware:
    """Record the final application-visible response, including safe 4xx/5xx errors.

    Install outside both RequestIDMiddleware and SafeExceptionMiddleware. A
    failed metadata write never changes the response that was already sent.
    """

    def __init__(self, app: ASGIApp, *, engine: Engine) -> None:
        self.app = app
        self.engine = engine
        self._warning = _BoundedWarning()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_api_request(scope):
            await self.app(scope, receive, send)
            return

        identity = RequestIdentity()
        context_token = begin_request_identity(identity)
        started_at_utc_us = time_ns() // 1_000
        started_at_monotonic_ns = monotonic_ns()
        status_code: int | None = None
        response_complete = False

        async def send_with_status(message: Message) -> None:
            nonlocal status_code, response_complete
            await send(message)
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            duration_us = max(0, (monotonic_ns() - started_at_monotonic_ns) // 1_000)
            end_request_identity(context_token)
            request_id = scope.get("state", {}).get(REQUEST_ID_STATE_ATTRIBUTE)
            if not isinstance(request_id, str):
                request_id = generate_request_id()
            try:
                entry = RequestLogWrite(
                    request_id=request_id,
                    method=_method(scope),
                    route_template=_route_template(scope),
                    status_code=status_code,
                    completion="completed" if response_complete else "interrupted",
                    occurred_at=started_at_utc_us,
                    duration_us=duration_us,
                    caller_id=identity.caller_id if status_code != 401 else None,
                    home_library_id=identity.home_library_id if status_code != 401 else None,
                    credential_id=identity.credential_id if status_code != 401 else None,
                )
                with anyio.CancelScope(shield=True):
                    writer = scope.get("state", {}).get(REQUEST_LOG_WRITER_STATE_ATTRIBUTE)
                    if not isinstance(writer, RequestLogWriter):
                        # Preserve direct ASGI/no-lifespan use. Normal servers
                        # receive the shared writer from lifespan state.
                        writer = RequestLogWriter()
                    await writer.run(partial(_write_request, self.engine, entry))
            except Exception:
                self._warning.warn("API request metadata could not be persisted.")
