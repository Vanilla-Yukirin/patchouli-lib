"""Native first-setup forms in an isolated real browser and migrated loopback app."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from test_browser_login import _available_port, _browser_executable, _DevTools, _page_target

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings

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
