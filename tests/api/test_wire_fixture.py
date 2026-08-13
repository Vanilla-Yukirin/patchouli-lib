from __future__ import annotations

import json
import re
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import Field, ValidationError
from starlette.requests import Request

from patchouli_lib.api.contracts import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    PROTECTED_CACHE_CONTROL,
    Citation,
    OpaqueIdentifier,
    PaginatedResponse,
    WireModel,
    build_api_v1_path,
    validate_api_v1_path,
)
from patchouli_lib.api.errors import (
    PROBLEM_MEDIA_TYPE,
    ProblemDetails,
    ValidationProblemDetails,
    install_api_exception_handlers,
    problem_response,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.retrieval.schemas import PageMetadata

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "api" / "agent_v1_wire.json"
_REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{32}$", re.ASCII)
_STRONG_ETAG_PATTERN = re.compile(r'^"[!#-~]+"$', re.ASCII)
_AUTH_CHALLENGES = {
    "authentication_required": "Bearer",
    "invalid_token": 'Bearer error="invalid_token"',
    "insufficient_scope": 'Bearer error="insufficient_scope"',
}
_MANAGED_PROBLEM_HEADERS = {
    "Content-Type",
    "WWW-Authenticate",
    REQUEST_ID_HEADER,
    "Cache-Control",
}


class SectionItem(WireModel):
    section_id: OpaqueIdentifier
    name: Annotated[str, Field(min_length=1, max_length=200)]


class FixtureRequestBody(WireModel):
    count: int = Field(ge=1)


def load_wire_fixture() -> dict[str, object]:
    value: object = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return as_object(value)


def as_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, object], value)


def as_headers(value: object) -> dict[str, str]:
    headers = as_object(value)
    assert all(isinstance(item, str) for item in headers.values())
    return cast(dict[str, str], headers)


def request_with_id(request_id: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/synthetic-fixture",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "http_version": "1.1",
            "server": ("fixture.invalid", 443),
            "client": ("fixture", 1),
        }
    )
    request.state.request_id = request_id
    return request


def fixed_request_id(request_id: str) -> str:
    return request_id


def test_collection_vectors_use_server_models_and_flat_pagination() -> None:
    fixture = load_wire_fixture()
    pagination = as_object(fixture["pagination"])
    responses = as_object(fixture["responses"])

    assert pagination == {
        "default_limit": DEFAULT_PAGE_LIMIT,
        "max_limit": MAX_PAGE_LIMIT,
    }

    sections = as_object(responses["sections"])
    section_body = as_object(sections["body"])
    parsed_sections = PaginatedResponse[SectionItem].model_validate(section_body)
    assert parsed_sections.model_dump(mode="json") == section_body
    assert parsed_sections.items == [
        SectionItem(section_id="sec_synthetic", name="Synthetic section")
    ]

    pages = as_object(responses["pages"])
    page_body = as_object(pages["body"])
    parsed_pages = PaginatedResponse[PageMetadata].model_validate(page_body)
    assert parsed_pages.model_dump(mode="json") == page_body
    assert parsed_pages.items[0].citation.revision_id == (
        parsed_pages.items[0].page.current_revision_id
    )
    assert parsed_pages.items[0].citation.revision_number == (
        parsed_pages.items[0].page.current_revision_number
    )

    search = as_object(responses["search"])
    search_body = as_object(search["body"])
    parsed_search = PaginatedResponse[dict[str, object]].model_validate(search_body)
    assert parsed_search.model_dump(mode="json") == search_body

    for response in (sections, pages, search):
        body = as_object(response["body"])
        assert set(body) == {"items", "next_cursor"}
        headers = as_headers(response["headers"])
        assert headers["Content-Type"] == "application/json"
        assert headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
        assert _REQUEST_ID_PATTERN.fullmatch(headers[REQUEST_ID_HEADER]) is not None


def test_page_collection_vector_rejects_non_current_citation() -> None:
    responses = as_object(load_wire_fixture()["responses"])
    pages = as_object(responses["pages"])
    page_body = as_object(pages["body"])
    items = cast(list[object], page_body["items"])
    item = as_object(items[0])
    citation = as_object(item["citation"])
    citation["revision_id"] = "rev_ffffffffffffffffffffffffffffffff"

    with pytest.raises(ValidationError, match="current Revision"):
        PaginatedResponse[PageMetadata].model_validate(page_body)


