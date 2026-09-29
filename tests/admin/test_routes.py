from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from starlette.concurrency import run_in_threadpool as starlette_run_in_threadpool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import (
    AgentTokenValue,
    Caller,
    Credential,
    MasterAuditEvent,
    MasterIdentity,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind
from patchouli_lib.auth.service import AuthenticationError, AuthenticationService, CredentialIssuer
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction

_ORIGIN = "https://admin.example.invalid"
_ADMIN_PASSWORD = "synthetic admin password"
_MASTER_TOKEN = "synthetic master token material old 0001"
_ROTATED_MASTER_TOKEN = "synthetic master token material new 0002"
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


def _initialize_master(web: AdminWeb) -> None:
    with immediate_transaction(web.engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)


def _login_master(web: AdminWeb) -> tuple[str, str]:
    response = _post(web.client, "/admin/login", data={"password": _MASTER_TOKEN})
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/admin" in cookie
    assert _MASTER_TOKEN not in cookie
    _assert_security_headers(response)
    encoded = web.client.cookies.get(_SESSION_COOKIE) or ""
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=600)
    session = codec.verify_master(encoded)
    assert session is not None
    assert session.identity_id == "a" * 32
    assert session.session_generation == 1
    assert codec.verify(encoded) is None
    return encoded, session.csrf_token


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


def _issue_web_agent(web: AdminWeb) -> tuple[str, str, str, str]:
    csrf = _login(web)
    bootstrapped = _post(web.client, "/admin/bootstrap", data=_bootstrap_data(csrf))
    assert bootstrapped.status_code == 200
    operator_token = _credential_from(bootstrapped.text)
    provisioned = _post(
        web.client,
        "/admin/agents/provision",
        data={
            "csrf_token": csrf,
            "operator_token": operator_token,
            "library_name": "Synthetic Web Library",
            "section_name": "Synthetic Web Section",
            "agent_name": "Synthetic Web Agent",
            "agent_description": "Synthetic Agent",
            "credential_ttl_seconds": "3600",
            "grants": ["section:query", "page:read"],
        },
    )
    assert provisioned.status_code == 200
    library_id, caller_id, credential_id = _metadata_from(provisioned.text)
    return _credential_from(provisioned.text), library_id, caller_id, credential_id


def test_production_private_http_preserves_login_csrf_and_token_boundaries(tmp_path: Path) -> None:
    origin = "http://100.64.0.7:8080"
    settings = Settings.model_validate(
        {
            "environment": "production",
            "database_url": f"sqlite:///{(tmp_path / 'private-http.db').as_posix()}",
            "retrieval_cursor_signing_secret": "r" * 32,
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_allow_private_http": True,
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)
    with TestClient(application, base_url=origin, follow_redirects=False) as client:
        assert client.get("/admin").status_code == 303
        assert (
            client.get("/admin/login", headers={"Host": "new.example.invalid"}).status_code == 200
        )
        for bad_origin in ("https://100.64.0.7:8080", "http://100.64.0.8:8080"):
            assert (
                _post(
                    client, "/admin/login", origin=bad_origin, data={"password": _ADMIN_PASSWORD}
                ).status_code
                == 403
            )
        assert (
            _post(client, "/admin/login", origin=origin, data={"password": "wrong"}).status_code
            == 401
        )
        signed_in = _post(client, "/admin/login", origin=origin, data={"password": _ADMIN_PASSWORD})
        assert signed_in.status_code == 303
        cookie = signed_in.headers["set-cookie"]
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert _ADMIN_PASSWORD not in cookie
        _assert_security_headers(signed_in)
        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text)
        assert csrf is not None
        assert (
            _post(
                client, "/admin/bootstrap", origin=origin, data=_bootstrap_data("wrong")
            ).status_code
            == 403
        )
        created = _post(
            client, "/admin/bootstrap", origin=origin, data=_bootstrap_data(csrf.group(1))
        )
        assert created.status_code == 200
        operator = _credential_from(created.text)
        # 管理页面会话不能替代 API Token；正确 Token 仍须通过既有身份验证。
        assert client.get("/api/v1/auth/whoami").status_code == 401
        assert (
            client.get(
                "/api/v1/auth/whoami", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {operator}"}
            ).status_code
            == 200
        )
        assert operator not in str(client.cookies)
        assert (
            _post(client, "/admin/logout", origin=origin, data={"csrf_token": "wrong"}).status_code
            == 403
        )
        assert (
            _post(
                client, "/admin/logout", origin=origin, data={"csrf_token": csrf.group(1)}
            ).status_code
            == 303
        )
        assert client.get("/admin").status_code == 303


