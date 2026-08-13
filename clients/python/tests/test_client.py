from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from conftest import load_agent_wire_fixture, protected_headers, sample_page

from patchouli_client import (
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    BearerToken,
    IdempotencyKey,
    MarkdownContent,
    PatchouliClient,
    ProblemError,
    ProtocolError,
    RetryPolicy,
    SearchRequest,
    SourceInput,
)


def test_capabilities_and_whoami_are_typed_and_token_is_call_scoped() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(
                200,
                headers=protected_headers(),
                json={
                    "api_versions": ["v1"],
                    "features": ["archive", "search"],
                    "limits": {
                        "max_content_bytes": 2 * 1024 * 1024,
                        "default_page_size": 20,
                        "max_page_size": 100,
                        "max_query_bytes": 4096,
                    },
                    "idempotency": {
                        "content_mutations": True,
                        "successful_replay_retention": "indefinite-alpha",
                    },
                    "future": "ignored",
                },
            )
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={
                "caller_id": "caller_synthetic",
                "credential_id": "credential_synthetic",
                "kind": "agent",
                "expires_at": "2026-09-01T00:00:00.000000Z",
                "policy_version": 3,
                "grants": [
                    {
                        "section_id": "sec_synthetic",
                        "actions": ["section:query", "page:read", "archive:write"],
                    }
                ],
            },
        )

    credential = BearerToken("cred_synthetic_123")
    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        capabilities = client.capabilities(token=credential)
        whoami = client.whoami(token=credential)

    assert capabilities.value.api_versions == ("v1",)
    assert capabilities.value.limits.max_content_bytes == 2 * 1024 * 1024
    assert capabilities.metadata.request_id == "req_synthetic"
    assert capabilities.metadata.cache_control.contains("no-store")
    assert whoami.value.grants[0].actions == (
        "section:query",
        "page:read",
        "archive:write",
    )
    assert all(
        request.headers["Authorization"] == "Bearer cred_synthetic_123" for request in requests
    )
    assert not hasattr(client, "token")


def test_collection_routes_expose_opaque_cursor() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        item: object
        if request.url.path.endswith("/books"):
            item = {
                "section_id": "sec_synthetic",
                "book_id": "book_synthetic",
                "title": "Synthetic book",
            }
        elif request.url.path.endswith("/pages"):
            document = sample_page(content=None)
            item = {
                "page": document["page"],
                "citation": document["citation"],
            }
        else:
            item = {"section_id": "sec_synthetic", "name": "Synthetic section"}
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [item], "next_cursor": "cursor_opaque", "future": 1},
        )

    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        sections = client.list_sections(token=BearerToken("cred_synthetic_123"))
        books = client.list_books(
            "sec_synthetic",
            token=BearerToken("cred_synthetic_123"),
            cursor="cursor_input",
        )
        pages = client.list_pages("sec_synthetic", token=BearerToken("cred_synthetic_123"))

    assert sections.value.items[0].name == "Synthetic section"
    assert books.value.items[0].book_id == "book_synthetic"
    assert pages.value.items[0].page.page_id == "20260811t091500123z-synthetic-session"
    assert pages.value.items[0].citation.revision_number == 1
    assert sections.value.next_cursor == "cursor_opaque"
    assert "limit=20" in paths[0]
    assert "cursor=cursor_input" in paths[1]


def test_page_collection_rejects_non_object_item() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [[]], "next_cursor": None},
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid",
            http_transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ProtocolError, match="response must be a JSON object"),
    ):
        client.list_pages("sec_synthetic", token=BearerToken("cred_synthetic_123"))


def test_search_is_post_json_and_returns_exact_citation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/sections/sec_synthetic/search"
        assert request.headers["Content-Type"].startswith("application/json")
        assert json.loads(request.content) == {
            "query": "synthetic query",
            "limit": 10,
            "cursor": "cursor_input",
        }
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={
                "items": [
                    {
                        "page": sample_page(content=None)["page"],
                        "citation": sample_page()["citation"],
                        "snippet": "Synthetic snippet",
                    }
                ],
                "next_cursor": None,
            },
        )

    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        result = client.search(
            "sec_synthetic",
            SearchRequest(query="synthetic query", limit=10, cursor="cursor_input"),
            token=BearerToken("cred_synthetic_123"),
        )

    assert result.value.items[0].citation.revision_number == 1
    assert result.value.items[0].citation.revision_id.startswith("rev_")


