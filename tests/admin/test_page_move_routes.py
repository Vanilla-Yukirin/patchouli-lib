"""Master movement forms, frozen replay and native same-tab recovery."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, select
from test_file_set_browser import file_set_browser as file_set_browser
from test_file_set_routes import (
    _ORIGIN,
    _TOKEN,
    Browser,
    _counts,
    _created,
    _current_etag,
    _master_session,
)
from test_file_set_routes import browser as browser
from test_search_browser import _LEGACY
from test_search_browser import browser as _uninitialized_browser_fixture

from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.move_receipts import MasterMoveReceiptRow
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.content.page_move_models import PageMoveEvent, PageMoveGuard
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewBook, NewSection

uninitialized_browser = _uninitialized_browser_fixture


def _target(engine: Engine, library_id: str) -> tuple[str, str]:
    with immediate_transaction(engine) as connection:
        repository = LibraryRepository(connection)
        repository.add_section(
            NewSection(
                id="5" * 32,
                library_id=library_id,
                name="Destination <script>synthetic</script>",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_book(
            NewBook(
                id="6" * 32,
                library_id=library_id,
                section_id="5" * 32,
                name="Destination Book",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return "5" * 32, "6" * 32


def _page(engine: Engine, library_id: str, page_id: str) -> PageRecord:
    with engine.connect() as connection:
        page = ContentRepository(connection).get_page(library_id, page_id)
        assert page is not None
        return page


def _move_counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (PageMoveEvent, PageMoveGuard, MasterMoveReceiptRow)
        )


def _move(
    browser: Browser,
    created: dict[str, Any],
    target: tuple[str, str],
    *,
    key: str = "c" * 32,
    csrf: str | None = None,
    body: object | None = None,
    origin: str = _ORIGIN,
    path: str | None = None,
) -> Response:
    return browser.client.post(
        browser.page_path(created["page_id"]) + "/move" if path is None else path,
        json=(
            {
                "target_section_id": target[0],
                "target_book_id": target[1],
                "confirm_move": "yes",
            }
            if body is None
            else body
        ),
        headers={
            "Origin": origin,
            "X-CSRF-Token": browser.csrf if csrf is None else csrf,
            "Idempotency-Key": key,
            "If-Match": created["etag"],
        },
    )


def test_move_form_and_original_success_survive_movement_deletion_and_relogin(
    browser: Browser,
) -> None:
    created = _created(browser)
    target = _target(browser.engine, browser.library_id)
    path = browser.page_path(created["page_id"]) + "/move"
    preview = browser.client.get(browser.page_path(created["page_id"]) + "?lang=zh-CN")
    assert preview.status_code == 200 and f'href="{path}">移动文档</a>' in preview.text
    form = browser.client.get(path + "?lang=zh-CN")
    assert form.status_code == 200
    assert 'name="confirm_move" value="yes" required' in form.text
    assert 'id="move-submit" type="submit" disabled' in form.text
    assert 'name="target_section_id" required' in form.text
    assert "&lt;script&gt;synthetic&lt;/script&gt;" in form.text
    assert "<script>synthetic</script>" not in form.text
    assert "不新增内容版本" in form.text and "<noscript>" in form.text
    assert "script-src 'self'" in form.headers["content-security-policy"]
    assert "connect-src 'self'" in form.headers["content-security-policy"]
    before = _page(browser.engine, browser.library_id, created["page_id"])
    moved = _move(browser, created, target)
    assert moved.status_code == 200
    result = moved.json()
    stable = f"/admin/libraries/{browser.library_id}/pages/{created['page_id']}"
    assert result["page_url"] == stable
    assert result["revision_url"] == stable + "/revisions/1"
    assert result["changed"] and not result["replayed"]
    after = _page(browser.engine, browser.library_id, created["page_id"])
    assert after.model_dump(exclude={"section_id", "book_id", "updated_at"}) == before.model_dump(
        exclude={"section_id", "book_id", "updated_at"}
    )
    assert (after.section_id, after.book_id) == target
    shell = browser.client.get(path)
    assert shell.status_code == 404 and 'data-available="no"' in shell.text
    assert 'id="page-move"' in shell.text and "Synthetic Page" not in shell.text
    assert _move(browser, created, target).json() == {**result, "replayed": True}
    AdminActionService(browser.engine).delete_page_as_master(
        browser.library_id,
        *target,
        created["page_id"],
        MasterDeletePageFormInput(
            expected_etag=_current_etag(browser.engine, browser.library_id, created["page_id"]),
            confirm_delete="yes",
        ),
        master_session=_master_session(browser),
    )
    counts = _counts(browser.engine)
    assert _move(browser, created, target).json() == {**result, "replayed": True}
    assert _page(browser.engine, browser.library_id, created["page_id"]).deleted_at is not None
    with immediate_transaction(browser.engine) as connection:
        assert MasterTokenRepository(connection).rotate(_TOKEN, _TOKEN + " rotated", now=2_000_000)
    assert _move(browser, created, target).status_code == 401
    assert (
        browser.client.post(
            "/admin/login", data={"password": _TOKEN + " rotated"}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', browser.client.get(path).text)
    assert csrf is not None
    assert _move(browser, created, target, csrf=csrf.group(1)).json() == {
        **result,
        "replayed": True,
    }
    assert _counts(browser.engine) == counts and _move_counts(browser.engine) == (1, 0, 1)


def test_move_noop_receipt_remains_noop_after_another_operation(browser: Browser) -> None:
    created = _created(browser)
    target = _target(browser.engine, browser.library_id)
    original = _move(browser, created, (browser.section_id, browser.book_id))
    assert original.status_code == 200 and original.json()["changed"] is False
    assert _move_counts(browser.engine) == (0, 0, 1)
    assert _move(browser, created, target, key="d" * 32).status_code == 200
    counts = _counts(browser.engine)
    replay = _move(browser, created, (browser.section_id, browser.book_id))
    assert replay.json() == {**original.json(), "replayed": True}
    current = _page(browser.engine, browser.library_id, created["page_id"])
    assert (current.section_id, current.book_id) == target
    stale_path = (
        f"/admin/libraries/{browser.library_id}/sections/{target[0]}/books/{target[1]}"
        f"/pages/{created['page_id']}/move"
    )
    assert (
        _move(
            browser, created, (browser.section_id, browser.book_id), key="e" * 32, path=stale_path
        ).status_code
        == 412
    )
    assert _counts(browser.engine) == counts and _move_counts(browser.engine) == (1, 0, 2)


def test_move_safety_and_audit_failures_are_not_successes(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _created(browser)
    target = _target(browser.engine, browser.library_id)
    assert _move(browser, created, target, csrf="wrong").status_code == 403
    assert (
        _move(browser, created, target, origin="https://other.example.invalid").status_code == 403
    )
    for body in ({}, {"confirm_move": "no"}, {"confirm_move": "yes", "unknown": True}):
        assert _move(browser, created, target, body=body).status_code == 422
    assert _move(browser, created, target, body={"padding": "x" * 600}).status_code == 413
    assert _move(browser, created, ("f" * 32, "f" * 32)).status_code == 404
    counts = _counts(browser.engine)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic private failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", fail)
    failed = _move(browser, created, target)
    assert failed.status_code == 500 and set(failed.json()) == {"message"}
    assert "Synthetic private" not in failed.text
    assert _counts(browser.engine) == counts and _move_counts(browser.engine) == (0, 0, 0)


def test_move_anonymous_and_legacy_sessions_are_rejected(
    uninitialized_browser: tuple[TestClient, Engine],
) -> None:
    client, _engine = uninitialized_browser
    path = "/admin/libraries/invalid/sections/invalid/books/invalid/pages/invalid/move"
    assert client.get(path).status_code == 401
    assert client.post(path, json={}, headers={"Origin": _ORIGIN}).status_code == 401
    assert (
        client.post(
            "/admin/login", data={"password": _LEGACY}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )
    assert client.get(path).status_code == 403
    assert client.post(path, json={}, headers={"Origin": _ORIGIN}).status_code == 403
