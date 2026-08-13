from __future__ import annotations

import builtins
import json
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TextIO, cast

import anyio
import httpx
import pytest
from cli.conftest import (
    capabilities_body,
    invoke_cli,
    protected_headers,
    sample_page,
    whoami_body,
)
from mcp import ClientSession, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from patchouli_cli.application import ArchiveApplication
from patchouli_cli.config import Profile
from patchouli_cli.errors import CliError, ExitCode
from patchouli_cli.journal import OperationJournal
from patchouli_client import (
    BearerToken,
    CacheControl,
    PatchouliClient,
    ProblemDetails,
    ProblemError,
    ProtocolError,
    ResponseMetadata,
    RetryPolicy,
    TransportError,
)
from patchouli_mcp import server as server_module
from patchouli_mcp.entrypoint import entrypoint as optional_entrypoint
from patchouli_mcp.server import McpRuntime, create_server, runtime_from_environment

_PAGE_ID = "20260811t091500123z-synthetic-session"
_TOOL_NAMES = {
    "capabilities",
    "whoami",
    "sections_list",
    "books_list",
    "section_search",
    "page_current",
    "page_revision",
    "archive_create",
    "archive_revise",
}


class CountingClient(PatchouliClient):
    def __init__(
        self,
        endpoint: str,
        handler: httpx.MockTransport,
        lifecycle_events: list[str] | None = None,
    ) -> None:
        super().__init__(
            endpoint,
            http_transport=handler,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self.close_calls = 0
        self.lifecycle_events = lifecycle_events

    def close(self) -> None:
        self.close_calls += 1
        if self.lifecycle_events is not None:
            self.lifecycle_events.append("client-close")
        super().close()


@dataclass(slots=True)
class RuntimeHarness:
    tmp_path: Path
    handler: httpx.MockTransport
    endpoint: str = "https://patchouli.example.invalid"
    token_value: str = "cred_synthetic_123"
    profile: str = "default"
    clients: list[CountingClient] = field(default_factory=list)
    lifecycle_events: list[str] | None = None

    @contextmanager
    def factory(self) -> Iterator[McpRuntime]:
        client = CountingClient(self.endpoint, self.handler, self.lifecycle_events)
        self.clients.append(client)
        journal = OperationJournal(self.tmp_path / "state", self.profile)
        token = BearerToken(self.token_value)
        profile = Profile(name=self.profile, endpoint=self.endpoint, api_version="v1")
        application = ArchiveApplication(
            endpoint=self.endpoint,
            api_version="v1",
            client=client,
            token=token,
            journal=journal,
        )
        try:
            yield McpRuntime(
                profile=profile,
                token=token,
                client=client,
                journal=journal,
                application=application,
            )
        finally:
            journal.close()
            client.close()


def _run_session[T](harness: RuntimeHarness, action: Callable[[ClientSession], Awaitable[T]]) -> T:
    async def execute() -> T:
        async with create_connected_server_and_client_session(
            create_server(runtime_factory=harness.factory)
        ) as session:
            return await action(session)

    return anyio.run(execute)


def _payload(result: mcp_types.CallToolResult) -> dict[str, object]:
    structured = result.structuredContent
    assert isinstance(structured, dict)
    return cast(dict[str, object], structured)


def test_tool_inventory_and_schemas_expose_no_secret_or_path_fields(tmp_path: Path) -> None:
    harness = RuntimeHarness(tmp_path, httpx.MockTransport(lambda request: _unexpected(request)))

    async def action(session: ClientSession) -> mcp_types.ListToolsResult:
        return await session.list_tools()

    result = _run_session(harness, action)
    tools = result.tools
    assert {tool.name for tool in tools} == _TOOL_NAMES
    serialized = json.dumps([tool.model_dump(by_alias=True) for tool in tools]).lower()
    for forbidden in (
        "token",
        "bearer",
        "credential_id",
        "endpoint",
        "idempotency_key",
        "journal_path",
        "file_path",
        "input_root",
    ):
        assert forbidden not in serialized
    for tool in tools:
        assert tool.inputSchema["additionalProperties"] is False
    assert harness.clients[0].close_calls == 1


def test_read_tools_use_exact_typed_client_requests_and_redact_credential_id(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/capabilities":
            body: object = capabilities_body()
        elif path == "/api/v1/auth/whoami":
            body = whoami_body(credential_id="credential_must_not_escape")
        elif path == "/api/v1/sections":
            body = {
                "items": [{"section_id": "sec_synthetic", "name": "Synthetic"}],
                "next_cursor": None,
            }
        elif path == "/api/v1/sections/sec_synthetic/books":
            body = {
                "items": [
                    {
                        "section_id": "sec_synthetic",
                        "book_id": "book_synthetic",
                        "title": "Synthetic",
                    }
                ],
                "next_cursor": None,
            }
        elif path == "/api/v1/sections/sec_synthetic/search":
            page = sample_page()
            body = {
                "items": [
                    {
                        "page": page["page"],
                        "citation": page["citation"],
                        "snippet": "synthetic hit",
                    }
                ],
                "next_cursor": None,
            }
        else:
            body = sample_page()
        headers = protected_headers()
        if path.endswith(f"/pages/{_PAGE_ID}"):
            headers["ETag"] = '"revision-synthetic-1"'
        return httpx.Response(200, headers=headers, json=body)

    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler))

    async def action(session: ClientSession) -> list[mcp_types.CallToolResult]:
        results = [
            await session.call_tool("capabilities", {}),
            await session.call_tool("whoami", {}),
            await session.call_tool("sections_list", {"limit": 20}),
            await session.call_tool("books_list", {"section_id": "sec_synthetic", "limit": 10}),
            await session.call_tool(
                "section_search",
                {"section_id": "sec_synthetic", "query": "synthetic", "limit": 5},
            ),
            await session.call_tool(
                "page_current", {"section_id": "sec_synthetic", "page_id": _PAGE_ID}
            ),
            await session.call_tool(
                "page_revision",
                {"section_id": "sec_synthetic", "page_id": _PAGE_ID, "revision_number": 1},
            ),
        ]
        return results

    results = _run_session(harness, action)
    assert all(not result.isError for result in results)
    assert "credential_must_not_escape" not in json.dumps(_payload(results[1]))
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/capabilities"),
        ("GET", "/api/v1/auth/whoami"),
        ("GET", "/api/v1/sections"),
        ("GET", "/api/v1/sections/sec_synthetic/books"),
        ("POST", "/api/v1/sections/sec_synthetic/search"),
        ("GET", f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}"),
        ("GET", f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions/1"),
    ]
    assert json.loads(requests[4].content) == {"query": "synthetic", "limit": 5}
    assert harness.clients[0].close_calls == 1


