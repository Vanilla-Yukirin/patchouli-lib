"""Native first-setup forms in an isolated real browser and migrated loopback app."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from test_browser_login import _available_port, _browser_executable, _DevTools, _page_target

from patchouli_lib.admin.master_setup_session import MasterSetupSessionCodec
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller, Credential, MasterAuditEvent, MasterIdentity
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.library.models import Library

_PROOF = "synthetic isolated browser setup proof 0001"
_LEGACY_PASSWORD = "synthetic isolated browser legacy password"
# Eleven characters are 33 UTF-8 bytes: native forms must use the server's byte contract.
_MASTER_TOKEN = "测" * 11
_SIGNING = "s" * 32
_SETUP_PATH = "/admin/master/setup"
_HOME_READY = (
    "location.pathname === \"/admin\" && document.querySelector('form[action$=logout]') !== null"
)


@dataclass(frozen=True)
class _Browser:
    tools: _DevTools
    engine: Engine
    origin: str


def _wait(tools: _DevTools, expression: str, *, description: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if tools.evaluate(expression):
                return
        except TimeoutError:
            pass
        time.sleep(0.05)
    pytest.fail(f"The isolated browser did not finish {description}.")


def _navigate(browser: _Browser, path: str, ready: str) -> None:
    assert path.startswith("/admin") and not path.startswith("//")
    # A redirect can return to the same setup path; require a new document, not the old DOM.
    assert browser.tools.evaluate("window.__setupNavigationPending = true; true")
    browser.tools.command("Page.navigate", {"url": browser.origin + path})
    _wait(
        browser.tools,
        f"window.__setupNavigationPending !== true && ({ready})",
        description="the local navigation",
    )


def _cookies(browser: _Browser) -> dict[str, dict[str, Any]]:
    result = browser.tools.command("Network.getCookies", {"urls": [browser.origin + _SETUP_PATH]})
    return {cookie["name"]: cookie for cookie in result["cookies"]}


def _assert_no_secret_storage(browser: _Browser) -> None:
    assert browser.tools.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    html = browser.tools.evaluate("document.documentElement.outerHTML")
    assert isinstance(html, str)
    assert all(value not in html for value in (_PROOF, _MASTER_TOKEN, _LEGACY_PASSWORD))
    cookies = _cookies(browser)
    assert all(
        secret not in str(cookie["value"])
        for cookie in cookies.values()
        for secret in (_PROOF, _MASTER_TOKEN, _LEGACY_PASSWORD)
    )


def _assert_no_store(browser: _Browser, path: str) -> None:
    responses = []
    for event in browser.tools.events:
        params = event.get("params", {})
        if event.get("method") == "Network.responseReceived":
            responses.append(params.get("response", {}))
        elif event.get("method") == "Network.requestWillBeSent" and "redirectResponse" in params:
            responses.append(params["redirectResponse"])
    matching = [response for response in responses if response.get("url") == browser.origin + path]
    assert matching, "The browser did not observe the expected local response."
    for response in matching:
        headers = {name.lower(): str(value) for name, value in response["headers"].items()}
        assert "no-store" in headers["cache-control"]


def _submit_setup(browser: _Browser, *, proof: bool) -> None:
    assert browser.tools.evaluate(
        f"""(() => {{
          const form = document.querySelector('form[action="{_SETUP_PATH}"]');
          if (!form) return false;
          form.querySelector('#master_token').value = {json.dumps(_MASTER_TOKEN)};
          form.querySelector('#confirmation').value = {json.dumps(_MASTER_TOKEN)};
          const proof = form.querySelector('#setup_proof');
          if ({json.dumps(proof)} !== (proof !== null)) return false;
          if (proof) proof.value = {json.dumps(_PROOF)};
          if (!form.checkValidity()) return false;
          form.requestSubmit();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        _HOME_READY,
        description="the native first-setup submission",
    )


def _login(browser: _Browser, password: str, *, accepted: bool = True) -> None:
    assert browser.tools.evaluate(
        f"""(() => {{
          const form = document.querySelector('form[action="/admin/login"]');
          if (!form) return false;
          form.querySelector('#password').value = {json.dumps(password)};
          if (!form.checkValidity()) return false;
          form.requestSubmit();
          return true;
        }})()"""
    )
    expression = (
        _HOME_READY
        if accepted
        else (
            'location.pathname === "/admin/login" && '
            'document.querySelector(".notice.error")?.textContent === "Invalid password."'
        )
    )
    _wait(browser.tools, expression, description="the native login submission")


@pytest.fixture
def setup_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[_Browser]:
    mode = request.param
    assert mode in {"proof", "legacy"}
    executable = _browser_executable()
    database_url = f"sqlite:///{(tmp_path / 'master-setup-browser.sqlite').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    application = create_app(
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": database_url,
                "admin_allow_private_http": True,
                "retrieval_cursor_signing_secret": "r" * 32,
                "admin_session_signing_secret": _SIGNING,
                "admin_session_ttl_seconds": 600,
                "admin_password_hash": hash_password(_LEGACY_PASSWORD)
                if mode == "legacy"
                else None,
                "admin_setup_token": _PROOF if mode == "proof" else None,
            }
        )
    )
    engine = application.state.engine
    port = _available_port()
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = Thread(target=server.run, daemon=True)
    process: subprocess.Popen[bytes] | None = None
    tools: _DevTools | None = None
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "The isolated loopback application did not start."
        debug_port = _available_port()
        stderr_path = tmp_path / "browser-stderr.log"
        with stderr_path.open("wb") as stderr_output:
            process = subprocess.Popen(
                [
                    executable,
                    "--headless=new",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--no-first-run",
                    "--no-sandbox",
                    "--no-proxy-server",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                    f"--remote-debugging-port={debug_port}",
                    f"--user-data-dir={tmp_path / 'browser-profile'}",
                    f"{origin}/admin/login?lang=en",
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr_output,
            )
        target = _page_target(debug_port, origin, process, stderr_path)
        tools = _DevTools(str(target["webSocketDebuggerUrl"]))
        tools.command("Network.enable")
        tools.command("Runtime.enable")
        ready = (
            'document.querySelector("#setup_proof") !== null'
            if mode == "proof"
            else 'document.querySelector("#password") !== null'
        )
        _wait(tools, ready, description="the initial local form")
        yield _Browser(tools, engine, origin)
    finally:
        if tools is not None:
            tools.close()
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        engine.dispose()
        assert not thread.is_alive(), "The isolated loopback application did not stop."


def _assert_single_identity(browser: _Browser) -> None:
    with browser.engine.connect() as connection:
        assert MasterTokenRepository(connection).authenticate(_MASTER_TOKEN) is not None
        assert connection.scalar(select(func.count()).select_from(MasterIdentity)) == 1
        events = connection.execute(select(MasterAuditEvent.__table__)).mappings().all()
        assert len(events) == 1 and events[0]["action"] == "auth.master.initialize"
        assert all(
            secret not in repr(events) for secret in (_PROOF, _MASTER_TOKEN, _LEGACY_PASSWORD)
        )
        for model in (Caller, Credential, Library, Page, Revision):
            assert connection.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("setup_browser", ["proof"], indirect=True)
def test_real_browser_proof_setup_and_unicode_token_relogin(setup_browser: _Browser) -> None:
    browser = setup_browser
    cookie = _cookies(browser)["patchouli_master_setup_session"]
    assert cookie["httpOnly"] is True and cookie["sameSite"] == "Strict"
    assert cookie["path"] == _SETUP_PATH and cookie["secure"] is False
    setup_codec = MasterSetupSessionCodec(_SIGNING.encode())
    assert setup_codec.verify(str(cookie["value"])) is not None
    assert "patchouli_admin_session" not in _cookies(browser)
    _assert_no_secret_storage(browser)

    # Visiting a protected page does not turn the restricted setup cookie into admission.
    _navigate(
        browser,
        "/admin/libraries",
        f'location.pathname === "{_SETUP_PATH}" && document.querySelector("#setup_proof") !== null',
    )
    with browser.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MasterIdentity)) == 0
    _assert_no_store(browser, _SETUP_PATH)
    _submit_setup(browser, proof=True)
    _assert_single_identity(browser)
    assert "patchouli_master_setup_session" not in _cookies(browser)
    codec = AdminSessionCodec(_SIGNING.encode(), ttl_seconds=600)
    assert (
        codec.verify_master(str(_cookies(browser)["patchouli_admin_session"]["value"])) is not None
    )
    _assert_no_secret_storage(browser)

    assert browser.tools.evaluate("window.__setupNavigationPending = true; true")
    browser.tools.command("Page.reload")
    _wait(
        browser.tools,
        f"window.__setupNavigationPending !== true && ({_HOME_READY})",
        description="the authenticated page reload",
    )
    assert browser.tools.evaluate(
        "document.querySelector('form[action=\"/admin/logout\"]').requestSubmit(); true"
    )
    _wait(
        browser.tools,
        'location.pathname === "/admin/login" && document.querySelector("#password") !== null',
        description="the native logout",
    )
    assert "patchouli_admin_session" not in _cookies(browser)
    assert browser.tools.evaluate("document.querySelector('#password').value") == ""
    _login(browser, _MASTER_TOKEN)
    _assert_no_secret_storage(browser)
    _assert_no_store(browser, "/admin/login")
    _assert_single_identity(browser)