def test_create_archive_multipart_and_response_headers() -> None:
    requests: list[httpx.Request] = []
    fixture = load_agent_wire_fixture()
    mutation_success = fixture["mutation_success"]
    assert isinstance(mutation_success, dict)
    create_replay = mutation_success["create_replay"]
    assert isinstance(create_replay, dict)
    status = cast(int, create_replay["status"])
    headers = cast(dict[str, str], create_replay["headers"])
    response_body = cast(dict[str, object], create_replay["body"])
    assert isinstance(status, int)
    assert isinstance(headers, dict)
    assert isinstance(response_body, dict)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(status, headers=headers, json=response_body)

    with PatchouliClient(
        "https://patchouli.example.invalid",
        http_transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    ) as client:
        result = client.create_archive(
            "sec_synthetic",
            "book_synthetic",
            ArchiveCreateMetadata(
                title="Synthetic session",
                occurred_at=datetime(2026, 8, 11, 9, 15, tzinfo=UTC),
                source=SourceInput(kind="conversation", locator="source:synthetic"),
            ),
            MarkdownContent.from_text("# Synthetic archive"),
            token=BearerToken("cred_synthetic_123"),
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )

    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert all(request.headers["Idempotency-Key"] == "op_synthetic_123" for request in requests)
    content_type = requests[0].headers["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=patchouli-")
    request_body = requests[0].content
    assert b'name="metadata"' in request_body
    assert b"Content-Type: application/json" in request_body
    assert b'name="content"' in request_body
    assert b"Content-Type: text/markdown;charset=utf-8" in request_body
    assert b"filename=" not in request_body
    assert b"source:synthetic" in request_body
    assert b"# Synthetic archive" in request_body
    assert result.metadata.etag == '"revision-synthetic-1"'
    assert result.metadata.location == headers["Location"]
    assert result.metadata.request_id == headers["X-Request-ID"]
    assert result.metadata.idempotency_replayed is True


def test_revise_archive_requires_and_sends_strong_if_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/revisions")
        assert request.headers["If-Match"] == '"revision-synthetic-1"'
        return httpx.Response(
            201,
            headers=protected_headers(
                ETag='"revision-synthetic-2"',
                Location=(
                    "/api/v1/sections/sec_synthetic/pages/"
                    "20260811t091500123z-synthetic-session/revisions/2"
                ),
            ),
            json=sample_page(
                revision_number=2,
                revision_id="rev_22222222222222222222222222222222",
            ),
        )

    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        result = client.revise_archive(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            ArchiveRevisionMetadata(source=SourceInput(kind="conversation")),
            MarkdownContent.from_text("# Complete revised body"),
            token=BearerToken("cred_synthetic_123"),
            idempotency_key=IdempotencyKey("op_synthetic_456"),
            if_match='"revision-synthetic-1"',
        )

        with pytest.raises(ProtocolError, match="strong ETag"):
            client.revise_archive(
                "sec_synthetic",
                "20260811t091500123z-synthetic-session",
                ArchiveRevisionMetadata(source=SourceInput(kind="conversation")),
                MarkdownContent.from_text("# Body"),
                token=BearerToken("cred_synthetic_123"),
                idempotency_key=IdempotencyKey("op_synthetic_789"),
                if_match='W/"weak"',
            )

    assert result.metadata.etag == '"revision-synthetic-2"'


def test_revise_location_must_identify_the_response_revision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(
                ETag='"revision-synthetic-2"',
                Location=(
                    "/api/v1/sections/sec_synthetic/pages/20260811t091500123z-synthetic-session"
                ),
            ),
            json=sample_page(
                revision_number=2,
                revision_id="rev_22222222222222222222222222222222",
            ),
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="Location"),
    ):
        client.revise_archive(
            "sec_synthetic",
            "page_alias",
            ArchiveRevisionMetadata(source=SourceInput(kind="conversation")),
            MarkdownContent.from_text("# Synthetic revision"),
            token=BearerToken("cred_synthetic_123"),
            idempotency_key=IdempotencyKey("op_synthetic_456"),
            if_match='"revision-synthetic-1"',
        )