def test_create_and_revise_are_explicit_and_keep_operation_key_internal(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(200, headers=protected_headers(), json=whoami_body())
        keys.append(request.headers["Idempotency-Key"])
        revision = request.url.path.endswith("/revisions")
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=(
                    f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions/2"
                    if revision
                    else f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}"
                ),
                ETag='"revision-synthetic-2"' if revision else '"revision-synthetic-1"',
            ),
            json=sample_page(
                revision_number=2 if revision else 1,
                revision_id=(
                    "rev_22222222222222222222222222222222"
                    if revision
                    else "rev_0123456789abcdef0123456789abcdef"
                ),
            ),
        )

    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler))

    async def action(session: ClientSession) -> list[mcp_types.CallToolResult]:
        create = await session.call_tool(
            "archive_create",
            {
                "section_id": "sec_synthetic",
                "book_id": "book_synthetic",
                "title": "Synthetic",
                "occurred_at": "2026-08-11T09:15:00Z",
                "source_kind": "conversation",
                "content": "# Synthetic",
            },
        )
        revise = await session.call_tool(
            "archive_revise",
            {
                "section_id": "sec_synthetic",
                "page_id": _PAGE_ID,
                "if_match": '"revision-synthetic-1"',
                "source_kind": "conversation",
                "content": "# Revision two",
            },
        )
        return [create, revise]

    create_result, revise_result = _run_session(harness, action)
    assert not create_result.isError
    assert not revise_result.isError
    create_payload = _payload(create_result)
    revise_payload = _payload(revise_result)
    assert "operation_id" in cast(dict[str, object], create_payload["metadata"])
    serialized = json.dumps([create_payload, revise_payload])
    assert all(key not in serialized for key in keys)
    assert requests[-1].headers["If-Match"] == '"revision-synthetic-1"'
    assert [request.url.path for request in requests] == [
        "/api/v1/auth/whoami",
        "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
        "/api/v1/auth/whoami",
        f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions",
    ]


