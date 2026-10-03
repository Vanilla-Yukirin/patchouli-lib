"""Synthetic HTTP acceptance of complete historical file-group restoration."""

from __future__ import annotations

from collections.abc import Sequence
from html import unescape
from re import search
from typing import Any

import pytest
from httpx2 import Response
from sqlalchemy import select
from test_file_set_routes import (
    _FILES_BINARY,
    _FILES_MARKDOWN,
    _FILES_MIXED,
    _ORIGIN,
    _TOKEN,
    Browser,
    _counts,
    _created,
    _current_etag,
    _json_error,
    _master_session,
    _post,
)
from test_file_set_routes import (
    browser as browser,
)

from patchouli_lib.admin import file_set_routes
from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.content.models import PageSource
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import immediate_transaction


def _restore(
    browser: Browser,
    path: str,
    etag: str,
    *,
    key: str = "c" * 32,
    confirmation: object = None,
    extra_headers: Sequence[tuple[str, str]] = (),
) -> Response:
    return browser.client.post(
        path,
        json={"confirm_restore": "yes"} if confirmation is None else confirmation,
        headers=[
            ("Origin", _ORIGIN),
            ("X-CSRF-Token", browser.csrf),
            ("Idempotency-Key", key),
            ("If-Match", etag),
            *extra_headers,
        ],
    )


def _revised(browser: Browser, first: dict[str, Any]) -> dict[str, Any]:
    response = _post(
        browser,
        browser.page_path(first["page_id"]) + "/file-revisions",
        {},
        (("content.md", b"# Later version\n"),),
        key="b" * 32,
        etag=first["etag"],
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.mark.parametrize("files", (_FILES_MIXED, _FILES_MARKDOWN, _FILES_BINARY))
def test_restore_form_roundtrip_noop_and_frozen_replay(
    browser: Browser, files: tuple[tuple[str, bytes], ...]
) -> None:
    first = _created(browser, files=files)
    second = _revised(browser, first)
    page_path = browser.page_path(first["page_id"])
    path = page_path + "/revisions/1/restore"
    historical = browser.client.get(page_path + "/revisions/1")
    assert f'href="{path}"' in historical.text
    form = browser.client.get(path + "?lang=zh-CN")
    assert form.status_code == 200
    assert 'id="revision-restore"' in form.text
    assert 'name="confirm_restore" value="yes" required' in form.text
    assert 'id="restore-submit" type="submit" disabled' in form.text
    assert "旧版本仍完整保留" in form.text
    assert "提交时服务器才核验完整文件字节" in form.text
    assert "<noscript>" in form.text
    assert 'data-available="yes"' in form.text
    assert "script-src 'self'" in form.headers["content-security-policy"]
    assert "connect-src 'self'" in form.headers["content-security-policy"]
    assert "unsafe-inline" not in form.headers["content-security-policy"]
    assert _form_etag(form.text) == second["etag"]

    restored = _restore(browser, path, second["etag"])
    assert restored.status_code == 200, restored.text
    result = restored.json()
    assert result["revision_number"] == 3 and result["changed"] and not result["replayed"]
    assert result["revision_url"] == page_path + "/revisions/3"
    assert result["snapshot_sha256"] == first["snapshot_sha256"]
    assert result["files"] == first["files"]
    assert restored.headers["etag"] == result["etag"]
    for name, content in files:
        download = browser.client.get(result["revision_url"] + "/files/" + name)
        assert download.status_code == 200 and download.content == content
    with browser.engine.connect() as connection:
        source = connection.execute(
            select(PageSource.kind, PageSource.locator).where(
                PageSource.revision_id == result["revision_id"]
            )
        ).one()
    assert tuple(source) == ("revision_restore", first["revision_id"])
    assert _counts(browser.engine) == (1, 3, 3, 3)
    replay = _restore(browser, path, second["etag"])
    assert replay.status_code == 200
    assert replay.json() == {**result, "replayed": True}
    no_op = _restore(browser, path, result["etag"], key="d" * 32)
    assert no_op.status_code == 200 and not no_op.json()["changed"]
    assert no_op.json()["revision_number"] == 3
    assert _counts(browser.engine) == (1, 3, 3, 3)
    _json_error(_restore(browser, path, second["etag"], key="e" * 32), 412)
    _json_error(_restore(browser, page_path + "/revisions/2/restore", second["etag"]), 409)


def _form_etag(html: str) -> str:
    match = search(r'name="expected_etag" value="([^"]+)"', html)
    assert match is not None
    return unescape(match.group(1))


def test_restore_replays_after_trash_without_restoring_deleted_page(browser: Browser) -> None:
    first = _created(browser)
    second = _revised(browser, first)
    page_path = browser.page_path(first["page_id"])
    path = page_path + "/revisions/1/restore"
    restored = _restore(browser, path, second["etag"])
    assert restored.status_code == 200
    original_result = restored.json()
    AdminActionService(browser.engine).delete_page_as_master(
        browser.library_id,
        browser.section_id,
        browser.book_id,
        first["page_id"],
        MasterDeletePageFormInput(
            expected_etag=_current_etag(browser.engine, browser.library_id, first["page_id"]),
            confirm_delete="yes",
        ),
        master_session=_master_session(browser),
    )
    counts = _counts(browser.engine)
    shell = browser.client.get(path)
    assert shell.status_code == 404
    assert 'id="revision-restore"' in shell.text and 'data-available="no"' in shell.text
    replay = _restore(browser, path, second["etag"])
    assert replay.status_code == 200
    assert replay.json() == {**original_result, "replayed": True}
    _json_error(_restore(browser, path, second["etag"], key="d" * 32), 404)
    assert _counts(browser.engine) == counts
    assert browser.client.get(page_path).status_code == 404
    with browser.engine.connect() as connection:
        page = ContentRepository(connection).get_page(browser.library_id, first["page_id"])
        assert page is not None and page.deleted_at is not None


@pytest.mark.parametrize("number", ("0", "01", "+1", "1e0", "-1", "9" * 20))
def test_restore_rejects_noncanonical_source_number(browser: Browser, number: str) -> None:
    first = _created(browser)
    path = browser.page_path(first["page_id"]) + f"/revisions/{number}/restore"
    _json_error(_restore(browser, path, first["etag"]), 422)
    assert browser.client.get(path).status_code == 422
    assert _counts(browser.engine) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "body", ({}, {"confirm_restore": "no"}, {"confirm_restore": "yes", "x": 1})
)
def test_restore_requires_explicit_unique_bounded_confirmation(
    browser: Browser, body: object
) -> None:
    first = _created(browser)
    path = browser.page_path(first["page_id"]) + "/revisions/1/restore"
    _json_error(_restore(browser, path, first["etag"], confirmation=body), 422)
    headers = {
        "Origin": _ORIGIN,
        "X-CSRF-Token": browser.csrf,
        "Idempotency-Key": "c" * 32,
        "If-Match": first["etag"],
    }
    _json_error(browser.client.post(path, data={"confirm_restore": "yes"}, headers=headers), 415)
    _json_error(
        browser.client.post(
            path,
            content=b'{"confirm_restore":"yes","confirm_restore":"yes"}',
            headers={**headers, "Content-Type": "application/json"},
        ),
        422,
    )
    _json_error(
        browser.client.post(
            path,
            content=b" " * 513,
            headers={**headers, "Content-Type": "application/json"},
        ),
        413,
    )
    assert _counts(browser.engine) == (1, 1, 1, 1)