def test_admin_routes_are_absent_when_configuration_is_disabled(client: TestClient) -> None:
    response = client.get("/admin")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "origin", ["https://admin.example.invalid", "https://other.example.invalid:8443"]
)
def test_multiple_entrypoints_work_without_origin_configuration(
    tmp_path: Path,
    origin: str,
) -> None:
    database_path = (tmp_path / "canonical-origin.db").as_posix()
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{database_path}",
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            # 旧设置不再限制访问，也不要求部署时先删除它。
            "admin_origin": "http://old.example.invalid:8080",
            "admin_allow_private_http": True,
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)

    with TestClient(application, base_url=origin, follow_redirects=False) as client:
        assert client.get("/admin/login").status_code == 200
        assert client.get("/admin/style.css").status_code == 200
        assert client.get("/admin").status_code == 303
        response = _post(
            client,
            "/admin/login",
            origin=origin,
            data={"password": _ADMIN_PASSWORD},
        )
        assert client.get("/admin").status_code == 200
        assert client.get("/api/v1/auth/whoami").status_code == 401

    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]


def test_login_fails_closed_for_wrong_origin_password_and_form_shape(
    admin_web: AdminWeb,
) -> None:
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

    assert wrong_origin.status_code == 403
    assert wrong_password.status_code == 401
    assert duplicate.status_code == 422
    assert unknown.status_code == 422
    assert wrong_media.status_code == 415
    for response in (
        wrong_origin,
        wrong_password,
        duplicate,
        unknown,
        wrong_media,
    ):
        assert _ADMIN_PASSWORD not in response.text
        assert _SESSION_COOKIE not in response.cookies
        _assert_security_headers(response)


def test_initialized_master_token_login_retires_legacy_web_login_but_not_operator(
    admin_web: AdminWeb,
) -> None:
    # This is a test-only local initialization, never an HTTP setup path.
    assert (
        _post(admin_web.client, "/admin/login", data={"password": _MASTER_TOKEN}).status_code == 401
    )

    legacy_csrf = _login(admin_web)
    legacy_encoded = admin_web.client.cookies.get(_SESSION_COOKIE) or ""
    bootstrapped = _post(admin_web.client, "/admin/bootstrap", data=_bootstrap_data(legacy_csrf))
    assert bootstrapped.status_code == 200
    library_id, _, _ = _metadata_from(bootstrapped.text)
    operator_token = _credential_from(bootstrapped.text)

    _initialize_master(admin_web)
    assert (
        admin_web.client.get(
            "/admin", headers={"Cookie": f"{_SESSION_COOKIE}={legacy_encoded}"}
        ).status_code
        == 303
    )
    assert (
        admin_web.client.post(
            "/admin/bootstrap",
            data=_bootstrap_data(legacy_csrf),
            headers={"Origin": _ORIGIN, "Cookie": f"{_SESSION_COOKIE}={legacy_encoded}"},
        ).status_code
        == 401
    )
    assert (
        _post(admin_web.client, "/admin/login", data={"password": _ADMIN_PASSWORD}).status_code
        == 401
    )

    wrong_origin = _post(
        admin_web.client,
        "/admin/login",
        data={"password": _MASTER_TOKEN},
        origin="https://wrong.example.invalid",
    )
    assert wrong_origin.status_code == 403
    assert _SESSION_COOKIE not in wrong_origin.cookies

    encoded, csrf = _login_master(admin_web)
    assert admin_web.client.get("/admin").status_code == 200
    assert admin_web.client.get("/admin/setup").status_code == 200
    assert admin_web.client.get("/api/v1/auth/whoami").status_code == 401
    assert _post(admin_web.client, "/admin/logout", data={"csrf_token": "wrong"}).status_code == 403
    for path, fields in (
        ("/admin/bootstrap", _bootstrap_data(csrf)),
        ("/admin/libraries", {"csrf_token": csrf, "name": "Cannot Create With Master Session"}),
        (
            f"/admin/libraries/{library_id}/tags",
            {"csrf_token": csrf, "name": "Synthetic Tag", "operator_token": operator_token},
        ),
        ("/admin/libraries/x/sections/y/trash/z/restore", {"csrf_token": csrf}),
    ):
        read_only = _post(admin_web.client, path, data=fields)
        assert read_only.status_code == 403
        assert "read-only" in read_only.text
    assert _post(admin_web.client, "/admin/logout", data={"csrf_token": csrf}).status_code == 303
    assert admin_web.client.get("/admin").status_code == 303

    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=600)
    assert codec.verify(legacy_encoded) is not None
    assert codec.verify_master(legacy_encoded) is None
    assert encoded != legacy_encoded


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/admin", 303),
        ("/admin/setup", 303),
        ("/admin/libraries", 303),
        ("/admin/guide", 303),
        ("/admin/login", 200),
    ],
)
def test_rotated_master_session_is_rejected_on_management_get(
    admin_web: AdminWeb, path: str, expected_status: int
) -> None:
    _initialize_master(admin_web)
    encoded, _ = _login_master(admin_web)
    with immediate_transaction(admin_web.engine) as connection:
        state = MasterTokenRepository(connection).rotate(
            _MASTER_TOKEN, _ROTATED_MASTER_TOKEN, now=1_001
        )
        assert state is not None
        assert state.session_generation == 2

    response = admin_web.client.get(path, headers={"Cookie": f"{_SESSION_COOKIE}={encoded}"})
    assert response.status_code == expected_status
    assert _MASTER_TOKEN not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/admin/logout",
        "/admin/bootstrap",
        "/admin/libraries",
        "/admin/libraries/x/tags",
        "/admin/libraries/x/sections/y/trash/z/restore",
    ],
)
def test_rotated_master_session_is_rejected_on_management_post(
    admin_web: AdminWeb, path: str
) -> None:
    _initialize_master(admin_web)
    encoded, csrf = _login_master(admin_web)
    with immediate_transaction(admin_web.engine) as connection:
        assert (
            MasterTokenRepository(connection).rotate(
                _MASTER_TOKEN, _ROTATED_MASTER_TOKEN, now=1_001
            )
            is not None
        )

    response = admin_web.client.post(
        path,
        data={"csrf_token": csrf},
        headers={"Origin": _ORIGIN, "Cookie": f"{_SESSION_COOKIE}={encoded}"},
    )
    assert response.status_code == 401
    assert _MASTER_TOKEN not in response.text