def test_concurrent_failed_mutations_keep_call_local_operation_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_mutation_entered = threading.Event()
    release_first_mutation = threading.Event()
    events: list[str] = []
    mutation_count = 0
    original_reset = ArchiveApplication.reset_operation

    def recording_reset(application: ArchiveApplication) -> None:
        events.append("reset")
        original_reset(application)

    monkeypatch.setattr(ArchiveApplication, "reset_operation", recording_reset)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation_count
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(200, headers=protected_headers(), json=whoami_body())
        mutation_count += 1
        current = mutation_count
        events.append(f"mutation-{current}-enter")
        if current == 1:
            first_mutation_entered.set()
            assert release_first_mutation.wait(timeout=2)
        events.append(f"mutation-{current}-complete")
        return httpx.Response(
            503,
            headers=protected_headers(**{"Content-Type": "application/problem+json"}),
            json={
                "type": "about:blank",
                "title": "Service unavailable",
                "status": 503,
                "detail": "Synthetic temporary failure.",
                "code": "search_unavailable",
                "request_id": "req_11111111111111111111111111111111",
                "details": {},
            },
        )

    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler))
    first_arguments: dict[str, object] = {
        "section_id": "sec_synthetic",
        "book_id": "book_synthetic",
        "title": "First",
        "occurred_at": "2026-08-11T09:15:00Z",
        "source_kind": "conversation",
        "content": "# First",
    }
    second_arguments: dict[str, object] = {
        **first_arguments,
        "title": "Second",
        "content": "# Second",
    }
    results: dict[str, mcp_types.CallToolResult] = {}

    async def call(session: ClientSession, label: str, arguments: dict[str, object]) -> None:
        results[label] = await session.call_tool("archive_create", arguments)

    async def exercise() -> None:
        async with (
            create_connected_server_and_client_session(
                create_server(runtime_factory=harness.factory)
            ) as session,
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(call, session, "first", first_arguments)
            await anyio.to_thread.run_sync(first_mutation_entered.wait)
            tasks.start_soon(call, session, "second", second_arguments)
            await anyio.sleep(0.05)
            release_first_mutation.set()

    anyio.run(exercise)
    first_error = cast(dict[str, object], _payload(results["first"])["error"])
    second_error = cast(dict[str, object], _payload(results["second"])["error"])
    first_operation = first_error["operation_id"]
    second_operation = second_error["operation_id"]
    assert isinstance(first_operation, str)
    assert isinstance(second_operation, str)
    assert first_operation != second_operation
    assert events.index("mutation-1-complete") < events.index("reset", 2)
    assert (tmp_path / "state" / "default" / f"{first_operation}.json").is_file()
    assert (tmp_path / "state" / "default" / f"{second_operation}.json").is_file()


def test_cli_and_mcp_share_identical_journal_fingerprint_and_replay_key(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    (tmp_path / "content.md").write_text("# Synthetic", encoding="utf-8")
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(200, headers=protected_headers(), json=whoami_body())
        keys.append(request.headers["Idempotency-Key"])
        headers = protected_headers(
            Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
            ETag='"revision-synthetic-1"',
        )
        if len(keys) == 2:
            headers["Idempotency-Replayed"] = "true"
        return httpx.Response(201, headers=headers, json=sample_page())

    cli = invoke_cli(
        [
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
        ],
        handler=handler,
        tmp_path=tmp_path,
    )
    operation_id = json.loads(cli.stdout)["metadata"]["operation_id"]
    record_path = tmp_path / "state" / "default" / f"{operation_id}.json"
    before = json.loads(record_path.read_text(encoding="utf-8"))
    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler))

    async def action(session: ClientSession) -> mcp_types.CallToolResult:
        return await session.call_tool(
            "archive_create",
            {
                "section_id": "sec_synthetic",
                "book_id": "book_synthetic",
                "title": "Synthetic",
                "occurred_at": "2026-08-11T09:15:00Z",
                "source_kind": "conversation",
                "content": "# Synthetic",
                "operation_id": operation_id,
            },
        )

    result = _run_session(harness, action)
    after = json.loads(record_path.read_text(encoding="utf-8"))
    assert not result.isError
    assert keys[0] == keys[1]
    assert before["fingerprint"] == after["fingerprint"]
    assert before["caller_id"] == after["caller_id"] == "caller_synthetic"


