from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Any, cast

import anyio
import jsonschema  # type: ignore[import-untyped]
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from patchouli_cli.application import ArchiveApplication
from patchouli_cli.config import Profile, default_state_path, resolve_profile
from patchouli_cli.credentials import KeyringSecretStore, SecretStore, resolve_token
from patchouli_cli.errors import CliError
from patchouli_cli.journal import OperationJournal
from patchouli_client import (
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    BearerToken,
    ClientResponse,
    MarkdownContent,
    Page,
    PatchouliClient,
    ProblemError,
    ProtocolError,
    SearchRequest,
    SourceInput,
    TransportError,
    WhoAmI,
)
from patchouli_client.models import MAX_ARCHIVE_BYTES, MAX_CURSOR_LENGTH, parse_rfc3339

MAX_QUERY_BYTES = 4_096
MAX_SOURCE_BYTES = 16_384
_SERVER_NAME = "patchouli-agent"
_SERVER_VERSION = "0.1.0a0"
_PUBLIC_PROBLEM_CODES = {
    "authentication_required",
    "content_too_large",
    "idempotency_mismatch",
    "insufficient_scope",
    "invalid_token",
    "precondition_required",
    "rate_limited",
    "request_timeout",
    "request_validation_failed",
    "resource_not_found",
    "revision_conflict",
    "search_unavailable",
    "temporarily_unavailable",
    "too_many_requests",
    "unsupported_media_type",
}

ClientFactory = Callable[[str], PatchouliClient]


@dataclass(slots=True)
class McpRuntime:
    profile: Profile = field(repr=False)
    token: BearerToken = field(repr=False)
    client: PatchouliClient = field(repr=False)
    journal: OperationJournal = field(repr=False)
    application: ArchiveApplication = field(repr=False)
    dispatch_lock: anyio.Lock = field(default_factory=anyio.Lock, repr=False)

    def __repr__(self) -> str:
        return "McpRuntime(profile=<redacted>, token=<redacted>, client=<shared>)"


RuntimeFactory = Callable[[], AbstractContextManager[McpRuntime]]


@contextmanager
def runtime_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: ClientFactory = PatchouliClient,
    secret_store: SecretStore | None = None,
) -> Iterator[McpRuntime]:
    """Resolve non-tool startup state and own exactly one client for the session."""
    resolved_environ = dict(os.environ if environ is None else environ)
    profile = resolve_profile(profile_name=None, config_path=None, environ=resolved_environ)
    resolved_token = resolve_token(
        profile=profile.name,
        token_stdin=False,
        environ=resolved_environ,
        stdin=io.StringIO(),
        secret_store=secret_store or KeyringSecretStore(),
    )
    client = client_factory(profile.endpoint)
    try:
        with OperationJournal(default_state_path(resolved_environ), profile.name) as journal:
            application = ArchiveApplication(
                endpoint=profile.endpoint,
                api_version=profile.api_version,
                client=client,
                token=resolved_token.token,
                journal=journal,
            )
            yield McpRuntime(
                profile=profile,
                token=resolved_token.token,
                client=client,
                journal=journal,
                application=application,
            )
    finally:
        client.close()


def create_server(*, runtime_factory: RuntimeFactory = runtime_from_environment) -> Server[Any]:
    @asynccontextmanager
    async def lifespan(server: Server[Any]) -> Any:
        del server
        with runtime_factory() as runtime:
            yield runtime

    server: Server[Any] = Server(
        _SERVER_NAME,
        version=_SERVER_VERSION,
        instructions=(
            "Section-scoped PatchouliLib Agent tools. Configuration and caller credentials "
            "are process startup concerns and are never tool inputs."
        ),
        lifespan=lifespan,
    )
    inventory = _tool_inventory()
    tool_schemas = {tool.name: tool.inputSchema for tool in inventory}

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return inventory

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        runtime = cast(McpRuntime, server.request_context.lifespan_context)
        operation_id: str | None = None
        try:
            schema = tool_schemas.get(name)
            if schema is None:
                raise ValueError("unknown tool")
            try:
                jsonschema.validate(instance=arguments, schema=schema)
            except jsonschema.ValidationError as exc:
                raise ValueError("tool input did not match its fixed schema") from exc
            async with runtime.dispatch_lock:
                operation_invoked = name in {"archive_create", "archive_revise"}
                if operation_invoked:
                    runtime.application.reset_operation()
                try:
                    payload = await anyio.to_thread.run_sync(
                        partial(_dispatch, runtime, name, arguments),
                        abandon_on_cancel=False,
                    )
                except Exception:
                    if operation_invoked:
                        operation_id = runtime.application.operation_id
                    raise
            return _tool_result(payload, is_error=False)
        except Exception as exc:
            return _tool_result(_safe_error(exc, operation_id=operation_id), is_error=True)

    return server


