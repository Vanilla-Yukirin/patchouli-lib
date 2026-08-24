from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from starlette.concurrency import run_in_threadpool as starlette_run_in_threadpool

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind
from patchouli_lib.auth.service import AuthenticationError, AuthenticationService
from patchouli_lib.config import Settings

_ORIGIN = "https://admin.example.invalid"
_ADMIN_PASSWORD = "synthetic admin password"
_ADMIN_PASSWORD_HASH = hash_password(
    _ADMIN_PASSWORD,
    salt_factory=lambda size: b"s" * size,
    iterations=300_000,
)
_SESSION_COOKIE = "patchouli_admin_session"
_LOCALE_COOKIE = "patchouli_admin_locale"


@dataclass(frozen=True)
class AdminWeb:
    client: TestClient
    engine: Engine


@pytest.fixture
def admin_web(tmp_path: Path) -> Iterator[AdminWeb]:
    database_path = (tmp_path / "admin-web.db").as_posix()
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{database_path}",
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": _ORIGIN,
            "admin_session_ttl_seconds": 600,
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)
    with TestClient(
        application,
        base_url=_ORIGIN,
        follow_redirects=False,
    ) as client:
        yield AdminWeb(client=client, engine=application.state.engine)


def _post(
    client: TestClient,
    path: str,
    *,
    data: Any,
    origin: str = _ORIGIN,
) -> Any:
    return client.post(path, data=data, headers={"Origin": origin})


def _assert_security_headers(response: Any) -> None:
    headers = response.headers
    assert headers["cache-control"] == "no-store, max-age=0"
    assert headers["content-security-policy"].startswith("default-src 'none'")
    assert headers["referrer-policy"] == "same-origin"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def _login(web: AdminWeb) -> str:
    response = _post(
        web.client,
        "/admin/login",
        data={"password": _ADMIN_PASSWORD},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/admin" in cookie
    assert _ADMIN_PASSWORD not in cookie
    _assert_security_headers(response)

    dashboard = web.client.get("/admin")
    assert dashboard.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text)
    assert match is not None
    return match.group(1)


def _bootstrap_data(csrf_token: str) -> dict[str, str]:
    return {
        "csrf_token": csrf_token,
        "library_name": "Synthetic Web Library",
        "section_name": "Synthetic Web Section",
        "section_description": "Synthetic Section",
        "book_name": "Synthetic Web Book",
        "book_summary": "Synthetic Book",
        "operator_name": "Synthetic Web Operator",
        "operator_description": "Synthetic Operator",
        "credential_ttl_seconds": "3600",
    }


def _credential_from(response_text: str) -> str:
    match = re.search(r'<code class="secret">(plb1\.[^<]+)</code>', response_text)
    assert match is not None
    return match.group(1)


def _metadata_from(response_text: str) -> tuple[str, str, str]:
    values = re.findall(r"<dd>([^<]+)</dd>", response_text)
    assert len(values) == 3
    return values[0], values[1], values[2]


def test_admin_routes_are_absent_when_configuration_is_disabled(client: TestClient) -> None:
    response = client.get("/admin")

    assert response.status_code == 404


def test_canonical_browser_origin_works_for_noncanonical_configuration(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "canonical-origin.db").as_posix()
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{database_path}",
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": "HTTPS://Admin.Example.Invalid:443/",
        }
    )
    application = create_app(settings)

    with TestClient(application, base_url=_ORIGIN, follow_redirects=False) as client:
        response = _post(
            client,
            "/admin/login",
            data={"password": _ADMIN_PASSWORD},
        )

    assert settings.admin_origin == _ORIGIN
    assert response.status_code == 303


