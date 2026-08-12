from __future__ import annotations

import io
import os
import shutil
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from patchouli_cli.main import run
from patchouli_client import PatchouliClient, RetryPolicy

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass(frozen=True, slots=True)
class CliResult:
    status: int
    stdout: str
    stderr: str


class MissingSecretStore:
    def get_token(self, profile: str) -> str | None:
        del profile
        return None


@pytest.fixture
def trusted_tmp_path(tmp_path: Path) -> Iterator[Path]:
    """Use a path whose Windows ancestors are not writable by sandbox peer accounts."""
    if os.name != "nt":
        yield tmp_path
        return
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        pytest.skip("LOCALAPPDATA is unavailable")
    parent = Path(local) / "PatchouliLibTests"
    parent.mkdir(exist_ok=True)
    path = parent / str(uuid.uuid4())
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def invoke_cli(
    argv: list[str],
    *,
    handler: Handler,
    tmp_path: Path,
    stdin: str | bytes = "",
    environ: Mapping[str, str] | None = None,
    caller_id: str = "caller_synthetic",
    credential_id: str = "credential_synthetic",
    observed_requests: list[httpx.Request] | None = None,
) -> CliResult:
    output = io.StringIO()
    error = io.StringIO()
    resolved_environ = {
        "PATCHOULI_ENDPOINT": "https://patchouli.example.invalid",
        "PATCHOULI_TOKEN": "cred_synthetic_123",
        "PATCHOULI_STATE_DIR": str(tmp_path / "state"),
        "PATCHOULI_INPUT_ROOT": str(tmp_path),
        **dict(environ or {}),
    }
    configured_endpoint = resolved_environ["PATCHOULI_ENDPOINT"]
    mutation = "archive" in argv

    def dispatch(request: httpx.Request) -> httpx.Response:
        if observed_requests is not None:
            observed_requests.append(request)
        if mutation and request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(
                200,
                headers=protected_headers(),
                json=whoami_body(caller_id=caller_id, credential_id=credential_id),
            )
        return handler(request)

    def client_factory(endpoint: str) -> PatchouliClient:
        assert endpoint == configured_endpoint
        return PatchouliClient(
            endpoint,
            http_transport=httpx.MockTransport(dispatch),
            retry_policy=RetryPolicy(max_attempts=1),
        )

    input_stream = io.BytesIO(stdin) if isinstance(stdin, bytes) else io.StringIO(stdin)
    status = run(
        argv,
        environ=resolved_environ,
        stdin=input_stream,
        stdout=output,
        stderr=error,
        client_factory=client_factory,
        secret_store=MissingSecretStore(),
    )
    return CliResult(status=status, stdout=output.getvalue(), stderr=error.getvalue())


def protected_headers(**extra: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "private, no-store",
        "X-Request-ID": "req_11111111111111111111111111111111",
        **extra,
    }


def capabilities_body() -> dict[str, object]:
    return {
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
    }


def whoami_body(
    *,
    caller_id: str = "caller_synthetic",
    credential_id: str = "credential_synthetic",
) -> dict[str, object]:
    return {
        "caller_id": caller_id,
        "credential_id": credential_id,
        "kind": "agent",
        "expires_at": "2027-01-01T00:00:00.000000Z",
        "policy_version": 3,
        "grants": [{"section_id": "sec_synthetic", "actions": ["archive:write"]}],
    }


def sample_page(
    *,
    revision_number: int = 1,
    revision_id: str = "rev_0123456789abcdef0123456789abcdef",
) -> dict[str, object]:
    page_id = "20260811t091500123z-synthetic-session"
    return {
        "page": {
            "section_id": "sec_synthetic",
            "book_id": "book_synthetic",
            "page_id": page_id,
            "title": "Synthetic session",
            "type": "archive",
            "occurred_at": "2026-08-11T09:15:00.123456Z",
            "current_revision_id": revision_id,
            "current_revision_number": revision_number,
        },
        "revision": {
            "page_id": page_id,
            "revision_id": revision_id,
            "revision_number": revision_number,
            "created_at": "2026-08-11T09:16:00.000000Z",
            "content_type": "text/markdown;charset=utf-8",
            "content_sha256": "a" * 64,
            "content": "# Synthetic archive",
        },
        "citation": {
            "section_id": "sec_synthetic",
            "page_id": page_id,
            "revision_id": revision_id,
            "revision_number": revision_number,
            "href": (f"/api/v1/sections/sec_synthetic/pages/{page_id}/revisions/{revision_number}"),
        },
    }
