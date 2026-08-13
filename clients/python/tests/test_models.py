from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from conftest import load_agent_wire_fixture, sample_page

from patchouli_client import (
    ApiLimits,
    ArchiveCreateMetadata,
    BearerToken,
    Capabilities,
    IdempotencyKey,
    IdempotencySupport,
    MarkdownContent,
    PageDocument,
    PageMetadata,
    ProblemDetails,
    ProtocolError,
    SearchRequest,
    Section,
    SourceInput,
    WhoAmI,
)
from patchouli_client.models import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    format_rfc3339_utc,
    parse_rfc3339,
    response_cursor,
    response_items,
    response_object,
)
from patchouli_client.multipart import build_archive_multipart


def test_collection_json_vectors_match_top_level_server_shape() -> None:
    fixture = load_agent_wire_fixture()
    pagination = response_object(fixture["pagination"])
    responses = response_object(fixture["responses"])

    assert pagination == {"default_limit": DEFAULT_PAGE_LIMIT, "max_limit": MAX_PAGE_LIMIT}
    for name in ("sections", "pages", "search"):
        response = response_object(responses[name])
        assert response["status"] == 200
        body = response_object(response["body"])
        assert set(body) == {"items", "next_cursor"}
        assert isinstance(response_items(body), list)
        assert response_cursor(body) is None or isinstance(response_cursor(body), str)

    sections = response_object(responses["sections"])
    section_body = response_object(sections["body"])
    section_item = response_object(response_items(section_body)[0])
    assert Section.from_dict(section_item) == Section(
        section_id="sec_synthetic",
        name="Synthetic section",
    )

    pages = response_object(responses["pages"])
    page_body = response_object(pages["body"])
    page_item = PageMetadata.from_dict(response_object(response_items(page_body)[0]))
    assert page_item.page.page_id == page_item.citation.page_id
    assert page_item.page.current_revision_id == page_item.citation.revision_id
    assert page_item.page.current_revision_number == page_item.citation.revision_number


def test_public_fixture_freezes_mutation_and_problem_envelopes() -> None:
    fixture = load_agent_wire_fixture()
    mutation = response_object(response_object(fixture["mutation_success"])["create_replay"])
    headers = response_object(mutation["headers"])

    assert mutation["status"] == 201
    assert set(headers) >= {
        "Location",
        "ETag",
        "Idempotency-Replayed",
        "X-Request-ID",
        "Cache-Control",
    }
    assert headers["Idempotency-Replayed"] == "true"
    assert headers["Cache-Control"] == "private, no-store"

    problems = response_object(fixture["problems"])
    expected_challenges = {
        "authentication_required": "Bearer",
        "invalid_token": 'Bearer error="invalid_token"',
        "insufficient_scope": 'Bearer error="insufficient_scope"',
    }
    for name, challenge in expected_challenges.items():
        problem = response_object(problems[name])
        problem_headers = response_object(problem["headers"])
        body = response_object(problem["body"])
        assert body["code"] == name
        assert problem_headers["WWW-Authenticate"] == challenge


def test_collection_response_requires_top_level_next_cursor() -> None:
    with pytest.raises(ProtocolError, match="next_cursor.*required"):
        response_cursor({"items": [], "pagination": {"next_cursor": None}})


def test_response_models_ignore_unknown_fields() -> None:
    capabilities = Capabilities.from_dict(
        {
            "api_versions": ["v1"],
            "features": ["archive", "search"],
            "limits": {
                "max_content_bytes": 2 * 1024 * 1024,
                "default_page_size": 20,
                "max_page_size": 100,
                "max_query_bytes": 4096,
                "future_limit": 7,
            },
            "idempotency": {
                "content_mutations": True,
                "successful_replay_retention": "indefinite-alpha",
                "future_mode": "ignored",
            },
            "future_capability": {"ignored": True},
        }
    )

    assert capabilities.api_versions == ("v1",)
    assert capabilities.limits.max_page_size == 100
    assert capabilities.idempotency.content_mutations is True


def test_page_and_revision_identifiers_remain_opaque() -> None:
    document = PageDocument.from_dict(sample_page())

    assert document.page.page_id == "20260811t091500123z-synthetic-session"
    assert document.revision.revision_id == "rev_0123456789abcdef0123456789abcdef"
    assert document.citation.page_id == document.page.page_id
    assert document.page.occurred_at == datetime(2026, 8, 11, 9, 15, 0, 123456, tzinfo=UTC)


def test_problem_details_preserve_unknown_extensions() -> None:
    problem = ProblemDetails.from_dict(
        {
            "type": "about:blank",
            "title": "Precondition failed",
            "status": 412,
            "detail": "The Page has a newer current Revision.",
            "code": "revision_conflict",
            "request_id": "req_synthetic",
            "details": {"current_revision": 2},
            "future_extension": {"retry": False},
        }
    )

    assert problem.details == {"current_revision": 2}
    assert problem.extensions == {"future_extension": {"retry": False}}


