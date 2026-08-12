from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

import patchouli_cli.main as main_module
from cli.conftest import invoke_cli, protected_headers
from patchouli_cli.errors import ExitCode


def problem_response(status: int, code: str) -> httpx.Response:
    request_id = "req_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    return httpx.Response(
        status,
        headers={
            "Content-Type": "application/problem+json",
            "Cache-Control": "private, no-store",
            "X-Request-ID": request_id,
        },
        json={
            "type": "about:blank",
            "title": "Synthetic problem",
            "status": status,
            "detail": "A fixed safe synthetic detail.",
            "code": code,
            "request_id": request_id,
            "details": {},
        },
    )


@pytest.mark.parametrize(
    ("status", "code", "exit_code", "category"),
    [
        (401, "invalid_token", ExitCode.AUTH, "auth"),
        (403, "insufficient_scope", ExitCode.SCOPE, "scope"),
        (404, "resource_not_found", ExitCode.NOT_FOUND, "not_found"),
        (409, "idempotency_mismatch", ExitCode.CONFLICT, "conflict"),
        (412, "revision_conflict", ExitCode.PRECONDITION, "precondition"),
        (413, "content_too_large", ExitCode.VALIDATION, "validation"),
        (415, "unsupported_media_type", ExitCode.VALIDATION, "validation"),
        (422, "request_validation_failed", ExitCode.VALIDATION, "validation"),
        (429, "rate_limited", ExitCode.SERVICE, "service"),
        (503, "search_unavailable", ExitCode.SERVICE, "service"),
        (418, "synthetic_problem", ExitCode.PROTOCOL, "application"),
    ],
)
def test_problem_exit_matrix_is_deterministic_and_json_stays_on_stderr(
    status: int,
    code: str,
    exit_code: ExitCode,
    category: str,
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return problem_response(status, code)

    result = invoke_cli(["--output", "json", "capabilities"], handler=handler, tmp_path=tmp_path)

    assert result.status == exit_code
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload == {
        "ok": False,
        "error": {
            "category": category,
            "code": code,
            "message": payload["error"]["message"],
            "request_id": "req_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
    }
    assert "fixed safe synthetic detail" not in result.stderr.lower()


@pytest.mark.parametrize(
    ("status", "code"), [(412, "revision_conflict"), (428, "precondition_required")]
)
def test_revision_precondition_error_acknowledges_journaled_operation(
    status: int, code: str, tmp_path: Path
) -> None:
    (tmp_path / "metadata.json").write_text('{"source":{"kind":"conversation"}}', encoding="utf-8")
    (tmp_path / "content.md").write_text("# Revision", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert list((tmp_path / "state" / "default").glob("*.json"))
        return problem_response(status, code)

    result = invoke_cli(
        [
            "archive",
            "revise",
            "--section",
            "sec_synthetic",
            "--page",
            "page_synthetic",
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

    assert result.status == ExitCode.PRECONDITION
    assert result.stdout == ""
    assert "operation_id=" in result.stderr
    assert "not applied" in result.stderr
    assert "start a new operation" in result.stderr


def test_transport_error_is_redacted_and_retains_operation_id(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text(
        '{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
        '"source":{"kind":"conversation"}}',
        encoding="utf-8",
    )
    (tmp_path / "content.md").write_text("private body marker", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private endpoint marker", request=request)

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
            "--content-file",
            "content.md",
        ],
        handler=handler,
        tmp_path=tmp_path,
    )

    assert result.status == ExitCode.TRANSPORT
    assert "operation_id=" in result.stderr
    assert "private endpoint marker" not in result.stderr
    assert "private body marker" not in result.stderr
    assert "cred_synthetic_123" not in result.stderr


def test_edge_gate_and_protocol_failures_have_distinct_exit_codes(tmp_path: Path) -> None:
    def edge_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, headers={"Content-Type": "text/html"}, text="edge marker")

    edge = invoke_cli(["capabilities"], handler=edge_handler, tmp_path=tmp_path)
    assert edge.status == ExitCode.EDGE_GATE
    assert "edge marker" not in edge.stderr

    def protocol_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers=protected_headers(), text="not-json")

    protocol = invoke_cli(["capabilities"], handler=protocol_handler, tmp_path=tmp_path)
    assert protocol.status == ExitCode.PROTOCOL
    assert "not-json" not in protocol.stderr


def test_unknown_argument_does_not_echo_possible_token_from_argv(tmp_path: Path) -> None:
    secret = "cred_should_never_be_in_argv"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"usage error reached network: {request.method}")

    result = invoke_cli(["--token", secret, "capabilities"], handler=handler, tmp_path=tmp_path)

    assert result.status == ExitCode.USAGE
    assert result.stdout == ""
    assert secret not in result.stderr
    assert "invalid_arguments" in result.stderr


@pytest.mark.parametrize("output_argument", [["--output", "json"], ["--output=json"]])
def test_explicit_json_mode_survives_argument_parse_failure(
    output_argument: list[str], tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"usage error reached network: {request.method}")

    result = invoke_cli(
        [*output_argument, "--unknown", "possible-secret", "capabilities"],
        handler=handler,
        tmp_path=tmp_path,
    )
    assert result.status == ExitCode.USAGE
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": {
            "category": "usage",
            "code": "invalid_arguments",
            "message": "invalid command arguments; use --help",
        },
    }
    assert "possible-secret" not in result.stderr


