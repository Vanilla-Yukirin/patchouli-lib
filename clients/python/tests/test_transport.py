from __future__ import annotations

from collections.abc import Callable
from typing import cast

import httpx
import pytest
from conftest import load_agent_wire_fixture

from patchouli_client import (
    BearerToken,
    IdempotencyKey,
    OperationKind,
    RetryPolicy,
    Transport,
    TransportError,
)

_RETRY_FIXTURE = cast(dict[str, object], load_agent_wire_fixture()["retry"])
ACCEPTED_RETRY_STATUSES = tuple(cast(list[int], _RETRY_FIXTURE["statuses"]))
NEVER_RETRY_STATUSES = tuple(cast(list[int], _RETRY_FIXTURE["never_statuses"]))


def _transport(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    attempts: int = 3,
    delays: list[float] | None = None,
) -> Transport:
    return Transport(
        "https://patchouli.example.invalid",
        http_transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(
            max_attempts=attempts,
            backoff_base_seconds=0.25,
            backoff_cap_seconds=1,
        ),
        sleep=(delays.append if delays is not None else lambda _: None),
        random_value=lambda: 0.5,
    )


@pytest.mark.parametrize("status", ACCEPTED_RETRY_STATUSES)
def test_read_retries_accepted_transient_statuses(status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status)
        return httpx.Response(200)

    with _transport(handler) as transport:
        response = transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/search",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
            json_body={"query": "synthetic", "limit": 20},
        )

    assert response.status_code == 200
    assert attempts == 2


def test_read_retries_bounded_connection_failures() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("synthetic connection failure", request=request)
        return httpx.Response(200)

    with _transport(handler, delays=delays) as transport:
        response = transport.send(
            "GET",
            "/api/v1/capabilities",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
        )

    assert response.status_code == 200
    assert attempts == 3
    assert delays == [0.125, 0.25]


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.WriteError,
        httpx.WriteTimeout,
        httpx.CloseError,
    ],
)
def test_all_approved_transient_request_errors_are_retried(
    error_type: type[httpx.RequestError],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type("synthetic transport failure", request=request)
        return httpx.Response(200)

    with _transport(handler) as transport:
        response = transport.send(
            "GET",
            "/api/v1/capabilities",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
        )

    assert response.status_code == 200
    assert attempts == 2


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.LocalProtocolError,
        httpx.ProxyError,
        httpx.UnsupportedProtocol,
        httpx.DecodingError,
        httpx.TooManyRedirects,
        httpx.RequestError,
    ],
)
def test_non_transient_request_errors_are_safely_mapped_without_retry(
    error_type: type[httpx.RequestError],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_type("unsafe synthetic upstream detail", request=request)

    with _transport(handler) as transport, pytest.raises(TransportError) as caught:
        transport.send(
            "GET",
            "/api/v1/capabilities",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
        )

    assert attempts == 1
    assert caught.value.attempts == 1
    assert "unsafe synthetic upstream detail" not in str(caught.value)


def test_write_retry_reuses_body_and_idempotency_key() -> None:
    seen: list[tuple[bytes, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.content,
                request.headers["Idempotency-Key"],
                request.headers["Authorization"],
            )
        )
        return httpx.Response(503 if len(seen) == 1 else 201)

    with _transport(handler) as transport:
        response = transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.WRITE,
            body=b"synthetic multipart body",
            replayable=True,
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )

    assert response.status_code == 201
    assert seen == [
        (b"synthetic multipart body", "op_synthetic_123", "Bearer cred_synthetic_123"),
        (b"synthetic multipart body", "op_synthetic_123", "Bearer cred_synthetic_123"),
    ]


def test_non_replayable_write_does_not_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    with _transport(handler) as transport:
        response = transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.WRITE,
            body=b"one-shot body",
            replayable=False,
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )

    assert response.status_code == 503
    assert attempts == 1


def test_non_replayable_write_does_not_retry_connection_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("synthetic connection failure", request=request)

    with _transport(handler) as transport, pytest.raises(TransportError) as caught:
        transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.WRITE,
            body=b"one-shot body",
            replayable=False,
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )

    assert attempts == 1
    assert caught.value.attempts == 1


@pytest.mark.parametrize("status", NEVER_RETRY_STATUSES)
def test_contract_rejection_statuses_are_not_retried(status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    with _transport(handler) as transport:
        response = transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/pages/page_synthetic/revisions",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.WRITE,
            body=b"synthetic body",
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )

    assert response.status_code == status
    assert attempts == 1


def test_retry_policy_can_only_narrow_the_approved_statuses() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502)

    with Transport(
        "https://patchouli.example.invalid",
        http_transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(retry_statuses=frozenset({503})),
        sleep=lambda _: None,
    ) as transport:
        response = transport.send(
            "GET",
            "/api/v1/capabilities",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
        )

    assert response.status_code == 502
    assert attempts == 1


def test_default_retry_policy_matches_public_wire_fixture() -> None:
    assert RetryPolicy().retry_statuses == frozenset(ACCEPTED_RETRY_STATUSES)


@pytest.mark.parametrize("status", [200, 201, *NEVER_RETRY_STATUSES, 500])
def test_retry_policy_rejects_statuses_outside_fixed_matrix(status: int) -> None:
    with pytest.raises(ValueError, match="subset"):
        RetryPolicy(retry_statuses=frozenset({status}))


def test_transport_error_does_not_expose_request_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        operation_key = request.headers["Idempotency-Key"]
        raise httpx.ConnectError(
            f"unsafe upstream message {authorization} {operation_key}", request=request
        )

    with _transport(handler, attempts=1) as transport, pytest.raises(TransportError) as caught:
        transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.WRITE,
            body=b"private synthetic body",
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )

    rendered = f"{caught.value!s} {caught.value!r}"
    assert "cred_synthetic_123" not in rendered
    assert "op_synthetic_123" not in rendered
    assert "private synthetic body" not in rendered


def test_transport_rejects_untyped_secret_headers() -> None:
    with (
        _transport(lambda _: httpx.Response(200)) as transport,
        pytest.raises(ValueError, match="typed parameters"),
    ):
        transport.send(
            "GET",
            "/api/v1/capabilities",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
            headers={"Authorization": "not-accepted"},
        )


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example.invalid/api/v1/capabilities",
        "/outside/v1/capabilities",
        "/api/v1/capabilities?private=query",
        "/api/v1/capabilities#fragment",
    ],
)
def test_transport_keeps_bearer_token_inside_api_namespace(path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe path must not reach the HTTP transport")

    with _transport(handler) as transport, pytest.raises(ValueError, match="namespace"):
        transport.send(
            "GET",
            path,
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.READ,
        )


def test_write_requires_idempotency_key() -> None:
    with (
        _transport(lambda _: httpx.Response(201)) as transport,
        pytest.raises(ValueError, match="idempotency"),
    ):
        transport.send(
            "POST",
            "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
            token=BearerToken("cred_synthetic_123"),
            operation=OperationKind.WRITE,
            body=b"synthetic",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://patchouli.example.invalid",
        "https://user@patchouli.example.invalid",
        "https://patchouli.example.invalid/base/path",
        "https://patchouli.example.invalid?secret=value",
        "https://patchouli.example.invalid#fragment",
    ],
)
def test_transport_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="base URL"):
        Transport(base_url, http_transport=httpx.MockTransport(lambda _: httpx.Response(200)))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"backoff_base_seconds": -1},
    ],
)
def test_retry_policy_rejects_unbounded_values(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="attempt|backoff"):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]
