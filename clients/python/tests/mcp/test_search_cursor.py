from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import httpx
import jsonschema  # type: ignore[import-untyped]
import pytest

from patchouli_client import BearerToken, PatchouliClient, ProblemError, RetryPolicy
from patchouli_client.models import MAX_CURSOR_LENGTH
from patchouli_mcp import server as server_module
from patchouli_mcp.server import McpRuntime

_TOKEN = "cred_synthetic_search_cursor"
_KEYWORD = "synthetic_private_query"
_CURSOR = "synthetic_cursor_for_next_batch"
_REQUEST_ID = "req_synthetic_cursor"


def _schema() -> dict[str, object]:
    return cast(
        dict[str, object],
        next(
            tool for tool in server_module._tool_inventory() if tool.name == "pages_search"
        ).inputSchema,
    )


def _runtime(client: PatchouliClient) -> McpRuntime:
    # Search dispatch only uses these two fields. Do not load startup config,
    # credentials, a journal, files, a database or a live service in these tests.
    return cast(McpRuntime, SimpleNamespace(client=client, token=BearerToken(_TOKEN)))


def _headers(*, problem: bool = False) -> dict[str, str]:
    return {
        "Content-Type": "application/problem+json" if problem else "application/json",
        "Cache-Control": "private, no-store",
        "X-Request-ID": _REQUEST_ID,
    }


@pytest.mark.parametrize("cursor", [None, _CURSOR, "x" * MAX_CURSOR_LENGTH])
def test_search_schema_accepts_optional_bounded_cursor(cursor: str | None) -> None:
    schema = _schema()
    jsonschema.validate({"keywords": [_KEYWORD]}, schema)
    jsonschema.validate({"keywords": [_KEYWORD], "cursor": cursor}, schema)
    assert "cursor" not in cast(list[str], schema["required"])
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("cursor", ["", "x" * (MAX_CURSOR_LENGTH + 1), 1, True, []])
def test_search_schema_rejects_invalid_cursor_shape(cursor: object) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"keywords": [_KEYWORD], "cursor": cursor}, _schema())


@pytest.mark.parametrize("include_cursor,cursor", [(False, None), (True, None), (True, _CURSOR)])
def test_search_dispatch_keeps_cursor_in_post_body_and_returns_plain_snippet(
    include_cursor: bool, cursor: str | None
) -> None:
    requests: list[httpx.Request] = []
    snippet = {
        "file_name": "说明.md",
        "text": "<script>plain synthetic text</script>",
        "matched": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers=_headers(),
            json={
                "items": [
                    {
                        "library_id": "lib_synthetic",
                        "section_id": "sec_synthetic",
                        "book_id": "book_synthetic",
                        "page_id": "page_synthetic",
                        "revision_id": "rev_synthetic",
                        "revision_number": 1,
                        "revision_files_href": (
                            "/api/v1/libraries/lib_synthetic/sections/sec_synthetic"
                            "/pages/page_synthetic/revisions/rev_synthetic/files"
                        ),
                        "title": "Synthetic title",
                        "occurred_at": 1_000_000,
                        "match_sources": [{"kind": "file_text", "file_name": "说明.md"}],
                        "snippet": snippet,
                    }
                ],
                "next_cursor": "synthetic_next_cursor",
            },
        )

    arguments: dict[str, object] = {"keywords": [_KEYWORD], "limit": 1}
    if include_cursor:
        arguments["cursor"] = cursor
    jsonschema.validate(arguments, _schema())
    with PatchouliClient(
        "https://patchouli.example.invalid", http_transport=httpx.MockTransport(handler)
    ) as client:
        payload = server_module._dispatch(_runtime(client), "pages_search", arguments)
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == ("POST", "/api/v1/search")
    assert not request.url.query
    assert _CURSOR not in str(request.url)
    assert _KEYWORD not in str(request.url)
    assert _CURSOR not in str(request.headers)
    assert _KEYWORD not in str(request.headers)
    body = json.loads(request.content)
    assert body["keywords"] == [_KEYWORD]
    if cursor is not None:
        assert body["cursor"] == cursor
    else:
        assert "cursor" not in body  # Retain the older first-batch wire shape.
    assert payload["ok"] is True
    data = cast(dict[str, object], payload["data"])
    assert data["next_cursor"] == "synthetic_next_cursor"
    items = cast(list[dict[str, object]], data["items"])
    assert items[0]["snippet"] == snippet
    tool_result = server_module._tool_result(payload, is_error=False)
    assert tool_result.structuredContent == payload
    encoded = tool_result.content[0].model_dump()["text"]
    assert json.loads(encoded) == payload
    assert _TOKEN not in encoded


@pytest.mark.parametrize(
    "code,expected_code",
    [("invalid_cursor", "invalid_cursor"), ("unknown_internal_probe", "application_error")],
)
def test_cursor_problem_is_safe_and_never_restarts_at_first_batch(
    code: str, expected_code: str
) -> None:
    requests: list[httpx.Request] = []
    reflected = f"{_TOKEN} {_KEYWORD} {_CURSOR}"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            headers=_headers(problem=True),
            json={
                "type": "https://patchouli.example.invalid/problems/invalid-cursor",
                "title": "Synthetic rejected cursor",
                "status": 400,
                "detail": reflected,
                "instance": "/synthetic/" + reflected,
                "code": code,
                "request_id": _REQUEST_ID,
            },
        )

    arguments = {"keywords": [_KEYWORD], "cursor": _CURSOR}
    jsonschema.validate(arguments, _schema())
    with (
        PatchouliClient(
            "https://patchouli.example.invalid",
            http_transport=httpx.MockTransport(handler),
            retry_policy=RetryPolicy(max_attempts=3),
        ) as client,
        pytest.raises(ProblemError) as caught,
    ):
        server_module._dispatch(_runtime(client), "pages_search", arguments)
    assert caught.value.problem.code == code
    assert len(requests) == 1
    assert json.loads(requests[0].content)["cursor"] == _CURSOR
    payload = server_module._safe_error(caught.value, operation_id=None)
    assert payload == {
        "ok": False,
        "error": {
            "category": "validation",
            "code": expected_code,
            "message": "request was rejected by validation",
            "request_id": _REQUEST_ID,
        },
    }
    result = server_module._tool_result(payload, is_error=True)
    assert result.isError
    encoded = json.dumps(result.model_dump())
    for private_value in (_TOKEN, _KEYWORD, _CURSOR, reflected):
        assert private_value not in encoded
