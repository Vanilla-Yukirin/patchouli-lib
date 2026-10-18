"""HTTP request records contain only bounded metadata and verified identities."""

from __future__ import annotations

import asyncio
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep, time_ns
from types import SimpleNamespace
from typing import cast
from urllib.request import ProxyHandler, Request, build_opener

import anyio
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import OperationalError

from patchouli_lib.app import create_app
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuthenticatedCaller, CallerKind, NewCaller
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.models import Base
from patchouli_lib.request_log import UNMATCHED_ROUTE, RequestLogRepository, RequestLogWrite
from patchouli_lib.request_log.identity import (
    RequestIdentity,
    begin_request_identity,
    end_request_identity,
    note_authenticated_identity,
)
from patchouli_lib.request_log.middleware import RequestLogMiddleware, cleanup_request_logs_once
from patchouli_lib.request_log.models import RequestLogRecord
from patchouli_lib.request_log.writer import (
    REQUEST_LOG_WRITER_STATE_ATTRIBUTE,
    RequestLogWriter,
)


def _app_with_schema(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'request-capture.db').as_posix()}",
        }
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    return app


def _seed_agent(engine: Engine) -> tuple[str, str, str, str]:
    now = time_ns() // 1_000
    with immediate_transaction(engine) as connection:
        structure = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=iter(("1" * 32, "2" * 32, "3" * 32)).__next__,
            clock=lambda: now,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Library",
                section_name="Synthetic Section",
                book_name="Synthetic Book",
            )
        )
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id="4" * 32,
                library_id=structure.library.id,
                kind=CallerKind.AGENT,
                name="Synthetic Agent",
                created_at=now,
                updated_at=now,
            )
        )
        token = (
            CredentialIssuer(
                auth,
                id_factory=lambda: "5" * 32,
                clock=lambda: now,
            )
            .issue(caller, expires_at=now + 3_600_000_000)
            .value
        )
    return caller.id, structure.library.id, "5" * 32, token


def _record(engine: Engine, request_id: str) -> RequestLogWrite:
    with engine.connect() as connection:
        row = RequestLogRepository(connection).get(request_id)
    assert row is not None
    return row


def test_success_401_404_500_and_non_api_record_boundaries(tmp_path: Path) -> None:
    app = _app_with_schema(tmp_path)
    caller_id, library_id, credential_id, token = _seed_agent(app.state.engine)

    def synthetic_error() -> None:
        raise RuntimeError("private failure payload")

    app.add_api_route("/api/v1/synthetic-error", synthetic_error, methods=["GET"])

    with TestClient(app, raise_server_exceptions=False) as client:
        success = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"})
        assert success.status_code == 200
        success_row = _record(app.state.engine, success.headers["X-Request-ID"])
        assert success_row.route_template == "/api/v1/auth/whoami"
        assert success_row.status_code == 200
        assert success_row.completion == "completed"
        assert success_row.caller_id == caller_id
        assert success_row.home_library_id == library_id
        assert success_row.credential_id == credential_id
        assert success_row.duration_us >= 0
        assert token not in repr(success_row)

        unauthorized = client.get(
            "/api/v1/auth/whoami", headers={"Authorization": "Bearer invalid"}
        )
        assert unauthorized.status_code == 401
        unauthorized_row = _record(app.state.engine, unauthorized.headers["X-Request-ID"])
        assert unauthorized_row.status_code == 401
        assert unauthorized_row.caller_id is None
        assert unauthorized_row.credential_id is None

        missing = client.get("/api/v1/private-document-title?search=private-search-terms")
        assert missing.status_code == 404
        missing_row = _record(app.state.engine, missing.headers["X-Request-ID"])
        assert missing_row.route_template == UNMATCHED_ROUTE
        assert "private" not in repr(missing_row)

        failure = client.get("/api/v1/synthetic-error")
        assert failure.status_code == 500
        failure_row = _record(app.state.engine, failure.headers["X-Request-ID"])
        assert failure_row.route_template == "/api/v1/synthetic-error"
        assert failure_row.status_code == 500
        assert "private failure payload" not in repr(failure_row)

        other_method = client.request("TRACE", "/api/v1/auth/whoami")
        assert other_method.status_code == 405
        other_row = _record(app.state.engine, other_method.headers["X-Request-ID"])
        assert other_row.method == "OTHER"
        assert other_row.status_code == 405

        assert client.get("/health/live").status_code == 200

    with app.state.engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(RequestLogRecord))
    assert count == 5


def test_metadata_write_failure_does_not_change_response(tmp_path: Path) -> None:
    app = _app_with_schema(tmp_path)
    with app.state.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE api_request_log")
    with TestClient(app) as client:
        response = client.get("/api/v1/missing")
    assert response.status_code == 404
    assert response.headers["X-Request-ID"].startswith("req_")