def test_current_and_exact_revision_fetch_keep_identifiers_opaque() -> None:
    raw_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_paths.append(request.url.raw_path)
        headers = protected_headers()
        if "/revisions/" not in request.url.path:
            headers["ETag"] = '"revision-synthetic-1"'
        return httpx.Response(200, headers=headers, json=sample_page())

    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        current = client.get_page(
            "sec_synthetic",
            "opaque%2Fidentifier",
            token=BearerToken("cred_synthetic_123"),
        )
        exact = client.get_revision(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            1,
            token=BearerToken("cred_synthetic_123"),
        )

    assert b"opaque%252Fidentifier" in raw_paths[0]
    assert raw_paths[1].endswith(b"/revisions/1")
    assert current.value.citation.page_id == "20260811t091500123z-synthetic-session"
    assert exact.value.revision.revision_number == 1


@pytest.mark.parametrize("operation", ["books", "pages", "search"])
def test_section_scoped_collections_validate_response_context(operation: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if operation == "books":
            item: dict[str, object] = {
                "section_id": "sec_other",
                "book_id": "book_synthetic",
                "title": "Synthetic book",
            }
        elif operation == "pages":
            document = sample_page()
            page = document["page"]
            citation = document["citation"]
            assert isinstance(page, dict)
            assert isinstance(citation, dict)
            page["section_id"] = "sec_other"
            citation["section_id"] = "sec_other"
            citation["href"] = (
                "/api/v1/sections/sec_other/pages/20260811t091500123z-synthetic-session/revisions/1"
            )
            item = {"page": page, "citation": citation}
        else:
            document = sample_page()
            page = document["page"]
            citation = document["citation"]
            assert isinstance(page, dict)
            assert isinstance(citation, dict)
            page["section_id"] = "sec_other"
            citation["section_id"] = "sec_other"
            citation["href"] = (
                "/api/v1/sections/sec_other/pages/20260811t091500123z-synthetic-session/revisions/1"
            )
            item = {"page": page, "citation": citation, "snippet": "Synthetic snippet"}
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [item], "next_cursor": None},
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="requested Section"),
    ):
        if operation == "books":
            client.list_books("sec_synthetic", token=BearerToken("cred_synthetic_123"))
        elif operation == "pages":
            client.list_pages("sec_synthetic", token=BearerToken("cred_synthetic_123"))
        else:
            client.search(
                "sec_synthetic",
                SearchRequest(query="synthetic"),
                token=BearerToken("cred_synthetic_123"),
            )


def test_current_page_validates_section_and_current_pointer() -> None:
    responses = [sample_page(), sample_page()]
    first_page = responses[0]["page"]
    first_citation = responses[0]["citation"]
    second_page = responses[1]["page"]
    assert isinstance(first_page, dict)
    assert isinstance(first_citation, dict)
    assert isinstance(second_page, dict)
    first_page["section_id"] = "sec_other"
    first_citation["section_id"] = "sec_other"
    first_citation["href"] = (
        "/api/v1/sections/sec_other/pages/20260811t091500123z-synthetic-session/revisions/1"
    )
    second_page["current_revision_id"] = "rev_22222222222222222222222222222222"
    second_page["current_revision_number"] = 2

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=protected_headers(ETag='"revision-synthetic"'),
            json=responses.pop(0),
        )

    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ProtocolError, match="requested Section"):
            client.get_page(
                "sec_synthetic",
                "page_alias",
                token=BearerToken("cred_synthetic_123"),
            )
        with pytest.raises(ProtocolError, match="current Page pointer"):
            client.get_page(
                "sec_synthetic",
                "page_alias",
                token=BearerToken("cred_synthetic_123"),
            )


def test_exact_revision_validates_requested_revision_number() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=protected_headers(),
            json=sample_page(
                revision_number=2,
                revision_id="rev_22222222222222222222222222222222",
            ),
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="requested revision number"),
    ):
        client.get_revision(
            "sec_synthetic",
            "page_alias",
            1,
            token=BearerToken("cred_synthetic_123"),
        )


def test_create_validates_requested_book() -> None:
    response = sample_page()
    page = response["page"]
    assert isinstance(page, dict)
    page["book_id"] = "book_other"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(
                ETag='"revision-synthetic"',
                Location=(
                    "/api/v1/sections/sec_synthetic/pages/20260811t091500123z-synthetic-session"
                ),
            ),
            json=response,
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="requested Book"),
    ):
        client.create_archive(
            "sec_synthetic",
            "book_synthetic",
            ArchiveCreateMetadata(
                title="Synthetic",
                occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
                source=SourceInput(kind="conversation"),
            ),
            MarkdownContent.from_text("# Synthetic"),
            token=BearerToken("cred_synthetic_123"),
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )


