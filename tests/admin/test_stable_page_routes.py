"""Stable master navigation admits and projects Page versions in one read snapshot."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine
from test_file_set_routes import (
    _FILES_MIXED,
    _ORIGIN,
    _TIME,
    _TOKEN,
    Browser,
    _counts,
    _created,
    _current_etag,
    _master_session,
    _post,
)
from test_file_set_routes import browser as browser
from test_search_browser import _LEGACY
from test_search_browser import browser as _uninitialized_browser_fixture

from patchouli_lib.admin.contracts import MasterDeletePageFormInput, MasterUpdatePageTitleInput
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.read_model import AdminReadModel, PageView
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.database import immediate_transaction

uninitialized_browser = _uninitialized_browser_fixture


def _stable(browser: Browser, page_id: str) -> str:
    return f"/admin/libraries/{browser.library_id}/pages/{page_id}"


def test_stable_current_and_exact_history_keep_deep_writes_and_receipt_urls(
    browser: Browser,
) -> None:
    first = _created(browser)
    deep = browser.page_path(first["page_id"])
    stable = _stable(browser, first["page_id"])
    second = _post(
        browser,
        deep + "/file-revisions",
        {},
        (("content.md", b"# Later stable version\n"),),
        key="b" * 32,
        etag=first["etag"],
    )
    assert second.status_code == 200
    assert second.json()["revision_url"] == deep + "/revisions/2"
    before = _counts(browser.engine)
    current = browser.client.get(stable)
    assert current.status_code == 200 and "location" not in current.headers
    assert "# Later stable version" in current.text
    assert f'href="{stable}/revisions/1"' in current.text
    assert f'action="{deep}"' in current.text
    assert f'action="{deep}/occurrence"' in current.text
    assert f'action="{deep}/delete"' in current.text
    assert f'href="{deep}/files/edit"' in current.text
    assert f'href="{deep}/revisions/2/restore"' in current.text
    assert f'href="{deep}/revisions/2/files/content.md"' in current.text
    historical = browser.client.get(stable + "/revisions/1?before_revision_number=2")
    assert historical.status_code == 200 and "location" not in historical.headers
    assert "# Synthetic content" in historical.text
    assert "# Later stable version" not in historical.text
    assert f'href="{stable}"' in historical.text
    assert f'href="{stable}/revisions/1?before_revision_number=2"' in historical.text
    assert f'href="{stable}/revisions/1?before_revision_number=3"' in historical.text
    assert f'href="{deep}/revisions/1/files/content.md"' in historical.text
    assert historical.headers["cache-control"] == "no-store, max-age=0"
    assert _counts(browser.engine) == before
    old_route = browser.client.get(deep)
    assert old_route.status_code == 200
    assert f'href="{deep}/revisions/1"' in old_route.text
    replay = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        _FILES_MIXED,
    )
    assert replay.status_code == 201
    assert replay.json() == {**first, "replayed": True}


def test_stable_invalid_versions_scope_and_deleted_pages_do_not_expose_content(
    browser: Browser,
) -> None:
    created = _created(browser)
    stable = _stable(browser, created["page_id"])
    paths = [
        f"/admin/libraries/{'f' * 32}/pages/{created['page_id']}",
        "/admin/libraries/invalid/pages/invalid",
        stable + "?before_revision_number=1",
        stable + "?before_revision_number=3",
        stable + "?before_revision_number=2&before_revision_number=2",
        *(stable + "/revisions/" + value for value in ("0", "01", "+1", "2", str(1 << 63))),
    ]
    for path in paths:
        response = browser.client.get(path)
        assert response.status_code == 404
        assert "# Synthetic content" not in response.text
        assert "Synthetic Library" not in response.text
    AdminActionService(browser.engine).delete_page_as_master(
        browser.library_id,
        browser.section_id,
        browser.book_id,
        created["page_id"],
        MasterDeletePageFormInput(
            expected_etag=_current_etag(browser.engine, browser.library_id, created["page_id"]),
            confirm_delete="yes",
        ),
        master_session=_master_session(browser),
    )
    for path in (stable, stable + "/revisions/1"):
        response = browser.client.get(path)
        assert response.status_code == 404
        assert "# Synthetic content" not in response.text and "payload.bin" not in response.text


def test_stable_navigation_rejects_anonymous_and_current_legacy_sessions(
    uninitialized_browser: tuple[TestClient, Engine],
) -> None:
    client, _engine = uninitialized_browser
    path = "/admin/libraries/invalid/pages/invalid/revisions/01"
    anonymous = client.get(path)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/admin/login"
    login = client.post("/admin/login", data={"password": _LEGACY}, headers={"Origin": _ORIGIN})
    assert login.status_code == 303
    assert client.get("/admin/libraries").status_code == 200
    assert client.get(path).status_code == 403
    assert client.get("/admin/libraries/invalid/pages/invalid").status_code == 403


def test_stable_read_admission_precedes_invalid_identity_in_a_real_begin(browser: Browser) -> None:
    admitted: list[bool] = []

    def reject(connection: Connection) -> bool:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        admitted.append(raw.in_transaction)
        return False

    with pytest.raises(AuthenticationError):
        AdminReadModel(browser.engine).get_page_by_id(
            "invalid", "invalid", revision_number=0, authorize=reject
        )
    assert admitted == [True]


def test_stable_read_keeps_one_snapshot_across_concurrent_title_change_and_rotation(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _created(browser)
    model = AdminReadModel(browser.engine)
    view = model.get_page(
        browser.library_id, browser.section_id, browser.book_id, created["page_id"]
    )
    assert view is not None
    session = _master_session(browser)
    with browser.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one() == "wal"
    original = AdminReadModel._get_page
    original_generation_check = MasterTokenRepository.is_session_generation_current
    admission_connections: list[Connection] = []
    projected: list[Connection] = []

    def record_generation_check(
        repository: MasterTokenRepository, identity_id: str, generation: int
    ) -> bool:
        admission_connections.append(repository._connection)
        return original_generation_check(repository, identity_id, generation)

    def concurrent_change(
        connection: Connection,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: int | None,
        before_revision_number: int | None,
    ) -> PageView | None:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection) and raw.in_transaction
        assert admission_connections == [connection]
        projected.append(connection)
        AdminActionService(browser.engine).update_page_title_as_master(
            library_id,
            section_id,
            book_id,
            page_id,
            MasterUpdatePageTitleInput(
                title="Concurrent new title", expected_updated_at=view.page.updated_at
            ),
            master_session=session,
        )
        with immediate_transaction(browser.engine) as writer:
            rotated = MasterTokenRepository(writer).rotate(
                _TOKEN, _TOKEN + " rotated", now=2_000_000
            )
            assert rotated is not None
        assert MasterTokenRepository(connection).is_session_generation_current(
            session.identity_id, session.session_generation
        )
        return original(
            connection,
            library_id,
            section_id,
            book_id,
            page_id,
            revision_number,
            before_revision_number,
        )

    stable = _stable(browser, created["page_id"])
    with monkeypatch.context() as context:
        context.setattr(
            MasterTokenRepository, "is_session_generation_current", record_generation_check
        )
        context.setattr(AdminReadModel, "_get_page", staticmethod(concurrent_change))
        response = browser.client.get(stable)
    assert response.status_code == 200 and len(projected) == 1
    assert "Concurrent new title" not in response.text
    assert "# Synthetic content" in response.text
    rejected = browser.client.get(stable)
    assert rejected.status_code == 303 and rejected.headers["location"] == "/admin/login"
    login = browser.client.post(
        "/admin/login", data={"password": _TOKEN + " rotated"}, headers={"Origin": _ORIGIN}
    )
    assert login.status_code == 303
    fresh = browser.client.get(stable)
    assert fresh.status_code == 200 and "Concurrent new title" in fresh.text
