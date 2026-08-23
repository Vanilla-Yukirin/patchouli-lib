from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.request import urlopen

import pytest
import uvicorn
from websockets.sync.client import connect

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings

_ADMIN_PASSWORD = "synthetic browser password"
_ADMIN_PASSWORD_HASH = hash_password(
    _ADMIN_PASSWORD,
    salt_factory=lambda size: b"b" * size,
    iterations=300_000,
)


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _browser_executable() -> str:
    configured = os.environ.get("PATCHOULI_BROWSER_EXECUTABLE")
    if configured:
        return configured

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "msedge",
        "chrome",
    ):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    for path in (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ):
        if path.is_file():
            return str(path)

    if os.environ.get("CI"):
        pytest.fail("A Chrome-family browser is required for the admin browser test.")
    pytest.skip("No Chrome-family browser is installed.")


@contextmanager
def _live_admin(tmp_path: Path) -> Iterator[str]:
    port = _available_port()
    origin = f"http://127.0.0.1:{port}"
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'browser.db').as_posix()}",
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": origin,
            "admin_session_ttl_seconds": 600,
        }
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("The temporary admin server did not start.")

    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            pytest.fail("The temporary admin server did not stop.")


def _page_target(debug_port: int, origin: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as response:
                targets = json.load(response)
        except OSError:
            time.sleep(0.05)
            continue
        for target in targets:
            if target.get("type") == "page" and str(target.get("url", "")).startswith(origin):
                return dict(target)
        time.sleep(0.05)
    pytest.fail("The browser did not open the admin page.")


class _DevTools:
    def __init__(self, websocket_url: str) -> None:
        self.connection = connect(websocket_url, open_timeout=5)
        self.next_id = 1
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        self.connection.close()

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        command_id = self.next_id
        self.next_id += 1
        self.connection.send(
            json.dumps({"id": command_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self.connection.recv(timeout=5))
            if message.get("id") == command_id:
                if "error" in message:
                    pytest.fail(f"Browser command {method} failed without exposing page data.")
                return dict(message.get("result", {}))
            self.events.append(dict(message))

    def evaluate(self, expression: str) -> Any:
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        return result.get("result", {}).get("value")


def _observed_login_origin(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("method") != "Network.requestWillBeSent":
            continue
        request = event.get("params", {}).get("request", {})
        if request.get("method") != "POST" or not str(request.get("url", "")).endswith(
            "/admin/login"
        ):
            continue
        for name, value in request.get("headers", {}).items():
            if name.casefold() == "origin":
                return str(value)
    return None


def test_real_browser_can_submit_same_origin_login(tmp_path: Path) -> None:
    browser = _browser_executable()
    debug_port = _available_port()

    with _live_admin(tmp_path) as origin:
        process = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--no-first-run",
                "--no-sandbox",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={tmp_path / 'browser-profile'}",
                f"{origin}/admin/login",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        devtools: _DevTools | None = None
        try:
            target = _page_target(debug_port, origin)
            devtools = _DevTools(str(target["webSocketDebuggerUrl"]))
            devtools.command("Network.enable")
            devtools.command("Runtime.enable")
            deadline = time.monotonic() + 10
            form_ready = False
            while time.monotonic() < deadline:
                form_ready = bool(
                    devtools.evaluate("document.querySelector('input[name=\"password\"]') !== null")
                )
                if form_ready:
                    break
                time.sleep(0.05)
            assert form_ready is True

            password_literal = json.dumps(_ADMIN_PASSWORD)
            submitted = devtools.evaluate(
                f"""
                (() => {{
                  const field = document.querySelector('input[name="password"]');
                  const form = document.querySelector('form');
                  if (field === null || form === null) return false;
                  field.value = {password_literal};
                  form.requestSubmit();
                  return true;
                }})()
                """
            )
            assert submitted is True

            deadline = time.monotonic() + 10
            path = ""
            while time.monotonic() < deadline:
                try:
                    path = str(devtools.evaluate("window.location.pathname"))
                except TimeoutError:
                    time.sleep(0.05)
                    continue
                if path == "/admin":
                    break
                time.sleep(0.05)

            assert path == "/admin"
            assert _observed_login_origin(devtools.events) == origin
        finally:
            if devtools is not None:
                devtools.close()
            process.terminate()
            process.wait(timeout=10)
