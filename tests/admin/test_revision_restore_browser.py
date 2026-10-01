"""Real-browser historical restore using only synthetic, migrated content."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from sqlalchemy import func, select
from test_file_set_browser import (
    _BINARY,
    _ORIGINAL_MD,
    _REVISED_MD,
    _assert_counts,
    _Browser,
    _download,
    _local_files,
    _navigate,
    _select_files,
    _submit,
    _upload_form,
    _wait,
)

from patchouli_lib.admin.file_set_receipts import MasterFileSetReceiptRow
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.content.models import Page, PageSource, Revision

pytest_plugins = ("test_file_set_browser",)


def _prepare_two_revisions(browser: _Browser, tmp_path: Path) -> tuple[str, str, str]:
    _upload_form(browser, browser.book_path + "/new-page", create=True)
    _select_files(browser, _local_files(tmp_path))
    first = _submit(browser, expected_status="Files saved.")
    page_path = first.rsplit("/revisions/", 1)[0]
    _upload_form(browser, page_path + "/files/edit", create=False)
    _select_files(browser, _local_files(tmp_path, revised=True))
    second = _submit(browser, expected_status="Files saved.")
    assert (first, second) == (
        page_path + "/revisions/1",
        page_path + "/revisions/2",
    )
    return page_path, first, second


def _open_restore_from_history(browser: _Browser, selected: str) -> str:
    target = selected + "/restore"
    _navigate(browser, selected, 'document.querySelector(".revision-history") !== null')
    assert browser.tools.evaluate(
        f"""(() => {{
          const link = Array.from(document.querySelectorAll('a'))
            .find(item => item.getAttribute('href') === {json.dumps(target)});
          if (!link) return false;
          link.click();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        f"location.pathname === {json.dumps(target)} && "
        'document.querySelector("#revision-restore") !== null && '
        'document.querySelector("#restore-submit")?.disabled === false',
        description="the historical restore confirmation",
    )
    assert (
        browser.tools.evaluate("document.querySelector('#revision-restore').getAttribute('action')")
        == target
    )
    return target


def _restore_posts(browser: _Browser, target: str) -> list[dict[str, Any]]:
    return [
        entry["params"]["request"] | {"requestId": entry["params"]["requestId"]}
        for entry in browser.tools.events
        if entry.get("method") == "Network.requestWillBeSent"
        and entry.get("params", {}).get("request", {}).get("method") == "POST"
        and entry.get("params", {}).get("request", {}).get("url") == browser.origin + target
    ]


def _submit_restore(browser: _Browser, *, status: str) -> str:
    assert browser.tools.evaluate(
        """(() => {
          document.querySelector('input[name="confirm_restore"]').checked = true;
          const button = document.querySelector('#restore-submit');
          if (button.disabled) return false;
          button.click();
          return true;
        })()"""
    )
    _wait(
        browser.tools,
        f"document.querySelector('#restore-status')?.textContent === {json.dumps(status)}",
        description="the historical restore response",
    )
    link = browser.tools.evaluate(
        "document.querySelector('#restore-result a')?.getAttribute('href')"
    )
    assert isinstance(link, str) and link.startswith(browser.book_path + "/pages/")
    return link


def _delete_current_page(browser: _Browser, page_path: str) -> None:
    _navigate(
        browser,
        page_path,
        'Array.from(document.forms).some(form => form.action.endsWith("/delete"))',
    )
    assert browser.tools.evaluate(
        f"""(() => {{
          const form = document.querySelector('form[action={json.dumps(page_path + "/delete")}]');
          if (!form) return false;
          form.querySelector('input[name="confirm_delete"]').checked = true;
          form.requestSubmit();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        "location.pathname.includes('/trash/')",
        description="the Page move to Trash",
    )
    with browser.engine.connect() as connection:
        assert connection.scalar(select(Page.deleted_at)) is not None


def test_browser_restores_historical_complete_file_set(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    page_path, first, second = _prepare_two_revisions(browser, tmp_path)
    with browser.engine.connect() as connection:
        original = connection.execute(
            select(Page.page_uid, Page.title, Page.occurred_at, Page.section_id, Page.book_id)
        ).one()
        selected_source = connection.scalar(
            select(Revision.revision_id).where(Revision.revision_number == 1)
        )
    assert isinstance(selected_source, str)
    target = _open_restore_from_history(browser, first)
    assert (
        browser.tools.evaluate(
            "document.querySelector('input[name=" + json.dumps("confirm_restore") + "]')?.required"
        )
        is True
    )
    assert browser.tools.evaluate(
        "document.querySelector('input[name=expected_etag]').value"
    ).startswith('"page-v2-')
    assert browser.tools.evaluate(
        "document.querySelector('script[src=" + json.dumps("/admin/revision-restore.js") + "]')"
        " !== null"
    )
    assert browser.tools.evaluate(
        "(() => { document.querySelector('#restore-submit').click(); return true; })()"
    )
    assert _restore_posts(browser, target) == []

    restored = _submit_restore(browser, status="Files restored in a new revision.")
    assert restored == page_path + "/revisions/3"
    requests = _restore_posts(browser, target)
    assert len(requests) == 1
    headers = {key.casefold(): value for key, value in requests[0]["headers"].items()}
    assert headers["content-type"] == "application/json"
    assert "x-csrf-token" in headers
    assert len(headers["idempotency-key"]) == 32
    assert headers["if-match"].startswith('"page-v2-')
    post_data = browser.tools.command(
        "Network.getRequestPostData", {"requestId": requests[0]["requestId"]}
    )["postData"]
    assert json.loads(post_data) == {"confirm_restore": "yes"}
    _assert_counts(browser, revisions=3, receipts=3)
    with browser.engine.connect() as connection:
        assert (
            connection.execute(
                select(Page.page_uid, Page.title, Page.occurred_at, Page.section_id, Page.book_id)
            ).one()
            == original
        )
        restored_source = connection.execute(
            select(PageSource.kind, PageSource.locator).where(PageSource.revision_number == 3)
        ).one()
    assert restored_source == ("revision_restore", selected_source)
    for revision, markdown in (
        (first, _ORIGINAL_MD),
        (second, _REVISED_MD),
        (restored, _ORIGINAL_MD),
    ):
        _download(browser, revision + "/files/content.md", markdown)
        _download(browser, revision + "/files/" + quote("payload.bin", safe=""), _BINARY)

    _open_restore_from_history(browser, first)
    unchanged = _submit_restore(browser, status="Identical files; no new revision.")
    assert unchanged == restored
    with browser.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 3
        assert connection.scalar(select(func.count()).select_from(PageSource)) == 3
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 3
        assert connection.scalar(select(func.count()).select_from(MasterFileSetReceiptRow)) == 4
    assert len(AdminReadModel(browser.engine).recent_content_activity()) == 3


@pytest.mark.parametrize("file_set_browser", ["insecure-http"], indirect=True)
def test_browser_replays_lost_response_from_deleted_shell_on_plain_http(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    assert browser.tools.evaluate("window.isSecureContext") is False
    page_path, first, _second = _prepare_two_revisions(browser, tmp_path)
    target = _open_restore_from_history(browser, first)
    storage_key = "patchouli-revision-restore-v1:" + target
    assert browser.tools.evaluate(
        """(() => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (...args) => {
            const response = await originalFetch(...args);
            if (args[1]?.method === 'POST') {
              window.syntheticLostRestore = {
                status: response.status, result: await response.clone().json()
              };
              throw new Error('synthetic committed response loss');
            }
            return response;
          };
          document.querySelector('input[name="confirm_restore"]').checked = true;
          document.querySelector('#restore-submit').click();
          return true;
        })()"""
    )
    _wait(
        browser.tools,
        "document.querySelector('#restore-status')?.textContent.startsWith('Unconfirmed result.')",
        description="the committed but unconfirmed historical restore",
    )
    lost = browser.tools.evaluate("window.syntheticLostRestore")
    assert isinstance(lost, dict) and lost["status"] == 200
    assert lost["result"]["changed"] is True
    assert lost["result"]["replayed"] is False
    assert lost["result"]["revision_url"] == page_path + "/revisions/3"
    assert lost["result"]["revision_number"] == 3
    assert lost["result"]["revision_id"].startswith("rev_")
    assert lost["result"]["page_id"] == page_path.rsplit("/", 1)[-1]
    assert lost["result"]["etag"].startswith('"page-v2-')
    assert len(lost["result"]["snapshot_sha256"]) == 64
    assert lost["result"]["warnings"] == []
    assert lost["result"]["files"] == [
        {
            "name": name,
            "size_bytes": len(content),
            "sha256": sha256(content).hexdigest(),
        }
        for name, content in (("content.md", _ORIGINAL_MD), ("payload.bin", _BINARY))
    ]
    _assert_counts(browser, revisions=3, receipts=3)
    pending = browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})")
    assert isinstance(pending, str)
    stored = json.loads(pending)
    assert set(stored) == {"version", "target", "key", "etag"}
    assert stored["version"] == 1 and stored["target"] == target
    assert len(stored["key"]) == 32 and set(stored["key"]) <= set("0123456789abcdef")
    assert stored["etag"] != lost["result"]["etag"]
    assert all(secret not in pending for secret in ("csrf_token", "content.md", "payload.bin"))
    first_request = _restore_posts(browser, target)
    assert len(first_request) == 1

    browser.tools.command("Page.reload")
    _wait(
        browser.tools,
        "document.querySelector('#restore-status')?.textContent === "
        "'Original restore retained. Confirm to retry.'",
        description="the refreshed unknown-result retry",
    )
    assert browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})") == pending
    assert (
        browser.tools.evaluate("document.querySelector('input[name=expected_etag]').value")
        != stored["etag"]
    )

    _delete_current_page(browser, page_path)
    _navigate(browser, target, 'document.querySelector("#revision-restore") !== null')
    _wait(
        browser.tools,
        "document.querySelector('#restore-status')?.textContent === "
        "'Original restore retained. Confirm to retry.'",
        description="the deleted Page retry shell",
    )
    assert (
        browser.tools.evaluate("document.querySelector('#revision-restore').dataset.available")
        == "no"
    )
    assert browser.tools.evaluate("document.querySelector('#restore-submit').disabled") is False
    assert browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})") == pending
    replay = _submit_restore(browser, status="Original restore returned; it may not be current.")
    assert replay == lost["result"]["revision_url"]
    assert browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})") is None
    requests = _restore_posts(browser, target)
    assert len(requests) == 2
    keys = [
        {key.casefold(): value for key, value in request["headers"].items()}["idempotency-key"]
        for request in requests
    ]
    assert keys == [stored["key"], stored["key"]]
    _navigate(browser, target, 'document.querySelector("#revision-restore") !== null')
    assert browser.tools.evaluate("document.querySelector('#restore-submit').disabled") is True
    with browser.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 3
        assert connection.scalar(select(func.count()).select_from(PageSource)) == 3
        assert connection.scalar(select(func.count()).select_from(MasterFileSetReceiptRow)) == 3
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 4
        assert connection.scalar(select(Page.deleted_at)) is not None
    activities = AdminReadModel(browser.engine).recent_content_activity()
    assert len(activities) == 4
    assert sum(item.action == "content.page.file_set.revise" for item in activities) == 2


def test_browser_without_session_storage_never_posts_restore(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    _page_path, first, _second = _prepare_two_revisions(browser, tmp_path)
    browser.tools.command("Page.enable")
    browser.tools.command(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
              window.syntheticStorageInterception = true;
              const originalGetItem = Storage.prototype.getItem;
              Storage.prototype.getItem = function(key) {
                if (String(key).startsWith('patchouli-revision-restore-v1:')) {
                  throw new Error('synthetic unavailable session storage');
                }
                return originalGetItem.call(this, key);
              };
            """
        },
    )
    target = first + "/restore"
    _navigate(browser, target, 'document.querySelector("#revision-restore") !== null')
    assert browser.tools.evaluate("window.syntheticStorageInterception") is True
    _wait(
        browser.tools,
        "document.querySelector('#restore-status')?.textContent === "
        "'Allow site session storage to keep retries.'",
        description="the unavailable session-storage guard",
    )
    assert browser.tools.evaluate("document.querySelector('#restore-submit').disabled") is True
    before = len(_restore_posts(browser, target))
    assert browser.tools.evaluate(
        """(() => {
          document.querySelector('input[name="confirm_restore"]').checked = true;
          document.querySelector('#restore-submit').click();
          return true;
        })()"""
    )
    assert len(_restore_posts(browser, target)) == before == 0
    _assert_counts(browser, revisions=2, receipts=2)
