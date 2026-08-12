import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.request_ids import (
    INBOUND_CORRELATION_STATE_ATTRIBUTE,
    REQUEST_ID_HEADER,
    RequestIDDependency,
    RequestIDMiddleware,
    validate_inbound_correlation_id,
)

_REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{32}$", re.ASCII)


def build_request_id_app(*, request_id: str = f"req_{'a' * 32}") -> FastAPI:
    application = FastAPI()
    application.add_middleware(
        RequestIDMiddleware,
        request_id_factory=lambda: request_id,
    )

    @application.get("/api/v1/echo")
    def protected_echo(
        request: Request,
        authoritative_request_id: RequestIDDependency,
    ) -> JSONResponse:
        response = JSONResponse(
            {
                "request_id": authoritative_request_id,
                "inbound_correlation_id": getattr(
                    request.state,
                    INBOUND_CORRELATION_STATE_ATTRIBUTE,
                ),
            }
        )
        response.headers[REQUEST_ID_HEADER] = "req_spoofed"
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


def test_server_request_id_is_authoritative_and_aligned() -> None:
    expected = f"req_{'a' * 32}"
    with TestClient(build_request_id_app()) as client:
        response = client.get(
            "/api/v1/echo",
            headers={REQUEST_ID_HEADER: "client-correlation.1"},
        )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == expected
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.json() == {
        "request_id": expected,
        "inbound_correlation_id": "client-correlation.1",
    }


def test_duplicate_inbound_request_ids_are_not_accepted_as_correlation() -> None:
    with TestClient(build_request_id_app()) as client:
        response = client.get(
            "/api/v1/echo",
            headers=[
                (REQUEST_ID_HEADER, "first-correlation"),
                (REQUEST_ID_HEADER, "second-correlation"),
            ],
        )

    assert response.json()["inbound_correlation_id"] is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        " contains-space",
        "contains space",
        "contains,comma",
        "contains/slash",
        "contains\r\ninjection",
        "非ascii",
        "a" * 65,
    ],
)
def test_untrusted_correlation_validation_rejects_unsafe_values(value: str) -> None:
    assert validate_inbound_correlation_id(value) is None


def test_unprotected_response_gets_request_id_without_protected_cache_policy() -> None:
    with TestClient(build_request_id_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == f"req_{'a' * 32}"
    assert "Cache-Control" not in response.headers


def test_invalid_custom_factory_cannot_inject_response_header() -> None:
    with TestClient(build_request_id_app(request_id="bad\r\nInjected: true")) as client:
        response = client.get("/api/v1/echo")

    assert _REQUEST_ID_PATTERN.fullmatch(response.headers[REQUEST_ID_HEADER]) is not None
    assert response.headers[REQUEST_ID_HEADER] != "bad\r\nInjected: true"