def test_missing_master_identity_rejects_existing_v2_session(admin_web: AdminWeb) -> None:
    _initialize_master(admin_web)
    encoded, csrf = _login_master(admin_web)
    with immediate_transaction(admin_web.engine) as connection:
        connection.execute(delete(MasterIdentity))

    assert (
        admin_web.client.get(
            "/admin", headers={"Cookie": f"{_SESSION_COOKIE}={encoded}"}
        ).status_code
        == 303
    )
    assert (
        admin_web.client.post(
            "/admin/logout",
            data={"csrf_token": csrf},
            headers={"Origin": _ORIGIN, "Cookie": f"{_SESSION_COOKIE}={encoded}"},
        ).status_code
        == 401
    )
    assert (
        _post(admin_web.client, "/admin/login", data={"password": _MASTER_TOKEN}).status_code == 401
    )


def test_matching_master_token_and_legacy_password_issues_revocable_v2_cookie(
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'matching-master.db').as_posix()}",
            "admin_password_hash": hash_password(
                _MASTER_TOKEN, salt_factory=lambda size: b"s" * size, iterations=300_000
            ),
            "admin_session_signing_secret": "s" * 32,
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)
    with TestClient(application, base_url=_ORIGIN, follow_redirects=False) as client:
        web = AdminWeb(client, application.state.engine)
        _initialize_master(web)
        encoded, _ = _login_master(web)

        with immediate_transaction(web.engine) as connection:
            assert (
                MasterTokenRepository(connection).rotate(
                    _MASTER_TOKEN, _ROTATED_MASTER_TOKEN, now=1_001
                )
                is not None
            )
        assert (
            client.get("/admin", headers={"Cookie": f"{_SESSION_COOKIE}={encoded}"}).status_code
            == 303
        )
        assert _post(client, "/admin/login", data={"password": _MASTER_TOKEN}).status_code == 401
        rotated_login = _post(client, "/admin/login", data={"password": _ROTATED_MASTER_TOKEN})
        assert rotated_login.status_code == 303
        rotated_cookie = client.cookies.get(_SESSION_COOKIE) or ""
        rotated_session = AdminSessionCodec(b"s" * 32, ttl_seconds=600).verify_master(
            rotated_cookie
        )
        assert rotated_session is not None
        assert rotated_session.session_generation == 2