def test_mcp_replay_allows_rotation_and_rejects_cross_caller_before_mutation(
    tmp_path: Path,
) -> None:
    caller = "caller_same"
    keys: list[str] = []
    requests: list[httpx.Request] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(
                200, headers=protected_headers(), json=whoami_body(caller_id=caller)
            )
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    arguments: dict[str, object] = {
        "section_id": "sec_synthetic",
        "book_id": "book_synthetic",
        "title": "Synthetic",
        "occurred_at": "2026-08-11T09:15:00Z",
        "source_kind": "conversation",
        "content": "# Synthetic",
    }
    first_harness = RuntimeHarness(
        tmp_path, httpx.MockTransport(first_handler), token_value="cred_first_synthetic"
    )

    async def create(session: ClientSession) -> mcp_types.CallToolResult:
        return await session.call_tool("archive_create", arguments)

    first = _run_session(first_harness, create)
    operation_id = cast(dict[str, object], _payload(first)["metadata"])["operation_id"]

    rotated_harness = RuntimeHarness(
        tmp_path, httpx.MockTransport(first_handler), token_value="cred_rotated_synthetic"
    )

    async def replay(session: ClientSession) -> mcp_types.CallToolResult:
        return await session.call_tool(
            "archive_create", {**arguments, "operation_id": operation_id}
        )

    rotated = _run_session(rotated_harness, replay)
    assert not rotated.isError
    assert keys[0] == keys[1]

    cross_requests: list[httpx.Request] = []

    def cross_handler(request: httpx.Request) -> httpx.Response:
        cross_requests.append(request)
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(
                200,
                headers=protected_headers(),
                json=whoami_body(caller_id="caller_different"),
            )
        return _unexpected(request)

    rejected = _run_session(RuntimeHarness(tmp_path, httpx.MockTransport(cross_handler)), replay)
    assert rejected.isError
    assert [request.url.path for request in cross_requests] == ["/api/v1/auth/whoami"]
    assert "different caller" in json.dumps(_payload(rejected))


