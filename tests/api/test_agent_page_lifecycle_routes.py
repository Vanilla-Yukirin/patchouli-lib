"""Synthetic HTTP lifecycle contract; no inherited Section permission expansion."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from content.conftest import OPERATION_TIME
from content.conftest import content_engine as content_engine
from content.test_agent_page_move import _agent
from content.test_master_revision_restore_service import _etag, _page, _story
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete

from patchouli_lib.api import page_lifecycle_routes
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import create_file_set_read_router
from patchouli_lib.api.page_lifecycle_routes import create_page_lifecycle_router
from patchouli_lib.api.page_move_routes import _preflight
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.auth.models import CredentialLibraryGrant
from patchouli_lib.database import immediate_transaction


def _app(engine: Engine) -> FastAPI:
    app = FastAPI()
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(create_page_lifecycle_router(engine, clock=lambda: OPERATION_TIME + 40))
    app.include_router(create_file_set_read_router(engine, clock=lambda: OPERATION_TIME + 40))
    return app


@pytest.mark.parametrize(
    "case,status",
    [
        ("success", 200),
        ("legacy", 403),
        ("read-only", 403),
        ("invalid", 401),
        ("missing-etag", 428),
        ("body", 422),
        ("query", 422),
        ("stale", 412),
    ],
)
def test_lifecycle_http_boundaries(content_engine: Engine, case: str, status: int) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(
        content_engine, page.library_id, opted_in=case != "legacy", write=case != "read-only"
    )
    headers = {
        "Authorization": f"Bearer {agent.token}",
        "If-Match": _etag(page),
        "Idempotency-Key": "delete",
    }
    if case == "invalid":
        headers["Authorization"] = "Bearer invalid"
    if case == "missing-etag":
        del headers["If-Match"]
    if case == "stale":
        headers["If-Match"] = '"page-v2-' + "0" * 64 + '"'
    path = f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}"
    with TestClient(_app(content_engine)) as client:
        response = client.request(
            "DELETE",
            path + ("?x=1" if case == "query" else ""),
            headers=headers,
            content=b"{}" if case == "body" else b"",
        )
        assert response.status_code == status
        if case == "success":
            assert response.headers["Cache-Control"] == "private, no-store"
            assert "title" not in response.json() and "files" not in response.json()
            restored = client.post(
                path + "/restore",
                headers={
                    **headers,
                    "If-Match": response.headers["ETag"],
                    "Idempotency-Key": "restore",
                },
            )
            assert restored.status_code == 200 and restored.json()["state"] == "active"
            assert (
                client.get(
                    f"/api/v1/libraries/{page.library_id}/sections/{page.section_id}/pages/{page.page_id}",
                    headers={"Authorization": f"Bearer {agent.token}"},
                ).status_code
                == 404
            )
            replay = client.request("DELETE", path, headers=headers)
            assert (
                replay.content == response.content
                and replay.headers["Idempotency-Replayed"] == "true"
            )


@pytest.mark.parametrize("legacy", [True, False])
def test_authorization_precedes_body_read(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id, opted_in=not legacy, write=False)

    async def forbidden_stream(self: Request) -> AsyncIterator[bytes]:
        raise AssertionError("Unauthorized body was read")
        yield b""

    monkeypatch.setattr(Request, "stream", forbidden_stream)
    with TestClient(_app(content_engine)) as client:
        response = client.request(
            "DELETE",
            f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}",
            headers={
                "Authorization": f"Bearer {agent.token}",
                "If-Match": _etag(page),
                "Idempotency-Key": "denied",
            },
            content=b"x",
        )
    assert response.status_code == 403
    assert _page(content_engine, page.library_id, page.page_id) == page


def test_revoke_after_preflight_prevents_delete(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    original = _preflight

    def revoke(*args: object) -> None:
        original(content_engine, agent.token, page.library_id, lambda: OPERATION_TIME + 40)
        with immediate_transaction(content_engine) as connection:
            connection.execute(delete(CredentialLibraryGrant))

    monkeypatch.setattr(page_lifecycle_routes, "_preflight", revoke)
    with TestClient(_app(content_engine)) as client:
        response = client.request(
            "DELETE",
            f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}",
            headers={
                "Authorization": f"Bearer {agent.token}",
                "If-Match": _etag(page),
                "Idempotency-Key": "revoked",
            },
        )
    assert response.status_code == 403
    assert _page(content_engine, page.library_id, page.page_id) == page