@pytest.mark.parametrize(
    "location",
    [
        "https://service.example.invalid/api/v1/sections/sec_synthetic/pages/page_synthetic",
        "/api/v1/sections/sec_synthetic/pages/page%2Dsynthetic",
        "/api/v1/sections/sec_synthetic/pages/../page_synthetic",
        "/api/v1/sections/sec_synthetic\\pages\\page_synthetic",
        "/api/v1/sections/sec_synthetic/pages/page_synthetic?query=value",
        "/api/v1/sections/sec_synthetic//pages/page_synthetic",
        "/api/v1/sections/sec_synthetic/pages/another-resource",
    ],
)
def test_mutation_location_must_be_canonical_and_match_response(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(ETag='"revision-synthetic"', Location=location),
            json=sample_page(),
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="Location"),
    ):
        client.create_archive(
            "sec_synthetic",
            "book_synthetic",
            ArchiveCreateMetadata(
                title="Synthetic",
                occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
                source=SourceInput(kind="conversation"),
            ),
            MarkdownContent.from_text("# Synthetic"),
            token=BearerToken("cred_synthetic_123"),
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )


def test_problem_details_are_typed_and_exception_is_safely_rendered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            412,
            headers={
                "Content-Type": "application/problem+json",
                "Cache-Control": "private, no-store",
                "X-Request-ID": "req_conflict",
            },
            json={
                "type": "about:blank",
                "title": "Precondition failed",
                "status": 412,
                "detail": "Private synthetic document detail must stay out of exceptions.",
                "code": "revision_conflict",
                "request_id": "req_conflict",
                "details": {"current_revision": 2},
                "future_extension": "accepted",
            },
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProblemError) as caught,
    ):
        client.get_page(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            token=BearerToken("cred_synthetic_123"),
        )

    rendered = f"{caught.value!s} {caught.value!r}"
    assert caught.value.problem.details == {"current_revision": 2}
    assert caught.value.problem.extensions == {"future_extension": "accepted"}
    assert "Private synthetic document" not in rendered
    assert "cred_synthetic_123" not in rendered


@pytest.mark.parametrize(
    "problem_name",
    [
        "authentication_required",
        "invalid_token",
        "insufficient_scope",
        "resource_not_found",
        "idempotency_mismatch",
        "revision_conflict",
        "precondition_required",
        "rate_limited",
        "search_unavailable",
        "content_too_large",
        "unsupported_media_type",
        "request_validation_failed",
    ],
)
def test_public_problem_vectors_are_consumed_by_client(problem_name: str) -> None:
    fixture = load_agent_wire_fixture()
    problems = cast(dict[str, object], fixture["problems"])
    vector = cast(dict[str, object], problems[problem_name])
    status = cast(int, vector["status"])
    headers = cast(dict[str, str], vector["headers"])
    body = cast(dict[str, object], vector["body"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json=body)

    with (
        PatchouliClient(
            "https://patchouli.example.invalid",
            http_transport=httpx.MockTransport(handler),
            retry_policy=RetryPolicy(max_attempts=1),
        ) as client,
        pytest.raises(ProblemError) as caught,
    ):
        client.get_revision(
            "sec_synthetic",
            "page_synthetic",
            1,
            token=BearerToken("cred_synthetic_123"),
        )

    assert caught.value.problem.code == problem_name
    assert caught.value.problem.status == status
    assert caught.value.metadata.request_id == headers["X-Request-ID"]
    assert headers["Cache-Control"] == "private, no-store"
    expected_challenges = {
        "authentication_required": "Bearer",
        "invalid_token": 'Bearer error="invalid_token"',
        "insufficient_scope": 'Bearer error="insufficient_scope"',
    }
    if problem_name in expected_challenges:
        assert headers["WWW-Authenticate"] == expected_challenges[problem_name]
    else:
        assert "WWW-Authenticate" not in headers


def test_problem_request_id_must_match_response_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={
                "Content-Type": "application/problem+json",
                "Cache-Control": "private, no-store",
                "X-Request-ID": "req_header",
            },
            json={
                "type": "about:blank",
                "title": "Not found",
                "status": 404,
                "detail": "The resource is absent or hidden.",
                "code": "resource_not_found",
                "request_id": "req_body",
                "details": {},
            },
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="request ID"),
    ):
        client.get_page(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            token=BearerToken("cred_synthetic_123"),
        )


@pytest.mark.parametrize("missing", ["X-Request-ID", "Cache-Control"])
def test_success_requires_protected_response_headers(missing: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = protected_headers()
        del headers[missing]
        return httpx.Response(200, headers=headers, json=sample_page())

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError),
    ):
        client.get_revision(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            1,
            token=BearerToken("cred_synthetic_123"),
        )