def test_without_legacy_hash_v1_cookie_and_password_are_rejected(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'master-only.db').as_posix()}",
            "admin_session_signing_secret": "s" * 32,
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)
    with TestClient(application, base_url=_ORIGIN, follow_redirects=False) as client:
        encoded, _ = AdminSessionCodec(b"s" * 32, ttl_seconds=600).issue()
        assert (
            client.get("/admin", headers={"Cookie": f"{_SESSION_COOKIE}={encoded}"}).status_code
            == 303
        )
        assert (
            client.post(
                "/admin/logout",
                data={"csrf_token": "not-admitted"},
                headers={"Origin": _ORIGIN, "Cookie": f"{_SESSION_COOKIE}={encoded}"},
            ).status_code
            == 401
        )
        assert _post(client, "/admin/login", data={"password": _ADMIN_PASSWORD}).status_code == 401

        web = AdminWeb(client, application.state.engine)
        _initialize_master(web)
        _login_master(web)
        assert client.get("/admin").status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "",
        "http://admin.example.invalid",
        "https://admin.example.invalid:8443",
        "https://admin.example.invalid.evil.invalid",
        "https://user@admin.example.invalid",
        "https://admin.example.invalid/",
        "https://admin.example.invalid/path",
        "https://admin.example.invalid?",
        "https://admin.example.invalid#",
        "https://admin.example.invalid:",
        "https://admin.example.invalid:0",
        "https://admin.example.invalid:65536",
        "https://admin.example.invalid:invalid",
        "https://admin.example.invalid,https://other.example.invalid",
        " https://admin.example.invalid",
        "https://admin.example.invalid\\",
        "https://[invalid]",
        "file://admin.example.invalid",
    ],
)
def test_login_rejects_cross_origin_and_malformed_origins(admin_web: AdminWeb, origin: str) -> None:
    response = _post(
        admin_web.client, "/admin/login", origin=origin, data={"password": _ADMIN_PASSWORD}
    )
    assert response.status_code == 403
    assert _SESSION_COOKIE not in response.cookies


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Origin", _ORIGIN), ("Origin", _ORIGIN)],
        [("Origin", _ORIGIN), ("Host", "admin.example.invalid"), ("Host", "other.invalid")],
        [("Origin", _ORIGIN), ("Host", "admin.example.invalid/ignored")],
    ],
)
def test_login_rejects_missing_or_ambiguous_headers(
    admin_web: AdminWeb, headers: list[tuple[str, str]]
) -> None:
    response = admin_web.client.post(
        "/admin/login", headers=headers, data={"password": _ADMIN_PASSWORD}
    )
    assert response.status_code == 403
    assert _SESSION_COOKIE not in response.cookies


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("HTTPS://Admin.Example.Invalid:443", ("https", "admin.example.invalid", 443)),
        ("http://admin.example.invalid:80", ("http", "admin.example.invalid", 80)),
        ("http://[fd00:0:0:0:0:0:0:7]:8080", ("http", "fd00::7", 8080)),
        ("http://[fd00::7%25eth0]", None),
        ("https://bücher.example.invalid", None),
        ("https://admin.example.invalid\n", None),
        ("https://", None),
    ],
)
def test_origin_comparison_normalizes_only_equivalent_addresses(
    origin: str, expected: tuple[str, str, int] | None
) -> None:
    assert admin_router._origin_parts(origin) == expected


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("Admin.Example.Invalid:443", _ORIGIN),
        ("admin.example.invalid", "https://admin.example.invalid:443"),
        ("[fd00:0:0:0:0:0:0:7]:8443", "https://[fd00::7]:8443"),
    ],
)
def test_equivalent_host_and_origin_forms_can_log_in(
    admin_web: AdminWeb, host: str, origin: str
) -> None:
    response = admin_web.client.post(
        "/admin/login",
        headers={"Host": host, "Origin": origin},
        data={"password": _ADMIN_PASSWORD},
    )
    assert response.status_code == 303


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("host", ["public.example.invalid", "private.example.invalid:8443"])
def test_https_proxy_login_uses_only_trusted_asgi_scheme(
    admin_web: AdminWeb, trusted: bool, host: str
) -> None:
    application = ProxyHeadersMiddleware(
        cast(Any, admin_web.client.app), trusted_hosts=["127.0.0.1"] if trusted else []
    )
    with TestClient(
        cast(Any, application),
        base_url=f"http://{host}",
        client=("127.0.0.1", 12345),
        follow_redirects=False,
    ) as client:
        headers = {"X-Forwarded-Proto": "https", "Origin": f"https://{host}"}
        assert client.get("/admin/login", headers=headers).status_code == 200
        assert client.get("/admin/style.css", headers=headers).status_code == 200
        response = client.post("/admin/login", headers=headers, data={"password": _ADMIN_PASSWORD})
        assert response.status_code == (303 if trusted else 403)
        if trusted:
            assert "Secure" in response.headers["set-cookie"]
            assert "Domain=" not in response.headers["set-cookie"]
        else:
            assert _SESSION_COOKIE not in response.cookies


