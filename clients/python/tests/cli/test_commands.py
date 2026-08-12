from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import httpx
import pytest

from cli.conftest import (
    capabilities_body,
    invoke_cli,
    protected_headers,
    sample_page,
)
from patchouli_cli.errors import ExitCode

_PAGE_ID = "20260811t091500123z-synthetic-session"


@pytest.mark.parametrize(
    ("argv", "method", "path", "body", "operation"),
    [
        (
            ["--output", "json", "doctor"],
            "GET",
            "/api/v1/capabilities",
            capabilities_body(),
            "doctor",
        ),
        (
            ["--output", "json", "capabilities"],
            "GET",
            "/api/v1/capabilities",
            capabilities_body(),
            "capabilities",
        ),
        (
            ["--output", "json", "whoami"],
            "GET",
            "/api/v1/auth/whoami",
            {
                "caller_id": "caller_synthetic",
                "credential_id": "credential_synthetic",
                "kind": "agent",
                "expires_at": "2027-01-01T00:00:00.000000Z",
                "policy_version": 3,
                "grants": [{"section_id": "sec_synthetic", "actions": ["section:query"]}],
            },
            "whoami",
        ),
        (
            ["--output", "json", "sections", "list", "--limit", "7", "--cursor", "cursor_in"],
            "GET",
            "/api/v1/sections",
            {
                "items": [{"section_id": "sec_synthetic", "name": "Synthetic section"}],
                "next_cursor": "cursor_out",
            },
            "sections.list",
        ),
        (
            [
                "--output",
                "json",
                "books",
                "list",
                "--section",
                "sec_synthetic",
            ],
            "GET",
            "/api/v1/sections/sec_synthetic/books",
            {
                "items": [
                    {
                        "section_id": "sec_synthetic",
                        "book_id": "book_synthetic",
                        "title": "Synthetic book",
                    }
                ],
                "next_cursor": None,
            },
            "books.list",
        ),
        (
            [
                "--output",
                "json",
                "page",
                "current",
                "--section",
                "sec_synthetic",
                "--page",
                _PAGE_ID,
            ],
            "GET",
            f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
            sample_page(),
            "page.current",
        ),
        (
            [
                "--output",
                "json",
                "page",
                "revision",
                "--section",
                "sec_synthetic",
                "--page",
                _PAGE_ID,
                "--revision",
                "1",
            ],
            "GET",
            f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions/1",
            sample_page(),
            "page.revision",
        ),
    ],
)
def test_read_command_surface_uses_shared_client_and_stable_json(
    argv: list[str],
    method: str,
    path: str,
    body: dict[str, object],
    operation: str,
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        assert request.url.path == path
        assert request.headers["Authorization"] == "Bearer cred_synthetic_123"
        if operation == "sections.list":
            assert dict(request.url.params) == {"limit": "7", "cursor": "cursor_in"}
        headers = protected_headers()
        if operation == "page.current":
            headers["ETag"] = '"revision-synthetic-1"'
        return httpx.Response(200, headers=headers, json=body)

    result = invoke_cli(argv, handler=handler, tmp_path=tmp_path)

    assert result.status == ExitCode.SUCCESS
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["operation"] == operation
    assert payload["metadata"]["request_id"].startswith("req_")
    assert payload["metadata"]["cache_control"] == ["private", "no-store"]
    if operation == "doctor":
        assert payload["data"]["credential_source"] == "environment"
        assert payload["data"]["profile"] == "default"
    if operation in {"page.current", "page.revision"}:
        assert payload["data"]["page"]["type"] == "archive"
        assert "page_type" not in payload["data"]["page"]


def test_search_reads_private_query_from_file_and_uses_post_json(tmp_path: Path) -> None:
    query_path = tmp_path / "query.txt"
    query_path.write_text("synthetic private query\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/sections/sec_synthetic/search"
        assert request.url.query == b""
        assert json.loads(request.content) == {"query": "synthetic private query", "limit": 5}
        return httpx.Response(
            200,
            headers=protected_headers(),
            json={
                "items": [
                    {
                        "page": sample_page()["page"],
                        "citation": sample_page()["citation"],
                        "snippet": "Synthetic snippet",
                    }
                ],
                "next_cursor": None,
            },
        )

    result = invoke_cli(
        [
            "--output",
            "json",
            "section",
            "search",
            "--section",
            "sec_synthetic",
            "--query-file",
            "query.txt",
            "--limit",
            "5",
        ],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.SUCCESS
    assert "query.txt" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["data"]["items"][0]["snippet"] == "Synthetic snippet"


def test_doctor_rejects_an_unadvertised_api_version(tmp_path: Path) -> None:
    body = capabilities_body()
    body["api_versions"] = ["v2"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=protected_headers(), json=body)

    result = invoke_cli(["doctor"], handler=handler, tmp_path=tmp_path)
    assert result.status == ExitCode.PROTOCOL
    assert result.stdout == ""
    assert "accepted Agent v1 contract" in result.stderr


def test_search_can_take_its_single_sensitive_value_from_stdin(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["query"] == "stdin query"
        return httpx.Response(
            200, headers=protected_headers(), json={"items": [], "next_cursor": None}
        )

    result = invoke_cli(
        [
            "--output",
            "json",
            "section",
            "search",
            "--section",
            "sec_synthetic",
            "--query-stdin",
        ],
        handler=handler,
        tmp_path=tmp_path,
        stdin="stdin query\n",
    )
    assert result.status == ExitCode.SUCCESS


def test_archive_content_stdin_preserves_exact_bytes(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    expected = b"line one\r\nline two\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert expected in request.content
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    result = invoke_cli(
        [
            "archive",
            "create",
            "--section",
            "sec_synthetic",
            "--book",
            "book_synthetic",
            "--metadata-file",
            "metadata.json",
            "--content-stdin",
        ],
        handler=handler,
        tmp_path=tmp_path,
        stdin=expected,
    )
    assert result.status == ExitCode.SUCCESS


def test_archive_create_journals_before_request_and_reuses_exact_key(tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"
    content_path = tmp_path / "content.md"
    metadata_path.write_text(
        json.dumps(
            {
                "title": "Synthetic session",
                "occurred_at": "2026-08-11T09:15:00.123456Z",
                "source": {"kind": "conversation", "locator": "synthetic-locator"},
            }
        ),
        encoding="utf-8",
    )
    content_path.write_bytes(b"# Synthetic archive\n")
    keys: list[str] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["Idempotency-Key"]
        keys.append(key)
        records = list((tmp_path / "state" / "default").glob("*.json"))
        assert len(records) == 1
        pending = json.loads(records[0].read_text(encoding="utf-8"))
        assert pending["status"] == ("pending" if len(keys) == 1 else "succeeded")
        assert pending["idempotency_key"] == key
        assert b"metadata.json" not in request.content
        assert b"content.md" not in request.content
        assert b"filename=" not in request.content
        headers = protected_headers(
            Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
            ETag='"revision-synthetic-1"',
        )
        if len(keys) == 2:
            headers["Idempotency-Replayed"] = "true"
        return httpx.Response(201, headers=headers, json=sample_page())

    base_args = [
        "--output",
        "json",
        "archive",
        "create",
        "--section",
        "sec_synthetic",
        "--book",
        "book_synthetic",
        "--metadata-file",
        "metadata.json",
        "--content-file",
        "content.md",
    ]
    first = invoke_cli(
        base_args,
        handler=handler,
        tmp_path=tmp_path,
        environ={"PATCHOULI_TOKEN": "cred_first_synthetic"},
        credential_id="credential_first_synthetic",
        observed_requests=requests,
    )
    first_payload = json.loads(first.stdout)
    operation_id = first_payload["metadata"]["operation_id"]
    second = invoke_cli(
        [*base_args, "--operation-id", operation_id],
        handler=handler,
        tmp_path=tmp_path,
        environ={"PATCHOULI_TOKEN": "cred_rotated_synthetic"},
        credential_id="credential_rotated_synthetic",
        observed_requests=requests,
    )

    assert first.status == second.status == ExitCode.SUCCESS
    assert re.fullmatch(r"[0-9a-f-]{36}", operation_id)
    assert keys[0] == keys[1]
    assert json.loads(second.stdout)["metadata"]["idempotency_replayed"] is True
    record = json.loads(
        (tmp_path / "state" / "default" / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "succeeded"
    assert record["caller_id"] == "caller_synthetic"
    assert "credential_id" not in record
    assert "cred_" not in json.dumps(record)
    assert record["request_id"].startswith("req_")
    assert "op_" not in first.stdout + first.stderr + second.stdout + second.stderr
    assert "metadata.json" not in first.stdout + first.stderr
    assert [request.url.path for request in requests] == [
        "/api/v1/auth/whoami",
        "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
        "/api/v1/auth/whoami",
        "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
    ]


def test_archive_replay_rejects_a_different_caller_before_mutation(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    (tmp_path / "content.md").write_text("# Synthetic", encoding="utf-8")

    def succeeded(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    args = [
        "--output",
        "json",
        "archive",
        "create",
        "--section",
        "sec_synthetic",
        "--book",
        "book_synthetic",
        "--metadata-file",
        "metadata.json",
        "--content-file",
        "content.md",
    ]
    first = invoke_cli(args, handler=succeeded, tmp_path=tmp_path, caller_id="caller_one")
    operation_id = json.loads(first.stdout)["metadata"]["operation_id"]
    requests: list[httpx.Request] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"cross-caller replay reached mutation: {request.method}")

    second = invoke_cli(
        [*args, "--operation-id", operation_id],
        handler=forbidden,
        tmp_path=tmp_path,
        caller_id="caller_two",
        observed_requests=requests,
    )

    assert second.status == ExitCode.JOURNAL
    assert second.stdout == ""
    assert "different caller" in second.stderr
    assert [request.url.path for request in requests] == ["/api/v1/auth/whoami"]


def test_failed_archive_request_reuses_persisted_idempotency_key(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    (tmp_path / "content.md").write_text("# Synthetic", encoding="utf-8")
    observed_keys: list[str] = []

    def failed(request: httpx.Request) -> httpx.Response:
        observed_keys.append(request.headers["Idempotency-Key"])
        raise httpx.ConnectError("synthetic failure", request=request)

    args = [
        "--output",
        "json",
        "archive",
        "create",
        "--section",
        "sec_synthetic",
        "--book",
        "book_synthetic",
        "--metadata-file",
        "metadata.json",
        "--content-file",
        "content.md",
    ]
    first = invoke_cli(args, handler=failed, tmp_path=tmp_path)
    operation_id = json.loads(first.stderr)["error"]["operation_id"]

    def succeeded(request: httpx.Request) -> httpx.Response:
        observed_keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    second = invoke_cli(
        [*args, "--operation-id", operation_id], handler=succeeded, tmp_path=tmp_path
    )
    assert first.status == ExitCode.TRANSPORT
    assert second.status == ExitCode.SUCCESS
    assert observed_keys[0] == observed_keys[1]
    assert observed_keys[0] not in first.stderr + second.stdout


def test_archive_revise_requires_if_match_and_sends_complete_body(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text('{"source":{"kind":"conversation"}}', encoding="utf-8")
    (tmp_path / "content.md").write_text("# Revision two", encoding="utf-8")
    revision_id = "rev_22222222222222222222222222222222"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions"
        assert request.headers["If-Match"] == '"revision-synthetic-1"'
        assert b"# Revision two" in request.content
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=(f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions/2"),
                ETag='"revision-synthetic-2"',
            ),
            json=sample_page(revision_number=2, revision_id=revision_id),
        )

    result = invoke_cli(
        [
            "archive",
            "revise",
            "--section",
            "sec_synthetic",
            "--page",
            _PAGE_ID,
            "--if-match",
            '"revision-synthetic-1"',
            "--metadata-file",
            "metadata.json",
            "--content-file",
            "content.md",
        ],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.SUCCESS
    assert "operation_id:" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("changed_input", ["metadata", "content"])
def test_journal_rejects_changed_replay_without_network(tmp_path: Path, changed_input: str) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    content = tmp_path / "content.md"
    content.write_text("first", encoding="utf-8")

    def first_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    args = [
        "--output",
        "json",
        "archive",
        "create",
        "--section",
        "sec_synthetic",
        "--book",
        "book_synthetic",
        "--metadata-file",
        "metadata.json",
        "--content-file",
        "content.md",
    ]
    first = invoke_cli(args, handler=first_handler, tmp_path=tmp_path)
    operation_id = cast(dict[str, object], json.loads(first.stdout)["metadata"])["operation_id"]
    if changed_input == "metadata":
        (tmp_path / "metadata.json").write_text(
            '{"title":"Changed","occurred_at":"2026-08-11T09:15:00Z",'
            '"source":{"kind":"conversation"}}',
            encoding="utf-8",
        )
    else:
        content.write_text("changed", encoding="utf-8")

    requests: list[httpx.Request] = []

    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"mismatched replay reached network: {request.method}")

    second = invoke_cli(
        [*args, "--operation-id", cast(str, operation_id)],
        handler=forbidden_handler,
        tmp_path=tmp_path,
        observed_requests=requests,
    )

    assert second.status == ExitCode.JOURNAL
    assert second.stdout == ""
    assert "does not match" in second.stderr
    assert requests == []


def test_journal_rejects_changed_if_match_without_network(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text('{"source":{"kind":"conversation"}}', encoding="utf-8")
    (tmp_path / "content.md").write_text("# Revision two", encoding="utf-8")

    def first_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions/2",
                ETag='"revision-synthetic-2"',
            ),
            json=sample_page(
                revision_number=2,
                revision_id="rev_22222222222222222222222222222222",
            ),
        )

    args = [
        "--output",
        "json",
        "archive",
        "revise",
        "--section",
        "sec_synthetic",
        "--page",
        _PAGE_ID,
        "--if-match",
        '"revision-synthetic-1"',
        "--metadata-file",
        "metadata.json",
        "--content-file",
        "content.md",
    ]
    first = invoke_cli(args, handler=first_handler, tmp_path=tmp_path)
    operation_id = json.loads(first.stdout)["metadata"]["operation_id"]
    replay_args = [*args, "--operation-id", operation_id]
    replay_args[replay_args.index("--if-match") + 1] = '"revision-synthetic-2"'
    requests: list[httpx.Request] = []

    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"mismatched replay reached network: {request.method}")

    second = invoke_cli(
        replay_args,
        handler=forbidden_handler,
        tmp_path=tmp_path,
        observed_requests=requests,
    )

    assert second.status == ExitCode.JOURNAL
    assert second.stdout == ""
    assert "does not match" in second.stderr
    assert requests == []


def test_journal_rejects_replay_after_profile_origin_changes(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    (tmp_path / "content.md").write_text("# Synthetic", encoding="utf-8")

    def first_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    args = [
        "--output",
        "json",
        "archive",
        "create",
        "--section",
        "sec_synthetic",
        "--book",
        "book_synthetic",
        "--metadata-file",
        "metadata.json",
        "--content-file",
        "content.md",
    ]
    first = invoke_cli(args, handler=first_handler, tmp_path=tmp_path)
    operation_id = json.loads(first.stdout)["metadata"]["operation_id"]
    requests: list[httpx.Request] = []

    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"cross-origin replay reached network: {request.method}")

    second = invoke_cli(
        [*args, "--operation-id", operation_id],
        handler=forbidden_handler,
        tmp_path=tmp_path,
        environ={"PATCHOULI_ENDPOINT": "https://changed.example.invalid"},
        observed_requests=requests,
    )
    assert second.status == ExitCode.JOURNAL
    assert "does not match" in second.stderr
    assert requests == []
