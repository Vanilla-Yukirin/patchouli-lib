"""Synthetic first setup -> browser-issued Agent -> standard HTTP file readback.

This uses an isolated Chrome profile and loopback app, not a user's browser or
clipboard. Secrets stay in process memory; no DB helper initializes identities.
"""

from __future__ import annotations

import json
from hashlib import sha256
from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlsplit

import pytest
from test_master_setup_browser import _Browser, _navigate, _submit_setup, _wait
from test_master_setup_browser import setup_browser as setup_browser

from patchouli_lib.search.index_v2 import rebuild_search_index

_AGENT = "Synthetic Onboarding Agent"
_TITLE = "Synthetic onboarding multi-file Page"
_FILES = (
    ("content.md", b"# Synthetic onboarding exact Revision\n"),
    ("attachment.bin", b"\xff\x00synthetic onboarding opaque bytes\x00"),
)


@pytest.mark.parametrize("setup_browser", ["proof"], indirect=True)
def test_library_scope_cards_search_switch_clear_and_roundtrip(setup_browser: _Browser) -> None:
    browser = setup_browser
    _submit_setup(browser, proof=True)
    paths = []
    for name in ("Scope Alpha", "Scope Beta"):
        _navigate(browser, "/admin/libraries", "document.querySelector('#name') !== null")
        paths.append(
            _submit_form(
                browser,
                "/admin/libraries",
                {"name": name},
                f"document.querySelector('h1')?.textContent === {json.dumps(name)}",
            )
        )
    first, second = (path.rsplit("/", 1)[-1] for path in paths)
    _navigate(browser, "/admin/agents", "document.querySelector('#agent_name') !== null")
    _wait(
        browser.tools,
        "document.querySelector('[name=credential_ttl_seconds]').required",
        description="the permission form script",
    )
    assert browser.tools.evaluate(f"""(() => {{
      const cards = [...document.querySelectorAll('[data-library-scope]')];
      const [read, write] = cards;
      if (cards.length !== 2 || document.querySelector('[name=home_library_id]')) return false;
      read.querySelector('input[value=exclude]').click();
      if (!write.querySelector('input[value=include]').checked) return false;
      const selected = read.querySelector('input[value="{first}"]');
      selected.click();
      const filter = read.querySelector('.scope-search');
      filter.value = 'Beta'; filter.dispatchEvent(new Event('input', {{bubbles:true}}));
      if (!selected.checked || !selected.closest('label').hidden) return false;
      read.querySelector('.scope-clear').click();
      if (selected.checked) return false;
      if (read.querySelector('.scope-summary').textContent.includes('Alpha')) return false;
      filter.value = ''; filter.dispatchEvent(new Event('input', {{bubbles:true}}));
      selected.click();
      read.querySelector('input[value=include]').click();
      if (!selected.checked || !read.querySelector('.scope-help').textContent.includes('allowed'))
        return false;
      read.querySelector('input[value=exclude]').click();
      write.querySelector('input[value="{second}"]').click();
      document.querySelector('#agent_name').value = 'Scope Browser Agent';
      document.querySelector('[name=never_expires]').click();
      document.querySelector('form[action="/admin/agents/create"]').requestSubmit();
      return true;
    }})()""")
    _wait(
        browser.tools,
        "document.querySelector('main code.secret') !== null",
        description="scope Agent creation",
    )
    detail = browser.tools.evaluate(
        "document.querySelector('main a[href*=\"/callers/\"]').getAttribute('href')"
    )
    assert isinstance(detail, str)
    _navigate(browser, detail, "document.querySelector('a[href$=\"/grants\"]') !== null")
    editor = browser.tools.evaluate(
        "document.querySelector('a[href$=\"/grants\"]').getAttribute('href')"
    )
    assert isinstance(editor, str)
    _navigate(browser, editor, "document.querySelector('[name=expected_scope_revision]') !== null")
    assert browser.tools.evaluate(f"""(() => {{
      const [read, write] = document.querySelectorAll('[data-library-scope]');
      if (!read.querySelector('input[value=exclude]').checked
        || !read.querySelector('input[value="{first}"]').checked
        || !write.querySelector('input[value=include]').checked
        || !write.querySelector('input[value="{second}"]').checked) return false;
      write.querySelector('input[value=exclude]').click();
      write.querySelector('.scope-clear').click();
      document.querySelector('form[action$="/grants"]').requestSubmit();
      return true;
    }})()""")
    _wait(
        browser.tools,
        "document.querySelector('[name=expected_scope_revision]')?.value === '1'",
        description="saved read/write scope revision",
    )
    assert browser.tools.evaluate(f"""(() => {{
      const [read, write] = document.querySelectorAll('[data-library-scope]');
      return read.querySelector('input[value=exclude]').checked
        && read.querySelector('input[value="{first}"]').checked
        && write.querySelector('input[value=exclude]').checked
        && write.querySelectorAll('input[type=checkbox]:checked').length === 0;
    }})()""")