@pytest.mark.parametrize("mismatch", ["endpoint", "content", "if_match"])
def test_mcp_replay_mismatch_has_zero_requests(tmp_path: Path, mismatch: str) -> None:
    def successful(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(200, headers=protected_headers(), json=whoami_body())
        revision = request.url.path.endswith("/revisions")
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=(
                    f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}/revisions/2"
                    if revision
                    else f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}"
                ),
                ETag='"revision-synthetic-2"' if revision else '"revision-synthetic-1"',
            ),
            json=sample_page(
                revision_number=2 if revision else 1,
                revision_id=(
                    "rev_22222222222222222222222222222222"
                    if revision
                    else "rev_0123456789abcdef0123456789abcdef"
                ),
            ),
        )

    base: dict[str, object]
    tool: str
    if mismatch == "if_match":
        tool = "archive_revise"
        base = {
            "section_id": "sec_synthetic",
            "page_id": _PAGE_ID,
            "if_match": '"revision-synthetic-1"',
            "source_kind": "conversation",
            "content": "# Synthetic",
        }
    else:
        tool = "archive_create"
        base = {
            "section_id": "sec_synthetic",
            "book_id": "book_synthetic",
            "title": "Synthetic",
            "occurred_at": "2026-08-11T09:15:00Z",
            "source_kind": "conversation",
            "content": "# Synthetic",
        }

    async def first_call(session: ClientSession) -> mcp_types.CallToolResult:
        return await session.call_tool(tool, base)

    first = _run_session(RuntimeHarness(tmp_path, httpx.MockTransport(successful)), first_call)
    operation_id = cast(dict[str, object], _payload(first)["metadata"])["operation_id"]
    changed = {**base, "operation_id": operation_id}
    if mismatch == "content":
        changed["content"] = "changed"
    elif mismatch == "if_match":
        changed["if_match"] = '"revision-synthetic-2"'

    requests: list[httpx.Request] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _unexpected(request)

    endpoint = (
        "https://changed.example.invalid"
        if mismatch == "endpoint"
        else "https://patchouli.example.invalid"
    )
    harness = RuntimeHarness(tmp_path, httpx.MockTransport(forbidden), endpoint=endpoint)

    async def replay(session: ClientSession) -> mcp_types.CallToolResult:
        return await session.call_tool(tool, changed)

    rejected = _run_session(harness, replay)
    assert rejected.isError
    assert requests == []


def test_invalid_query_and_content_fail_before_network(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _unexpected(request)

    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler))

    async def action(session: ClientSession) -> list[mcp_types.CallToolResult]:
        query = await session.call_tool(
            "section_search", {"section_id": "sec_synthetic", "query": "x" * 4_097}
        )
        content = await session.call_tool(
            "archive_create",
            {
                "section_id": "sec_synthetic",
                "book_id": "book_synthetic",
                "title": "Synthetic",
                "occurred_at": "2026-08-11T09:15:00Z",
                "source_kind": "conversation",
                "content": "bad\x00content",
            },
        )
        return [query, content]

    results = _run_session(harness, action)
    assert all(result.isError for result in results)
    assert requests == []
    assert all(_payload(result)["error"] for result in results)


def test_problem_and_internal_errors_are_redacted(tmp_path: Path) -> None:
    secret = "cred_private_should_not_escape"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={
                "Content-Type": "application/problem+json",
                "Cache-Control": "private, no-store",
                "X-Request-ID": "req_11111111111111111111111111111111",
                "WWW-Authenticate": 'Bearer realm="patchouli"',
            },
            json={
                "type": "https://errors.example.invalid/authentication-required",
                "title": "Authentication required",
                "status": 401,
                "detail": f"unsafe {secret}",
                "code": "authentication_required",
                "request_id": "req_11111111111111111111111111111111",
                "details": {"unsafe": secret},
            },
        )

    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler), token_value=secret)

    async def action(session: ClientSession) -> mcp_types.CallToolResult:
        return await session.call_tool("capabilities", {})

    result = _run_session(harness, action)
    serialized = json.dumps(_payload(result))
    assert result.isError is True
    assert secret not in serialized
    assert "errors.example.invalid" not in serialized
    assert "caller credential was rejected" in serialized


def test_cancelled_session_closes_shared_client_without_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _unexpected(request)

    harness = RuntimeHarness(tmp_path, httpx.MockTransport(handler))

    async def exercise() -> None:
        async with create_connected_server_and_client_session(
            create_server(runtime_factory=harness.factory)
        ) as session:
            with anyio.CancelScope() as scope:
                scope.cancel()
                await session.call_tool("capabilities", {})

    anyio.run(exercise)
    assert requests == []
    assert harness.clients[0].close_calls == 1