def test_forwarded_host_cannot_override_actual_request_host(admin_web: AdminWeb) -> None:
    response = admin_web.client.post(
        "/admin/login",
        headers={
            "Origin": "https://other.example.invalid",
            "X-Forwarded-Host": "other.example.invalid",
            "Forwarded": "host=other.example.invalid;proto=https",
        },
        data={"password": _ADMIN_PASSWORD},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("private_http", [False, True])
def test_secure_cookie_depends_on_request_transport_and_http_opt_in(
    tmp_path: Path, private_http: bool
) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'cookie.db').as_posix()}",
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_allow_private_http": private_http,
        }
    )
    app = create_app(settings)
    Caller.metadata.create_all(app.state.engine)
    # 同一个应用通过多个入口访问，不需要为入口名称分别修改设置。
    for origin in ("http://private.example.invalid:8080", "https://public.example.invalid"):
        with TestClient(app, base_url=origin, follow_redirects=False) as client:
            locale = client.get("/admin/login?lang=zh-CN")
            login = _post(client, "/admin/login", origin=origin, data={"password": _ADMIN_PASSWORD})
            expected_secure = origin.startswith("https://") or not private_http
            assert login.status_code == 303
            for response in (locale, login):
                assert ("Secure" in response.headers["set-cookie"]) == expected_secure
            assert client.get("/admin").status_code == (
                303 if origin.startswith("http:") and not private_http else 200
            )


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
    setup = admin_web.client.get("/admin/setup")
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
    assert "内容近况" in dashboard.text
    assert 'href="/admin/setup"' in dashboard.text
    assert "首次设置" in setup.text
    assert "只有明确要新建另一个知识库时才再次填写" in setup.text
    assert "个人使用通常一个知识库就够了" in setup.text
    assert "Agent 权限的边界" in setup.text
    assert "首次生成的管理员令牌可以使用多久" in setup.text
    assert "管理员令牌有效期（秒）" in setup.text
    assert 'aria-describedby="library_name-help"' in setup.text
    assert 'class="field-help" id="library_name-help"' in setup.text
    assert setup.text.count("可选。") >= 3
    assert "当前管理员凭据" in setup.text
    assert "退出登录" in dashboard.text
    assert "管理员指南" in guide.text
    assert "恢复管理员凭据会使此前仍有效的管理员凭据失效" in guide.text
    assert "Agent 使用说明" in agent.text
    assert "MCP 配置" in mcp.text
    assert "提交的表单包含未知字段。" in invalid_form.text
    assert 'href="/admin/setup?lang=en"' in invalid_form.text
    assert "/admin/bootstrap?lang=" not in invalid_form.text
    assert initialized.status_code == 200
    assert "知识库已初始化" in initialized.text
    assert 'href="/admin/setup"' in initialized.text
    assert "此值仅在本次响应中显示" in initialized.text
    assert "知识库 ID" in initialized.text
    assert "调用方 ID" in initialized.text
    assert "凭据 ID" in initialized.text
    assert 'class="language-switch"' not in initialized.text
    assert "/admin/bootstrap?lang=" not in initialized.text
    for response in (dashboard, setup, guide, agent, mcp, invalid_form, initialized):
        _assert_security_headers(response)


