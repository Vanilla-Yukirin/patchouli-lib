from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AdminStructureAuditEvent, Caller
from patchouli_lib.config import Settings
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.library.schemas import CreateLibraryInput

_ORIGIN = "https://admin.example.invalid"
_PASSWORD = "synthetic admin password"
_HASH = hash_password(_PASSWORD, salt_factory=lambda size: b"x" * size, iterations=300_000)


@pytest.fixture
def admin_browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": f"sqlite:///{(tmp_path / 'admin.db').as_posix()}",
                "admin_password_hash": _HASH,
                "admin_session_signing_secret": "s" * 32,
            }
        )
    )
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _login(client: TestClient) -> str:
    assert (
        client.post(
            "/admin/login", data={"password": _PASSWORD}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )
    response = client.get("/admin/libraries")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _post(client: TestClient, path: str, csrf: str, **fields: str) -> Any:
    return client.post(
        path,
        data={"csrf_token": csrf, **fields},
        headers={"Origin": _ORIGIN},
    )


def test_create_hierarchy_from_web_with_distinct_session_audit(
    admin_browser: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_browser
    assert (
        client.post(
            "/admin/libraries", data={"name": "No session"}, headers={"Origin": _ORIGIN}
        ).status_code
        == 401
    )
    csrf = _login(client)
    description = '<script>alert("synthetic")</script>'
    library = _post(
        client,
        "/admin/libraries",
        csrf,
        name="Synthetic Library",
        description=description,
    )
    assert library.status_code == 303
    library_path = library.headers["location"]
    detail = client.get(library_path).text
    directory = client.get("/admin/libraries").text
    assert "Synthetic Library" in detail
    assert "&lt;script&gt;" in detail and "&lt;script&gt;" in directory
    assert description not in detail and description not in directory

    section = _post(
        client, library_path + "/sections", csrf, name="Synthetic Section", description="Brief"
    )
    assert section.status_code == 303
    section_path = section.headers["location"]
    assert "Synthetic Section" in client.get(section_path).text

    book = _post(client, section_path + "/books", csrf, name="Synthetic Book", summary="Summary")
    assert book.status_code == 303
    assert "Synthetic Book" in client.get(book.headers["location"]).text

    with engine.connect() as connection:
        events = connection.execute(
            select(
                AdminStructureAuditEvent.action,
                AdminStructureAuditEvent.session_fingerprint,
            ).order_by(AdminStructureAuditEvent.occurred_at)
        ).all()
        assert {event.action for event in events} == {
            "library.create",
            "section.create",
            "book.create",
        }
        assert len(events) == 3
        assert len({event.session_fingerprint for event in events}) == 1
        assert all(len(event.session_fingerprint) == 32 for event in events)
        assert all(event.session_fingerprint != csrf.encode() for event in events)
        assert connection.scalar(select(func.count()).select_from(Caller)) == 0
        assert connection.scalar(select(func.count()).select_from(Library)) == 1
        assert connection.scalar(select(Library.description)) == description
        assert connection.scalar(select(func.count()).select_from(Section)) == 1
        assert connection.scalar(select(func.count()).select_from(Book)) == 1


def test_create_rejects_conflict_wrong_parent_origin_and_csrf(
    admin_browser: tuple[TestClient, Engine],
) -> None:
    client, engine = admin_browser
    csrf = _login(client)
    path = _post(client, "/admin/libraries", csrf, name="Synthetic Library").headers["location"]
    assert _post(client, "/admin/libraries", csrf, name="Synthetic Library").status_code == 409
    assert _post(client, "/admin/libraries", "bad", name="Other").status_code == 403
    assert (
        client.post(
            "/admin/libraries",
            data={"csrf_token": csrf, "name": "Other"},
            headers={"Origin": "https://other.example.invalid"},
        ).status_code
        == 403
    )
    assert _post(client, "/admin/libraries", csrf, name=" ").status_code == 422
    assert (
        _post(
            client, "/admin/libraries", csrf, name="Too long", description="x" * 4_001
        ).status_code
        == 422
    )
    missing = "f" * 32
    assert (
        _post(client, f"/admin/libraries/{missing}/sections", csrf, name="Orphan").status_code
        == 404
    )
    assert _post(client, f"{path}/sections", csrf, name="Synthetic Section").status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Library)) == 1
        assert connection.scalar(select(func.count()).select_from(Section)) == 1
        assert connection.scalar(select(func.count()).select_from(AdminStructureAuditEvent)) == 2


def test_structure_and_audit_insert_are_atomic(admin_browser: tuple[TestClient, Engine]) -> None:
    _, engine = admin_browser
    service = AdminActionService(engine, request_id_factory=lambda: "x")
    with pytest.raises(IntegrityError):
        service.create_library(
            CreateLibraryInput(name="Would roll back"), session_fingerprint=b"s" * 32
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Library)) == 0
        assert connection.scalar(select(func.count()).select_from(AdminStructureAuditEvent)) == 0
