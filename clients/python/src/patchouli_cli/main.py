from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Never, TextIO, cast

from patchouli_cli.application import ArchiveApplication
from patchouli_cli.config import Profile, default_state_path, resolve_profile
from patchouli_cli.credentials import KeyringSecretStore, SecretStore, resolve_token
from patchouli_cli.errors import (
    CliError,
    ExitCode,
    input_error,
    usage_error,
)
from patchouli_cli.files import (
    MAX_MARKDOWN_BYTES,
    MAX_METADATA_BYTES,
    MAX_QUERY_BYTES,
    InputRoot,
    decode_text,
    open_input_root,
    read_stdin,
)
from patchouli_cli.journal import OperationJournal
from patchouli_cli.render import emit_error, emit_success
from patchouli_client import (
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    MarkdownContent,
    PatchouliClient,
    ProblemError,
    ProtocolError,
    SearchRequest,
    SourceInput,
    TransportError,
)
from patchouli_client.headers import ClientResponse
from patchouli_client.models import parse_rfc3339

ClientFactory = Callable[[str], PatchouliClient]


@dataclass(slots=True)
class RunState:
    operation_id: str | None = None


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise usage_error()


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="patchouli", description="PatchouliLib Agent CLI")
    parser.add_argument("--profile", help="non-secret profile name")
    parser.add_argument("--config", help="non-secret TOML profile file")
    parser.add_argument("--input-root", help="root allowed for sensitive input files")
    parser.add_argument(
        "--token-stdin", action="store_true", help="read only the bearer token from stdin"
    )
    parser.add_argument("--output", choices=("human", "json"), default="human")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "doctor", help="validate profile, credential, and API compatibility"
    ).set_defaults(handler="doctor")
    commands.add_parser("capabilities", help="show safe server capabilities").set_defaults(
        handler="capabilities"
    )
    commands.add_parser("whoami", help="show the current caller grants").set_defaults(
        handler="whoami"
    )

    sections = commands.add_parser("sections", help="Section operations").add_subparsers(
        dest="sections_action", required=True
    )
    section_list = sections.add_parser("list", help="list granted Sections")
    _add_pagination(section_list)
    section_list.set_defaults(handler="sections.list")

    books = commands.add_parser("books", help="Book operations").add_subparsers(
        dest="books_action", required=True
    )
    book_list = books.add_parser("list", help="list Books in a Section")
    book_list.add_argument("--section", required=True)
    _add_pagination(book_list)
    book_list.set_defaults(handler="books.list")

    pages = commands.add_parser("pages", help="Page metadata operations").add_subparsers(
        dest="pages_action", required=True
    )
    page_list = pages.add_parser("list", help="list Page metadata in a Section")
    page_list.add_argument("--section", required=True)
    _add_pagination(page_list)
    page_list.set_defaults(handler="pages.list")

    section = commands.add_parser("section", help="Section-scoped queries").add_subparsers(
        dest="section_action", required=True
    )
    search = section.add_parser("search", help="search current Revisions in one Section")
    search.add_argument("--section", required=True)
    query = search.add_mutually_exclusive_group(required=True)
    query.add_argument("--query-file")
    query.add_argument("--query-stdin", action="store_true")
    _add_pagination(search)
    search.set_defaults(handler="section.search")

    page = commands.add_parser("page", help="Page and Revision reads").add_subparsers(
        dest="page_action", required=True
    )
    current = page.add_parser("current", help="fetch a current Page body and ETag")
    current.add_argument("--section", required=True)
    current.add_argument("--page", required=True)
    current.set_defaults(handler="page.current")
    exact = page.add_parser("revision", help="fetch an exact immutable Revision")
    exact.add_argument("--section", required=True)
    exact.add_argument("--page", required=True)
    exact.add_argument("--revision", required=True, type=int)
    exact.set_defaults(handler="page.revision")

    archive = commands.add_parser("archive", help="explicit archive mutations").add_subparsers(
        dest="archive_action", required=True
    )
    create = archive.add_parser("create", help="create a new archive Page")
    create.add_argument("--section", required=True)
    create.add_argument("--book", required=True)
    _add_sensitive_inputs(create)
    create.set_defaults(handler="archive.create")
    revise = archive.add_parser("revise", help="append a complete archive Revision")
    revise.add_argument("--section", required=True)
    revise.add_argument("--page", required=True)
    revise.add_argument("--if-match", required=True, help="strong ETag from a current Page read")
    _add_sensitive_inputs(revise)
    revise.set_defaults(handler="archive.revise")
    return parser


