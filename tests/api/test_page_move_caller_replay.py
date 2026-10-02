"""HTTP replay admission follows the current Page before receiving upload bytes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from content.conftest import OPERATION_TIME, ArchiveScope
from content.conftest import archive_scope as archive_scope
from content.conftest import content_engine as content_engine
from content.test_page_move_caller_replay import (
    KINDS,
    TARGET,
    Command,
    _counts,
    _destination,
    _move,
    _prepare,
)
from fastapi import FastAPI
from sqlalchemy import Engine, delete
from starlette.types import Message, Scope

from patchouli_lib.api.archive_routes import create_archive_router
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.file_set_write_routes import create_file_set_write_router
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.auth.models import SectionGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.file_set_write_service import FileSetAppendCommand
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    CorrectArchiveOccurrenceCommand,
    CreateArchiveCommand,
    PageLifecycleCommand,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import canonical_utc_wire


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]
    received: int


def _app(engine: Engine) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: "req_" + "e" * 32)
    app.include_router(create_archive_router(engine, clock=lambda: OPERATION_TIME))
    app.include_router(create_file_set_write_router(engine, clock=lambda: OPERATION_TIME))
    return app


def _wire(kind: str, command: Command) -> tuple[str, str, str, bytes]:
    page_base = f"/api/v1/sections/{command.section_id}/pages"
    metadata: dict[str, object]
    if isinstance(command, PageLifecycleCommand):
        return (
            "DELETE" if kind == "delete" else "POST",
            f"{page_base}/{command.page_id}" + ("" if kind == "delete" else "/restore"),
            "",
            b"",
        )
    if isinstance(command, CorrectArchiveOccurrenceCommand):
        return (
            "PATCH",
            f"{page_base}/{command.page_id}/occurrence",
            "application/json",
            json.dumps({"occurred_at": canonical_utc_wire(command.occurred_at)}).encode(),
        )
    metadata = {"source": command.source.model_dump(exclude_none=True)}
    if isinstance(command, (CreateArchiveCommand, FileSetCreateCommand)):
        metadata["title"] = command.title
        if command.occurred_at is not None:
            metadata["occurred_at"] = canonical_utc_wire(command.occurred_at)
    boundary = "synthetic-replay-admission-boundary"
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode(),
        json.dumps(metadata).encode(),
        b"\r\n",
    ]
    if isinstance(command, (CreateArchiveCommand, AppendArchiveRevisionCommand)):
        parts.extend(
            (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="content"\r\n'
                    "Content-Type: text/markdown;charset=utf-8\r\n\r\n"
                ).encode(),
                command.content_md,
                b"\r\n",
            )
        )
        path = (
            f"/api/v1/sections/{command.section_id}/books/{command.book_id}/pages"
            if isinstance(command, CreateArchiveCommand)
            else f"{page_base}/{command.page_id}/revisions"
        )
    else:
        for name, content in command.files:
            parts.extend(
                (
                    (
                        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                        f'filename="{name}"\r\n'
                        "Content-Type: application/octet-stream\r\n\r\n"
                    ).encode(),
                    content,
                    b"\r\n",
                )
            )
        prefix = f"/api/v1/libraries/{command.library_id}/sections/{command.section_id}"
        path = (
            f"{prefix}/books/{command.book_id}/pages"
            if isinstance(command, FileSetCreateCommand)
            else f"{prefix}/pages/{command.page_id}/file-revisions"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return "POST", path, f"multipart/form-data; boundary={boundary}", b"".join(parts)


def _call(
    app: FastAPI,
    scope: ArchiveScope,
    kind: str,
    command: Command,
    *,
    key: str = "original",
    token: str | None = None,
    oversized: bool = False,
) -> HttpResult:
    method, path, media, body = _wire(kind, command)
    headers = [
        (b"authorization", f"Bearer {token or scope.token.value}".encode()),
        (b"idempotency-key", key.encode()),
    ]
    if media:
        headers.append((b"content-type", media.encode()))
    if isinstance(
        command,
        (
            AppendArchiveRevisionCommand,
            CorrectArchiveOccurrenceCommand,
            PageLifecycleCommand,
            FileSetAppendCommand,
        ),
    ):
        assert command.expected_etag is not None
        headers.append((b"if-match", command.expected_etag.encode()))
    if oversized:
        if isinstance(command, CorrectArchiveOccurrenceCommand):
            body = b"x" * 1_025
        else:
            headers.append((b"content-length", str(1 << 30).encode()))
    request_scope: Scope = dict(
        type="http",
        asgi={"version": "3.0", "spec_version": "2.3"},
        http_version="1.1",
        method=method,
        scheme="http",
        path=path,
        raw_path=path.encode(),
        query_string=b"",
        root_path="",
        headers=headers,
        server=("synthetic", 80),
        client=("synthetic", 1),
    )
    received = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        received += 1
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(request_scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    return HttpResult(
        start["status"],
        b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        ),
        {name.decode().lower(): value.decode() for name, value in start["headers"]},
        received,
    )


def _grant(engine: Engine, scope: ArchiveScope, section_id: str) -> None:
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=scope.library_id,
                caller_id=scope.caller_id,
                section_id=section_id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME,
            )
        )


def _remove(engine: Engine, scope: ArchiveScope, section_id: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            delete(SectionGrant).where(
                SectionGrant.library_id == scope.library_id,
                SectionGrant.caller_id == scope.caller_id,
                SectionGrant.section_id == section_id,
            )
        )


@pytest.mark.parametrize("kind", KINDS)
def test_http_replay_uses_current_permission_before_receiving_body(
    content_engine: Engine, archive_scope: ArchiveScope, kind: str
) -> None:
    page, command, original = _prepare(content_engine, archive_scope, kind)
    app = _app(content_engine)
    before = _call(app, archive_scope, kind, command)
    assert before.status == original.response_status and before.body == original.response_body
    _move(content_engine, page, TARGET, "move-out", _destination(content_engine, archive_scope))
    counts = _counts(content_engine)
    altered = (
        command.model_copy(update={"expected_etag": '"page-v2-' + "0" * 64 + '"'})
        if isinstance(command, PageLifecycleCommand)
        else command.model_copy(update={"occurred_at": command.occurred_at + 1})
        if isinstance(command, CorrectArchiveOccurrenceCommand)
        else command.model_copy(
            update={"source": command.source.model_copy(update={"kind": "changed"})}
        )
    )
    for request in (command, altered):
        denied = _call(app, archive_scope, kind, request)
        assert denied.status == 404 and denied.received == 0
        assert json.loads(denied.body)["code"] == "resource_not_found"
    _grant(content_engine, archive_scope, TARGET[0])
    _remove(content_engine, archive_scope, archive_scope.section_id)
    replay = _call(app, archive_scope, kind, command)
    assert replay.status == before.status and replay.body == before.body
    assert replay.headers["etag"] == before.headers["etag"]
    assert replay.headers.get("location") == before.headers.get("location")
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.received > 0
    conflict = _call(app, archive_scope, kind, altered)
    assert conflict.status == 409 and conflict.received > 0
    assert json.loads(conflict.body)["code"] == "idempotency_mismatch"
    fresh = _call(app, archive_scope, kind, command, key="fresh-old-path")
    assert fresh.status == 404 and fresh.received == 0
    assert _counts(content_engine) == counts


@pytest.mark.parametrize("kind", ("create", "file-create"))
def test_http_cannot_use_another_callers_receipt_to_admit_body(
    content_engine: Engine, archive_scope: ArchiveScope, kind: str
) -> None:
    page, command, _ = _prepare(content_engine, archive_scope, kind)
    _move(content_engine, page, TARGET, "move-out", _destination(content_engine, archive_scope))
    token = generate_token()
    with immediate_transaction(content_engine) as connection:
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id="a" * 32,
                library_id=archive_scope.library_id,
                kind=CallerKind.AGENT,
                name="Synthetic other Caller",
                created_at=OPERATION_TIME - 1,
                updated_at=OPERATION_TIME - 1,
            )
        )
        repository.add_credential(
            NewCredential(
                id="b" * 32,
                library_id=archive_scope.library_id,
                caller_id="a" * 32,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                created_at=OPERATION_TIME - 1,
                updated_at=OPERATION_TIME - 1,
                expires_at=OPERATION_TIME + 100_000,
            )
        )
        repository.add_grant(
            NewSectionGrant(
                library_id=archive_scope.library_id,
                caller_id="a" * 32,
                section_id=TARGET[0],
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME,
            )
        )
    counts = _counts(content_engine)
    denied = _call(_app(content_engine), archive_scope, kind, command, token=token.value)
    assert denied.status == 404 and denied.received == 0
    assert _counts(content_engine) == counts


@pytest.mark.parametrize("kind", ("create", "revise", "file-create", "file-changed", "correction"))
def test_http_authorized_old_receipt_does_not_bypass_body_budget(
    content_engine: Engine, archive_scope: ArchiveScope, kind: str
) -> None:
    page, command, _ = _prepare(content_engine, archive_scope, kind)
    _move(content_engine, page, TARGET, "move-out", _destination(content_engine, archive_scope))
    _grant(content_engine, archive_scope, TARGET[0])
    _remove(content_engine, archive_scope, archive_scope.section_id)
    counts = _counts(content_engine)
    rejected = _call(_app(content_engine), archive_scope, kind, command, oversized=True)
    assert rejected.status == 413
    assert _counts(content_engine) == counts