def _submit_form(
    browser: _Browser,
    action: str,
    values: dict[str, str],
    ready: str,
    *,
    grants: tuple[str, ...] = (),
) -> str:
    assert browser.tools.evaluate(
        f"""(() => {{
          const form = Array.from(document.querySelectorAll('form'))
            .find(item => item.getAttribute('action') === {json.dumps(action)});
          if (!form) return false;
          for (const [name, value] of Object.entries({json.dumps(values)})) {{
            const control = form.elements.namedItem(name);
            if (!control) return false;
            control.value = value;
          }}
          for (const action of ['read', 'write']) {{
            for (const control of form.querySelectorAll(`input[name=${{action}}_libraries]`))
              control.checked = {json.dumps(grants)}.includes(`${{control.value}}:${{action}}`);
          }}
          if (!form.checkValidity()) return false;
          document.documentElement.dataset.onboardingPending = 'yes';
          form.requestSubmit();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        f"document.documentElement.dataset.onboardingPending === undefined && ({ready})",
        description="the native onboarding form submission",
    )
    path = browser.tools.evaluate("location.pathname")
    assert isinstance(path, str) and path.startswith("/admin/")
    return path


def _click_show(browser: _Browser) -> None:
    position = browser.tools.evaluate(
        """(() => {
          const button = document.querySelector('.token-show');
          button.scrollIntoView({block: 'center'});
          const rect = button.getBoundingClientRect();
          const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
          return {x, y, hit: document.elementFromPoint(x, y) === button};
        })()"""
    )
    assert isinstance(position, dict) and position["hit"] is True
    for event_type in ("mousePressed", "mouseReleased"):
        browser.tools.command(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": position["x"],
                "y": position["y"],
                "button": "left",
                "clickCount": 1,
            },
        )


def _no_token(token: str, value: str) -> None:
    # Do not let assertion rewriting include a credential in failure diagnostics.
    if token in value:
        pytest.fail("A synthetic credential appeared outside its protected display.")


def _http(
    browser: _Browser,
    path: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    origin = urlsplit(browser.origin)
    assert origin.scheme == "http" and origin.hostname == "127.0.0.1"
    assert path.startswith("/api/v1/") and not path.startswith("//")
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection = HTTPConnection("127.0.0.1", origin.port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def _json(body: bytes) -> dict[str, Any]:
    result = json.loads(body)
    assert isinstance(result, dict)
    return result


def _multipart() -> tuple[str, bytes]:
    boundary = "synthetic-onboarding-boundary"
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps({"title": _TITLE, "source": {"kind": "manual"}}).encode(),
        b"\r\n",
    ]
    for filename, content in _FILES:
        parts.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


@pytest.mark.parametrize("setup_browser", ["proof"], indirect=True)
@pytest.mark.parametrize("unlimited", [False, True])
def test_real_browser_master_onboarding_to_cross_library_agent_http(
    setup_browser: _Browser,
    unlimited: bool,
) -> None:
    browser = setup_browser
    _submit_setup(browser, proof=True)
    csrf = browser.tools.evaluate("document.querySelector('input[name=csrf_token]').value")

    libraries = []
    for name in ("Onboarding Agent Home", "Onboarding Upload Target"):
        _navigate(browser, "/admin/libraries", "document.querySelector('#name') !== null")
        libraries.append(
            _submit_form(
                browser,
                "/admin/libraries",
                {"name": name},
                f"document.querySelector('h1')?.textContent === {json.dumps(name)}",
            )
        )
    home_path, target_path = libraries
    home_id, target_id = (path.rsplit("/", 1)[-1] for path in libraries)
    assert home_id != target_id
    section_path = _submit_form(
        browser,
        target_path + "/sections",
        {"name": "Onboarding Target Section"},
        "document.querySelector('h1')?.textContent === 'Onboarding Target Section'",
    )
    book_path = _submit_form(
        browser,
        section_path + "/books",
        {"name": "Onboarding Target Book"},
        "document.querySelector('h1')?.textContent === 'Onboarding Target Book'",
    )
    section_id, book_id = (path.rsplit("/", 1)[-1] for path in (section_path, book_path))
    _navigate(browser, "/admin/agents", "document.querySelector('#agent_name') !== null")
    if unlimited:
        _wait(
            browser.tools,
            "document.querySelector('[name=credential_ttl_seconds]').required === true",
            description="the lifetime control script",
        )
        assert browser.tools.evaluate(
            """(() => {
              const box = document.querySelector('[name=never_expires]');
              const duration = document.querySelector('[name=credential_ttl_seconds]');
              box.click();
              if (!duration.disabled || duration.required) return false;
              box.click();
              if (duration.disabled || !duration.required) return false;
              box.click();
              return duration.disabled && !duration.required;
            })()"""
        )
    assert (
        _submit_form(
            browser,
            "/admin/agents/create",
            {
                "agent_name": _AGENT,
                "agent_description": "Synthetic cross-library onboarding",
                "credential_ttl_seconds": "3600",
            },
            "document.querySelector('main code.secret') !== null",
            grants=(f"{target_id}:read", f"{target_id}:write"),
        )
        == "/admin/agents/create"
    )
    # Ignore the initial delivery value. Follow the UI and request protected reveal.
    detail_path = browser.tools.evaluate(
        f"document.querySelector('main a[href^={json.dumps(home_path + '/callers/')} ]')"
        ".getAttribute('href')"
    )
    assert isinstance(detail_path, str) and detail_path.startswith(home_path + "/callers/")
    assert browser.tools.evaluate(
        f"document.querySelector('a[href={json.dumps(detail_path)}]').click(); true"
    )
    _wait(
        browser.tools,
        f"location.pathname === {json.dumps(detail_path)} && "
        "document.querySelector('.token-show') !== null",
        description="the Agent detail link",
    )
    assert browser.tools.evaluate("document.querySelector('.token-output').textContent === ''")
    _click_show(browser)
    _wait(
        browser.tools,
        "document.querySelector('.token-output').hidden === false && "
        "document.querySelector('.token-output').textContent.length > 0",
        description="the protected Token display",
    )
    token = browser.tools.evaluate("document.querySelector('.token-output').textContent")
    if not isinstance(token, str) or not token.startswith("plb1."):
        pytest.fail("The protected display did not return an Agent credential.")
    _click_show(browser)
    assert browser.tools.evaluate("document.querySelector('.token-output').textContent === ''")
    _no_token(token, browser.tools.evaluate("document.documentElement.outerHTML"))

    _navigate(browser, "/admin", "document.querySelector('form[action$=logout]') !== null")
    assert browser.tools.evaluate("document.querySelector('input[name=csrf_token]').value") == csrf
    browser.tools.command("Page.navigate", {"url": browser.origin + "/connect"})
    _wait(
        browser.tools,
        "document.querySelector('#instruction')?.value.includes('/api/v1/agent/skill/manifest')",
        description="the public connection instruction",
    )
    _no_token(token, browser.tools.evaluate("document.querySelector('#instruction').value"))
    _no_token(token, browser.tools.evaluate("document.documentElement.outerHTML"))
    assert browser.tools.evaluate("localStorage.length === 0 && sessionStorage.length === 0")

    assert _http(browser, "/api/v1/auth/whoami")[0] == 401
    status, _, raw = _http(browser, "/api/v1/auth/whoami", token=token)
    assert status == 200
    identity = _json(raw)
    assert identity["name"] == _AGENT
    assert (identity["expires_at"] is None) == unlimited
    assert identity["caller_id"] == detail_path.rsplit("/", 1)[-1]
    assert identity["grants"] == []
    assert identity["policy_mode"] == "library_grants"
    assert identity["library_grants"] == [{"library_id": target_id, "actions": ["read", "write"]}]
    _no_token(token, raw.decode())
    status, _, raw = _http(browser, "/api/v1/capabilities", token=token)
    assert status == 200 and "file-sets" in _json(raw)["features"]
    manifest_path = "/api/v1/agent/skill/manifest"
    assert _http(browser, manifest_path)[0] == 401
    status, headers, raw = _http(browser, manifest_path, token=token)
    assert status == 200 and headers["cache-control"] == "private, no-store"
    entries = _json(raw)["files"]
    assert {entry["path"] for entry in entries} == {
        "SKILL.md",
        "references/http.md",
        "references/local-token.md",
    }
    for entry in entries:
        assert _http(browser, entry["href"])[0] == 401
        status, headers, downloaded = _http(browser, entry["href"], token=token)
        assert status == 200 and headers["cache-control"] == "private, no-store"
        assert len(downloaded) == entry["bytes"]
        assert sha256(downloaded).hexdigest() == entry["sha256"]

    media, body = _multipart()
    create_path = f"/api/v1/libraries/{target_id}/sections/{section_id}/books/{book_id}/pages"
    status, headers, raw = _http(
        browser,
        create_path,
        token=token,
        method="POST",
        body=body,
        extra_headers={
            "Content-Type": media,
            "Idempotency-Key": "synthetic-onboarding-files-create",
        },
    )
    assert status == 201
    _no_token(token, raw.decode())
    created = _json(raw)
    assert [entry["filename"] for entry in created["files"]] == sorted(name for name, _ in _FILES)
    exact_path = headers["location"]
    status, _, raw = _http(browser, exact_path, token=token)
    assert status == 200
    exact = _json(raw)
    assert exact["page_id"] == created["page_id"]
    assert exact["revision_id"] == created["revision_id"]
    assert exact["snapshot_sha256"] == created["snapshot_sha256"]
    assert exact["revision_number"] == 1
    for filename, content in _FILES:
        status, headers, downloaded = _http(browser, exact_path + "/" + filename, token=token)
        assert status == 200 and downloaded == content
        assert headers["x-content-type-options"] == "nosniff"
        summary = next(entry for entry in exact["files"] if entry["filename"] == filename)
        assert summary["content_sha256"] == sha256(content).hexdigest()
        assert summary["size_bytes"] == len(content)

    rebuild_search_index(browser.engine)
    status, _, raw = _http(
        browser,
        "/api/v1/search",
        token=token,
        method="POST",
        body=json.dumps({"keywords": ["Synthetic onboarding"]}).encode(),
        extra_headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert any(item["page_id"] == created["page_id"] for item in _json(raw)["items"])

    _navigate(browser, "/admin", "document.querySelector('form[action$=logout]') !== null")
    activities = browser.tools.evaluate(
        """(() => {
          const section = Array.from(document.querySelectorAll('main section'))
            .find(item => item.querySelector('h2')?.textContent === 'Content activity');
          return Array.from(section.querySelectorAll('li'), item => ({
            text: item.textContent,
            links: Array.from(item.querySelectorAll('a'), link => link.getAttribute('href'))
          }));
        })()"""
    )
    assert isinstance(activities, list) and len(activities) == 1
    assert _AGENT in activities[0]["text"] and "Created a page" in activities[0]["text"]
    assert _TITLE in activities[0]["text"]
    assert detail_path in activities[0]["links"]
    page_link = next(link for link in activities[0]["links"] if "/pages/" in link)
    assert page_link.startswith(book_path + "/pages/") and page_link.endswith("/revisions/1")
    assert browser.tools.evaluate("document.querySelector('input[name=csrf_token]').value") == csrf
    _no_token(token, browser.tools.evaluate("document.documentElement.outerHTML"))