def run(
    argv: list[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: BinaryIO | TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    client_factory: ClientFactory = PatchouliClient,
    secret_store: SecretStore | None = None,
) -> int:
    resolved_environ = dict(os.environ if environ is None else environ)
    input_stream: BinaryIO | TextIO = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    output_mode = _requested_output(argv)
    state = RunState()
    client: PatchouliClient | None = None
    try:
        args = build_parser().parse_args(argv)
        output_mode = cast(str, args.output)
        _validate_stdin_ownership(args)
        profile = resolve_profile(
            profile_name=cast(str | None, args.profile),
            config_path=cast(str | None, args.config),
            environ=resolved_environ,
        )
        resolved_token = resolve_token(
            profile=profile.name,
            token_stdin=cast(bool, args.token_stdin),
            environ=resolved_environ,
            stdin=input_stream,
            secret_store=secret_store or KeyringSecretStore(),
        )
        client = client_factory(profile.endpoint)
        operation = cast(str, args.handler)
        response, state.operation_id = _dispatch(
            operation,
            args,
            profile=profile,
            token=resolved_token.token,
            credential_source=resolved_token.source,
            environ=resolved_environ,
            stdin=input_stream,
            client=client,
            state=state,
        )
        emit_success(
            output_stream,
            output=output_mode,
            operation=operation,
            value=response.value,
            metadata=response.metadata,
            operation_id=state.operation_id,
        )
        return int(ExitCode.SUCCESS)
    except CliError as exc:
        emit_error(error_stream, output=output_mode, error=exc, operation_id=state.operation_id)
        return int(exc.exit_code)
    except ProblemError as exc:
        mapped = _map_problem(exc, operation_id=state.operation_id)
        emit_error(
            error_stream,
            output=output_mode,
            error=mapped,
            request_id=exc.problem.request_id,
            operation_id=state.operation_id,
        )
        return int(mapped.exit_code)
    except TransportError:
        mapped = CliError(
            ExitCode.TRANSPORT,
            "transport",
            "transport_failure",
            "request failed after bounded transport retries; a journaled write can be replayed",
        )
        emit_error(error_stream, output=output_mode, error=mapped, operation_id=state.operation_id)
        return int(mapped.exit_code)
    except ProtocolError as exc:
        if str(exc) == "error response was not RFC 9457 Problem Details":
            mapped = CliError(
                ExitCode.EDGE_GATE,
                "edge_gate",
                "edge_gate_or_nonconforming_upstream",
                "an outer access gate or non-conforming upstream rejected the request",
            )
        else:
            mapped = CliError(
                ExitCode.PROTOCOL,
                "protocol",
                "protocol_error",
                "server response did not satisfy the accepted Agent v1 contract",
            )
        emit_error(error_stream, output=output_mode, error=mapped, operation_id=state.operation_id)
        return int(mapped.exit_code)
    except ValueError:
        mapped = input_error("command input did not satisfy the accepted client contract")
        emit_error(error_stream, output=output_mode, error=mapped, operation_id=state.operation_id)
        return int(mapped.exit_code)
    except KeyboardInterrupt:
        mapped = CliError(
            ExitCode.INTERRUPTED,
            "interrupted",
            "interrupted",
            "operation interrupted; a journaled write can be replayed",
        )
        emit_error(error_stream, output=output_mode, error=mapped, operation_id=state.operation_id)
        return int(mapped.exit_code)
    except Exception:
        mapped = CliError(
            ExitCode.INTERNAL,
            "internal",
            "internal_error",
            "CLI failed closed without exposing internal details",
        )
        emit_error(error_stream, output=output_mode, error=mapped, operation_id=state.operation_id)
        return int(mapped.exit_code)
    finally:
        if client is not None:
            client.close()


def entrypoint() -> None:
    raise SystemExit(run(sys.argv[1:]))


def _requested_output(argv: list[str]) -> str:
    """Identify an explicit output mode without parsing or rendering argument values."""
    requested = "human"
    for index, value in enumerate(argv):
        candidate: str | None = None
        if value == "--output" and index + 1 < len(argv):
            candidate = argv[index + 1]
        elif value.startswith("--output="):
            candidate = value.partition("=")[2]
        if candidate in {"human", "json"}:
            requested = candidate
    return requested


def _dispatch(
    operation: str,
    args: argparse.Namespace,
    *,
    profile: Profile,
    token: object,
    credential_source: str,
    environ: Mapping[str, str],
    stdin: BinaryIO | TextIO,
    client: PatchouliClient,
    state: RunState,
) -> tuple[ClientResponse[object], str | None]:
    from patchouli_client import BearerToken

    caller_token = cast(BearerToken, token)
    if operation == "doctor":
        response = client.capabilities(token=caller_token)
        if profile.api_version not in response.value.api_versions:
            raise ProtocolError("server does not advertise the configured API version")
        value = {
            "profile": profile.name,
            "api_version": profile.api_version,
            "credential_source": credential_source,
            "capabilities": response.value,
        }
        return ClientResponse(value=value, metadata=response.metadata), None
    if operation == "capabilities":
        return cast(ClientResponse[object], client.capabilities(token=caller_token)), None
    if operation == "whoami":
        return cast(ClientResponse[object], client.whoami(token=caller_token)), None
    if operation == "sections.list":
        return (
            cast(
                ClientResponse[object],
                client.list_sections(token=caller_token, limit=args.limit, cursor=args.cursor),
            ),
            None,
        )
    if operation == "books.list":
        return (
            cast(
                ClientResponse[object],
                client.list_books(
                    args.section,
                    token=caller_token,
                    limit=args.limit,
                    cursor=args.cursor,
                ),
            ),
            None,
        )
    if operation == "pages.list":
        return (
            cast(
                ClientResponse[object],
                client.list_pages(
                    args.section,
                    token=caller_token,
                    limit=args.limit,
                    cursor=args.cursor,
                ),
            ),
            None,
        )
    if operation == "section.search":
        if args.query_stdin:
            query_data = read_stdin(stdin, max_bytes=MAX_QUERY_BYTES)
        else:
            with open_input_root(cast(str | None, args.input_root), environ) as input_root:
                query_data = _read_sensitive(args, "query", stdin, input_root, MAX_QUERY_BYTES)
        query = decode_text(query_data, label="search query", trim_terminal_newline=True)
        request = SearchRequest(query=query, limit=args.limit, cursor=args.cursor)
        return cast(
            ClientResponse[object], client.search(args.section, request, token=caller_token)
        ), None
    if operation == "page.current":
        return cast(
            ClientResponse[object], client.get_page(args.section, args.page, token=caller_token)
        ), None
    if operation == "page.revision":
        return (
            cast(
                ClientResponse[object],
                client.get_revision(args.section, args.page, args.revision, token=caller_token),
            ),
            None,
        )
    if operation in {"archive.create", "archive.revise"}:
        return _archive_operation(
            operation,
            args,
            profile=profile,
            token=caller_token,
            environ=environ,
            stdin=stdin,
            client=client,
            state=state,
        )
    raise AssertionError("parser selected an unknown operation")  # pragma: no cover


def _archive_operation(
    operation: str,
    args: argparse.Namespace,
    *,
    profile: Profile,
    token: object,
    environ: Mapping[str, str],
    stdin: BinaryIO | TextIO,
    client: PatchouliClient,
    state: RunState,
) -> tuple[ClientResponse[object], str]:
    from patchouli_client import BearerToken

    with open_input_root(cast(str | None, args.input_root), environ) as input_root:
        metadata_data = _read_sensitive(args, "metadata", stdin, input_root, MAX_METADATA_BYTES)
        content_data = _read_sensitive(args, "content", stdin, input_root, MAX_MARKDOWN_BYTES)
    metadata_object = _parse_json_object(metadata_data, label="archive metadata")
    content = MarkdownContent(content_data)
    if operation == "archive.create":
        metadata: ArchiveCreateMetadata | ArchiveRevisionMetadata = _create_metadata(
            metadata_object
        )
    else:
        metadata = _revision_metadata(metadata_object)

    caller_token = cast(BearerToken, token)

    with OperationJournal(default_state_path(environ), profile.name) as journal:
        application = ArchiveApplication(
            endpoint=profile.endpoint,
            api_version=profile.api_version,
            client=client,
            token=caller_token,
            journal=journal,
        )
        try:
            if operation == "archive.create":
                result = application.create_archive(
                    args.section,
                    args.book,
                    cast(ArchiveCreateMetadata, metadata),
                    content,
                    operation_id=args.operation_id,
                )
            else:
                result = application.revise_archive(
                    args.section,
                    args.page,
                    cast(ArchiveRevisionMetadata, metadata),
                    content,
                    if_match=args.if_match,
                    operation_id=args.operation_id,
                )
        finally:
            state.operation_id = application.operation_id
        return cast(ClientResponse[object], result.response), result.operation_id


def _read_sensitive(
    args: argparse.Namespace,
    label: str,
    stdin: BinaryIO | TextIO,
    input_root: InputRoot,
    limit: int,
) -> bytes:
    if cast(bool, getattr(args, f"{label}_stdin")):
        return read_stdin(stdin, max_bytes=limit)
    path_value = cast(str, getattr(args, f"{label}_file"))
    return input_root.read(path_value, max_bytes=limit)


def _parse_json_object(data: bytes, *, label: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise input_error(f"{label} must be a valid UTF-8 JSON object") from exc
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise input_error(f"{label} must be a JSON object")
    return cast(dict[str, object], parsed)


def _create_metadata(data: Mapping[str, object]) -> ArchiveCreateMetadata:
    if set(data) != {"title", "occurred_at", "source"}:
        raise input_error("create metadata requires exactly title, occurred_at, and source")
    title = data["title"]
    occurred_at = data["occurred_at"]
    if not isinstance(title, str) or not isinstance(occurred_at, str):
        raise input_error("create metadata title and occurred_at must be strings")
    try:
        timestamp: datetime = parse_rfc3339(occurred_at)
    except ProtocolError as exc:
        raise input_error("create metadata occurred_at must be accepted RFC 3339") from exc
    return ArchiveCreateMetadata(title=title, occurred_at=timestamp, source=_source(data["source"]))


def _revision_metadata(data: Mapping[str, object]) -> ArchiveRevisionMetadata:
    if set(data) != {"source"}:
        raise input_error("revision metadata requires exactly source")
    return ArchiveRevisionMetadata(source=_source(data["source"]))


def _source(value: object) -> SourceInput:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise input_error("archive source must be a JSON object")
    data = cast(dict[str, object], value)
    if not {"kind"} <= set(data) <= {"kind", "locator"}:
        raise input_error("archive source requires kind and optional locator")
    kind = data["kind"]
    locator = data.get("locator")
    if not isinstance(kind, str) or (locator is not None and not isinstance(locator, str)):
        raise input_error("archive source kind and locator must be strings")
    return SourceInput(kind=kind, locator=locator)


def _validate_stdin_ownership(args: argparse.Namespace) -> None:
    consumers = [cast(bool, args.token_stdin)]
    for name in ("query_stdin", "metadata_stdin", "content_stdin"):
        consumers.append(bool(getattr(args, name, False)))
    if sum(consumers) > 1:
        raise usage_error("stdin can supply only one of token, query, metadata, or content")


def _add_pagination(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cursor")


def _add_sensitive_inputs(parser: argparse.ArgumentParser) -> None:
    metadata = parser.add_mutually_exclusive_group(required=True)
    metadata.add_argument("--metadata-file")
    metadata.add_argument("--metadata-stdin", action="store_true")
    content = parser.add_mutually_exclusive_group(required=True)
    content.add_argument("--content-file")
    content.add_argument("--content-stdin", action="store_true")
    parser.add_argument(
        "--operation-id", help="reuse a matching permission-restricted journal entry"
    )


def _map_problem(error: ProblemError, *, operation_id: str | None) -> CliError:
    status = error.problem.status
    code = error.problem.code
    if status == 401 or code in {"authentication_required", "invalid_token"}:
        return CliError(ExitCode.AUTH, "auth", code, "caller credential was rejected")
    if status == 403 or code == "insufficient_scope":
        return CliError(ExitCode.SCOPE, "scope", code, "caller lacks the required Section action")
    if status == 404 or code == "resource_not_found":
        return CliError(
            ExitCode.NOT_FOUND, "not_found", code, "resource was not found or is hidden"
        )
    if status == 409 or code == "idempotency_mismatch":
        return CliError(
            ExitCode.CONFLICT, "conflict", code, "operation key conflicts with another request"
        )
    if status in {412, 428} or code in {"revision_conflict", "precondition_required"}:
        if operation_id is None:
            message = (
                "revision precondition was not accepted; fetch the current Page before retrying"
            )
        else:
            message = (
                "revision was not applied; retain this journal for exact replay, "
                "or fetch the current Page and start a new operation when changing If-Match"
            )
        return CliError(ExitCode.PRECONDITION, "precondition", code, message)
    if status in {400, 413, 415, 422}:
        return CliError(
            ExitCode.VALIDATION, "validation", code, "request was rejected by validation"
        )
    if status in {408, 429, 502, 503, 504}:
        return CliError(ExitCode.SERVICE, "service", code, "service is temporarily unavailable")
    return CliError(ExitCode.PROTOCOL, "application", code, "server returned an application error")
