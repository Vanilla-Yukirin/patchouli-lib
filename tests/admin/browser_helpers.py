from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import pytest
from websockets.sync.client import connect

_LOCAL_HTTP = build_opener(ProxyHandler({}))


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


def _cdp_error(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return f"{type(error).__name__} ({type(error.reason).__name__})"
    return type(error).__name__


def _target_locations(targets: Any, origin: str) -> list[str]:
    if not isinstance(targets, list):
        return ["<invalid target list>"]
    locations = []
    expected_origin = urlsplit(origin)
    for target in targets[:5]:
        if not isinstance(target, dict):
            locations.append("<invalid target>")
            continue
        url = str(target.get("url", ""))
        try:
            parsed = urlsplit(url)
        except ValueError:
            locations.append("<invalid URL>")
            continue
        if parsed.scheme == expected_origin.scheme and parsed.netloc == expected_origin.netloc:
            locations.append("/admin" if parsed.path == "/admin" else "<other local path>")
        elif url == "about:blank":
            locations.append("about:blank")
        else:
            locations.append("<other origin>")
    return locations


def _browser_stderr_signals(path: Path) -> str:
    try:
        with path.open("rb") as output:
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 16_384))
            tail = output.read().decode("utf-8", errors="replace").casefold()
    except OSError:
        return "<unavailable>"
    if not tail:
        return "<empty>"
    known_signals = (
        ("devtools listening on", "devtools-listening"),
        ("address already in use", "port-in-use"),
        ("failed to create a processsingleton", "profile-start-failed"),
        ("no usable sandbox", "sandbox-unavailable"),
        ("error while loading shared libraries", "missing-shared-library"),
        ("failed to move to new namespace", "namespace-unavailable"),
        ("segmentation fault", "browser-crashed"),
    )
    signals = [label for marker, label in known_signals if marker in tail]
    return ", ".join(signals) if signals else "<unrecognized output>"


def _admin_probe(origin: str) -> str:
    try:
        with _LOCAL_HTTP.open(f"{origin}/admin", timeout=2) as response:
            return f"HTTP {response.status}"
    except OSError as error:
        return _cdp_error(error)


def _page_target(
    debug_port: int, origin: str, process: subprocess.Popen[bytes], stderr_path: Path
) -> dict[str, Any]:
    # Hosted runners can start Chrome before its CDP HTTP endpoint responds.
    deadline = time.monotonic() + 30
    last_error = "no response"
    last_targets: list[str] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with _LOCAL_HTTP.open(
                f"http://127.0.0.1:{debug_port}/json/list", timeout=1
            ) as response:
                targets = json.load(response)
        except (OSError, ValueError) as error:
            last_error = _cdp_error(error)
            time.sleep(0.05)
            continue
        last_error = "responded"
        last_targets = _target_locations(targets, origin)
        for target in targets if isinstance(targets, list) else []:
            if (
                isinstance(target, dict)
                and target.get("type") == "page"
                and str(target.get("url", "")).startswith(origin)
            ):
                return dict(target)
        time.sleep(0.05)
    exit_status = process.poll()
    pytest.fail(
        "The browser did not open the admin page. "
        f"Chrome: {'running' if exit_status is None else f'exited ({exit_status})'}; "
        f"admin: {_admin_probe(origin)}; CDP: {last_error}; "
        f"targets: {last_targets}; stderr signals: {_browser_stderr_signals(stderr_path)}"
    )


class _DevTools:
    def __init__(self, websocket_url: str) -> None:
        self.connection = connect(websocket_url, open_timeout=5, proxy=None)
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