def test_login_session_protected_guides_and_logout(admin_web: AdminWeb) -> None:
    unauthenticated = admin_web.client.get("/admin")
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/admin/login"
    assert admin_web.client.get("/admin/setup").status_code == 303
    sign_in = admin_web.client.get("/admin/login")
    assert "run host commands" in sign_in.text

    csrf = _login(admin_web)
    dashboard = admin_web.client.get("/admin")
    setup = admin_web.client.get("/admin/setup")
    assert "Content activity" in dashboard.text
    assert "First-time setup" not in dashboard.text
    assert "First-time setup" in setup.text
    assert "One Library is usually enough for personal use" in setup.text
    assert "only when you deliberately want another Library" in setup.text
    assert "3600 seconds is one hour" in setup.text
    assert _ADMIN_PASSWORD not in setup.text

    guide = admin_web.client.get("/admin/guide")
    agent = admin_web.client.get("/admin/agent")
    mcp = admin_web.client.get("/admin/mcp")
    stylesheet = admin_web.client.get("/admin/style.css")
    assert "no image update" in guide.text
    assert 'href="/connect"' in agent.text
    assert "Standard HTTP" in agent.text
    assert "patchouli-mcp" in mcp.text
    assert stylesheet.headers["content-type"].startswith("text/css")
    for response in (dashboard, setup, guide, agent, mcp, stylesheet):
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
    assert 'href="/admin/setup"' in revoked.text
    assert recovered_token not in revoked.text

    with admin_web.engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(AuthRepository(connection)).authenticate(agent_token)


def test_agent_token_reveal_requires_current_master_session_origin_and_csrf(
    admin_web: AdminWeb,
) -> None:
    token, library_id, caller_id, credential_id = _issue_web_agent(admin_web)
    detail_path = f"/admin/libraries/{library_id}/callers/{caller_id}"
    reveal_path = f"{detail_path}/credentials/{credential_id}/reveal"
    legacy_page = admin_web.client.get(detail_path)
    assert legacy_page.status_code == 200
    assert token not in legacy_page.text
    assert "token-reveal" not in legacy_page.text
    legacy_post = _post(admin_web.client, reveal_path, data={"csrf_token": "wrong"})
    assert legacy_post.status_code == 403
    assert token not in legacy_post.text
    admin_web.client.cookies.clear()
    unsigned = _post(admin_web.client, reveal_path, data={"csrf_token": "wrong"})
    assert unsigned.status_code == 401
    assert token not in unsigned.text

    _initialize_master(admin_web)
    encoded, csrf = _login_master(admin_web)
    detail = admin_web.client.get(detail_path)
    assert detail.status_code == 200
    assert token not in detail.text
    assert f"plb1…{token[-4:]}" in detail.text
    assert f'action="{reveal_path}"' in detail.text
    assert 'src="/admin/reveal.js"' in detail.text
    assert "script-src 'self'" in detail.headers["content-security-policy"]
    assert "connect-src 'self'" in detail.headers["content-security-policy"]
    assert "'unsafe-inline'" not in detail.headers["content-security-policy"]
    assert "Clipboard unavailable. Select the displayed Token to copy it manually." in detail.text
    chinese_detail = admin_web.client.get(f"{detail_path}?lang=zh-CN")
    assert "剪贴板不可用。请选中显示的 Token 手动复制。" in chinese_detail.text
    dashboard = admin_web.client.get("/admin")
    assert "connect-src 'self'" not in dashboard.headers["content-security-policy"]
    script = admin_web.client.get("/admin/reveal.js")
    assert script.status_code == 200
    assert "navigator.clipboard.writeText" in script.text
    assert "form.dataset.manualCopyLabel" in script.text
    assert token not in script.text
    _assert_security_headers(script)
    assert admin_web.client.get(reveal_path).status_code == 405

    for data, origin, status in (
        ({"csrf_token": csrf}, "https://other.example.invalid", 403),
        ({"csrf_token": "wrong"}, _ORIGIN, 403),
        ({"csrf_token": csrf, "extra": "x"}, _ORIGIN, 422),
    ):
        rejected = _post(admin_web.client, reveal_path, data=data, origin=origin)
        assert rejected.status_code == status
        assert token not in rejected.text
        _assert_security_headers(rejected)

    for wrong_path in (
        f"{detail_path}/credentials/{'f' * 32}/reveal",
        f"/admin/libraries/{'f' * 32}/callers/{caller_id}/credentials/{credential_id}/reveal",
        f"/admin/libraries/{library_id}/callers/{'f' * 32}/credentials/{credential_id}/reveal",
    ):
        rejected = _post(admin_web.client, wrong_path, data={"csrf_token": csrf})
        assert rejected.status_code == 404
        assert token not in rejected.text

    with admin_web.engine.connect() as connection:
        assert connection.execute(select(MasterAuditEvent.id)).all() == []

    revealed = _post(admin_web.client, reveal_path, data={"csrf_token": csrf})
    assert revealed.status_code == 200
    assert revealed.text == token
    assert revealed.headers["content-type"].startswith("text/plain")
    assert token not in str(revealed.request.url)
    assert token not in (admin_web.client.cookies.get(_SESSION_COOKIE) or "")
    _assert_security_headers(revealed)
    assert _post(admin_web.client, reveal_path, data={"csrf_token": csrf}).text == token
    with admin_web.engine.connect() as connection:
        events = connection.execute(
            select(
                MasterAuditEvent.identity_id,
                MasterAuditEvent.session_generation,
                MasterAuditEvent.session_fingerprint,
                MasterAuditEvent.action,
                MasterAuditEvent.target_type,
                MasterAuditEvent.target_id,
            )
        ).all()
    assert len(events) == 2
    for event in events:
        assert event.identity_id == "a" * 32
        assert event.session_generation == 1
        assert len(event.session_fingerprint) == 32
        assert event.action == "auth.agent_token.reveal"
        assert event.target_type == "credential"
        assert event.target_id == credential_id
        assert token.encode() not in bytes(event.session_fingerprint)

    with immediate_transaction(admin_web.engine) as connection:
        assert (
            MasterTokenRepository(connection).rotate(
                _MASTER_TOKEN, _ROTATED_MASTER_TOKEN, now=1_001
            )
            is not None
        )
    rotated = admin_web.client.post(
        reveal_path,
        data={"csrf_token": csrf},
        headers={"Origin": _ORIGIN, "Cookie": f"{_SESSION_COOKIE}={encoded}"},
    )
    assert rotated.status_code == 401
    assert token not in rotated.text


