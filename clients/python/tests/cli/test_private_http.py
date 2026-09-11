from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest

from patchouli_cli.config import resolve_profile
from patchouli_cli.errors import CliError
from patchouli_cli.main import run
from patchouli_client import BearerToken, PatchouliClient, ProtocolError
from patchouli_client.origins import validate_origin
from patchouli_mcp.server import runtime_from_environment


@pytest.mark.parametrize(
    "origin",
    [
        "http://100.64.0.7:8080",
        "http://127.0.0.1",
        "http://192.168.3.4",
        "http://[fd00::1]",
    ],
)
def test_private_http_is_explicit(origin: str) -> None:
    with pytest.raises(ValueError):
        validate_origin(origin)
    assert validate_origin(origin, allow_private_http=True) == origin


def test_https_idn_remains_supported() -> None:
    assert validate_origin("https://bücher.example.invalid") == "https://bücher.example.invalid"


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example.invalid",
        "http://8.8.8.8",
        "http://0.0.0.0",
        "http://169.254.169.254",
        "http://[::]",
        "http://[fd00::1%25eth0]",
        "http://127.0.0.1:0",
        "http://127.0.0.1:bad",
        "http://[invalid",
        "http://@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?",
        "http://127.0.0.1#",
        " http://127.0.0.1",
        "http://127.0.0.1\n",
        "http://127.0.0.1\\example.invalid",
        "http://bücher.example.invalid",
    ],
)
def test_private_http_rejects_other_origins(origin: str) -> None:
    with pytest.raises(ValueError):
        validate_origin(origin, allow_private_http=True)


def test_private_http_does_not_follow_redirects_or_use_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(307, headers={"location": "https://other.example.invalid"})

    def transport(*args: Any, **kwargs: Any) -> httpx.MockTransport:
        assert kwargs.get("proxy") is None
        return httpx.MockTransport(handler)

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    # httpx 的默认 Client 使用内部 transport 引用。
    monkeypatch.setattr("httpx._client.HTTPTransport", transport)
    with (
        PatchouliClient("http://100.64.0.7:8080", allow_private_http=True) as client,
        pytest.raises(ProtocolError),
    ):
        client.whoami(token=BearerToken("synthetic-token"))
    assert len(observed) == 1
    assert str(observed[0].url) == "http://100.64.0.7:8080/api/v1/auth/whoami"
    assert observed[0].headers["authorization"] == "Bearer synthetic-token"


@pytest.mark.parametrize("value", ["yes", "1", "anything"])
def test_cli_http_environment_flag_is_strict(value: str) -> None:
    with pytest.raises(CliError, match="must be true or false"):
        resolve_profile(
            profile_name=None,
            config_path=None,
            environ={
                "PATCHOULI_ENDPOINT": "http://100.64.0.7:8080",
                "PATCHOULI_ALLOW_PRIVATE_HTTP": value,
            },
        )


def test_cli_and_mcp_pass_private_http_policy_to_real_client(
    monkeypatch: pytest.MonkeyPatch,
    trusted_tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401)

    def transport(*args: Any, **kwargs: Any) -> httpx.MockTransport:
        assert kwargs.get("proxy") is None
        return httpx.MockTransport(handler)

    monkeypatch.setattr("httpx._client.HTTPTransport", transport)
    environment = {
        "PATCHOULI_ENDPOINT": "http://100.64.0.7:8080",
        "PATCHOULI_ALLOW_PRIVATE_HTTP": "true",
        "PATCHOULI_TOKEN": "synthetic-token",
        "PATCHOULI_STATE_DIR": str(trusted_tmp_path / "state"),
    }
    output, errors = io.StringIO(), io.StringIO()
    run(["whoami"], environ=environment, stdout=output, stderr=errors)
    # 不执行工具；验证 MCP 正常创建 HTTP 客户端且最终关闭。
    with runtime_from_environment(environ=environment) as runtime:
        assert runtime.profile.allow_private_http
        with pytest.raises(ProtocolError):
            runtime.client.whoami(token=runtime.token)
    assert len(requests) == 2
    assert "synthetic-token" not in output.getvalue() + errors.getvalue()


def test_profile_http_opt_in_and_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    text = '[profiles.default]\nendpoint = "http://100.64.0.7:8080"\nallow_private_http = true\n'
    # 此处仅测试配置语义；文件所有权检查由独立的 secure_fs 测试覆盖。
    monkeypatch.setattr("patchouli_cli.config.read_trusted_file", lambda *a, **kw: text.encode())
    assert resolve_profile(profile_name=None, config_path=None, environ={}).allow_private_http
    with pytest.raises(CliError):
        resolve_profile(
            profile_name=None,
            config_path=None,
            environ={
                "PATCHOULI_ALLOW_PRIVATE_HTTP": "false",
            },
        )
    text = text.replace("= true", '= "true"')
    with pytest.raises(CliError, match="must be a boolean"):
        resolve_profile(profile_name=None, config_path=None, environ={})
