"""One synthetic management journey through real same-session browser controls."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select
from test_file_set_browser import (
    _Browser,
    _download,
    _navigate,
    _select_files,
    _submit,
    _wait,
)
from test_file_set_browser import file_set_browser as file_set_browser
from test_search_form_browser import _submit_search

from patchouli_lib.content.models import Page
from patchouli_lib.search.index_v2 import rebuild_search_index

_TITLE = "中文 journeyneedle document"
_ORIGINAL = b"# journeyneedle original synthetic content\n"
_REVISED = b"# journeyneedle revised synthetic content\n"


def _follow(browser: _Browser, path: str, ready: str) -> None:
    assert browser.tools.evaluate(
        f"""(() => {{
          const link = Array.from(document.querySelectorAll('a'))
            .find(item => item.getAttribute('href') === {json.dumps(path)});
          if (!link) return false;
          link.click();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        f"location.pathname === {json.dumps(path)} && ({ready})",
        description="the management journey link",
    )


def _submit_form(browser: _Browser, action: str, values: dict[str, str | bool], ready: str) -> str:
    assert browser.tools.evaluate(
        f"""(() => {{
          const form = Array.from(document.querySelectorAll('form'))
            .find(item => item.getAttribute('action') === {json.dumps(action)});
          if (!form) return false;
          for (const [name, value] of Object.entries({json.dumps(values)})) {{
            const control = form.elements.namedItem(name);
            if (!control) return false;
            if (control.type === 'checkbox') control.checked = value;
            else control.value = value;
          }}
          if (!form.checkValidity()) return false;
          document.documentElement.dataset.syntheticJourneyPending = 'yes';
          form.requestSubmit();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        f"document.documentElement.dataset.syntheticJourneyPending === undefined && ({ready})",
        description="the management journey form navigation",
    )
    path = browser.tools.evaluate("location.pathname")
    assert isinstance(path, str) and path.startswith("/admin/libraries/")
    return path


def _search(browser: _Browser, library_id: str, *, visible: bool) -> None:
    _follow(browser, "/admin/search", "document.querySelector('#keywords') !== null")
    assert browser.tools.evaluate(
        f"""(() => {{
          document.querySelector('#keywords').value = 'journeyneedle';
          for (const option of document.querySelector('#library_id').options)
            option.selected = option.value === {json.dumps(library_id)};
          return true;
        }})()"""
    )
    posted = _submit_search(
        browser,
        status=200,
        ready=(
            "document.querySelectorAll('section .item-list li').length === 1"
            if visible
            else "document.querySelector('main').textContent.includes('No matching Pages.')"
        ),
    )
    assert posted["library_id"] == [library_id]
    assert posted["keywords"] == ["journeyneedle"]
    assert browser.tools.evaluate(
        "Array.from(document.querySelectorAll('section .item-list li a'), a => a.textContent)"
    ) == ([_TITLE] if visible else [])


def test_master_browser_management_journey_preserves_identity_and_history(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    # Establish the documented index prerequisite once, before any journey writes.
    # Later search requests must observe normal transactional write projection.
    rebuild_search_index(browser.engine)
    session_csrf = browser.tools.evaluate("document.querySelector('input[name=csrf_token]').value")
    _navigate(browser, "/admin/libraries", "document.querySelector('#name') !== null")
    library_path = _submit_form(
        browser,
        "/admin/libraries",
        {"name": "Journey Library", "description": "Synthetic journey library"},
        "document.querySelector('h1')?.textContent === 'Journey Library'",
    )
    section_path = _submit_form(
        browser,
        library_path + "/sections",
        {"name": "Journey Section", "description": "Synthetic journey section"},
        "document.querySelector('h1')?.textContent === 'Journey Section'",
    )
    book_path = _submit_form(
        browser,
        section_path + "/books",
        {"name": "Journey Book", "summary": "Synthetic journey book"},
        "document.querySelector('h1')?.textContent === 'Journey Book'",
    )
    assert section_path.startswith(library_path + "/sections/")
    assert book_path.startswith(section_path + "/books/")
    browser = replace(browser, book_path=book_path)
    library_id = library_path.rsplit("/", 1)[-1]

    original_dir, revised_dir = tmp_path / "original-journey", tmp_path / "revised-journey"
    original_dir.mkdir()
    revised_dir.mkdir()
    original_file, revised_file = original_dir / "content.md", revised_dir / "content.md"
    original_file.write_bytes(_ORIGINAL)
    revised_file.write_bytes(_REVISED)
    _follow(
        browser,
        book_path + "/new-page",
        "document.querySelector('#upload-submit')?.disabled === false",
    )
    assert browser.tools.evaluate(
        f"""(() => {{
          document.querySelector('#upload-title').value = {json.dumps(_TITLE)};
          document.querySelector('#upload-time').value = '2026-08-13T10:00:00.123456Z';
          return true;
        }})()"""
    )
    _select_files(browser, (original_file,))
    first = _submit(browser, expected_status="Files saved.")
    assert first.endswith("/revisions/1")
    page_path = first.rsplit("/revisions/", 1)[0]
    _follow(browser, first, "document.querySelector('.revision-files a[download]') !== null")

    _search(browser, library_id, visible=True)
    _follow(browser, page_path, "document.querySelector('.revision-history') !== null")
    _follow(
        browser,
        page_path + "/files/edit",
        "document.querySelector('#upload-submit')?.disabled === false",
    )
    _select_files(browser, (revised_file,))
    second = _submit(browser, expected_status="Files saved.")
    assert second == page_path + "/revisions/2"
    _follow(browser, second, "document.querySelector('.revision-history') !== null")
    assert browser.tools.evaluate("document.querySelector('.markdown-preview').textContent") == (
        _REVISED.decode()
    )
    _follow(browser, first, "document.querySelector('.selected-marker') !== null")
    assert browser.tools.evaluate("document.querySelector('.markdown-preview').textContent") == (
        _ORIGINAL.decode()
    )
    _follow(browser, page_path, "document.querySelector('input[name=confirm_delete]') !== null")
    trash_path = section_path + "/trash/" + page_path.rsplit("/", 1)[-1]
    assert (
        _submit_form(
            browser,
            page_path + "/delete",
            {"confirm_delete": True},
            f"location.pathname === {json.dumps(trash_path)} && "
            "document.querySelector('form[action$=\"/restore\"]') !== null",
        )
        == trash_path
    )
    assert browser.tools.evaluate("document.querySelector('.markdown-preview') === null")
    _search(browser, library_id, visible=False)

    _navigate(browser, section_path, "document.querySelector('h1') !== null")
    _follow(browser, section_path + "/trash", "document.querySelector('.item-list') !== null")
    _follow(browser, trash_path, "document.querySelector('input[name=expected_etag]') !== null")
    assert (
        _submit_form(
            browser,
            trash_path + "/restore",
            {},
            f"location.pathname === {json.dumps(section_path)} && "
            "document.querySelector('h1')?.textContent === 'Journey Section'",
        )
        == section_path
    )
    _search(browser, library_id, visible=True)
    _follow(browser, page_path, "document.querySelector('.revision-history') !== null")
    assert browser.tools.evaluate(
        "Array.from(document.querySelectorAll('.revision-history a'), a => a.textContent)"
    ) == ["Version 2", "Version 1"]
    assert browser.tools.evaluate("document.querySelector('input[name=csrf_token]').value") == (
        session_csrf
    )
    _download(browser, first + "/files/content.md", _ORIGINAL)
    _download(browser, second + "/files/content.md", _REVISED)
    with browser.engine.connect() as connection:
        page = connection.execute(
            select(Page.page_id, Page.current_revision_number, Page.deleted_at).where(
                Page.library_id == library_id
            )
        ).one()
    assert tuple(page) == (page_path.rsplit("/", 1)[-1], 2, None)