def test_inflight_cancellation_leaves_no_background_client_work(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    lifecycle_events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        assert release.wait(timeout=2)
        lifecycle_events.append("worker-complete")
        return httpx.Response(200, headers=protected_headers(), json=capabilities_body())

    harness = RuntimeHarness(
        tmp_path, httpx.MockTransport(handler), lifecycle_events=lifecycle_events
    )

    async def exercise() -> None:
        async with (
            create_connected_server_and_client_session(
                create_server(runtime_factory=harness.factory)
            ) as session,
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(session.call_tool, "capabilities", {})
            await anyio.to_thread.run_sync(entered.wait)
            timer = threading.Timer(0.05, release.set)
            timer.start()
            tasks.cancel_scope.cancel()
            timer.join(timeout=1)

    anyio.run(exercise)
    assert lifecycle_events == ["worker-complete", "client-close"]
    assert harness.clients[0].close_calls == 1


def test_real_stdio_entrypoint_has_protocol_clean_stdout_and_safe_stderr(tmp_path: Path) -> None:
    async def exercise() -> str:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "patchouli_mcp"],
                cwd=Path.cwd(),
                env={
                    "PATCHOULI_ENDPOINT": "https://patchouli.example.invalid",
                    "PATCHOULI_TOKEN": "cred_synthetic_123",
                    "PATCHOULI_STATE_DIR": str(tmp_path / "state"),
                },
            )
            async with (
                stdio_client(params, errlog=cast(TextIO, error)) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == _TOOL_NAMES
            error.seek(0)
            return error.read()

    assert anyio.run(exercise) == ""


def test_official_sdk_license_and_console_entrypoint_are_packaged() -> None:
    distribution = metadata.distribution("mcp")
    assert distribution.version.startswith("1.")
    assert distribution.metadata["License"] == "MIT"
    assert any(
        "dist-info/licenses/LICENSE" in str(path).replace("\\", "/")
        for path in distribution.files or ()
    )
    scripts = {item.name: item.value for item in metadata.entry_points(group="console_scripts")}
    assert scripts["patchouli-mcp"] == "patchouli_mcp.entrypoint:entrypoint"


def test_environment_runtime_resolves_startup_state_and_closes_once(tmp_path: Path) -> None:
    clients: list[CountingClient] = []

    def factory(endpoint: str) -> PatchouliClient:
        client = CountingClient(endpoint, httpx.MockTransport(lambda request: _unexpected(request)))
        clients.append(client)
        return client

    with runtime_from_environment(
        environ={
            "PATCHOULI_ENDPOINT": "https://patchouli.example.invalid",
            "PATCHOULI_TOKEN": "cred_synthetic_123",
            "PATCHOULI_STATE_DIR": str(tmp_path / "state"),
        },
        client_factory=factory,
    ) as runtime:
        assert runtime.profile.api_version == "v1"
        assert "cred_synthetic" not in repr(runtime)
        assert "patchouli.example" not in repr(runtime)
    assert clients[0].close_calls == 1


@pytest.mark.parametrize(
    ("status", "code", "operation_id", "category"),
    [
        (403, "insufficient_scope", None, "scope"),
        (404, "resource_not_found", None, "not_found"),
        (409, "idempotency_mismatch", None, "conflict"),
        (412, "revision_conflict", None, "precondition"),
        (428, "precondition_required", "11111111-1111-4111-8111-111111111111", "precondition"),
        (422, "request_validation_failed", None, "validation"),
        (503, "temporarily_unavailable", None, "service"),
        (500, "synthetic_failure", None, "application"),
    ],
)
def test_problem_error_mapping_is_stable_and_redacted(
    status: int, code: str, operation_id: str | None, category: str
) -> None:
    request_id = "req_11111111111111111111111111111111"
    problem = ProblemDetails(
        type="https://errors.example.invalid/synthetic",
        title="Synthetic",
        status=status,
        detail="unsafe response detail cred_private",
        code=code,
        request_id=request_id,
        details={"unsafe": "cred_private"},
    )
    response_metadata = ResponseMetadata(
        request_id=request_id,
        cache_control=CacheControl(("private", "no-store")),
        etag=None,
        location=None,
        idempotency_replayed=False,
    )
    payload = server_module._safe_error(
        ProblemError(problem, response_metadata), operation_id=operation_id
    )
    serialized = json.dumps(payload)
    assert cast(dict[str, object], payload["error"])["category"] == category
    assert request_id in serialized
    assert "cred_private" not in serialized
    assert "errors.example.invalid" not in serialized
    if code == "synthetic_failure":
        assert "synthetic_failure" not in serialized
        assert "application_error" in serialized
    if operation_id is not None:
        assert operation_id in serialized


def test_shared_wire_problem_codes_are_preserved() -> None:
    fixture_path = Path(__file__).resolve().parents[4] / "tests/fixtures/api/agent_v1_wire.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    problems = cast(dict[str, dict[str, object]], fixture["problems"])
    for vector in problems.values():
        body = cast(dict[str, object], vector["body"])
        problem = ProblemDetails.from_dict(body)
        response_metadata = ResponseMetadata(
            request_id=problem.request_id,
            cache_control=CacheControl(("private", "no-store")),
            etag=None,
            location=None,
            idempotency_replayed=False,
        )
        payload = server_module._safe_error(
            ProblemError(problem, response_metadata), operation_id=None
        )
        error = cast(dict[str, object], payload["error"])
        assert error["code"] == problem.code
        if problem.code in {"rate_limited", "search_unavailable"}:
            assert error["category"] == "service"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TransportError(operation="read", attempts=1), "transport"),
        (ProtocolError("error response was not RFC 9457 Problem Details"), "edge_gate"),
        (ProtocolError("synthetic protocol detail"), "protocol"),
        (ValueError("unsafe input detail"), "validation"),
        (RuntimeError("unsafe internal detail"), "internal"),
        (
            CliError(ExitCode.JOURNAL, "journal", "journal_error", "safe journal message"),
            "journal",
        ),
    ],
)
def test_non_problem_error_mapping_never_exposes_exception_detail(
    error: Exception, category: str
) -> None:
    payload = server_module._safe_error(error, operation_id=None)
    serialized = json.dumps(payload)
    assert cast(dict[str, object], payload["error"])["category"] == category
    assert "unsafe" not in serialized