def test_request_models_are_strict_and_canonical() -> None:
    metadata = ArchiveCreateMetadata(
        title="Synthetic session",
        occurred_at=datetime(2026, 8, 11, 17, 15, tzinfo=UTC),
        source=SourceInput(kind="conversation"),
    )

    assert metadata.to_wire() == {
        "title": "Synthetic session",
        "occurred_at": "2026-08-11T17:15:00.000000Z",
        "source": {"kind": "conversation"},
    }
    with pytest.raises(TypeError):
        SearchRequest(query="synthetic", future=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="UTC offset"):
        ArchiveCreateMetadata(
            title="Synthetic",
            occurred_at=datetime(2026, 8, 11, 17, 15),
            source=SourceInput(kind="conversation"),
        )


def test_rfc3339_normalizes_exact_server_syntax_to_canonical_utc() -> None:
    parsed = parse_rfc3339("2026-08-12T09:15:30.12+08:00")

    assert parsed == datetime(2026, 8, 12, 1, 15, 30, 120_000, tzinfo=UTC)
    assert format_rfc3339_utc(parsed) == "2026-08-12T01:15:30.120000Z"
    assert format_rfc3339_utc(datetime(1, 2, 3, 4, 5, 6, 7, tzinfo=UTC)) == (
        "0001-02-03T04:05:06.000007Z"
    )
    assert parse_rfc3339("0001-02-03T04:05:06Z") == datetime(1, 2, 3, 4, 5, 6, tzinfo=UTC)
    assert parse_rfc3339("2026-08-12T09:30:00+08:00") == datetime(2026, 8, 12, 1, 30, tzinfo=UTC)
    assert (
        format_rfc3339_utc(datetime(2026, 8, 12, 9, 30, tzinfo=timezone(timedelta(hours=8))))
        == "2026-08-12T01:30:00.000000Z"
    )


@pytest.mark.parametrize(
    ("wire_value", "datetime_value"),
    [
        (
            "0001-01-01T00:00:00+01:00",
            datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        ),
        (
            "9999-12-31T23:59:59-01:00",
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(-timedelta(hours=1))),
        ),
    ],
)
def test_rfc3339_utc_boundary_overflow_is_stable_protocol_error(
    wire_value: str,
    datetime_value: datetime,
) -> None:
    for operation in (
        lambda: parse_rfc3339(wire_value),
        lambda: format_rfc3339_utc(datetime_value),
    ):
        with pytest.raises(ProtocolError, match="cannot be represented in UTC"):
            operation()


@pytest.mark.parametrize(
    ("wire_value", "datetime_value", "expected", "canonical"),
    [
        (
            "0001-01-01T01:00:00+01:00",
            datetime(1, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
            datetime(1, 1, 1, tzinfo=UTC),
            "0001-01-01T00:00:00.000000Z",
        ),
        (
            "9999-12-31T22:59:59-01:00",
            datetime(9999, 12, 31, 22, 59, 59, tzinfo=timezone(-timedelta(hours=1))),
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            "9999-12-31T23:59:59.000000Z",
        ),
    ],
)
def test_rfc3339_utc_offset_boundaries_that_fit_match_server_behavior(
    wire_value: str,
    datetime_value: datetime,
    expected: datetime,
    canonical: str,
) -> None:
    assert parse_rfc3339(wire_value) == expected
    assert format_rfc3339_utc(datetime_value) == canonical


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-12T01:02:03",
        "2026-08-12 01:02:03Z",
        "2026-08-12T01:02:03.1234567Z",
        "2026-08-12T01:02:03-00:00",
        "2026-08-12T01:02:60Z",
        "2026-08-12T01:02:03z",
        "2026-08-12T01:02:03,1Z",
        "0000-01-01T00:00:00Z",
    ],
)
def test_rfc3339_rejects_non_server_wire_syntax(timestamp: str) -> None:
    with pytest.raises(ProtocolError, match="RFC 3339"):
        parse_rfc3339(timestamp)