def test_restore_admission_precedes_body_read_and_old_sessions_are_rejected(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _created(browser)
    path = browser.page_path(first["page_id"]) + "/revisions/1/restore"
    reads = 0

    async def reject_read(_request: object) -> None:
        nonlocal reads
        reads += 1
        raise AssertionError("Unauthorized request body read")

    monkeypatch.setattr(file_set_routes, "_restore_confirmation", reject_read)
    headers = {
        "Origin": _ORIGIN,
        "X-CSRF-Token": browser.csrf,
        "Idempotency-Key": "c" * 32,
        "If-Match": first["etag"],
    }
    for changes, status in (
        ({"Origin": "https://other.invalid"}, 403),
        ({"X-CSRF-Token": "bad"}, 403),
        ({"Idempotency-Key": "bad key"}, 422),
        ({"If-Match": 'W/"weak"'}, 422),
    ):
        _json_error(
            browser.client.post(
                path, json={"confirm_restore": "yes"}, headers={**headers, **changes}
            ),
            status,
        )
    _json_error(
        _restore(browser, path, first["etag"], extra_headers=(("X-CSRF-Token", browser.csrf),)), 403
    )
    _json_error(
        _restore(browser, path, first["etag"], extra_headers=(("Idempotency-Key", "d" * 32),)),
        422,
    )
    _json_error(
        _restore(browser, path, first["etag"], extra_headers=(("If-Match", first["etag"]),)),
        422,
    )
    missing_etag = {name: value for name, value in headers.items() if name != "If-Match"}
    _json_error(
        browser.client.post(path, json={"confirm_restore": "yes"}, headers=missing_etag), 428
    )
    with immediate_transaction(browser.engine) as connection:
        assert MasterTokenRepository(connection).rotate(_TOKEN, _TOKEN + "new", now=2_000_000)
    _json_error(_restore(browser, path, first["etag"]), 401)
    assert browser.client.get(path).status_code == 401
    assert reads == 0
    assert _counts(browser.engine) == (1, 1, 1, 1)


def test_restore_audit_failure_is_safe_and_atomic(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _created(browser)
    second = _revised(browser, first)
    path = browser.page_path(first["page_id"]) + "/revisions/1/restore"

    def reject_audit(self: MasterAuditRepository, **_kwargs: object) -> None:
        raise RuntimeError("synthetic private failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    response = _restore(browser, path, second["etag"])
    _json_error(response, 500)
    assert "synthetic private" not in response.text
    assert _counts(browser.engine) == (1, 2, 2, 2)