@pytest.mark.parametrize("state", ["revoked", "rotated", "expired", "disabled", "legacy"])
def test_agent_token_reveal_rejects_inactive_or_unrecoverable_values(
    admin_web: AdminWeb, state: str
) -> None:
    token, library_id, caller_id, credential_id = _issue_web_agent(admin_web)
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)
    path = f"/admin/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}/reveal"
    with immediate_transaction(admin_web.engine) as connection:
        repository = AuthRepository(connection)
        credential = repository.get_credential(library_id, caller_id, credential_id)
        assert credential is not None
        now = time_ns() // 1_000
        if state == "revoked":
            repository.revoke_credential(credential, revoked_at=now)
        elif state == "rotated":
            caller = repository.get_caller(library_id, caller_id)
            assert caller is not None
            replacement = CredentialIssuer(repository).issue(caller, expires_at=now + 3_600_000_000)
            repository.mark_credential_rotated(
                credential, replacement.credential.id, rotated_at=now
            )
        elif state == "expired":
            connection.execute(
                update(Credential).where(Credential.id == credential_id).values(expires_at=now - 1)
            )
        elif state == "disabled":
            repository.disable_caller(library_id, caller_id, disabled_at=now)
        else:
            connection.execute(
                delete(AgentTokenValue).where(AgentTokenValue.credential_id == credential_id)
            )

    response = _post(admin_web.client, path, data={"csrf_token": csrf})
    assert response.status_code == 410
    assert token not in response.text
    assert (
        token not in admin_web.client.get(f"/admin/libraries/{library_id}/callers/{caller_id}").text
    )
    if state == "legacy":
        with admin_web.engine.connect() as connection:
            assert AuthenticationService(AuthRepository(connection)).authenticate(token)
    _assert_security_headers(response)


def test_agent_token_reveal_fails_closed_when_audit_cannot_commit(
    admin_web: AdminWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, library_id, caller_id, credential_id = _issue_web_agent(admin_web)
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)

    def reject_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit write failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    path = f"/admin/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}/reveal"
    failed = _post(admin_web.client, path, data={"csrf_token": csrf})
    assert failed.status_code == 500
    assert token not in failed.text
    with admin_web.engine.connect() as connection:
        assert connection.execute(select(MasterAuditEvent.id)).all() == []
    assert token not in str(admin_web.client.cookies)


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
