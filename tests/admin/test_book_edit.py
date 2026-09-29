from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from starlette.concurrency import run_in_threadpool as starlette_run_in_threadpool

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AdminStructureAuditEvent, Caller, MasterAuditEvent
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Book

_ORIGIN = "https://admin.example.invalid"
_MASTER_TOKEN = "synthetic master token material 0001"
_PASSWORD = "synthetic admin password"


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": f"sqlite:///{(tmp_path / 'admin.db').as_posix()}",
                "admin_password_hash": hash_password(
                    _PASSWORD, salt_factory=lambda size: b"s" * size, iterations=300_000
                ),
                "admin_session_signing_secret": "s" * 32,
            }
        )
    )
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _post(client: TestClient, path: str, data: dict[str, str], *, origin: str = _ORIGIN) -> Any:
    return client.post(path, data=data, headers={"Origin": origin})


def _csrf(client: TestClient, path: str = "/admin/libraries") -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _setup_master(client: TestClient, engine: Engine) -> str:
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    return _csrf(client)


def _book(client: TestClient, csrf: str, *, name: str = "First") -> tuple[str, int]:
    library = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Library"})
    assert library.status_code == 303
    section = _post(
        client,
        library.headers["location"] + "/sections",
        {"csrf_token": csrf, "name": "Section"},
    )
    assert section.status_code == 303
    book = _post(
        client,
        section.headers["location"] + "/books",
        {"csrf_token": csrf, "name": name, "summary": "Original"},
    )
    assert book.status_code == 303
    path = book.headers["location"]
    detail = client.get(path)
    assert detail.status_code == 200
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', detail.text)
    assert match is not None
    return path, int(match.group(1))


def _edit(csrf: str, expected: int, *, name: str, summary: str) -> dict[str, str]:
    return {
        "csrf_token": csrf,
        "expected_updated_at": str(expected),
        "name": name,
        "summary": summary,
    }


def _update_events(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(
                    MasterAuditEvent.action,
                    MasterAuditEvent.target_type,
                    MasterAuditEvent.target_id,
                    MasterAuditEvent.identity_id,
                    MasterAuditEvent.session_fingerprint,
                ).where(MasterAuditEvent.action == "book.update")
            ).all()
        )


