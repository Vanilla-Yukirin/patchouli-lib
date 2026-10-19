"""Native search controls survive real POST navigation in an isolated HTTP browser."""

from __future__ import annotations

from urllib.parse import parse_qs

from test_file_set_browser import _Browser, _wait
from test_file_set_browser import file_set_browser as file_set_browser


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