def test_entrypoint_statuses_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del tmp_path
    monkeypatch.setattr("patchouli_mcp.server.anyio.run", lambda function, server: None)
    assert server_module.run() == 0

    def interrupted(function: object, server: object) -> None:
        del function, server
        raise KeyboardInterrupt

    monkeypatch.setattr("patchouli_mcp.server.anyio.run", interrupted)
    assert server_module.run() == 130

    def failed(function: object, server: object) -> None:
        del function, server
        raise RuntimeError("cred_private endpoint private.example")

    monkeypatch.setattr("patchouli_mcp.server.anyio.run", failed)
    assert server_module.run() == 70
    assert "cred_private" not in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["patchouli-mcp", "unsafe-value"])
    with pytest.raises(SystemExit) as raised:
        server_module.entrypoint()
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsafe-value" not in captured.err


def test_optional_entrypoint_delegates_or_fails_without_import_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(server_module, "entrypoint", lambda: called.append(True))
    optional_entrypoint()
    assert called == [True]

    original_import = builtins.__import__

    def missing_sdk(
        name: str,
        globals_value: Mapping[str, object] | None = None,
        locals_value: Mapping[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "patchouli_mcp.server":
            raise ModuleNotFoundError("unsafe local import detail", name="mcp")
        return original_import(name, globals_value, locals_value, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", missing_sdk)
    with pytest.raises(SystemExit) as raised:
        optional_entrypoint()
    assert raised.value.code == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsafe local" not in captured.err
    assert "patchouli-client[mcp]" in captured.err


def _unexpected(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")
