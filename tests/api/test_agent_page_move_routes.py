"""Synthetic HTTP movement: bounded bodies and transaction-visible grants."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from content.conftest import OPERATION_TIME
from content.conftest import content_engine as content_engine
from content.page_move_helpers import _target
from content.test_agent_page_move import _agent, _delete, _move_command
from content.test_master_revision_restore_service import _page, _story
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete

from patchouli_lib.api import page_move_routes
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import create_file_set_read_router
from patchouli_lib.api.page_move_routes import create_page_move_router
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.auth.models import CredentialLibraryGrant
from patchouli_lib.content.page_move_schemas import PageMoveInput
from patchouli_lib.database import immediate_transaction


def _app(engine: Engine) -> FastAPI:
    app = FastAPI()
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(create_page_move_router(engine, clock=lambda: OPERATION_TIME + 30))
    app.include_router(create_file_set_read_router(engine, clock=lambda: OPERATION_TIME + 30))
    return app


def test_http_move_and_deleted_replay_preserve_original_response(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    command = _move_command(page, _target(content_engine, page))
    path = f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}/move"
    body = command.model_dump(
        include={"source_section_id", "source_book_id", "target_section_id", "target_book_id"}
    )
    headers = {
        "Authorization": f"Bearer {agent.token}",
        "Idempotency-Key": "synthetic-http-move",
        "If-Match": command.expected_etag,
    }
    with TestClient(_app(content_engine)) as client:
        moved = client.post(path, json=body, headers=headers)
        assert moved.status_code == 200
        assert moved.json()["changed"] is True
        assert set(moved.json()) == {
            "source_section_id",
            "source_book_id",
            "target_section_id",
            "target_book_id",
            "changed",
            "library_id",
            "page_id",
            "revision_id",
            "revision_number",
            "occurred_at",
            "original_updated_at",
            "updated_at",
            "request_etag",
        }
        assert "title" not in moved.json() and "files" not in moved.json()
        assert moved.headers["Cache-Control"] == "private, no-store"
        # Only WRITE was granted: the old file read contract hides unreadable
        # Library resources with 404, rather than exposing a manifest or ETag.
        current_href = (
            f"/api/v1/libraries/{page.library_id}/sections/{command.target_section_id}"
            f"/pages/{page.page_id}"
        )
        assert (
            client.get(current_href, headers={"Authorization": f"Bearer {agent.token}"}).status_code
            == 404
        )
        _delete(content_engine, story)
        replay = client.post(path, json=body, headers=headers)
        assert replay.status_code == 200 and replay.content == moved.content
        assert replay.headers["ETag"] == moved.headers["ETag"]
        assert replay.headers["Idempotency-Replayed"] == "true"
        fresh = client.post(
            path, json=body, headers={**headers, "Idempotency-Key": "new-after-delete"}
        )
        assert fresh.status_code == 404


@pytest.mark.parametrize("mode", ["legacy", "read-only", "invalid"])
def test_denied_request_does_not_receive_body(
    content_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(
        content_engine, page.library_id, opted_in=mode != "legacy", write=mode != "read-only"
    )
    request = _move_command(page, _target(content_engine, page))

    async def forbidden_stream(self: Request) -> AsyncIterator[bytes]:
        raise AssertionError("Unauthorized body was read")
        yield b""

    monkeypatch.setattr(Request, "stream", forbidden_stream)
    with TestClient(_app(content_engine)) as client:
        response = client.post(
            f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}/move",
            content=b"x" * 10_000,
            headers={
                "Authorization": f"Bearer {agent.token if mode != 'invalid' else 'invalid'}",
                "Idempotency-Key": "deny-before-body",
                "If-Match": request.expected_etag,
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == (401 if mode == "invalid" else 403)
    assert _page(content_engine, page.library_id, page.page_id) == page


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing-etag", 428),
        ("weak-etag", 422),
        ("missing-key", 422),
        ("oversized", 413),
        ("duplicate-json", 422),
        ("foreign-target", 404),
        ("wrong-source", 404),
        ("stale-etag", 412),
        ("unsupported-type", 415),
    ],
)
def test_http_bounded_validation_and_conditions(
    content_engine: Engine, case: str, status: int
) -> None:
    import json

    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    command = _move_command(page, _target(content_engine, page))
    body = command.model_dump(
        include={"source_section_id", "source_book_id", "target_section_id", "target_book_id"}
    )
    headers = {
        "Authorization": f"Bearer {agent.token}",
        "Idempotency-Key": "validate-http",
        "If-Match": command.expected_etag,
        "Content-Type": "application/json",
    }
    if case == "missing-etag":
        del headers["If-Match"]
    if case == "missing-key":
        del headers["Idempotency-Key"]
    if case == "weak-etag":
        headers["If-Match"] = "W/" + command.expected_etag
    if case == "stale-etag":
        headers["If-Match"] = '"page-v2-' + "0" * 64 + '"'
    if case == "unsupported-type":
        headers["Content-Type"] = "text/plain"
    if case == "foreign-target":
        body.update(target_section_id="8" * 32, target_book_id="9" * 32)
    if case == "wrong-source":
        body.update(source_book_id="f" * 32)
    content = json.dumps(body).encode()
    if case == "oversized":
        content = b" " * 1_025
    if case == "duplicate-json":
        content = content[:-1] + b',"source_book_id":"' + page.book_id.encode() + b'"}'
    with TestClient(_app(content_engine)) as client:
        response = client.post(
            f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}/move",
            content=content,
            headers=headers,
        )
    assert response.status_code == status
    assert _page(content_engine, page.library_id, page.page_id) == page


def test_revoke_between_preflight_and_write_lock_cannot_move(
    content_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    command = _move_command(page, _target(content_engine, page))
    original_body = page_move_routes._body

    async def revoke(request: Request) -> PageMoveInput:
        body = await original_body(request)
        with immediate_transaction(content_engine) as connection:
            connection.execute(
                delete(CredentialLibraryGrant).where(
                    CredentialLibraryGrant.credential_id == agent.credential_id
                )
            )
        return body

    monkeypatch.setattr(page_move_routes, "_body", revoke)
    with TestClient(_app(content_engine)) as client:
        response = client.post(
            f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}/move",
            json=command.model_dump(
                include={
                    "source_section_id",
                    "source_book_id",
                    "target_section_id",
                    "target_book_id",
                }
            ),
            headers={
                "Authorization": f"Bearer {agent.token}",
                "Idempotency-Key": "revoke-gap",
                "If-Match": command.expected_etag,
            },
        )
    assert response.status_code == 403
    assert _page(content_engine, page.library_id, page.page_id) == page