@pytest.mark.parametrize(
    "body, message",
    [
        (b"", "empty"),
        (b"contains\x00nul", "NUL"),
        (b"\xff", "UTF-8"),
        (b"x" * (2 * 1024 * 1024 + 1), "ceiling"),
    ],
    ids=["empty", "nul", "invalid-utf8", "over-limit"],
)
def test_markdown_content_rejects_invalid_bodies(body: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MarkdownContent(body)


def test_secret_wrappers_redact_normal_representations() -> None:
    credential = BearerToken("cred_synthetic_123")
    operation_key = IdempotencyKey("op_synthetic_123")

    combined = f"{credential!s} {credential!r} {operation_key!s} {operation_key!r}"
    assert "cred_synthetic_123" not in combined
    assert "op_synthetic_123" not in combined
    assert combined.count("<redacted>") == 4


def test_sensitive_request_and_response_fields_are_omitted_from_repr() -> None:
    metadata = ArchiveCreateMetadata(
        title="Private synthetic title",
        occurred_at=datetime(2026, 8, 11, 17, 15, tzinfo=UTC),
        source=SourceInput(kind="conversation", locator="private:synthetic-locator"),
    )
    search = SearchRequest(query="private synthetic query")
    document = PageDocument.from_dict(sample_page(content="private synthetic body"))
    problem = ProblemDetails.from_dict(
        {
            "type": "about:blank",
            "title": "Synthetic problem",
            "status": 400,
            "detail": "private synthetic problem detail",
            "code": "invalid_input",
            "request_id": "req_synthetic",
            "details": {},
        }
    )

    rendered = f"{metadata!r} {search!r} {document!r} {problem!r}"
    assert "Private synthetic title" not in rendered
    assert "private:synthetic-locator" not in rendered
    assert "private synthetic query" not in rendered
    assert "private synthetic body" not in rendered
    assert "private synthetic problem detail" not in rendered


def test_page_document_rejects_mismatched_exact_citation() -> None:
    response = sample_page()
    citation = response["citation"]
    assert isinstance(citation, dict)
    citation["revision_id"] = "rev_ffffffffffffffffffffffffffffffff"

    with pytest.raises(ProtocolError, match="did not agree"):
        PageDocument.from_dict(response)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("section_id", "sec_other"),
        ("page_id", "page_other"),
        ("revision_id", "rev_ffffffffffffffffffffffffffffffff"),
        ("revision_number", 2),
    ],
)
def test_page_metadata_rejects_non_current_citation(field: str, value: object) -> None:
    document = sample_page(content=None)
    citation = document["citation"]
    assert isinstance(citation, dict)
    citation[field] = value
    if field == "section_id":
        citation["href"] = (
            "/api/v1/sections/sec_other/pages/20260811t091500123z-synthetic-session/revisions/1"
        )
    elif field == "page_id":
        citation["href"] = "/api/v1/sections/sec_synthetic/pages/page_other/revisions/1"
    elif field == "revision_number":
        citation["href"] = (
            "/api/v1/sections/sec_synthetic/pages/20260811t091500123z-synthetic-session/revisions/2"
        )

    with pytest.raises(ProtocolError, match="current Revision"):
        PageMetadata.from_dict(
            {
                "page": document["page"],
                "citation": citation,
            }
        )


@pytest.mark.parametrize(
    "item",
    [
        {"citation": sample_page(content=None)["citation"]},
        {"page": sample_page(content=None)["page"]},
        {
            "page": sample_page(content=None)["page"],
            "citation": sample_page(content=None)["citation"],
            "future_outer_field": "rejected",
        },
    ],
    ids=["missing-page", "missing-citation", "extra-outer-field"],
)
def test_page_metadata_requires_exact_outer_shape(item: dict[str, object]) -> None:
    with pytest.raises(ProtocolError, match="exactly 'page' and 'citation'"):
        PageMetadata.from_dict(item)


@pytest.mark.parametrize("field", ["page", "citation"])
def test_page_metadata_requires_nested_objects(field: str) -> None:
    document = sample_page(content=None)
    item = {
        "page": document["page"],
        "citation": document["citation"],
    }
    item[field] = []

    with pytest.raises(ProtocolError, match=rf"{field} must be a JSON object"):
        PageMetadata.from_dict(item)


def test_page_metadata_preserves_nested_extension_compatibility() -> None:
    document = sample_page(content=None)
    page = document["page"]
    citation = document["citation"]
    assert isinstance(page, dict)
    assert isinstance(citation, dict)
    assert "future_page_field" in page
    assert "future_citation_field" in citation

    parsed = PageMetadata.from_dict({"page": page, "citation": citation})

    assert parsed.page.page_id == parsed.citation.page_id


