from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
import pytest

from cli.conftest import invoke_cli, protected_headers, sample_page
from patchouli_cli.errors import ExitCode

_PAGE_ID = "20260811t091500123z-synthetic-session"


def _page_metadata() -> dict[str, object]:
    document = sample_page()
    return {"page": document["page"], "citation": document["citation"]}


def test_pages_list_preserves_exact_page_citation_items_and_cursor(tmp_path: Path) -> None:
    expected_item = _page_metadata()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/sections/sec_synthetic/pages"
        assert dict(request.url.params) == {"limit": "7", "cursor": "cursor_in"}
        assert request.headers["Authorization"] == "Bearer cred_synthetic_123"
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [expected_item], "next_cursor": "cursor_out"},
        )

    result = invoke_cli(
        [
            "--output",
            "json",
            "pages",
            "list",
            "--section",
            "sec_synthetic",
            "--limit",
            "7",
            "--cursor",
            "cursor_in",
        ],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.SUCCESS
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["operation"] == "pages.list"
    assert payload["data"] == {"items": [expected_item], "next_cursor": "cursor_out"}
    assert payload["metadata"]["request_id"].startswith("req_")
    assert payload["metadata"]["cache_control"] == ["private", "no-store"]


def test_pages_list_human_output_uses_the_canonical_envelope_data(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"limit": "20"}
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [], "next_cursor": None},
        )

    result = invoke_cli(
        ["pages", "list", "--section", "sec_synthetic"],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.SUCCESS
    assert result.stderr == ""
    assert result.stdout.startswith("operation: pages.list\n")
    assert '"items": []' in result.stdout
    assert '"next_cursor": null' in result.stdout


@pytest.mark.parametrize(
    ("arguments", "expected_status"),
    [
        (["pages", "list"], ExitCode.USAGE),
        (["pages", "list", "--section", "sec_synthetic", "--limit", "0"], ExitCode.VALIDATION),
        (
            ["pages", "list", "--section", "sec_synthetic", "--limit", "101"],
            ExitCode.VALIDATION,
        ),
        (
            ["pages", "list", "--section", "sec_synthetic", "--cursor", ""],
            ExitCode.VALIDATION,
        ),
    ],
)
def test_pages_list_rejects_missing_or_invalid_input_without_network(
    arguments: list[str], expected_status: ExitCode, tmp_path: Path
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"invalid command reached network: {request.method}")

    result = invoke_cli(arguments, handler=forbidden, tmp_path=tmp_path)

    assert result.status == expected_status
    assert result.stdout == ""


def test_pages_list_rejects_a_malformed_page_citation_pair(tmp_path: Path) -> None:
    malformed = _page_metadata()
    citation = dict(cast(dict[str, object], malformed["citation"]))
    citation["revision_number"] = 2
    malformed["citation"] = citation

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={"items": [malformed], "next_cursor": None},
        )

    result = invoke_cli(
        ["--output", "json", "pages", "list", "--section", "sec_synthetic"],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.PROTOCOL
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "protocol_error"
    assert _PAGE_ID not in result.stderr


def test_pages_list_maps_application_errors_without_rendering_server_detail(tmp_path: Path) -> None:
    request_id = "req_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            403,
            headers={
                "Content-Type": "application/problem+json",
                "Cache-Control": "private, no-store",
                "X-Request-ID": request_id,
            },
            json={
                "type": "about:blank",
                "title": "Synthetic problem",
                "status": 403,
                "detail": "private server detail marker",
                "code": "insufficient_scope",
                "request_id": request_id,
                "details": {},
            },
        )

    result = invoke_cli(
        ["--output", "json", "pages", "list", "--section", "sec_synthetic"],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.SCOPE
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == {
        "category": "scope",
        "code": "insufficient_scope",
        "message": "caller lacks the required Section action",
        "request_id": request_id,
    }
    assert "private server detail marker" not in result.stderr
