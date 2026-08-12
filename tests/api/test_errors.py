from collections.abc import Mapping
from typing import Protocol

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import Field, ValidationError

from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL, WireModel
from patchouli_lib.api.errors import (
    PROBLEM_MEDIA_TYPE,
    ApplicationProblem,
    ProblemDetails,
    authentication_required,
    install_api_exception_handlers,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware

PRIVATE_MARKER = "synthetic-private-marker"
EXPECTED_FIELDS = {
    "type",
    "title",
    "status",
    "detail",
    "code",
    "request_id",
    "details",
}


class ResponseLike(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def json(self) -> dict[str, object]: ...


class RequestBody(WireModel):
    count: int = Field(ge=1)


def build_error_app(*, with_middleware: bool = True) -> FastAPI:
    application = FastAPI()
    install_api_exception_handlers(application)
    if with_middleware:
        application.add_middleware(
            RequestIDMiddleware,
            request_id_factory=lambda: f"req_{'b' * 32}",
        )

    @application.get("/api/v1/auth-required")
    def auth_required() -> None:
        raise authentication_required()

    @application.get("/api/v1/invalid-token")
    def invalid_credential() -> None:
        raise invalid_token()

    @application.get("/api/v1/forbidden")
    def forbidden() -> None:
        raise insufficient_scope()

    @application.get("/api/v1/hidden")
    def hidden() -> None:
        raise resource_not_found()

    @application.get("/api/v1/details")
    def malformed_details() -> None:
        raise ApplicationProblem(
            status_code=409,
            code="synthetic_conflict",
            title="Synthetic conflict",
            detail="The synthetic operation conflicts with current state.",
            details={
                "idempotency_key": PRIVATE_MARKER,
                "safe_but_unreviewed": "must-not-cross-the-wire",
            },
        )

    @application.post("/api/v1/request-validation")
    def request_validation(_payload: RequestBody) -> dict[str, bool]:
        return {"accepted": True}

    @application.get("/api/v1/pydantic-validation")
    def pydantic_validation() -> None:
        RequestBody.model_validate({"count": PRIVATE_MARKER})

    @application.get("/api/v1/http-error")
    def http_error() -> None:
        raise HTTPException(status_code=400, detail=f"SQL exception {PRIVATE_MARKER}")

    @application.get("/api/v1/method-error")
    def method_error() -> None:
        raise HTTPException(
            status_code=405,
            detail=PRIVATE_MARKER,
            headers={
                "Allow": "GET, HEAD",
                "Set-Cookie": PRIVATE_MARKER,
                "X-Synthetic-Secret": PRIVATE_MARKER,
            },
        )

    @application.get("/api/v1/rate-error")
    def rate_error() -> None:
        raise HTTPException(
            status_code=429,
            detail=PRIVATE_MARKER,
            headers={"Retry-After": "120"},
        )

    @application.get("/api/v1/unsafe-headers")
    def unsafe_headers() -> None:
        raise HTTPException(
            status_code=429,
            detail=PRIVATE_MARKER,
            headers={
                "Allow": "GET\r\nX-Synthetic-Secret: exposed",
                "Retry-After": "Sun, 06 Nov 1994 08:49:37 +0100",
            },
        )

    @application.get("/api/v1/unknown-http-error")
    def unknown_http_error() -> None:
        raise HTTPException(status_code=499, detail=PRIVATE_MARKER)

    @application.get("/api/v1/invalid-http-status")
    def invalid_http_status() -> None:
        raise HTTPException(status_code=700, detail=PRIVATE_MARKER)

    @application.get("/api/v1/crash")
    def crash() -> None:
        raise RuntimeError(f"SQL exception with token {PRIVATE_MARKER}")

    @application.get("/api/v1/malformed-problem")
    def malformed_problem() -> None:
        raise ApplicationProblem(
            status_code=400,
            code="INVALID-CODE",
            title="Malformed problem",
            detail=PRIVATE_MARKER,
        )

    return application


def assert_problem_response(response_status: int, response: ResponseLike) -> None:
    assert response.status_code == response_status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    payload = response.json()
    assert set(payload) == EXPECTED_FIELDS
    assert payload["type"] == "about:blank"
    assert payload["status"] == response_status
    assert payload["request_id"] == response.headers[REQUEST_ID_HEADER]


@pytest.mark.parametrize(
    ("path", "status_code", "code", "challenge"),
    [
        ("/api/v1/auth-required", 401, "authentication_required", "Bearer"),
        (
            "/api/v1/invalid-token",
            401,
            "invalid_token",
            'Bearer error="invalid_token"',
        ),
        (
            "/api/v1/forbidden",
            403,
            "insufficient_scope",
            'Bearer error="insufficient_scope"',
        ),
        ("/api/v1/hidden", 404, "resource_not_found", None),
    ],
)
def test_safe_application_problem_factories_use_fixed_bearer_challenges(
    path: str,
    status_code: int,
    code: str,
    challenge: str | None,
) -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.get(path)

    assert_problem_response(status_code, response)
    assert response.json()["code"] == code
    if challenge is not None:
        assert response.headers["WWW-Authenticate"] == challenge
    else:
        assert "WWW-Authenticate" not in response.headers


def test_untyped_problem_details_are_excluded_by_allow_list() -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/details")

    assert_problem_response(409, response)
    assert response.json()["details"] == {}
    assert PRIVATE_MARKER not in response.text
    assert "idempotency_key" not in response.text
    assert "safe_but_unreviewed" not in response.text


def test_problem_model_rejects_unreviewed_detail_keys() -> None:
    with pytest.raises(ValidationError):
        ProblemDetails.model_validate(
            {
                "title": "Synthetic problem",
                "status": 409,
                "detail": "The synthetic operation conflicts with current state.",
                "code": "synthetic_conflict",
                "request_id": f"req_{'c' * 32}",
                "details": {"idempotency_key": PRIVATE_MARKER},
            }
        )


def test_request_validation_does_not_echo_body_or_input() -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/request-validation",
            headers={"Authorization": f"Bearer {PRIVATE_MARKER}"},
            json={"count": PRIVATE_MARKER},
        )

    assert_problem_response(422, response)
    payload = response.json()
    assert payload["code"] == "request_validation_failed"
    assert payload["details"] == {
        "errors": [
            {
                "location": ["body", "count"],
                "type": "int_parsing",
                "message": "Invalid value.",
            }
        ],
        "truncated": False,
    }
    assert PRIVATE_MARKER not in response.text


def test_internal_pydantic_validation_is_fixed_redacted_500() -> None:
    with TestClient(build_error_app()) as client:
        response = client.get("/api/v1/pydantic-validation")

    assert_problem_response(500, response)
    assert response.json()["code"] == "internal_error"
    assert response.json()["details"] == {}
    assert PRIVATE_MARKER not in response.text


@pytest.mark.parametrize(
    ("path", "status_code", "code"),
    [
        ("/api/v1/http-error", 400, "malformed_request"),
        ("/api/v1/unknown-http-error", 499, "request_error"),
        ("/api/v1/not-present", 404, "resource_not_found"),
    ],
)
def test_http_exceptions_use_fixed_details(
    path: str,
    status_code: int,
    code: str,
) -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.get(path)

    assert_problem_response(status_code, response)
    assert response.json()["code"] == code
    assert PRIVATE_MARKER not in response.text
    assert path not in response.text


def test_http_exception_preserves_only_safe_allow_header() -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/method-error")

    assert_problem_response(405, response)
    assert response.headers["Allow"] == "GET, HEAD"
    assert "Set-Cookie" not in response.headers
    assert "X-Synthetic-Secret" not in response.headers
    assert PRIVATE_MARKER not in response.text


def test_http_exception_preserves_safe_retry_after_header() -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/rate-error")

    assert_problem_response(429, response)
    assert response.headers["Retry-After"] == "120"
    assert PRIVATE_MARKER not in response.text


def test_http_exception_drops_noncanonical_or_injectable_safe_headers() -> None:
    with TestClient(build_error_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/unsafe-headers")

    assert_problem_response(429, response)
    assert "Allow" not in response.headers
    assert "Retry-After" not in response.headers
    assert "X-Synthetic-Secret" not in response.headers


def test_unknown_exception_uses_fixed_safe_detail() -> None:
    with TestClient(build_error_app()) as client:
        response = client.get("/api/v1/crash")

    assert_problem_response(500, response)
    assert response.json()["detail"] == "The server could not complete the request."
    assert response.json()["code"] == "internal_error"
    assert PRIVATE_MARKER not in response.text


def test_invalid_http_exception_status_fails_closed() -> None:
    with TestClient(build_error_app()) as client:
        response = client.get("/api/v1/invalid-http-status")

    assert_problem_response(500, response)
    assert response.json()["code"] == "internal_error"
    assert PRIVATE_MARKER not in response.text


def test_malformed_application_problem_fails_closed() -> None:
    with TestClient(build_error_app()) as client:
        response = client.get("/api/v1/malformed-problem")

    assert_problem_response(500, response)
    assert response.json()["code"] == "internal_error"
    assert PRIVATE_MARKER not in response.text


def test_problem_handler_is_safe_without_request_id_middleware() -> None:
    with TestClient(
        build_error_app(with_middleware=False),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/api/v1/auth-required")

    assert_problem_response(401, response)
    assert response.headers[REQUEST_ID_HEADER].startswith("req_")


def test_application_problem_rejects_non_error_status() -> None:
    with pytest.raises(ValueError):
        ApplicationProblem(
            status_code=200,
            code="not_an_error",
            title="Not an error",
            detail="This should not be constructed.",
        )


def test_wire_model_validation_error_type_is_available_for_callers() -> None:
    with pytest.raises(ValidationError):
        RequestBody.model_validate({"count": 0})
