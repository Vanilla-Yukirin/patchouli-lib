"""Exercise the public AI-native copy flow in a real isolated browser."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from test_browser_login import (
    _available_port,
    _browser_executable,
    _DevTools,
    _live_admin,
    _page_target,
)


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


def _click(tools: _DevTools) -> None:
    position = tools.evaluate(
        """(() => {
          const button = document.querySelector('#copy-instruction');
          button.scrollIntoView({block: 'center'});
          const rect = button.getBoundingClientRect();
          const x = rect.left + rect.width / 2;
          const y = rect.top + rect.height / 2;
          return {x, y, hit: document.elementFromPoint(x, y)?.id};
        })()"""
    )
    assert isinstance(position, dict)
    assert position["hit"] == "copy-instruction"
    for event_type in ("mousePressed", "mouseReleased"):
        tools.command(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": position["x"],
                "y": position["y"],
                "button": "left",
                "clickCount": 1,
            },
        )


def test_real_browser_copies_credential_free_instruction_and_handles_failure(
    tmp_path: Path,
) -> None:
    executable = _browser_executable()
    debug_port = _available_port()
    synthetic_token = "synthetic-device-token-must-not-enter-clipboard"

    with _live_admin(tmp_path) as origin:
        stderr_path = tmp_path / "browser-stderr.log"
        with stderr_path.open("wb") as stderr_output:
            process = subprocess.Popen(
                [
                    executable,
                    "--headless=new",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-sandbox",
                    "--no-proxy-server",
                    f"--remote-debugging-port={debug_port}",
                    f"--user-data-dir={tmp_path / 'browser-profile'}",
                    f"{origin}/connect",
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr_output,
            )
        tools: _DevTools | None = None
        try:
            target = _page_target(debug_port, origin, process, stderr_path)
            tools = _DevTools(str(target["webSocketDebuggerUrl"]))
            tools.command("Runtime.enable")
            _wait(
                tools,
                "document.readyState === 'complete' && "
                f"document.querySelector('#instruction')?.value.includes({json.dumps(origin)}) && "
                "!document.querySelector('#instruction').value.includes('{{BASE_URL}}')",
                description="the credential-free connection instruction",
            )
            assert (
                tools.evaluate(
                    f"(window.__syntheticDeviceToken = {json.dumps(synthetic_token)}, true)"
                )
                is True
            )
            instruction = tools.evaluate("document.querySelector('#instruction').value")
            assert isinstance(instruction, str)
            assert instruction.startswith("请帮我将 PatchouliLib 接入当前本地 Agent 环境。")
            assert f"服务入口：{origin}" in instruction
            assert "/api/v1/agent/skill/manifest" in instruction
            assert synthetic_token not in instruction
            # Keep the browser interaction real without changing the user's OS clipboard.
            assert (
                tools.evaluate(
                    """(() => {
                      Object.defineProperty(navigator, 'clipboard', {
                        configurable: true,
                        value: {writeText: async value => {
                          window.__copiedInstruction = value;
                          window.__clipboardWriteCount =
                            (window.__clipboardWriteCount || 0) + 1;
                        }}
                      });
                      return true;
                    })()"""
                )
                is True
            )

            _click(tools)
            _wait(
                tools,
                "document.querySelector('#copy-status').textContent.length > 0",
                description="the clipboard result",
            )
            assert tools.evaluate("document.querySelector('#copy-status').textContent") == (
                "已复制无密钥接入指令。"
            )
            copied = tools.evaluate("window.__copiedInstruction")
            assert isinstance(copied, str)
            assert copied == instruction
            assert synthetic_token not in copied
            assert tools.evaluate("window.__clipboardWriteCount") == 1

            assert (
                tools.evaluate(
                    """(() => {
                  Object.defineProperty(navigator, 'clipboard', {
                    configurable: true,
                    value: {writeText: async () => { throw new Error('unavailable'); }}
                  });
                  return true;
                })()"""
                )
                is True
            )
            _click(tools)
            _wait(
                tools,
                "document.querySelector('#copy-status').textContent === "
                "'自动复制不可用，请手动复制已选中的文字。'",
                description="the manual-copy fallback",
            )
            selection = tools.evaluate(
                """(() => {
                  const field = document.querySelector('#instruction');
                  return {
                    start: field.selectionStart,
                    end: field.selectionEnd,
                    length: field.value.length,
                    active: document.activeElement === field
                  };
                })()"""
            )
            assert selection == {
                "start": 0,
                "end": len(instruction),
                "length": len(instruction),
                "active": True,
            }
        finally:
            if tools is not None:
                tools.close()
            process.terminate()
            process.wait(timeout=10)
