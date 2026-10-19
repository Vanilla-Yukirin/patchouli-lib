"""Credential scope controls in an isolated Chrome profile and loopback app."""

from __future__ import annotations

import json

import pytest
from test_master_setup_browser import _Browser, _navigate, _submit_setup, _wait
from test_master_setup_browser import setup_browser as setup_browser


@pytest.mark.parametrize("setup_browser", ["proof"], indirect=True)
@pytest.mark.parametrize("locale", ["en"])
def test_library_scope_cards_search_switch_clear_and_roundtrip(
    setup_browser: _Browser, locale: str
) -> None:
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
    _navigate(
        browser, f"/admin/agents?lang={locale}", "document.querySelector('#agent_name') !== null"
    )
    _wait(
        browser.tools,
        "document.querySelector('[name=credential_ttl_seconds]').required",
        description="the permission form script",
    )
    # Exercise the existing page's actual submission policy, without injecting
    # Origin as the HTTP helper does. Generation must stay in the drawer.
    assert browser.tools.evaluate("document.querySelector('#ticket-form').requestSubmit(); true")
    _wait(
        browser.tools,
        "document.querySelector('#ticket-drawer').open && "
        "/^[A-Za-z0-9]{16}$/.test(document.querySelector('#ticket-code')?.value || '')",
        description="the inline onboarding ticket",
    )
    assert browser.tools.evaluate("""(() => {
      const drawer = document.querySelector('#ticket-drawer');
      const result = drawer.querySelector('#ticket-result');
      const valid = location.pathname === '/admin/agents'
        && result.querySelector('#ticket-prompt').value.includes('/api/v1/onboarding/redeem')
        && result.querySelector('time[data-ticket-expires]') !== null
        && result.querySelector('[role=alert]') === null;
      drawer.close();
      return valid;
    })()""")
    all_libraries = "所有知识库" if locale == "zh-CN" else "All Libraries"
    allowed_help = "允许" if locale == "zh-CN" else "allowed"
    assert browser.tools.evaluate(f"""(() => {{
      const cards = [...document.querySelectorAll('[data-library-scope]')];
      return cards.every(card => card.querySelector('input[value=exclude]')
        .closest('label').textContent.trim() === {json.dumps(all_libraries)});
    }})()""")
    # Native checkbox semantics and keyboard control survive the custom mark.
    browser.tools.evaluate("document.querySelector('.scope-library input').focus(); true")
    for event in ("keyDown", "keyUp"):
        browser.tools.command(
            "Input.dispatchKeyEvent", {"type": event, "key": " ", "code": "Space"}
        )
    assert browser.tools.evaluate("""(() => {
      const input = document.querySelector('.scope-library input');
      return input.checked && getComputedStyle(input, '::before').content.includes('✓')
        && getComputedStyle(input, '::before').visibility === 'visible';
    })()""")
    browser.tools.command(
        "Emulation.setEmulatedMedia", {"features": [{"name": "forced-colors", "value": "active"}]}
    )
    assert browser.tools.evaluate("""(() => {
      const input = document.querySelector('.scope-library input');
      const card = input.closest('[data-library-scope]');
      card.querySelector('input[value=exclude]').click();
      const style = getComputedStyle(input);
      const visible = getComputedStyle(input, '::before').visibility === 'visible'
        && getComputedStyle(input, '::before').content.includes('×')
        && style.color !== style.backgroundColor;
      card.querySelector('input[value=include]').click();
      input.focus();
      return visible;
    })()""")
    browser.tools.command("Emulation.setEmulatedMedia", {"features": []})
    for event in ("keyDown", "keyUp"):
        browser.tools.command(
            "Input.dispatchKeyEvent", {"type": event, "key": " ", "code": "Space"}
        )
    assert browser.tools.evaluate(f"""(() => {{
      const cards = [...document.querySelectorAll('[data-library-scope]')];
      const [read, write] = cards;
      if (cards.length !== 2 || document.querySelector('[name=home_library_id]')) return false;
      read.querySelector('input[value=exclude]').click();
      if (!write.querySelector('input[value=include]').checked) return false;
      const selected = read.querySelector('input[value="{first}"]');
      selected.click();
      if (!getComputedStyle(selected, '::before').content.includes('×')
        || getComputedStyle(selected, '::before').visibility !== 'visible') return false;
      const filter = read.querySelector('.scope-search');
      filter.value = 'Beta'; filter.dispatchEvent(new Event('input', {{bubbles:true}}));
      if (!selected.checked || !selected.closest('label').hidden) return false;
      read.querySelector('.scope-clear').click();
      if (selected.checked || getComputedStyle(selected, '::before').visibility !== 'hidden')
        return false;
      if (read.querySelector('.scope-summary').textContent.includes('Alpha')) return false;
      filter.value = ''; filter.dispatchEvent(new Event('input', {{bubbles:true}}));
      selected.click();
      read.querySelector('input[value=include]').click();
      if (!selected.checked
        || !read.querySelector('.scope-help').textContent.includes({json.dumps(allowed_help)})
        || !getComputedStyle(selected, '::before').content.includes('✓'))
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
      if (!getComputedStyle(read.querySelector('input[value="{first}"]'), '::before')
        .content.includes('×')
        || !getComputedStyle(write.querySelector('input[value="{second}"]'), '::before')
        .content.includes('✓')) return false;
      write.querySelector('input[value=exclude]').click();
      if (!getComputedStyle(write.querySelector('input[value="{second}"]'), '::before')
        .content.includes('×')) return false;
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