@pytest.mark.parametrize(
    ("revision_number", "href"),
    [
        (0, "/api/v1/sections/sec_synthetic/pages/page_synthetic/revisions/1"),
        (1, "https://other.example.invalid/api/v1/revisions/1"),
        (1, "/api/v1/revisions/1?private=query"),
        (
            1,
            "/api/v1/sections/sec_synthetic/pages/"
            "20260811t091500123z-synthetic-session/revisions/1#fragment",
        ),
        (
            1,
            "/api/v1/sections/sec_synthetic/pages/"
            "20260811t091500123z-synthetic%2Dsession/revisions/1",
        ),
        (
            1,
            "/api/v1/sections/sec_synthetic/pages/../"
            "20260811t091500123z-synthetic-session/revisions/1",
        ),
        (
            1,
            "/api/v1/sections/sec_synthetic\\pages\\"
            "20260811t091500123z-synthetic-session\\revisions\\1",
        ),
        (
            1,
            "/api/v1/sections/sec_synthetic/pages/"
            "20260811t091500123z-synthetic-session/revisions/2",
        ),
        (
            1,
            "/api/v1/sections/sec_synthetic/pages/"
            "20260811t091500123z-synthetic-session/revisions/1\n",
        ),
    ],
)
def test_citation_requires_positive_revision_and_relative_href(
    revision_number: int, href: str
) -> None:
    response = sample_page()
    citation = response["citation"]
    assert isinstance(citation, dict)
    citation["revision_number"] = revision_number
    citation["href"] = href

    with pytest.raises(ProtocolError, match="citation|Revision|resource path"):
        PageDocument.from_dict(response)


def test_response_model_type_errors_are_bounded_protocol_errors() -> None:
    with pytest.raises(ProtocolError, match="limits must be a JSON object"):
        Capabilities.from_dict(
            {"api_versions": [], "features": [], "limits": [], "idempotency": {}}
        )
    with pytest.raises(ProtocolError, match="section_id.*string"):
        Section.from_dict({"section_id": 1, "name": "Synthetic"})
    with pytest.raises(ProtocolError, match="instance.*string or null"):
        ProblemDetails.from_dict(
            {
                "type": "about:blank",
                "title": "Synthetic",
                "status": 400,
                "detail": "Synthetic detail",
                "code": "invalid_input",
                "request_id": "req_synthetic",
                "instance": 3,
            }
        )
    with pytest.raises(ProtocolError, match="max_content_bytes.*integer"):
        ApiLimits.from_dict(
            {
                "max_content_bytes": True,
                "default_page_size": 20,
                "max_page_size": 100,
                "max_query_bytes": 4096,
            }
        )
    with pytest.raises(ProtocolError, match="content_mutations.*boolean"):
        IdempotencySupport.from_dict(
            {"content_mutations": "yes", "successful_replay_retention": "indefinite"}
        )
    with pytest.raises(ProtocolError, match="features.*array of strings"):
        Capabilities.from_dict(
            {
                "api_versions": ["v1"],
                "features": [1],
                "limits": {},
                "idempotency": {},
            }
        )
    with pytest.raises(ProtocolError, match="grants.*array"):
        WhoAmI.from_dict(
            {
                "caller_id": "caller_synthetic",
                "credential_id": "credential_synthetic",
                "kind": "agent",
                "expires_at": "2026-09-01T00:00:00Z",
                "policy_version": 1,
                "grants": {},
            }
        )


@pytest.mark.parametrize("timestamp", ["not-a-time", "2026-08-11T09:15:00"])
def test_response_timestamp_requires_valid_rfc3339_offset(timestamp: str) -> None:
    response = sample_page()
    page = response["page"]
    assert isinstance(page, dict)
    page["occurred_at"] = timestamp

    with pytest.raises(ProtocolError, match="timestamp|timezone-free"):
        PageDocument.from_dict(response)


def test_request_value_invariants_are_strict() -> None:
    with pytest.raises(ValueError, match="source kind"):
        SourceInput(kind="")
    with pytest.raises(ValueError, match="title"):
        ArchiveCreateMetadata(
            title="",
            occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
            source=SourceInput(kind="conversation"),
        )
    with pytest.raises(ValueError, match="query"):
        SearchRequest(query="")
    with pytest.raises(ValueError, match="limit"):
        SearchRequest(query="synthetic", limit=101)
    with pytest.raises(ValueError, match="integer"):
        SearchRequest(query="synthetic", limit=True)
    with pytest.raises(ValueError, match="cursor"):
        SearchRequest(query="synthetic", cursor=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bytes"):
        MarkdownContent("synthetic")  # type: ignore[arg-type]


def test_multipart_rejects_unsafe_boundary() -> None:
    with pytest.raises(ValueError, match="boundary"):
        build_archive_multipart(
            {"source": {"kind": "conversation"}},
            MarkdownContent.from_text("# Synthetic"),
            boundary='unsafe"boundary',
        )


@pytest.mark.parametrize("value", ["", "contains space", "contains\nnewline", "é"])
def test_secret_wrappers_reject_unsafe_header_values(value: str) -> None:
    with pytest.raises(ValueError):
        BearerToken(value)
    with pytest.raises(ValueError):
        IdempotencyKey(value)

    with pytest.raises(ValueError, match="too long"):
        IdempotencyKey("x" * 201)