def test_identity_cell_is_isolated_across_worker_threads_and_concurrent_requests() -> None:
    async def request(suffix: str) -> tuple[str | None, str | None]:
        identity = RequestIdentity()
        reset_token = begin_request_identity(identity)
        try:
            authenticated = SimpleNamespace(
                caller=SimpleNamespace(id=suffix * 32, library_id="a" * 32),
                credential=SimpleNamespace(id="b" * 32),
            )
            await anyio.to_thread.run_sync(
                lambda: note_authenticated_identity(cast(AuthenticatedCaller, authenticated))
            )
            await asyncio.sleep(0)
            return identity.caller_id, identity.credential_id
        finally:
            end_request_identity(reset_token)

    async def run() -> list[tuple[str | None, str | None]]:
        return list(await asyncio.gather(request("1"), request("2")))

    assert asyncio.run(run()) == [("1" * 32, "b" * 32), ("2" * 32, "b" * 32)]


def test_started_but_incomplete_response_is_recorded_as_interrupted(tmp_path: Path) -> None:
    app = _app_with_schema(tmp_path)
    engine = app.state.engine
    request_id = "req_" + "a" * 32

    async def failing_application(scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("private streaming failure")

    recorder = RequestLogMiddleware(failing_application, engine=engine)

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):  # type: ignore[no-untyped-def]
        return None

    async def run() -> None:
        with pytest.raises(RuntimeError, match="private streaming failure"):
            await recorder(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "method": "GET",
                    "path": "/api/v1/stream",
                    "state": {"request_id": request_id},
                },
                receive,
                send,
            )

    asyncio.run(run())
    row = _record(engine, request_id)
    assert row.completion == "interrupted"
    assert row.status_code == 200
    assert row.route_template == UNMATCHED_ROUTE


def test_retention_deletes_only_expired_records_in_bounded_batches(tmp_path: Path) -> None:
    app = _app_with_schema(tmp_path)
    now = 4_000_000_000_000_000
    cutoff = now - 30 * 86_400_000_000
    with immediate_transaction(app.state.engine) as connection:
        repository = RequestLogRepository(connection)
        for suffix, occurred_at in (("1", cutoff - 1), ("2", cutoff - 2), ("3", cutoff)):
            repository.add(
                RequestLogWrite(
                    request_id="req_" + suffix * 32,
                    method="GET",
                    route_template="/api/v1/synthetic",
                    status_code=200,
                    completion="completed",
                    occurred_at=occurred_at,
                    duration_us=1,
                )
            )

    assert cleanup_request_logs_once(app.state.engine, now_utc_us=now, batch_size=1) == 1
    assert cleanup_request_logs_once(app.state.engine, now_utc_us=now, batch_size=1) == 1
    assert cleanup_request_logs_once(app.state.engine, now_utc_us=now, batch_size=1) == 0
    assert _record(app.state.engine, "req_" + "3" * 32).occurred_at == cutoff


def test_lifespan_runs_retention_on_startup_and_again_periodically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app_with_schema(tmp_path)
    calls = 0
    repeated = Event()

    def observe_cleanup(_engine: Engine) -> int:
        nonlocal calls
        calls += 1
        if calls >= 2:
            repeated.set()
        return 0

    monkeypatch.setattr(
        "patchouli_lib.request_log.middleware.cleanup_request_logs_once", observe_cleanup
    )
    monkeypatch.setattr("patchouli_lib.request_log.middleware._RETENTION_INTERVAL_SECONDS", 0.01)
    with TestClient(app):
        assert repeated.wait(timeout=2)


@pytest.mark.parametrize("concurrency", [1, 4])
def test_personal_concurrency_persists_every_request_id_across_reused_lifespans(
    tmp_path: Path, concurrency: int
) -> None:
    app = _app_with_schema(tmp_path)
    _, _, _, token = _seed_agent(app.state.engine)
    writers: list[object] = []
    request_ids: list[str] = []
    for _ in range(2):
        with TestClient(app) as client:
            writers.append(client.app_state[REQUEST_LOG_WRITER_STATE_ATTRIBUTE])

            def request(_index: int) -> str:
                response = client.get(
                    "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
                )
                assert response.status_code == 200
                request_id = response.headers["X-Request-ID"]
                assert _record(app.state.engine, request_id).status_code == 200
                return request_id

            with ThreadPoolExecutor(max_workers=concurrency) as workers:
                request_ids.extend(workers.map(request, range(12)))
    assert writers[0] is not writers[1]
    assert len(set(request_ids)) == 24
    with app.state.engine.connect() as connection:
        persisted = set(connection.scalars(select(RequestLogRecord.request_id)))
    assert persisted == set(request_ids)


