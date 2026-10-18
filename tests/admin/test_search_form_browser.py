"""Native search controls survive real POST navigation in an isolated HTTP browser."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import pytest
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from test_file_set_browser import _Browser, _navigate, _wait
from test_file_set_browser import file_set_browser as file_set_browser

from patchouli_lib.database import immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.tags.repository import TagRepository


def _seed_search(browser: _Browser) -> tuple[list[str], list[str]]:
    path = re.fullmatch(
        r"/admin/libraries/([^/]+)/sections/([^/]+)/books/([^/]+)", browser.book_path
    )
    assert path is not None
    structures = (
        path.groups(),
        seed_library_structure(browser.engine, prefix="4", label="Second"),
        seed_library_structure(browser.engine, prefix="7", label="Third"),
    )
    selected_tags = []
    with immediate_transaction(browser.engine) as connection:
        repository = TagRepository(connection)
        for index, (library_id, section_id, book_id) in enumerate(structures):
            page = page_graph_values(
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_byte=0x11 + index,
                revision_hex=f"{0x21 + index:02x}",
                title=f"中文 needle document {index}",
                content_md=b"# needle synthetic browser content",
                occurrence_wire="2026-08-14T10:00:00.123456Z",
            )
            insert_page_graph(connection, page)
            tag_id = f"{0xA + index:x}" * 32
            repository.add_tag(
                library_id=library_id, tag_id=tag_id, name=f"Tag {index}", created_at=0
            )
            repository.attach_page(
                library_id=library_id, page_uid=page[0].page_uid, tag_id=tag_id, created_at=0
            )
            if index < 2:
                selected_tags.append(f"{library_id}:{tag_id}")
    rebuild_search_index(browser.engine, clock=lambda: 3_000_000)
    return [structure[0] for structure in structures[:2]], selected_tags


def _controls(browser: _Browser) -> object:
    return browser.tools.evaluate(
        """(() => ({
          keywords: document.querySelector('#keywords').value,
          libraries: Array.from(document.querySelector('#library_id').selectedOptions,
            option => option.value),
          tags: Array.from(document.querySelector('#tags').selectedOptions, option => option.value),
          from: document.querySelector('#occurred_from').value,
          before: document.querySelector('#occurred_before').value
        }))()"""
    )


def _submit_search(browser: _Browser, *, status: int, ready: str) -> dict[str, list[str]]:
    expected_controls = _controls(browser)
    event_offset = len(browser.tools.events)
    assert browser.tools.evaluate(
        """(() => {
          const form = document.querySelector('form[action="/admin/search"]');
          if (!form.checkValidity()) return false;
          document.documentElement.dataset.syntheticSearchPending = 'yes';
          form.requestSubmit();
          return true;
        })()"""
    )
    _wait(
        browser.tools,
        "document.documentElement.dataset.syntheticSearchPending === undefined && "
        "location.pathname === '/admin/search' && document.querySelector('#keywords') !== null && "
        f"({ready})",
        description="the native search POST navigation",
    )
    assert _controls(browser) == expected_controls
    assert browser.tools.evaluate("location.href") == browser.origin + "/admin/search"
    events = browser.tools.events[event_offset:]
    requests = [
        event["params"]["request"]
        for event in events
        if event.get("method") == "Network.requestWillBeSent"
        and event.get("params", {}).get("request", {}).get("url")
        == browser.origin + "/admin/search"
    ]
    assert len(requests) == 1 and requests[0]["method"] == "POST"
    responses = [
        event["params"]["response"]
        for event in events
        if event.get("method") == "Network.responseReceived"
        and event.get("params", {}).get("response", {}).get("url")
        == browser.origin + "/admin/search"
    ]
    assert len(responses) == 1 and responses[0]["status"] == status
    return parse_qs(requests[0]["postData"], keep_blank_values=True)


@pytest.mark.parametrize("file_set_browser", ["insecure-http"], indirect=True)
def test_native_multi_library_search_keeps_controls_and_recovers_from_empty_and_invalid_results(
    file_set_browser: _Browser,
) -> None:
    browser = file_set_browser
    libraries, tags = _seed_search(browser)
    assert browser.tools.evaluate("location.protocol") == "http:"
    assert browser.tools.evaluate("window.isSecureContext") is False
    _navigate(
        browser,
        "/admin/search?lang=zh-CN",
        "document.documentElement.lang === 'zh-CN' && document.querySelector('#keywords') !== null",
    )
    assert browser.tools.evaluate(
        "document.querySelector('#library_id').multiple && document.querySelector('#tags').multiple"
    )
    assert browser.tools.evaluate(
        f"""(() => {{
          document.querySelector('#keywords').value = '  needle  ';
          const libraries = {json.dumps(libraries)};
          for (const option of document.querySelector('#library_id').options)
            option.selected = libraries.includes(option.value);
          document.querySelector('#occurred_from').value = '2026-08-13T00:00:01';
          document.querySelector('#occurred_before').value = '2026-08-15T00:00:01';
          return true;
        }})()"""
    )
    posted = _submit_search(
        browser, status=200, ready="document.querySelectorAll('section .item-list li').length === 2"
    )
    assert sorted(posted["library_id"]) == sorted(libraries)
    assert posted["keywords"] == ["  needle  "]
    sources = browser.tools.evaluate(
        "Array.from(document.querySelectorAll('.search-match-sources'), item => item.textContent)"
    )
    assert len(sources) == 2 and all(source.startswith("命中来源: 文档标题") for source in sources)
    assert browser.tools.evaluate(
        "Array.from(document.querySelectorAll('section .item-list li a'), item => item.textContent)"
        ".sort()"
    ) == ["中文 needle document 0", "中文 needle document 1"]

    assert browser.tools.evaluate(
        f"""(() => {{
          const tags = {json.dumps(tags)};
          for (const option of document.querySelector('#tags').options)
            option.selected = tags.includes(option.value);
          document.querySelector('#keywords').value = 'synthetic-no-results';
          return true;
        }})()"""
    )
    posted = _submit_search(
        browser,
        status=200,
        ready="document.querySelector('main').textContent.includes('没有匹配的页面。')",
    )
    assert sorted(posted["tags"]) == sorted(tags)
    assert browser.tools.evaluate(
        """(() => {
          document.querySelector('#keywords').value = 'needle';
          document.querySelector('#occurred_from').value = '2026-08-16T00:00:01';
          return true;
        })()"""
    )
    _submit_search(
        browser,
        status=422,
        ready="document.querySelector('main').textContent.includes('搜索表单无效')",
    )
    assert browser.tools.evaluate(
        """(() => {
          document.querySelector('#occurred_from').value = '2026-08-13T00:00:01';
          return true;
        })()"""
    )
    _submit_search(
        browser, status=200, ready="document.querySelectorAll('section .item-list li').length === 2"
    )