def _dispatch(runtime: McpRuntime, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    token = runtime.token
    response: ClientResponse[object]
    operation_id: str | None = None
    if name == "capabilities":
        response = cast(ClientResponse[object], runtime.client.capabilities(token=token))
    elif name == "whoami":
        response = cast(ClientResponse[object], runtime.client.whoami(token=token))
    elif name == "sections_list":
        response = cast(
            ClientResponse[object],
            runtime.client.list_sections(
                token=token,
                limit=_limit(arguments),
                cursor=_optional_string(arguments, "cursor"),
            ),
        )
    elif name == "books_list":
        response = cast(
            ClientResponse[object],
            runtime.client.list_books(
                _required_string(arguments, "section_id"),
                token=token,
                limit=_limit(arguments),
                cursor=_optional_string(arguments, "cursor"),
            ),
        )
    elif name == "section_search":
        query = _bounded_text(
            _required_string(arguments, "query"), label="search query", max_bytes=MAX_QUERY_BYTES
        )
        request = SearchRequest(
            query=query,
            limit=_limit(arguments),
            cursor=_optional_string(arguments, "cursor"),
        )
        response = cast(
            ClientResponse[object],
            runtime.client.search(_required_string(arguments, "section_id"), request, token=token),
        )
    elif name == "page_current":
        response = cast(
            ClientResponse[object],
            runtime.client.get_page(
                _required_string(arguments, "section_id"),
                _required_string(arguments, "page_id"),
                token=token,
            ),
        )
    elif name == "page_revision":
        response = cast(
            ClientResponse[object],
            runtime.client.get_revision(
                _required_string(arguments, "section_id"),
                _required_string(arguments, "page_id"),
                _required_integer(arguments, "revision_number"),
                token=token,
            ),
        )
    elif name == "archive_create":
        content = _markdown(arguments)
        create_metadata = ArchiveCreateMetadata(
            title=_bounded_text(
                _required_string(arguments, "title"), label="archive title", max_bytes=65_536
            ),
            occurred_at=parse_rfc3339(_required_string(arguments, "occurred_at")),
            source=_source(arguments),
        )
        result = runtime.application.create_archive(
            _required_string(arguments, "section_id"),
            _required_string(arguments, "book_id"),
            create_metadata,
            content,
            operation_id=_optional_string(arguments, "operation_id"),
        )
        response = cast(ClientResponse[object], result.response)
        operation_id = result.operation_id
    elif name == "archive_revise":
        content = _markdown(arguments)
        revision_metadata = ArchiveRevisionMetadata(source=_source(arguments))
        result = runtime.application.revise_archive(
            _required_string(arguments, "section_id"),
            _required_string(arguments, "page_id"),
            revision_metadata,
            content,
            if_match=_required_string(arguments, "if_match"),
            operation_id=_optional_string(arguments, "operation_id"),
        )
        response = cast(ClientResponse[object], result.response)
        operation_id = result.operation_id
    else:
        raise ValueError("unknown tool")
    return _success(response, operation_id=operation_id)


def _success(response: ClientResponse[object], *, operation_id: str | None) -> dict[str, object]:
    metadata: dict[str, object] = {
        "request_id": response.metadata.request_id,
        "cache_control": list(response.metadata.cache_control.directives),
        "etag": response.metadata.etag,
        "location": response.metadata.location,
        "idempotency_replayed": response.metadata.idempotency_replayed,
    }
    if operation_id is not None:
        metadata["operation_id"] = operation_id
    return {"ok": True, "data": _jsonable(response.value), "metadata": metadata}


def _safe_error(exc: Exception, *, operation_id: str | None) -> dict[str, object]:
    request_id: str | None = None
    if isinstance(exc, CliError):
        category, code, message = exc.category, exc.code, exc.public_message
    elif isinstance(exc, ProblemError):
        category, code, message = _problem_error(exc, operation_id=operation_id)
        request_id = exc.problem.request_id
    elif isinstance(exc, TransportError):
        category, code = "transport", "transport_failure"
        message = (
            "request failed after bounded transport retries; a journaled write can be replayed"
        )
    elif isinstance(exc, ProtocolError):
        if str(exc) == "error response was not RFC 9457 Problem Details":
            category, code = "edge_gate", "edge_gate_or_nonconforming_upstream"
            message = "an outer access gate or non-conforming upstream rejected the request"
        else:
            category, code = "protocol", "protocol_error"
            message = "server response did not satisfy the accepted Agent v1 contract"
    elif isinstance(exc, ValueError | UnicodeError):
        category, code, message = (
            "validation",
            "invalid_input",
            "tool input did not satisfy the accepted client contract",
        )
    else:
        category, code, message = (
            "internal",
            "internal_error",
            "MCP adapter failed closed without exposing internal details",
        )
    detail: dict[str, object] = {"category": category, "code": code, "message": message}
    if request_id is not None:
        detail["request_id"] = request_id
    if operation_id is not None:
        detail["operation_id"] = operation_id
    return {"ok": False, "error": detail}


def _problem_error(exc: ProblemError, *, operation_id: str | None) -> tuple[str, str, str]:
    status, code = exc.problem.status, exc.problem.code
    public_code = code if code in _PUBLIC_PROBLEM_CODES else "application_error"
    if status == 401 or code in {"authentication_required", "invalid_token"}:
        return "auth", public_code, "caller credential was rejected"
    if status == 403 or code == "insufficient_scope":
        return "scope", public_code, "caller lacks the required Section action"
    if status == 404 or code == "resource_not_found":
        return "not_found", public_code, "resource was not found or is hidden"
    if status == 409 or code == "idempotency_mismatch":
        return "conflict", public_code, "operation key conflicts with another request"
    if status in {412, 428} or code in {"revision_conflict", "precondition_required"}:
        if operation_id is None:
            message = (
                "revision precondition was not accepted; fetch the current Page before retrying"
            )
        else:
            message = (
                "revision was not applied; retain this operation for exact replay, or fetch the "
                "current Page and start a new operation when changing If-Match"
            )
        return "precondition", public_code, message
    if status in {400, 413, 415, 422}:
        return "validation", public_code, "request was rejected by validation"
    if status in {408, 429, 502, 503, 504}:
        return "service", public_code, "service is temporarily unavailable"
    return "application", public_code, "server returned an application error"


def _tool_result(payload: dict[str, object], *, is_error: bool) -> types.CallToolResult:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=encoded)],
        structuredContent=payload,
        isError=is_error,
    )


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, WhoAmI):
        return {
            "caller_id": value.caller_id,
            "kind": value.kind,
            "expires_at": _jsonable(value.expires_at),
            "policy_version": value.policy_version,
            "grants": _jsonable(value.grants),
        }
    if isinstance(value, Page):
        return {
            "section_id": value.section_id,
            "book_id": value.book_id,
            "page_id": value.page_id,
            "title": value.title,
            "type": value.page_type,
            "occurred_at": _jsonable(value.occurred_at),
            "current_revision_id": value.current_revision_id,
            "current_revision_number": value.current_revision_number,
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    raise TypeError("unsupported MCP output value")


def _required_string(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("required string input is invalid")
    return value


def _optional_string(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("optional string input is invalid")
    return value


def _required_integer(arguments: Mapping[str, object], name: str) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("required integer input is invalid")
    return value


def _limit(arguments: Mapping[str, object]) -> int:
    value = arguments.get("limit", 20)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("collection limit is invalid")
    return value


def _bounded_text(value: str, *, label: str, max_bytes: int) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if not encoded or len(encoded) > max_bytes or b"\x00" in encoded:
        raise ValueError(f"{label} is empty, oversized, or contains NUL")
    return value


def _markdown(arguments: Mapping[str, object]) -> MarkdownContent:
    value = _bounded_text(
        _required_string(arguments, "content"),
        label="Markdown content",
        max_bytes=MAX_ARCHIVE_BYTES,
    )
    return MarkdownContent.from_text(value)


def _source(arguments: Mapping[str, object]) -> SourceInput:
    kind = _bounded_text(
        _required_string(arguments, "source_kind"), label="source kind", max_bytes=1_024
    )
    locator = _optional_string(arguments, "source_locator")
    if locator is not None:
        locator = _bounded_text(locator, label="source locator", max_bytes=MAX_SOURCE_BYTES)
    return SourceInput(kind=kind, locator=locator)


def _tool_inventory() -> list[types.Tool]:
    read = types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    write = types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    return [
        _tool("capabilities", "Read Agent v1 capabilities.", {}, [], read),
        _tool("whoami", "Read the current caller identity and Section grants.", {}, [], read),
        _tool("sections_list", "List granted Sections.", _pagination_properties(), [], read),
        _tool(
            "books_list",
            "List Books in one explicit Section.",
            {"section_id": _string_schema(), **_pagination_properties()},
            ["section_id"],
            read,
        ),
        _tool(
            "section_search",
            "Search current Revisions within one explicit Section.",
            {
                "section_id": _string_schema(),
                "query": _string_schema(max_length=MAX_QUERY_BYTES),
                **_pagination_properties(),
            },
            ["section_id", "query"],
            read,
        ),
        _tool(
            "page_current",
            "Fetch a current Page body and strong ETag.",
            {"section_id": _string_schema(), "page_id": _string_schema()},
            ["section_id", "page_id"],
            read,
        ),
        _tool(
            "page_revision",
            "Fetch one exact immutable Revision by positive revision number.",
            {
                "section_id": _string_schema(),
                "page_id": _string_schema(),
                "revision_number": {"type": "integer", "minimum": 1},
            },
            ["section_id", "page_id", "revision_number"],
            read,
        ),
        _tool(
            "archive_create",
            "Create a new archive Page; reuse operation_id only for an exact replay.",
            {
                "section_id": _string_schema(),
                "book_id": _string_schema(),
                "title": _string_schema(),
                "occurred_at": _string_schema(),
                "source_kind": _string_schema(),
                "source_locator": _nullable_string_schema(),
                "content": _string_schema(max_length=MAX_ARCHIVE_BYTES),
                "operation_id": _nullable_string_schema(max_length=36),
            },
            ["section_id", "book_id", "title", "occurred_at", "source_kind", "content"],
            write,
        ),
        _tool(
            "archive_revise",
            "Append a complete archive Revision using a required strong If-Match value.",
            {
                "section_id": _string_schema(),
                "page_id": _string_schema(),
                "if_match": _string_schema(),
                "source_kind": _string_schema(),
                "source_locator": _nullable_string_schema(),
                "content": _string_schema(max_length=MAX_ARCHIVE_BYTES),
                "operation_id": _nullable_string_schema(max_length=36),
            },
            ["section_id", "page_id", "if_match", "source_kind", "content"],
            write,
        ),
    ]


def _tool(
    name: str,
    description: str,
    properties: dict[str, object],
    required: list[str],
    annotations: types.ToolAnnotations,
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        annotations=annotations,
    )


def _string_schema(*, max_length: int = MAX_CURSOR_LENGTH) -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _nullable_string_schema(*, max_length: int = MAX_CURSOR_LENGTH) -> dict[str, object]:
    return {"anyOf": [_string_schema(max_length=max_length), {"type": "null"}], "default": None}


def _pagination_properties() -> dict[str, object]:
    return {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "cursor": _nullable_string_schema(),
    }


async def serve_stdio(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(), experimental_capabilities={}
            ),
        )


def run(*, runtime_factory: RuntimeFactory = runtime_from_environment) -> int:
    try:
        anyio.run(serve_stdio, create_server(runtime_factory=runtime_factory))
    except KeyboardInterrupt:
        return 130
    except Exception:
        sys.stderr.write("patchouli-mcp: startup or protocol session failed safely\n")
        return 70
    return 0


def entrypoint() -> None:
    if len(sys.argv) != 1:
        sys.stderr.write("patchouli-mcp: this stdio server accepts no command-line arguments\n")
        raise SystemExit(2)
    raise SystemExit(run())
