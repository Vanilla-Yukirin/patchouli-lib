from __future__ import annotations

import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_e2e  # noqa: E402


class _FakeProcess:
    def __init__(
        self,
        *,
        terminate_times_out: bool = False,
        kill_fails: bool = False,
        final_wait_times_out: bool = False,
    ) -> None:
        self.running = True
        self.terminate_times_out = terminate_times_out
        self.kill_fails = kill_fails
        self.final_wait_times_out = final_wait_times_out
        self.events: list[str] = []

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")
        if self.kill_fails:
            raise OSError("private kill detail")
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.events.append("wait")
        if self.terminate_times_out and "kill" not in self.events:
            raise subprocess.TimeoutExpired("synthetic", 1)
        if self.final_wait_times_out and "kill" in self.events:
            raise subprocess.TimeoutExpired("private final wait detail", 1)
        self.running = False
        return 0


class _ReadyResponse:
    status = 200

    def __enter__(self) -> _ReadyResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _ReadyOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, url: str, *, timeout: int) -> _ReadyResponse:
        assert timeout == 2
        self.urls.append(url)
        return _ReadyResponse()


def test_clean_environment_removes_inherited_application_and_network_state() -> None:
    cleaned = agent_e2e._clean_environment(
        {
            "PATH": "synthetic-path",
            "PATCHOULI_TOKEN": "private-token",
            "PATCHOULI_ENDPOINT": "https://private.invalid",
            "PYTHONPATH": "private-path",
            "VIRTUAL_ENV": "private-environment",
            "HTTPS_PROXY": "https://private-proxy.invalid",
            "SSL_CERT_FILE": "private-ca",
        },
        loopback_only=True,
    )

    assert cleaned == {
        "PATH": "synthetic-path",
        "NO_PROXY": "localhost,127.0.0.1",
    }


def test_command_failure_does_not_render_command_input_or_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "plb1.synthetic-secret"

    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(["synthetic", secret], 1, secret, secret)

    monkeypatch.setattr(subprocess, "run", failed)

    with pytest.raises(agent_e2e.E2EFailure) as captured:
        agent_e2e._run_checked(
            ("synthetic", secret),
            cwd=tmp_path,
            environment={},
            input_text=secret,
            step="Synthetic command",
        )

    assert str(captured.value) == "Synthetic command failed."
    assert secret not in str(captured.value)


def test_invalid_json_failure_is_redacted() -> None:
    secret = "plb1.synthetic-secret"

    with pytest.raises(agent_e2e.E2EFailure) as captured:
        agent_e2e._parse_json(f"not-json-{secret}", step="Synthetic response")

    assert str(captured.value) == "Synthetic response returned an invalid response."
    assert secret not in str(captured.value)


def test_main_redacts_unexpected_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "plb1.synthetic-secret"

    def unexpected() -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(agent_e2e, "run", unexpected)

    assert agent_e2e.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("Packaged Agent E2E failed closed without exposing internal details.\n")
    assert secret not in captured.err


def test_operation_recovery_uses_only_one_valid_journal_filename(tmp_path: Path) -> None:
    profile = tmp_path / "default"
    profile.mkdir()
    operation_id = "12345678-1234-4123-8123-123456789abc"
    (profile / f"{operation_id}.json").write_text("private journal", encoding="utf-8")

    assert agent_e2e._operation_id_from_state(tmp_path) == operation_id

    (profile / "87654321-4321-4321-8321-cba987654321.json").write_text(
        "private journal",
        encoding="utf-8",
    )
    with pytest.raises(agent_e2e.E2EFailure):
        agent_e2e._operation_id_from_state(tmp_path)


def test_process_cleanup_terminates_then_kills_after_timeout() -> None:
    graceful = _FakeProcess()
    agent_e2e._stop_process(graceful)
    assert graceful.events == ["terminate", "wait"]

    forced = _FakeProcess(terminate_times_out=True)
    agent_e2e._stop_process(forced)
    assert forced.events == ["terminate", "wait", "kill", "wait"]


@pytest.mark.parametrize("failure", ["kill", "final-wait"])
def test_process_cleanup_exhaustion_fails_closed_with_redacted_error(failure: str) -> None:
    process = _FakeProcess(
        terminate_times_out=True,
        kill_fails=failure == "kill",
        final_wait_times_out=failure == "final-wait",
    )

    with pytest.raises(agent_e2e.E2EFailure) as captured:
        agent_e2e._stop_process(process)

    assert str(captured.value) == "Packaged server cleanup failed."
    assert "private" not in str(captured.value)
    expected = ["terminate", "wait", "kill"]
    if failure == "final-wait":
        expected.append("wait")
    assert process.events == expected


def test_cleanup_exhaustion_prevents_success_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def cleanup_failure() -> None:
        agent_e2e._stop_process(_FakeProcess(terminate_times_out=True, kill_fails=True))

    monkeypatch.setattr(agent_e2e, "run", cleanup_failure)

    assert agent_e2e.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("Packaged Agent E2E failed: Packaged server cleanup failed.\n")


def test_readiness_disables_inherited_proxies_and_uses_only_loopback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.invalid")
    opener = _ReadyOpener()
    handlers: list[object] = []

    def build_opener(*values: object) -> _ReadyOpener:
        handlers.extend(values)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: object())
    certificate = tmp_path / "synthetic-ca.crt"
    certificate.write_text("synthetic", encoding="utf-8")

    agent_e2e._wait_until_ready(
        "https://127.0.0.1:18443",
        ca_certificate=certificate,
        process=_FakeProcess(),
    )

    proxy_handlers = [value for value in handlers if isinstance(value, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert cast(Any, proxy_handlers[0]).proxies == {}
    assert opener.urls == ["https://127.0.0.1:18443/health/ready"]

    with pytest.raises(agent_e2e.E2EFailure) as captured:
        agent_e2e._wait_until_ready(
            "https://remote.example.invalid:18443",
            ca_certificate=certificate,
            process=_FakeProcess(),
        )
    assert str(captured.value) == ("Packaged server readiness endpoint was not loopback HTTPS.")
    assert opener.urls == ["https://127.0.0.1:18443/health/ready"]


def test_private_file_creation_is_exclusive(tmp_path: Path) -> None:
    target = tmp_path / "synthetic.txt"
    agent_e2e._write_private(target, "synthetic")
    assert target.read_text(encoding="utf-8") == "synthetic"

    with pytest.raises(FileExistsError):
        agent_e2e._write_private(target, "replacement")