def test_login_fails_closed_for_wrong_host_origin_password_and_form_shape(
    admin_web: AdminWeb,
) -> None:
    wrong_host = admin_web.client.get("/admin/login", headers={"Host": "wrong.example.invalid"})
    wrong_origin = _post(
        admin_web.client,
        "/admin/login",
        data={"password": _ADMIN_PASSWORD},
        origin="https://wrong.example.invalid",
    )
    wrong_password = _post(
        admin_web.client,
        "/admin/login",
        data={"password": "incorrect synthetic password"},
    )
    duplicate = admin_web.client.post(
        "/admin/login",
        content="password=first&password=second",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": _ORIGIN,
        },
    )
    unknown = _post(
        admin_web.client,
        "/admin/login",
        data={"password": _ADMIN_PASSWORD, "token": "must-not-be-accepted"},
    )
    wrong_media = admin_web.client.post(
        "/admin/login",
        json={"password": _ADMIN_PASSWORD},
        headers={"Origin": _ORIGIN},
    )

    assert wrong_host.status_code == 403
    assert wrong_origin.status_code == 403
    assert wrong_password.status_code == 401
    assert duplicate.status_code == 422
    assert unknown.status_code == 422
    assert wrong_media.status_code == 415
    for response in (
        wrong_host,
        wrong_origin,
        wrong_password,
        duplicate,
        unknown,
        wrong_media,
    ):
        assert _ADMIN_PASSWORD not in response.text
        assert _SESSION_COOKIE not in response.cookies
        _assert_security_headers(response)


def test_language_switch_is_scoped_persistent_and_localizes_errors(
    admin_web: AdminWeb,
) -> None:
    chinese = admin_web.client.get("/admin/login?lang=zh-CN")

    assert chinese.status_code == 200
    assert chinese.headers["content-language"] == "zh-CN"
    assert '<html lang="zh-CN">' in chinese.text
    assert "PatchouliLib 管理面板" in chinese.text
    assert "管理密码" in chinese.text
    assert 'href="/admin/login?lang=en"' in chinese.text
    assert 'lang="zh-CN" aria-current="page"' in chinese.text
    locale_cookie = chinese.headers["set-cookie"]
    assert f"{_LOCALE_COOKIE}=zh-CN" in locale_cookie
    assert "HttpOnly" in locale_cookie
    assert "Secure" in locale_cookie
    assert "SameSite=strict" in locale_cookie
    assert "Path=/admin" in locale_cookie
    assert _ADMIN_PASSWORD not in locale_cookie

    wrong_password = _post(
        admin_web.client,
        "/admin/login",
        data={"password": "incorrect synthetic password"},
    )
    assert wrong_password.status_code == 401
    assert wrong_password.headers["content-language"] == "zh-CN"
    assert "密码不正确。" in wrong_password.text

    invalid_choice = admin_web.client.get("/admin/login?lang=not-a-locale")
    assert invalid_choice.status_code == 200
    assert invalid_choice.headers["content-language"] == "zh-CN"
    assert "not-a-locale" not in invalid_choice.text
    assert _LOCALE_COOKIE not in invalid_choice.headers.get("set-cookie", "")

    english = admin_web.client.get("/admin/login?lang=en")
    assert english.status_code == 200
    assert english.headers["content-language"] == "en"
    assert '<html lang="en">' in english.text
    assert "Administration password" in english.text
    assert f"{_LOCALE_COOKIE}=en" in english.headers["set-cookie"]


def test_chinese_language_persists_across_dashboard_guides_and_form_errors(
    admin_web: AdminWeb,
) -> None:
    switched = admin_web.client.get("/admin/login?lang=zh-CN")
    assert switched.status_code == 200

    csrf = _login(admin_web)
    dashboard = admin_web.client.get("/admin")
    guide = admin_web.client.get("/admin/guide")
    agent = admin_web.client.get("/admin/agent")
    mcp = admin_web.client.get("/admin/mcp")
    invalid_form = _post(
        admin_web.client,
        "/admin/bootstrap",
        data={**_bootstrap_data(csrf), "unknown": "must-not-be-accepted"},
    )
    initialized = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data(csrf),
    )

    assert dashboard.headers["content-language"] == "zh-CN"
    assert "初始化知识库" in dashboard.text
    assert "当前管理员凭据" in dashboard.text
    assert "退出登录" in dashboard.text
    assert "管理员指南" in guide.text
    assert "恢复管理员凭据会使此前仍有效的管理员凭据失效" in guide.text
    assert "Agent 使用说明" in agent.text
    assert "MCP 配置" in mcp.text
    assert "提交的表单包含未知字段。" in invalid_form.text
    assert 'href="/admin?lang=en"' in invalid_form.text
    assert "/admin/bootstrap?lang=" not in invalid_form.text
    assert initialized.status_code == 200
    assert "知识库已初始化" in initialized.text
    assert "此值仅在本次响应中显示" in initialized.text
    assert "知识库 ID" in initialized.text
    assert "调用方 ID" in initialized.text
    assert "凭据 ID" in initialized.text
    assert 'class="language-switch"' not in initialized.text
    assert "/admin/bootstrap?lang=" not in initialized.text
    for response in (dashboard, guide, agent, mcp, invalid_form, initialized):
        _assert_security_headers(response)


