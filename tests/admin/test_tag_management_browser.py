"""Exercise native tag management forms in an isolated local browser."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from test_master_setup_browser import _Browser, _navigate, _submit_setup, _wait
from test_master_setup_browser import setup_browser as setup_browser
from test_tag_browser import _seed_structure

from patchouli_lib.database import immediate_transaction
from patchouli_lib.tags.repository import TagRepository


@pytest.mark.parametrize("setup_browser", ["proof"], indirect=True)
def test_tag_management_selection_filter_rename_delete(setup_browser: _Browser) -> None:
    browser = setup_browser
    browser.tools.command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 1280,
            "height": 960,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )
    _navigate(browser, "/admin/setup", "document.querySelector('#master_token') !== null")
    _submit_setup(browser, proof=True)
    first, _, _ = _seed_structure(browser.engine, prefix="1")
    second, _, _ = _seed_structure(browser.engine, prefix="4")
    with immediate_transaction(browser.engine) as connection:
        for library, name in ((first, "技术"), (second, "新闻")):
            TagRepository(connection).add_tag(
                library_id=library, tag_id="a" * 32, name=name, created_at=1_000_000
            )
    _navigate(
        browser, "/admin/tags?lang=zh-CN", "document.querySelector('.tag-management') !== null"
    )
    assert browser.tools.evaluate("document.querySelectorAll('[data-tag-row]').length") == 2
    screenshot_dir = os.environ.get("PATCHOULI_TEST_SCREENSHOT_DIR")
    if screenshot_dir:
        screenshot = browser.tools.command("Page.captureScreenshot", {"format": "png"})
        (Path(screenshot_dir) / "tag-management-desktop.png").write_bytes(
            base64.b64decode(screenshot["data"])
        )
    browser.tools.command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 390,
            "height": 844,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )
    assert browser.tools.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshot_dir:
        screenshot = browser.tools.command("Page.captureScreenshot", {"format": "png"})
        (Path(screenshot_dir) / "tag-management-mobile.png").write_bytes(
            base64.b64decode(screenshot["data"])
        )
    browser.tools.command("Emulation.clearDeviceMetricsOverride")
    assert (
        browser.tools.evaluate("getComputedStyle(document.querySelector('.tag-chip')).borderRadius")
        == "999px"
    )
    assert browser.tools.evaluate("document.querySelector('button[value=rename]').disabled")
    assert browser.tools.evaluate("document.querySelector('#tag-select-all').click(); true")
    assert browser.tools.evaluate("document.querySelectorAll('.tag-select:checked').length") == 2
    assert (
        browser.tools.evaluate(f"""(() => {{
      const filter = document.querySelector('#tag-library');
      filter.value = {json.dumps(first)}; filter.dispatchEvent(new Event('change'));
      return document.querySelectorAll('.tag-select:checked').length;
    }})()""")
        == 1
    )
    assert browser.tools.evaluate("""(() => {
      const row = Array.from(document.querySelectorAll('[data-tag-row]')).find(row => !row.hidden);
      row.querySelector('.tag-new-name').value = '技术归档';
      document.documentElement.dataset.pending = 'yes';
      document.querySelector('form.tag-management').requestSubmit(document.querySelector('button[value=rename]'));
      return true;
    })()""")
    _wait(
        browser.tools,
        "!document.documentElement.dataset.pending && "
        "document.body.textContent.includes('技术归档')",
        description="renaming a selected tag",
    )
    assert (
        browser.tools.evaluate(f"""(() => {{
      const row = document.querySelector('[data-library-id="{first}"]');
      row.querySelector('.tag-select').click();
      document.querySelector('form.tag-management').requestSubmit(document.querySelector('button[value=delete]'));
      return document.querySelector('[role=status]').textContent;
    }})()""")
        == "请先勾选删除确认。"
    )
    assert browser.tools.evaluate("""(() => {
      document.querySelector('[name=confirm_delete]').click();
      document.documentElement.dataset.pending = 'yes';
      document.querySelector('form.tag-management').requestSubmit(document.querySelector('button[value=delete]'));
      return true;
    })()""")
    _wait(
        browser.tools,
        "!document.documentElement.dataset.pending && "
        "document.querySelectorAll('[data-tag-row]').length === 1",
        description="deleting only the selected tag",
    )
    assert browser.tools.evaluate("document.querySelector('.tag-chip').textContent") == "新闻"
    path = browser.tools.evaluate("document.querySelector('.tag-chip').getAttribute('href')")
    _navigate(browser, path, "document.querySelector('main h1')?.textContent === '新闻'")
    assert browser.tools.evaluate("document.body.textContent.includes('暂无已标记的页面。')")
