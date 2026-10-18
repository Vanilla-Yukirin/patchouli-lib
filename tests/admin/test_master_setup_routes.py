"""Real-schema HTTP first-setup admission and secret boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from re import search
from urllib.parse import urlencode

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, func, select

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller, Credential, MasterAuditEvent, MasterIdentity
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.library.models import Library

_ORIGIN = "https://admin.example.invalid"
_PROOF = "synthetic out-of-band setup proof 12345"
_TOKEN = "synthetic newly saved master token 12345"
_LEGACY = "synthetic old administration password"
_SIGNING = "s" * 32
_SETUP = "/admin/master/setup"


@dataclass(frozen=True)
class Browser:
    client: TestClient
    engine: Engine
    settings: Settings

    def form(self) -> str:
        response = self.client.get(_SETUP)
        assert response.status_code == 200
        match = search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        return unescape(match.group(1))

    def submit(self, csrf: str, **overrides: str) -> Response:
        values = {
            "csrf_token": csrf,
            "master_token": _TOKEN,
            "confirmation": _TOKEN,
            "setup_proof": _PROOF,
        }
        values.update(overrides)
        return self.client.post(
            _SETUP, data=values, headers={"Origin": str(self.client.base_url).rstrip("/")}
        )


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Browser]:
    url = f"sqlite:///{(tmp_path / 'setup.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": url,
            "admin_setup_token": _PROOF,
            "admin_session_signing_secret": _SIGNING,
        }
    )
    app = create_app(settings)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield Browser(client, app.state.engine, settings)


def _assert_empty(engine: Engine) -> None:
    with engine.connect() as connection:
        for model in (
            MasterIdentity,
            MasterAuditEvent,
            Library,
            Caller,
            Credential,
            Page,
            Revision,
        ):
            assert connection.scalar(select(func.count()).select_from(model)) == 0


def test_proof_setup_creates_only_master_and_audit_and_logs_in(browser: Browser) -> None:
    assert browser.client.get("/admin/login").headers["location"] == _SETUP
    csrf = browser.form()
    setup_cookie = browser.client.cookies.get("patchouli_master_setup_session")
    assert setup_cookie is not None
    assert browser.client.cookies.get("patchouli_admin_session") is None
    # A restricted setup cookie has no administration admission.
    assert browser.client.get("/admin").status_code == 303
    response = browser.submit(csrf)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert "no-store" in response.headers["cache-control"]
    cookie = browser.client.cookies.get("patchouli_admin_session")
    assert cookie is not None
    codec = AdminSessionCodec(_SIGNING.encode(), ttl_seconds=1_800)
    master = codec.verify_master(cookie)
    assert master is not None
    assert codec.verify(cookie) is None
    assert browser.client.cookies.get("patchouli_master_setup_session") is None
    assert browser.client.get("/admin").status_code == 200
    assert browser.client.get(_SETUP).headers["location"] == "/admin/login"
    with browser.engine.connect() as connection:
        assert MasterTokenRepository(connection).authenticate(_TOKEN) is not None
        assert connection.scalar(select(func.count()).select_from(MasterIdentity)) == 1
        audit = connection.execute(select(MasterAuditEvent)).mappings().one()
        assert audit["action"] == "auth.master.initialize"
        assert audit["identity_id"] == master.identity_id
        assert audit["target_id"] == master.identity_id
        for model in (Library, Caller, Credential, Page, Revision):
            assert connection.scalar(select(func.count()).select_from(model)) == 0
    assert _PROOF not in response.text and _TOKEN not in response.text


@pytest.mark.parametrize(
    ("override", "status"),
    [
        ({"setup_proof": "wrong"}, 403),
        ({"csrf_token": "wrong"}, 403),
        ({"csrf_token": "非ASCII"}, 403),
        ({"master_token": "short", "confirmation": "short"}, 422),
        ({"confirmation": "does-not-match"}, 422),
        ({"master_token": "t" * 1_025, "confirmation": "t" * 1_025}, 422),
    ],
)
def test_bad_setup_is_rejected_without_reflecting_secrets(
    browser: Browser, override: dict[str, str], status: int
) -> None:
    csrf = browser.form()
    response = browser.submit(csrf, **override)
    assert response.status_code == status
    assert _TOKEN not in response.text and _PROOF not in response.text
    for field in ("master_token", "confirmation", "setup_proof"):
        assert f'name="{field}" value=' not in response.text
    _assert_empty(browser.engine)


@pytest.mark.parametrize("origin", [None, "https://other.example.invalid", "null"])
def test_cross_origin_setup_denied(browser: Browser, origin: str | None) -> None:
    csrf = browser.form()
    headers = {} if origin is None else {"Origin": origin}
    response = browser.client.post(
        _SETUP,
        data={
            "csrf_token": csrf,
            "master_token": _TOKEN,
            "confirmation": _TOKEN,
            "setup_proof": _PROOF,
        },
        headers=headers,
    )
    assert response.status_code == 403
    _assert_empty(browser.engine)


def test_missing_cookie_cannot_initialize_even_with_correct_proof(browser: Browser) -> None:
    csrf = browser.form()
    browser.client.cookies.clear()
    assert browser.submit(csrf).status_code == 401
    _assert_empty(browser.engine)


@pytest.mark.parametrize("kind", ["duplicate", "unknown", "too-large", "json"])
def test_setup_form_parser_bounds(browser: Browser, kind: str) -> None:
    csrf = browser.form()
    values = {
        "csrf_token": csrf,
        "master_token": _TOKEN,
        "confirmation": _TOKEN,
        "setup_proof": _PROOF,
    }
    body = urlencode(values)
    status = 422
    content_type = "application/x-www-form-urlencoded"
    if kind == "duplicate":
        body += "&master_token=duplicate"
    elif kind == "unknown":
        body += "&unexpected=field"
    elif kind == "too-large":
        body += "&unexpected=" + "a" * 16_384
        status = 413
    else:
        body = "{}"
        content_type = "application/json"
        status = 415
    response = browser.client.post(
        _SETUP, content=body, headers={"Origin": _ORIGIN, "Content-Type": content_type}
    )
    assert response.status_code == status
    _assert_empty(browser.engine)


def test_second_browser_cannot_overwrite_identity(browser: Browser) -> None:
    csrf = browser.form()
    with TestClient(browser.client.app, base_url=_ORIGIN, follow_redirects=False) as second:
        other = Browser(second, browser.engine, browser.settings)
        other_csrf = other.form()
        assert browser.submit(csrf).status_code == 303
        assert (
            other.submit(
                other_csrf,
                master_token="other token material" * 3,
                confirmation="other token material" * 3,
            ).status_code
            == 409
        )
    with browser.engine.connect() as connection:
        assert MasterTokenRepository(connection).authenticate(_TOKEN) is not None
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 1


def test_audit_failure_rolls_back_identity(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", fail)
    csrf = browser.form()
    response = browser.submit(csrf)
    assert response.status_code == 500
    assert _PROOF not in response.text and _TOKEN not in response.text
    assert "synthetic audit failure" not in response.text
    _assert_empty(browser.engine)


@pytest.mark.parametrize("private_http", [False, True])
def test_setup_cookie_flags_and_chinese_form(browser: Browser, private_http: bool) -> None:
    # Config is copied without changing any tracked or deployment configuration.
    original = browser.settings
    settings = original.model_copy(update={"admin_allow_private_http": private_http})
    with TestClient(
        create_app(settings), base_url="http://private.example.invalid", follow_redirects=False
    ) as client:
        response = client.get(_SETUP + "?lang=zh-CN")
        cookie = response.headers["set-cookie"].split(", patchouli_admin_locale", 1)[0]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Path=/admin/master/setup" in cookie and "Max-Age=300" in cookie
        assert ("Secure" in cookie) is not private_http
        assert (
            "首次设置主 Token" in response.text and 'autocomplete="new-password"' in response.text
        )
        assert _PROOF not in response.text and _TOKEN not in response.text
        assert response.headers["content-language"] == "zh-CN"
        assert "style-src 'self'" in response.headers["content-security-policy"]
        if private_http:
            other = Browser(client, browser.engine, settings)
            assert other.submit(other.form()).status_code == 303
            assert client.get("/admin").status_code == 200


def test_no_proof_empty_database_does_not_allow_remote_setup(browser: Browser) -> None:
    original = browser.settings
    settings = original.model_copy(update={"admin_setup_token": None})
    with TestClient(create_app(settings), base_url=_ORIGIN, follow_redirects=False) as client:
        response = client.get(_SETUP)
        assert response.status_code == 403
        assert "<form" not in response.text
        assert client.cookies.get("patchouli_master_setup_session") is None
        assert (
            client.post(
                _SETUP, data={"master_token": _TOKEN}, headers={"Origin": _ORIGIN}
            ).status_code
            == 401
        )
    _assert_empty(browser.engine)


def test_legacy_password_session_can_initialize_then_is_invalidated(browser: Browser) -> None:
    original = browser.settings
    settings = original.model_copy(
        update={"admin_setup_token": None, "admin_password_hash": SecretStr(hash_password(_LEGACY))}
    )
    with TestClient(create_app(settings), base_url=_ORIGIN, follow_redirects=False) as client:
        assert client.get(_SETUP).headers["location"] == "/admin/login"
        assert (
            client.post(
                "/admin/login", data={"password": _LEGACY}, headers={"Origin": _ORIGIN}
            ).status_code
            == 303
        )
        old_cookie = client.cookies.get("patchouli_admin_session")
        assert old_cookie is not None
        other = Browser(client, browser.engine, settings)
        csrf = other.form()
        assert client.cookies.get("patchouli_master_setup_session") is None
        assert 'name="setup_proof"' not in client.get(_SETUP).text
        assert (
            client.post(
                _SETUP,
                data={"csrf_token": csrf, "master_token": _TOKEN, "confirmation": _TOKEN},
                headers={"Origin": _ORIGIN},
            ).status_code
            == 303
        )
        client.cookies.set(
            "patchouli_admin_session", old_cookie, domain="admin.example.invalid", path="/admin"
        )
        assert client.get("/admin").status_code == 303
        assert (
            client.post(
                "/admin/login", data={"password": _LEGACY}, headers={"Origin": _ORIGIN}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/admin/login", data={"password": _TOKEN}, headers={"Origin": _ORIGIN}
            ).status_code
            == 303
        )
        assert client.get("/admin").status_code == 200