def test_login_session_protected_guides_and_logout(admin_web: AdminWeb) -> None:
    unauthenticated = admin_web.client.get("/admin")
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/admin/login"
    sign_in = admin_web.client.get("/admin/login")
    assert "run host commands" in sign_in.text

    csrf = _login(admin_web)
    dashboard = admin_web.client.get("/admin")
    assert "Initialize the library" in dashboard.text
    assert _ADMIN_PASSWORD not in dashboard.text

    guide = admin_web.client.get("/admin/guide")
    agent = admin_web.client.get("/admin/agent")
    mcp = admin_web.client.get("/admin/mcp")
    stylesheet = admin_web.client.get("/admin/style.css")
    assert "no image update" in guide.text
    assert "patchouli capabilities" in agent.text
    assert "patchouli-mcp" in mcp.text
    assert stylesheet.headers["content-type"].startswith("text/css")
    for response in (dashboard, guide, agent, mcp, stylesheet):
        _assert_security_headers(response)

    rejected = _post(
        admin_web.client,
        "/admin/logout",
        data={"csrf_token": "wrong"},
    )
    assert rejected.status_code == 403

    logout = _post(
        admin_web.client,
        "/admin/logout",
        data={"csrf_token": csrf},
    )
    assert logout.status_code == 303
    assert logout.headers["location"] == "/admin/login"
    assert "Max-Age=0" in logout.headers["set-cookie"]


def test_tampered_session_and_oversized_form_are_rejected(admin_web: AdminWeb) -> None:
    _login(admin_web)
    admin_web.client.cookies.set(
        _SESSION_COOKIE,
        "tampered.value",
        domain="admin.example.invalid",
        path="/admin",
    )

    tampered = admin_web.client.get("/admin")
    assert tampered.status_code == 303
    assert tampered.headers["location"] == "/admin/login"

    oversized = admin_web.client.post(
        "/admin/login",
        content="password=" + ("x" * 16_385),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": _ORIGIN,
        },
    )
    assert oversized.status_code == 413


def test_oversized_form_chunk_is_rejected_before_buffer_growth() -> None:
    body = bytearray(b"existing")

    with pytest.raises(ValueError, match="form is too large"):
        admin_router._extend_form_body(body, b"x" * 16_384)

    assert body == b"existing"


def test_csrf_rejection_happens_before_bootstrap(admin_web: AdminWeb) -> None:
    _login(admin_web)

    response = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data("wrong"),
    )

    assert response.status_code == 403
    with admin_web.engine.connect() as connection:
        assert connection.execute(select(Caller.id)).first() is None


def test_non_ascii_csrf_fails_closed_for_logout_and_actions(admin_web: AdminWeb) -> None:
    _login(admin_web)

    logout = _post(
        admin_web.client,
        "/admin/logout",
        data={"csrf_token": "界"},
    )
    bootstrap = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data("界"),
    )

    assert logout.status_code == 403
    assert bootstrap.status_code == 403
    with admin_web.engine.connect() as connection:
        assert connection.execute(select(Caller.id)).first() is None


