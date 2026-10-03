"""Synthetic HTTP search pagination and bounded plain-text response parsing."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from conftest import protected_headers

from patchouli_client import (
    BearerToken,
    CurrentPageSearchRequest,
    CurrentPageSearchResult,
    PatchouliClient,
    ProblemError,
    ProtocolError,
)
from patchouli_client.models import MAX_CURSOR_LENGTH


def _item() -> dict[str, object]:
    return {
        "library_id": "library_synthetic",
        "section_id": "section_synthetic",
        "book_id": "book_synthetic",
        "page_id": "page_synthetic",
        "revision_id": "revision_synthetic",
        "revision_number": 2,
        "revision_files_href": (
            "/api/v1/libraries/library_synthetic/sections/section_synthetic"
            "/pages/page_synthetic/revisions/revision_synthetic/files"
        ),
        "title": "Synthetic 中文报告",
        "occurred_at": 1_000_000,
        "match_sources": [{"kind": "file_text", "file_name": "report.md"}],
    }


def test_omitted_cursor_and_old_responses_preserve_search_compatibility() -> None:
    request = CurrentPageSearchRequest(keywords=("synthetic",))
    assert "cursor" not in request.to_wire()
    assert CurrentPageSearchRequest.from_dict(request.to_wire()) == request
    explicit_null = CurrentPageSearchRequest.from_dict({**request.to_wire(), "cursor": None})
    assert "cursor" not in explicit_null.to_wire()
    item = _item()
    for body in (
        {"items": [item]},
        {"items": [{**item, "snippet": None}], "next_cursor": None},
    ):
        result = CurrentPageSearchResult.from_dict(body)
        assert result.next_cursor is None and result.items[0].snippet is None


def test_search_follows_opaque_cursor_in_post_body_and_parses_plain_snippet() -> None:
    request_model = CurrentPageSearchRequest(keywords=("private synthetic query",), limit=1)
    cursor = "synthetic-opaque-search-cursor"
    bodies: list[object] = []
    text = '<script>alert("Synthetic")</script> 中文 plain text'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/api/v1/search"
        assert not request.url.query
        assert request.headers["Authorization"] == "Bearer synthetic-device-token"
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert body == request_model.to_wire() and "cursor" not in body
            return httpx.Response(
                200,
                headers=protected_headers(),
                json={
                    "items": [
                        {
                            **_item(),
                            "snippet": {"file_name": "report.md", "text": text, "matched": True},
                        }
                    ],
                    "next_cursor": cursor,
                },
            )
        assert body == {**request_model.to_wire(), "cursor": cursor}
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [{**_item(), "snippet": None}], "next_cursor": None},
        )

    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        first = client.search_pages(request_model, token=BearerToken("synthetic-device-token"))
        assert first.value.next_cursor == cursor
        snippet = first.value.items[0].snippet
        assert snippet is not None and snippet.file_name == "report.md"
        assert snippet.text == text and snippet.matched is True
        assert text not in repr(snippet)
        second = client.search_pages(
            replace(request_model, cursor=first.value.next_cursor),
            token=BearerToken("synthetic-device-token"),
        )
    assert len(bodies) == 2
    assert second.value.next_cursor is None and second.value.items[0].snippet is None


@pytest.mark.parametrize("cursor", ["", "x" * (MAX_CURSOR_LENGTH + 1), 1, False])
def test_invalid_request_cursor_is_rejected_without_echo(cursor: object) -> None:
    with pytest.raises(ValueError, match="cursor") as rejected:
        CurrentPageSearchRequest.from_dict({"keywords": ["synthetic"], "cursor": cursor})
    assert "synthetic" not in str(rejected.value)


@pytest.mark.parametrize("cursor", ["", "x" * (MAX_CURSOR_LENGTH + 1), 1, False])
def test_invalid_response_cursor_is_not_ignored(cursor: object) -> None:
    with pytest.raises(ProtocolError, match="next_cursor"):
        CurrentPageSearchResult.from_dict({"items": [], "next_cursor": cursor})


@pytest.mark.parametrize(
    "snippet",
    [
        "not-an-object",
        {"file_name": "report.md", "text": "x" * 241, "matched": True},
        {"file_name": "../private.md", "text": "synthetic", "matched": True},
        {"file_name": "report.md", "text": "synthetic\x00text", "matched": True},
        {"file_name": "report.md", "text": "\ud800", "matched": True},
        {"file_name": "report.md", "text": "synthetic", "matched": 1},
        {"file_name": "report.md", "text": 1, "matched": True},
        {"file_name": "report.md", "text": "synthetic"},
    ],
)
def test_invalid_snippet_fails_closed_without_echo(snippet: object) -> None:
    with pytest.raises(ProtocolError) as rejected:
        CurrentPageSearchResult.from_dict({"items": [{**_item(), "snippet": snippet}]})
    assert "synthetic" not in str(rejected.value)
    assert "private.md" not in str(rejected.value)


def test_maximum_cursor_and_snippet_bounds_are_accepted() -> None:
    cursor = "x" * MAX_CURSOR_LENGTH
    request = CurrentPageSearchRequest(keywords=("synthetic",), cursor=cursor)
    assert request.to_wire()["cursor"] == cursor
    assert CurrentPageSearchRequest.from_dict(request.to_wire()) == request
    result = CurrentPageSearchResult.from_dict(
        {
            "items": [
                {
                    **_item(),
                    "snippet": {"file_name": "report.md", "text": "中" * 240, "matched": False},
                }
            ],
            "next_cursor": cursor,
        }
    )
    assert result.next_cursor == cursor
    assert result.items[0].snippet is not None
    assert len(result.items[0].snippet.text) == 240 and not result.items[0].snippet.matched


def test_invalid_cursor_problem_is_propagated_without_automatic_restart() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            headers={**protected_headers(), "Content-Type": "application/problem+json"},
            json={
                "type": "about:blank",
                "title": "Invalid cursor",
                "status": 400,
                "detail": "Restart this search from the first page.",
                "code": "invalid_cursor",
                "request_id": "req_synthetic",
            },
        )

    with (
        PatchouliClient(
            "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ProblemError) as rejected,
    ):
        client.search_pages(
            CurrentPageSearchRequest(keywords=("private synthetic query",), cursor="old-cursor"),
            token=BearerToken("synthetic-device-token"),
        )
    assert calls == 1 and rejected.value.problem.code == "invalid_cursor"
    assert "private synthetic query" not in str(rejected.value)