def test_mutation_vector_uses_representable_server_contracts() -> None:
    fixture = load_wire_fixture()
    mutation = as_object(as_object(fixture["mutation_success"])["create_replay"])
    headers = as_headers(mutation["headers"])
    body = as_object(mutation["body"])
    citation = Citation.model_validate(as_object(body["citation"]))

    assert mutation["status"] == HTTPStatus.CREATED
    assert headers == {
        "Content-Type": "application/json",
        "Location": ("/api/v1/sections/sec_synthetic/pages/20260811t091500123z-synthetic-session"),
        "ETag": '"revision-synthetic-1"',
        "Idempotency-Replayed": "true",
        REQUEST_ID_HEADER: "req_33333333333333333333333333333333",
        "Cache-Control": PROTECTED_CACHE_CONTROL,
    }
    assert validate_api_v1_path(headers["Location"]) == headers["Location"]
    assert headers["Location"] == build_api_v1_path(
        "sections", citation.section_id, "pages", citation.page_id
    )
    assert _STRONG_ETAG_PATTERN.fullmatch(headers["ETag"]) is not None
    assert _REQUEST_ID_PATTERN.fullmatch(headers[REQUEST_ID_HEADER]) is not None


def test_every_problem_vector_matches_models_headers_and_problem_helper() -> None:
    problems = as_object(load_wire_fixture()["problems"])
    assert {
        "content_too_large",
        "unsupported_media_type",
        "request_validation_failed",
    } <= set(problems)

    for name, raw_vector in problems.items():
        vector = as_object(raw_vector)
        status = vector["status"]
        headers = as_headers(vector["headers"])
        body = as_object(vector["body"])
        assert isinstance(status, int)

        parsed = ProblemDetails.model_validate(body)
        assert parsed.model_dump(mode="json") == body
        assert parsed.status == status
        assert parsed.code == name
        assert headers["Content-Type"] == PROBLEM_MEDIA_TYPE
        assert headers[REQUEST_ID_HEADER] == parsed.request_id
        assert headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
        assert _REQUEST_ID_PATTERN.fullmatch(parsed.request_id) is not None
        if name == "request_validation_failed":
            assert isinstance(parsed.details, ValidationProblemDetails)
        else:
            assert not isinstance(parsed.details, ValidationProblemDetails)

        if name in _AUTH_CHALLENGES:
            assert headers["WWW-Authenticate"] == _AUTH_CHALLENGES[name]
        else:
            assert "WWW-Authenticate" not in headers

        safe_headers = {
            header_name: header_value
            for header_name, header_value in headers.items()
            if header_name not in _MANAGED_PROBLEM_HEADERS
        }
        rendered = problem_response(
            request_with_id(parsed.request_id),
            status_code=parsed.status,
            code=parsed.code,
            title=parsed.title,
            detail=parsed.detail,
            details=parsed.details,
            safe_headers=safe_headers,
        )
        assert rendered.status_code == status
        assert json.loads(bytes(rendered.body)) == body
        for header_name, header_value in headers.items():
            assert rendered.headers[header_name] == header_value


def test_new_problem_vectors_match_merged_kernel_handlers() -> None:
    problems = as_object(load_wire_fixture()["problems"])

    for name in ("content_too_large", "unsupported_media_type", "request_validation_failed"):
        vector = as_object(problems[name])
        headers = as_headers(vector["headers"])
        request_id = headers[REQUEST_ID_HEADER]
        application = FastAPI()
        install_api_exception_handlers(application)
        application.add_middleware(
            RequestIDMiddleware,
            request_id_factory=partial(fixed_request_id, request_id),
        )

        if name == "request_validation_failed":

            @application.post("/api/v1/synthetic-fixture")
            def invalid_request(_payload: FixtureRequestBody) -> dict[str, bool]:
                return {"accepted": True}

        else:
            status = cast(int, vector["status"])

            @application.post("/api/v1/synthetic-fixture")
            def http_problem(status: int = status) -> None:
                raise HTTPException(status_code=status, detail="synthetic private detail")

        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/v1/synthetic-fixture",
                json={"count": "not-an-integer"},
            )

        assert response.status_code == vector["status"]
        assert response.json() == vector["body"]
        for header_name, header_value in headers.items():
            assert response.headers[header_name] == header_value
        assert "synthetic private detail" not in response.text


def test_retry_matrix_is_fixed_and_classifies_new_problem_statuses() -> None:
    fixture = load_wire_fixture()
    retry = as_object(fixture["retry"])
    problems = as_object(fixture["problems"])

    assert retry == {
        "statuses": [408, 429, 502, 503, 504],
        "never_statuses": [401, 412, 413, 415, 422, 428],
    }
    retry_statuses = set(cast(list[int], retry["statuses"]))
    never_statuses = set(cast(list[int], retry["never_statuses"]))
    assert retry_statuses.isdisjoint(never_statuses)
    assert retry_statuses == {
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
    assert {
        cast(int, as_object(problems[name])["status"])
        for name in (
            "content_too_large",
            "unsupported_media_type",
            "request_validation_failed",
        )
    } <= never_statuses