def test_stdin_cannot_be_shared_by_token_and_sensitive_input(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"stdin ownership error reached network: {request.method}")

    result = invoke_cli(
        [
            "--token-stdin",
            "section",
            "search",
            "--section",
            "sec_synthetic",
            "--query-stdin",
        ],
        handler=handler,
        tmp_path=tmp_path,
        stdin="cred_synthetic_123\n",
    )

    assert result.status == ExitCode.USAGE
    assert "only one" in result.stderr


def test_local_value_error_keyboard_interrupt_and_internal_failure_are_distinct(
    tmp_path: Path,
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"local validation reached network: {request.method}")

    invalid = invoke_cli(
        [
            "page",
            "revision",
            "--section",
            "sec_synthetic",
            "--page",
            "page_synthetic",
            "--revision",
            "0",
        ],
        handler=forbidden,
        tmp_path=tmp_path,
    )
    assert invalid.status == ExitCode.VALIDATION

    def interrupted(request: httpx.Request) -> httpx.Response:
        del request
        raise KeyboardInterrupt

    interruption = invoke_cli(["capabilities"], handler=interrupted, tmp_path=tmp_path)
    assert interruption.status == ExitCode.INTERRUPTED

    def internal(request: httpx.Request) -> httpx.Response:
        del request
        raise RuntimeError("private internal marker")

    failure = invoke_cli(["capabilities"], handler=internal, tmp_path=tmp_path)
    assert failure.status == ExitCode.INTERNAL
    assert "private internal marker" not in failure.stderr


@pytest.mark.parametrize(
    ("operation", "metadata", "message"),
    [
        ("create", b"not-json", "valid UTF-8 JSON object"),
        ("create", b"[]", "JSON object"),
        ("create", b'{"source":{"kind":"conversation"}}', "requires exactly"),
        (
            "create",
            b'{"title":1,"occurred_at":"2026-08-11T09:15:00Z","source":{"kind":"conversation"}}',
            "must be strings",
        ),
        (
            "create",
            b'{"title":"Synthetic","occurred_at":"invalid","source":{"kind":"conversation"}}',
            "accepted RFC 3339",
        ),
        (
            "create",
            b'{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z","source":1}',
            "source must be a JSON object",
        ),
        (
            "create",
            b'{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z",'
            b'"source":{"kind":"conversation","extra":true}}',
            "requires kind",
        ),
        (
            "create",
            b'{"title":"Synthetic","occurred_at":"2026-08-11T09:15:00Z","source":{"kind":1}}',
            "must be strings",
        ),
        (
            "revise",
            b'{"source":{"kind":"conversation"},"extra":true}',
            "requires exactly source",
        ),
    ],
)
def test_archive_metadata_validation_is_strict_and_safe(
    operation: str, metadata: bytes, message: str, tmp_path: Path
) -> None:
    (tmp_path / "metadata.json").write_bytes(metadata)
    (tmp_path / "content.md").write_text("# Synthetic", encoding="utf-8")
    route = ["archive", operation, "--section", "sec_synthetic"]
    if operation == "create":
        route.extend(["--book", "book_synthetic"])
    else:
        route.extend(["--page", "page_synthetic", "--if-match", '"etag"'])
    route.extend(["--metadata-file", "metadata.json", "--content-file", "content.md"])

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"invalid metadata reached network: {request.method}")

    result = invoke_cli(route, handler=forbidden, tmp_path=tmp_path)
    assert result.status == ExitCode.VALIDATION
    assert message in result.stderr
    assert metadata.decode("utf-8", errors="ignore") not in result.stderr


def test_entrypoint_converts_run_status_to_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "run", lambda argv: 17)
    monkeypatch.setattr(sys, "argv", ["patchouli", "capabilities"])
    with pytest.raises(SystemExit) as raised:
        main_module.entrypoint()
    assert raised.value.code == 17