@pytest.mark.parametrize("concurrency", [1, 4])
def test_real_http_personal_concurrency_persists_every_request_id_after_shutdown(
    tmp_path: Path, concurrency: int
) -> None:
    app = _app_with_schema(tmp_path)
    _, _, _, token = _seed_agent(app.state.engine)
    request_ids: list[str] = []
    # Bind once to a random loopback port; do not touch any existing service.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
        )
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 5
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started

            def request(_index: int) -> str:
                opener = build_opener(ProxyHandler({}))
                http_request = Request(
                    f"http://127.0.0.1:{port}/api/v1/auth/whoami",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with opener.open(http_request, timeout=5) as response:
                    assert response.status == 200
                    response.read()
                    request_id = response.headers["X-Request-ID"]
                    assert isinstance(request_id, str)
                    return request_id

            with ThreadPoolExecutor(max_workers=concurrency) as workers:
                request_ids.extend(workers.map(request, range(12)))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
    # Persistence follows response completion: check after graceful shutdown,
    # not at the instant the client receives the response headers/body.
    assert len(set(request_ids)) == 12
    with app.state.engine.connect() as connection:
        rows = connection.execute(
            select(RequestLogRecord.request_id, RequestLogRecord.status_code)
        ).all()
    assert {row.request_id for row in rows} == set(request_ids)
    assert all(row.status_code == 200 for row in rows)


def test_retention_and_capture_share_gate_before_worker_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from patchouli_lib.request_log import middleware

    app = _app_with_schema(tmp_path)
    cleanup_entered = Event()
    cleanup_release = Event()
    write_entered = Event()
    original_cleanup = middleware.cleanup_request_logs_once
    original_write = middleware._write_request

    def cleanup(engine: Engine) -> int:
        cleanup_entered.set()
        assert cleanup_release.wait(timeout=2)
        return original_cleanup(engine)

    def write(engine: Engine, entry: RequestLogWrite) -> None:
        write_entered.set()
        original_write(engine, entry)

    monkeypatch.setattr(middleware, "cleanup_request_logs_once", cleanup)
    monkeypatch.setattr(middleware, "_write_request", write)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as workers:
        assert cleanup_entered.wait(timeout=2)
        response = workers.submit(client.get, "/api/v1/missing")
        assert not write_entered.wait(timeout=0.05)
        cleanup_release.set()
        result = response.result(timeout=2)
        assert result.status_code == 404
        assert _record(app.state.engine, result.headers["X-Request-ID"]).status_code == 404
    assert write_entered.is_set()


def test_permanent_busy_failure_keeps_response_and_warning_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app_with_schema(tmp_path)
    attempts = 0

    def fail(_engine: Engine, _entry: RequestLogWrite) -> None:
        nonlocal attempts
        attempts += 1
        original = sqlite3.OperationalError("private-token-body-value")
        original.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise OperationalError("private SQL", {"token": "private-token-body-value"}, original)

    monkeypatch.setattr("patchouli_lib.request_log.middleware._write_request", fail)
    with TestClient(app) as client:
        response = client.get("/api/v1/missing?token=private-token-body-value")
    assert response.status_code == 404
    assert attempts == 3
    assert "API request metadata could not be persisted." in caplog.text
    assert "private-token-body-value" not in caplog.text
    assert "private SQL" not in caplog.text


def test_cancelled_retention_waits_for_its_transaction_before_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from patchouli_lib.request_log import middleware

    app = _app_with_schema(tmp_path)
    entered = Event()
    release = Event()
    finished = Event()

    def cleanup(_engine: Engine) -> int:
        entered.set()
        assert release.wait(timeout=2)
        finished.set()
        return 0

    monkeypatch.setattr(middleware, "cleanup_request_logs_once", cleanup)

    async def run() -> None:
        writer = RequestLogWriter()
        retention = asyncio.create_task(
            middleware.run_request_log_retention(app.state.engine, writer)
        )
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        retention.cancel()
        await asyncio.sleep(0.02)
        assert not retention.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await retention
        await writer.close()
        assert finished.is_set()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("code", "expected_attempts"), [(sqlite3.SQLITE_BUSY, 3), (sqlite3.SQLITE_LOCKED, 1)]
)
def test_cancelled_retention_exits_even_when_final_transaction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, expected_attempts: int
) -> None:
    from patchouli_lib.request_log import middleware

    app = _app_with_schema(tmp_path)
    entered = Event()
    release = Event()
    attempts = 0

    def fail(_engine: Engine) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(timeout=2)
        error = sqlite3.OperationalError("private synthetic cleanup failure")
        error.sqlite_errorcode = code
        raise OperationalError("private SQL", {}, error)

    monkeypatch.setattr(middleware, "cleanup_request_logs_once", fail)

    async def run() -> None:
        writer = RequestLogWriter()
        retention = asyncio.create_task(
            middleware.run_request_log_retention(app.state.engine, writer)
        )
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        retention.cancel()
        await asyncio.sleep(0)
        release.set()
        # Observe without issuing a second cancellation: wait_for would hide
        # the old regression by cancelling retention's 900-second sleep again.
        done, pending = await asyncio.wait({retention}, timeout=1)
        if pending:
            retention.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retention
        assert done == {retention}
        with pytest.raises(asyncio.CancelledError):
            await retention
        assert attempts == expected_attempts
        await writer.close()

    asyncio.run(run())