def test_current_page_requires_etag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=protected_headers(), json=sample_page())

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="did not contain an ETag"),
    ):
        client.get_page(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            token=BearerToken("cred_synthetic_123"),
        )


@pytest.mark.parametrize("exact", [False, True])
def test_page_fetch_requires_revision_content(exact: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = protected_headers()
        if not exact:
            headers["ETag"] = '"revision-synthetic-1"'
        return httpx.Response(200, headers=headers, json=sample_page(content=None))

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="content"),
    ):
        if exact:
            client.get_revision(
                "sec_synthetic",
                "20260811t091500123z-synthetic-session",
                1,
                token=BearerToken("cred_synthetic_123"),
            )
        else:
            client.get_page(
                "sec_synthetic",
                "20260811t091500123z-synthetic-session",
                token=BearerToken("cred_synthetic_123"),
            )


@pytest.mark.parametrize("missing", ["Location", "ETag"])
def test_mutation_requires_location_and_etag(missing: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = protected_headers(
            ETag='"revision-synthetic-1"',
            Location=("/api/v1/sections/sec_synthetic/pages/20260811t091500123z-synthetic-session"),
        )
        del headers[missing]
        return httpx.Response(201, headers=headers, json=sample_page())

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match=missing),
    ):
        client.create_archive(
            "sec_synthetic",
            "book_synthetic",
            ArchiveCreateMetadata(
                title="Synthetic",
                occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
                source=SourceInput(kind="conversation"),
            ),
            MarkdownContent.from_text("# Synthetic"),
            token=BearerToken("cred_synthetic_123"),
            idempotency_key=IdempotencyKey("op_synthetic_123"),
        )


@pytest.mark.parametrize("problem_status", [400, 500])
def test_problem_status_must_match_http_status(problem_status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={
                "Content-Type": "application/problem+json",
                "Cache-Control": "private, no-store",
                "X-Request-ID": "req_synthetic",
            },
            json={
                "type": "about:blank",
                "title": "Conflict",
                "status": problem_status,
                "detail": "Synthetic mismatch",
                "code": "idempotency_mismatch",
                "request_id": "req_synthetic",
                "details": {},
            },
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="status"),
    ):
        client.get_revision(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            1,
            token=BearerToken("cred_synthetic_123"),
        )


def test_invalid_json_is_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=protected_headers(),
            content=b"not-json",
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match="valid JSON"),
    ):
        client.get_revision(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            1,
            token=BearerToken("cred_synthetic_123"),
        )


def test_client_rejects_invalid_local_route_inputs_without_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid local input must not reach the transport")

    client = PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ValueError, match="positive"):
            client.get_revision(
                "sec_synthetic",
                "20260811t091500123z-synthetic-session",
                0,
                token=BearerToken("cred_synthetic_123"),
            )
        with pytest.raises(ValueError, match="identifiers"):
            client.list_books("", token=BearerToken("cred_synthetic_123"))
        with pytest.raises(ValueError, match="limit"):
            client.list_sections(token=BearerToken("cred_synthetic_123"), limit=0)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"ETag": 'W/"weak"'}, "strong ETag"),
        ({"ETag": '"unterminated'}, "strong ETag"),
        ({"ETag": '"bad"quote"'}, "strong ETag"),
        ({"ETag": '"strong"', "Cache-Control": "public"}, "caching"),
        ({"ETag": '"strong"', "Idempotency-Replayed": "maybe"}, "replay marker"),
    ],
)
def test_response_header_contract_is_enforced(headers: dict[str, str], message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        merged = protected_headers(ETag='"strong"')
        merged.update(headers)
        return httpx.Response(200, headers=merged, json=sample_page())

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError, match=message),
    ):
        client.get_page(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            token=BearerToken("cred_synthetic_123"),
        )


@pytest.mark.parametrize(
    ("status", "content_type"),
    [(200, "text/plain"), (201, "application/json"), (500, "application/json")],
)
def test_unexpected_status_or_media_type_is_protocol_error(status: int, content_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers=protected_headers(**{"Content-Type": content_type}),
            json=sample_page(),
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProtocolError),
    ):
        client.get_revision(
            "sec_synthetic",
            "20260811t091500123z-synthetic-session",
            1,
            token=BearerToken("cred_synthetic_123"),
        )
