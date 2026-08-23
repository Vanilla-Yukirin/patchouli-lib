from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

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
    assert headers["referrer-policy"] == "no-referrer"
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