def test_admin_actions_use_the_worker_thread_boundary(
    admin_web: AdminWeb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = _login(admin_web)
    calls: list[Any] = []

    async def record_threadpool_call(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        calls.append(function)
        return await starlette_run_in_threadpool(function, *args, **kwargs)

    monkeypatch.setattr(admin_router, "run_in_threadpool", record_threadpool_call)

    response = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data(csrf),
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_admin_actions_require_session_and_exact_origin(admin_web: AdminWeb) -> None:
    unauthenticated = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data("synthetic-csrf"),
    )
    assert unauthenticated.status_code == 401

    csrf = _login(admin_web)
    wrong_origin = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data(csrf),
        origin="https://wrong.example.invalid",
    )
    assert wrong_origin.status_code == 403

    with admin_web.engine.connect() as connection:
        assert connection.execute(select(Caller.id)).first() is None


def test_bootstrap_recovery_provision_and_revoke_without_secret_retention(
    admin_web: AdminWeb,
) -> None:
    csrf = _login(admin_web)

    bootstrapped = _post(
        admin_web.client,
        "/admin/bootstrap",
        data=_bootstrap_data(csrf),
    )
    assert bootstrapped.status_code == 200
    operator_token = _credential_from(bootstrapped.text)
    library_id, operator_id, operator_credential_id = _metadata_from(bootstrapped.text)
    assert operator_token not in str(bootstrapped.request.url)
    session_cookie = admin_web.client.cookies.get(_SESSION_COOKIE) or ""
    assert operator_token not in session_cookie
    assert _ADMIN_PASSWORD not in bootstrapped.text
    _assert_security_headers(bootstrapped)

    recovered = _post(
        admin_web.client,
        "/admin/recover",
        data={
            "csrf_token": csrf,
            "library_name": "Synthetic Web Library",
            "credential_ttl_seconds": "3600",
        },
    )
    assert recovered.status_code == 200
    recovered_token = _credential_from(recovered.text)
    recovered_library, recovered_operator, recovered_credential = _metadata_from(recovered.text)
    assert recovered_library == library_id
    assert recovered_operator == operator_id
    assert recovered_credential != operator_credential_id

    with admin_web.engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(AuthRepository(connection)).authenticate(operator_token)

    provisioned = _post(
        admin_web.client,
        "/admin/agents/provision",
        data={
            "csrf_token": csrf,
            "operator_token": recovered_token,
            "library_name": "Synthetic Web Library",
            "section_name": "Synthetic Web Section",
            "agent_name": "Synthetic Web Agent",
            "agent_description": "Synthetic Agent",
            "credential_ttl_seconds": "3600",
            "grants": ["section:query", "page:read"],
        },
    )
    assert provisioned.status_code == 200
    agent_token = _credential_from(provisioned.text)
    _, agent_id, agent_credential_id = _metadata_from(provisioned.text)
    assert recovered_token not in provisioned.text
    assert agent_token not in str(provisioned.request.url)
    session_cookie = admin_web.client.cookies.get(_SESSION_COOKIE) or ""
    assert agent_token not in session_cookie

    with admin_web.engine.connect() as connection:
        agent = AuthenticationService(AuthRepository(connection)).authenticate(agent_token)
    assert agent.caller.kind is CallerKind.AGENT

    revoked = _post(
        admin_web.client,
        "/admin/agents/revoke",
        data={
            "csrf_token": csrf,
            "operator_token": recovered_token,
            "library_name": "Synthetic Web Library",
            "caller_id": agent_id,
            "credential_id": agent_credential_id,
        },
    )
    assert revoked.status_code == 200
    assert "no longer active" in revoked.text
    assert recovered_token not in revoked.text

    with admin_web.engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(AuthRepository(connection)).authenticate(agent_token)


def test_action_errors_are_redacted_and_do_not_echo_operator_token(
    admin_web: AdminWeb,
) -> None:
    csrf = _login(admin_web)
    synthetic_token = "plb1.synthetic-private-value"

    response = _post(
        admin_web.client,
        "/admin/agents/provision",
        data={
            "csrf_token": csrf,
            "operator_token": synthetic_token,
            "library_name": "Missing Library",
            "section_name": "Missing Section",
            "agent_name": "Missing Agent",
            "credential_ttl_seconds": "3600",
            "grants": "section:query",
        },
    )

    assert response.status_code == 404
    assert synthetic_token not in response.text
    assert "requested local resource was not found" in response.text