@pytest.mark.parametrize("setup_browser", ["legacy"], indirect=True)
def test_real_browser_legacy_setup_replaces_v1_admission(setup_browser: _Browser) -> None:
    browser = setup_browser
    _login(browser, _LEGACY_PASSWORD)
    codec = AdminSessionCodec(_SIGNING.encode(), ttl_seconds=600)
    old_cookie = str(_cookies(browser)["patchouli_admin_session"]["value"])
    assert codec.verify(old_cookie) is not None and codec.verify_master(old_cookie) is None
    _navigate(browser, "/admin/setup", "document.querySelector('a[href$=setup]') !== null")
    assert browser.tools.evaluate(
        "document.querySelector('a[href=\"/admin/master/setup\"]').click(); true"
    )
    _wait(
        browser.tools,
        f'location.pathname === "{_SETUP_PATH}" && '
        'document.querySelector("#master_token") !== null',
        description="the legacy-authorized setup form",
    )
    assert browser.tools.evaluate("document.querySelector('#setup_proof') === null")
    assert "patchouli_master_setup_session" not in _cookies(browser)
    _submit_setup(browser, proof=False)
    current_cookie = str(_cookies(browser)["patchouli_admin_session"]["value"])
    assert codec.verify_master(current_cookie) is not None and codec.verify(current_cookie) is None
    _assert_single_identity(browser)
    _assert_no_secret_storage(browser)

    # Replaying the genuine pre-setup cookie must not regain access after initialization.
    browser.tools.command("Network.clearBrowserCookies")
    assert browser.tools.command(
        "Network.setCookie",
        {
            "name": "patchouli_admin_session",
            "value": old_cookie,
            "url": browser.origin + "/admin",
            "path": "/admin",
            "httpOnly": True,
            "sameSite": "Strict",
            "secure": False,
        },
    )["success"]
    _navigate(
        browser,
        "/admin",
        'location.pathname === "/admin/login" && document.querySelector("#password") !== null',
    )
    _login(browser, _LEGACY_PASSWORD, accepted=False)
    _login(browser, _MASTER_TOKEN)
    _assert_single_identity(browser)
    _assert_no_secret_storage(browser)
    _assert_no_store(browser, _SETUP_PATH)
