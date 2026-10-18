"""Security regression for the Python HTTP example shipped with the Agent Skill."""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest


@pytest.mark.parametrize("preconfigured_base", [True, False])
def test_packaged_python_example_never_forwards_token_to_redirect(
    monkeypatch: pytest.MonkeyPatch,
    preconfigured_base: bool,
) -> None:
    reference = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "patchouli-agent"
        / "references"
        / "http.md"
    ).read_text(encoding="utf-8")
    match = re.search(r"## 本机 Python 标准库示例.*?```python\n(.*?)\n```", reference, re.S)
    assert match is not None

    forwarded: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            forwarded.append(self.headers.get("Authorization"))
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        base = f"http://127.0.0.1:{redirect.server_port}"
        if preconfigured_base:
            monkeypatch.setenv("PATCHOULI_BASE_URL", base)
        else:
            monkeypatch.delenv("PATCHOULI_BASE_URL", raising=False)
            monkeypatch.setattr("builtins.input", lambda _prompt: base)
        monkeypatch.setenv("PATCHOULI_TOKEN", "synthetic-test-token")
        with pytest.raises(HTTPError) as error:
            exec(compile(match.group(1), "http.md example", "exec"), {"__name__": "__main__"})
        assert error.value.code == 302
        assert forwarded == []
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