def test_master_book_edit_updates_metadata_only_with_one_redacted_audit(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _book(client, csrf)
    book_id = path.rsplit("/", 1)[-1]
    with engine.connect() as connection:
        original = connection.execute(select(Book.__table__)).mappings().one()
    name = '<img src=x onerror="synthetic()">'
    summary = "<script>synthetic()</script>"
    result = _post(client, path, _edit(csrf, expected, name=name, summary=summary))
    assert result.status_code == 303
    assert result.headers["location"] == path
    detail = client.get(path)
    assert detail.status_code == 200
    assert name not in detail.text and summary not in detail.text
    assert "&lt;img src=x onerror=&quot;synthetic()&quot;&gt;" in detail.text
    assert "&lt;script&gt;synthetic()&lt;/script&gt;" in detail.text
    with engine.connect() as connection:
        updated = connection.execute(select(Book.__table__)).mappings().one()
        assert connection.scalar(select(func.count()).select_from(AdminStructureAuditEvent)) == 0
    assert updated["id"] == original["id"] == book_id
    assert updated["section_id"] == original["section_id"]
    assert updated["library_id"] == original["library_id"]
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_at"] > expected
    assert (updated["name"], updated["summary"]) == (name, summary)
    events = _update_events(engine)
    assert len(events) == 1
    assert events[0].target_type == "book" and events[0].target_id == book_id
    assert events[0].identity_id == "a" * 32
    assert len(events[0].session_fingerprint) == 32
    assert name not in repr(events) and summary not in repr(events)


def test_book_edit_noop_stale_conflict_duplicate_and_wrong_scope(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _book(client, csrf)
    unchanged = _post(client, path, _edit(csrf, expected, name="First", summary="Original"))
    assert unchanged.status_code == 303
    assert _update_events(engine) == []
    changed = _post(client, path, _edit(csrf, expected, name="Renamed", summary="New"))
    assert changed.status_code == 303
    stale = _post(client, path, _edit(csrf, expected, name="Renamed", summary="New"))
    assert stale.status_code == 409
    assert len(_update_events(engine)) == 1

    section_path = path.rsplit("/books/", 1)[0]
    second = _post(
        client,
        section_path + "/books",
        {"csrf_token": csrf, "name": "Second", "summary": "Second"},
    )
    assert second.status_code == 303
    fresh = client.get(path)
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', fresh.text)
    assert match is not None
    current = int(match.group(1))
    duplicate = _post(client, path, _edit(csrf, current, name="Second", summary="New"))
    assert duplicate.status_code == 409
    library_id = path.split("/libraries/", 1)[1].split("/", 1)[0]
    section_id = path.split("/sections/", 1)[1].split("/", 1)[0]
    wrong_section_path = path.replace(f"/sections/{section_id}/", f"/sections/{'f' * 32}/")
    assert (
        _post(
            client,
            wrong_section_path,
            _edit(csrf, current, name="Wrong scope", summary="New"),
        ).status_code
        == 404
    )
    wrong_library_path = path.replace(f"/libraries/{library_id}/", f"/libraries/{'f' * 32}/")
    assert (
        _post(
            client, wrong_library_path, _edit(csrf, current, name="Wrong scope", summary="New")
        ).status_code
        == 404
    )
    with engine.connect() as connection:
        name = connection.scalar(select(Book.name).where(Book.id == path.rsplit("/", 1)[-1]))
        assert name == "Renamed"
    assert len(_update_events(engine)) == 1


def test_book_edit_requires_master_origin_csrf_and_current_generation(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _book(client, csrf)
    fields = _edit(csrf, expected, name="Blocked", summary="Blocked")
    assert _post(client, path, fields, origin="https://other.example.invalid").status_code == 403
    assert _post(client, path, {**fields, "csrf_token": "bad"}).status_code == 403
    assert _post(client, path, {**fields, "unknown": "x"}).status_code == 422
    assert _post(client, path, {**fields, "name": " "}).status_code == 422
    assert _post(client, path, {**fields, "summary": "x" * 4_001}).status_code == 422
    assert _update_events(engine) == []

    async def rotate_before_action(action: Any, *args: Any, **kwargs: Any) -> Any:
        with immediate_transaction(engine) as connection:
            assert (
                MasterTokenRepository(connection).rotate(
                    _MASTER_TOKEN, "synthetic rotated master token material 0002", now=1_001
                )
                is not None
            )
        return await starlette_run_in_threadpool(action, *args, **kwargs)

    monkeypatch.setattr(admin_router, "run_in_threadpool", rotate_before_action)
    assert _post(client, path, fields).status_code == 401
    with engine.connect() as connection:
        assert connection.scalar(select(Book.name)) == "First"
    assert _update_events(engine) == []


def test_book_edit_audit_failure_rolls_back_and_legacy_session_cannot_edit(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    assert _post(client, "/admin/login", {"password": _PASSWORD}).status_code == 303
    legacy_csrf = _csrf(client)
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    # After setup, the old cookie cannot read or write through the browser.
    assert client.get("/admin/libraries").status_code == 303
    assert (
        _post(
            client,
            "/admin/libraries/" + "f" * 32 + "/sections/" + "e" * 32 + "/books/" + "d" * 32,
            _edit(legacy_csrf, 1, name="No", summary="No"),
        ).status_code
        == 401
    )
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    csrf = _csrf(client)
    path, expected = _book(client, csrf)

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    result = _post(client, path, _edit(csrf, expected, name="Rollback", summary="Rollback"))
    assert result.status_code == 500
    with engine.connect() as connection:
        assert connection.scalar(select(Book.name)) == "First"
    assert _update_events(engine) == []


def test_legacy_session_cannot_edit_book_before_master_setup(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    assert _post(client, "/admin/login", {"password": _PASSWORD}).status_code == 303
    csrf = _csrf(client)
    library = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Library"})
    section = _post(
        client,
        library.headers["location"] + "/sections",
        {"csrf_token": csrf, "name": "Section"},
    )
    book = _post(
        client,
        section.headers["location"] + "/books",
        {"csrf_token": csrf, "name": "First", "summary": "Original"},
    )
    path = book.headers["location"]
    assert 'name="expected_updated_at"' not in client.get(path).text
    with engine.connect() as connection:
        expected = connection.scalar(select(Book.updated_at))
    assert expected is not None
    denied = _post(client, path, _edit(csrf, expected, name="Denied", summary="Denied"))
    assert denied.status_code == 403
    with engine.connect() as connection:
        assert connection.scalar(select(Book.name)) == "First"
    assert _update_events(engine) == []


def test_book_edit_accepts_long_unicode_summary_without_raising_other_form_limits(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _book(client, csrf)
    chinese_summary = "中" * 2_000
    chinese_fields = _edit(csrf, expected, name="First", summary=chinese_summary)
    assert len(urlencode(chinese_fields).encode("ascii")) > 16_384
    assert _post(client, path, chinese_fields).status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(Book.summary)) == chinese_summary
        updated_at = connection.scalar(select(Book.updated_at))
    assert updated_at is not None

    # A four-byte character is the worst-case Unicode URL encoding (12 bytes).
    largest_summary = "𠮷" * 4_000
    largest_fields = _edit(csrf, updated_at, name="First", summary=largest_summary)
    assert len(urlencode(largest_fields).encode("ascii")) < 65_536
    assert _post(client, path, largest_fields).status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(Book.summary)) == largest_summary
        updated_at = connection.scalar(select(Book.updated_at))
    assert updated_at is not None

    oversized_fields = _edit(csrf, updated_at, name="First", summary="𠮷" * 5_500)
    assert len(urlencode(oversized_fields).encode("ascii")) > 65_536
    assert _post(client, path, oversized_fields).status_code == 413
    with engine.connect() as connection:
        assert connection.scalar(select(Book.summary)) == largest_summary
    assert len(_update_events(engine)) == 2

    # Every other form still uses the original 16 KiB bound.
    other_route = _post(
        client,
        "/admin/libraries",
        {"csrf_token": csrf, "name": "Other", "description": chinese_summary},
    )
    assert other_route.status_code == 413
