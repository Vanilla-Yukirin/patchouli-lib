from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from cli.conftest import protected_headers, sample_page, whoami_body
from patchouli_cli.application import ArchiveApplication
from patchouli_cli.errors import CliError
from patchouli_cli.journal import OperationJournal
from patchouli_client import (
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    BearerToken,
    MarkdownContent,
    PatchouliClient,
    RetryPolicy,
    SourceInput,
    TransportError,
)

_ENDPOINT = "https://patchouli.example.invalid"
_PAGE_ID = "20260811t091500123z-synthetic-session"


def _create_metadata(title: str = "Synthetic") -> ArchiveCreateMetadata:
    return ArchiveCreateMetadata(
        title=title,
        occurred_at=datetime(2026, 8, 11, 9, 15, tzinfo=UTC),
        source=SourceInput(kind="conversation"),
    )


def _application(
    tmp_path: Path,
    handler: httpx.MockTransport,
    *,
    endpoint: str = _ENDPOINT,
    token: str = "cred_synthetic_one",
) -> tuple[ArchiveApplication, PatchouliClient, OperationJournal]:
    client = PatchouliClient(
        endpoint,
        http_transport=handler,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    journal = OperationJournal(tmp_path / "state", "default")
    return (
        ArchiveApplication(
            endpoint=endpoint,
            api_version="v1",
            client=client,
            token=BearerToken(token),
            journal=journal,
        ),
        client,
        journal,
    )


def test_application_reuses_key_for_same_caller_after_credential_rotation(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(200, headers=protected_headers(), json=whoami_body())
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(
            201,
            headers=protected_headers(
                Location=f"/api/v1/sections/sec_synthetic/pages/{_PAGE_ID}",
                ETag='"revision-synthetic-1"',
            ),
            json=sample_page(),
        )

    transport = httpx.MockTransport(handler)
    first, first_client, first_journal = _application(tmp_path, transport)
    result = first.create_archive(
        "sec_synthetic",
        "book_synthetic",
        _create_metadata(),
        MarkdownContent.from_text("# Synthetic"),
    )
    first_journal.close()
    first_client.close()

    second, second_client, second_journal = _application(
        tmp_path, transport, token="cred_synthetic_rotated"
    )
    replay = second.create_archive(
        "sec_synthetic",
        "book_synthetic",
        _create_metadata(),
        MarkdownContent.from_text("# Synthetic"),
        operation_id=result.operation_id,
    )
    second_journal.close()
    second_client.close()

    assert replay.operation_id == result.operation_id
    assert keys[0] == keys[1]
    assert [request.url.path for request in requests] == [
        "/api/v1/auth/whoami",
        "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
        "/api/v1/auth/whoami",
        "/api/v1/sections/sec_synthetic/books/book_synthetic/pages",
    ]
    assert "cred_" not in repr(first) + repr(result)
    assert "op_" not in repr(result)


@pytest.mark.parametrize("mismatch", ["endpoint", "metadata", "content", "if_match"])
def test_application_replay_preflight_mismatch_has_zero_network(
    tmp_path: Path, mismatch: str
) -> None:
    caller = "caller_synthetic"

    def successful(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(
                200, headers=protected_headers(), json=whoami_body(caller_id=caller)
            )
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

    first, client, journal = _application(tmp_path, httpx.MockTransport(successful))
    if mismatch == "if_match":
        created = first.revise_archive(
            "sec_synthetic",
            _PAGE_ID,
            ArchiveRevisionMetadata(source=SourceInput(kind="conversation")),
            MarkdownContent.from_text("# Synthetic"),
            if_match='"revision-synthetic-1"',
        )
    else:
        created = first.create_archive(
            "sec_synthetic",
            "book_synthetic",
            _create_metadata(),
            MarkdownContent.from_text("# Synthetic"),
        )
    journal.close()
    client.close()

    requests: list[httpx.Request] = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("mismatched replay reached the network")

    endpoint = "https://changed.example.invalid" if mismatch == "endpoint" else _ENDPOINT
    replay, replay_client, replay_journal = _application(
        tmp_path, httpx.MockTransport(forbidden), endpoint=endpoint
    )
    with pytest.raises(CliError, match="does not match"):
        if mismatch == "if_match":
            replay.revise_archive(
                "sec_synthetic",
                _PAGE_ID,
                ArchiveRevisionMetadata(source=SourceInput(kind="conversation")),
                MarkdownContent.from_text("# Synthetic"),
                if_match='"revision-synthetic-2"',
                operation_id=created.operation_id,
            )
        else:
            replay.create_archive(
                "sec_synthetic",
                "book_synthetic",
                _create_metadata("Changed" if mismatch == "metadata" else "Synthetic"),
                MarkdownContent.from_text("changed" if mismatch == "content" else "# Synthetic"),
                operation_id=created.operation_id,
            )
    replay_journal.close()
    replay_client.close()
    assert requests == []


def test_application_failure_leaves_caller_bound_pending_record(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/whoami":
            return httpx.Response(200, headers=protected_headers(), json=whoami_body())
        raise httpx.ConnectError("synthetic low-level detail", request=request)

    application, client, journal = _application(tmp_path, httpx.MockTransport(handler))
    with pytest.raises(TransportError, match="transport failed"):
        application.create_archive(
            "sec_synthetic",
            "book_synthetic",
            _create_metadata(),
            MarkdownContent.from_text("# Synthetic"),
        )
    operation_id = application.operation_id
    assert operation_id is not None
    record = json.loads(
        (tmp_path / "state" / "default" / f"{operation_id}.json").read_text(encoding="utf-8")
    )
    journal.close()
    client.close()
    assert record["caller_id"] == "caller_synthetic"
    assert record["status"] == "pending"
    assert "credential" not in record
