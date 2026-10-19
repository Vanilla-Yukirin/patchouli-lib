"""Short-lived ticket UI preserves the master boundary and avoids secret caching."""

import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_routes import AdminWeb, _initialize_master, _login, _login_master, _post
from test_routes import admin_web as admin_web

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.onboarding_pages import TICKET_SCRIPT, onboarding_ticket_page
from patchouli_lib.admin.pages import AdminLocale, callers_page
from patchouli_lib.api.agent_skill_routes import SkillBundle
from patchouli_lib.auth.onboarding import OnboardingTicketService
from patchouli_lib.auth.service import AuthenticationError


def test_credential_labels_and_ticket_entry() -> None:
    labels: tuple[tuple[AdminLocale, str, str], ...] = (
        ("en", "Credential management", "Onboarding ticket"),
        ("zh-CN", "凭据管理", "接入小票"),
    )
    for locale, heading, ticket in labels:
        page = callers_page("csrf", (), locale=locale, allow_master_actions=True)
        assert heading in page and ticket in page
        assert "Create Agent" not in page and "创建 Agent" not in page
        assert 'href="/admin/onboarding-tickets"' in page
    assert "/admin/onboarding-tickets" not in callers_page("csrf", ())


def test_ticket_output_escapes_untrusted_values_and_discloses_authority() -> None:
    for locale in ("en", "zh-CN"):
        page = onboarding_ticket_page(
            'csrf"<&',
            locale=locale,
            code="</textarea><script>alert(1)</script>",
            expires_at=300_000_000,
            origin='https://example.invalid/<img src=x onerror="bad">',
        )
        assert "<script>alert(1)</script>" not in page
        assert "<img src=x" not in page
        assert "&lt;/textarea&gt;" in page
        assert 'data-ticket-copy="ticket-prompt"' in page
        assert "1970-01-01T00:05:00+00:00" in page
        assert "/api/v1/auth/whoami" in page
        assert "/api/v1/onboarding/redeem" in page
        assert "/docs/skills.md" not in page
        assert "/api/v1/agent/skill/manifest" in page
        assert "/connect" in page
        assert "未来知识库" in page if locale == "zh-CN" else "future Libraries" in page
    assert "pagehide" in TICKET_SCRIPT and "navigator.clipboard.writeText" in TICKET_SCRIPT


def test_ticket_prompt_points_to_registered_bootstrap_and_packaged_skill(
    admin_web: AdminWeb,
) -> None:
    bootstrap = admin_web.client.get("/connect")
    assert bootstrap.status_code == 200
    assert "/api/v1/onboarding/redeem" in bootstrap.text
    # The prompt's next step is authenticated: it must reach the actual router,
    # not a guessed documentation alias. Full authenticated downloads are tested
    # with the shared Skill manifest contract.
    assert admin_web.client.get("/api/v1/agent/skill/manifest").status_code == 401
    bundle = SkillBundle()
    assert "SKILL.md" in bundle.contents
    assert "references/onboarding-ticket.md" in bundle.contents
    assert all(
        isinstance(entry["href"], str) and entry["href"].startswith("/api/v1/agent/skill/files/")
        for entry in bundle.file_entries
    )


def test_ticket_requires_master_session(admin_web: AdminWeb) -> None:
    assert admin_web.client.get("/admin/onboarding-tickets").status_code == 303
    assert _post(admin_web.client, "/admin/onboarding-tickets", data={}).status_code == 401
    csrf = _login(admin_web)
    assert admin_web.client.get("/admin/onboarding-tickets").status_code == 403
    assert (
        _post(admin_web.client, "/admin/onboarding-tickets", data={"csrf_token": csrf}).status_code
        == 403
    )


def test_real_generation_delivers_one_working_ticket(admin_web: AdminWeb) -> None:
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)
    response = _post(admin_web.client, "/admin/onboarding-tickets", data={"csrf_token": csrf})
    assert response.status_code == 200
    matched = re.search(r'id="ticket-code"[^>]*>([A-Za-z0-9]{16})</textarea>', response.text)
    assert matched is not None
    assert OnboardingTicketService(admin_web.engine).discover(matched.group(1)) == ()


def test_master_ticket_generation_guards_and_delivery(
    admin_web: AdminWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)
    service = Mock()
    service.create.return_value = SimpleNamespace(code="AbCdEf0123456789", expires_at=300_000_000)
    monkeypatch.setattr(admin_router, "OnboardingTicketService", lambda engine: service)
    form = admin_web.client.get("/admin/onboarding-tickets?lang=zh-CN")
    assert form.status_code == 200
    assert "所有当前和未来知识库" in form.text
    assert 'name="csrf_token"' in form.text
    assert 'name="read_mode"' not in form.text
    assert "no-store" in form.headers["cache-control"]
    assert form.headers["referrer-policy"] == "no-referrer"
    for data, origin in (
        ({"csrf_token": "wrong"}, "https://admin.example.invalid"),
        ({"csrf_token": csrf}, "https://attacker.example.invalid"),
    ):
        assert (
            _post(
                admin_web.client, "/admin/onboarding-tickets", data=data, origin=origin
            ).status_code
            == 403
        )
    invalid = _post(
        admin_web.client,
        "/admin/onboarding-tickets",
        data={"csrf_token": csrf, "read_mode": "exclude"},
    )
    assert invalid.status_code == 422
    service.create.assert_not_called()
    response = _post(admin_web.client, "/admin/onboarding-tickets", data={"csrf_token": csrf})
    assert response.status_code == 200
    service.create.assert_called_once()
    assert "AbCdEf0123456789" in response.text
    assert "https://admin.example.invalid" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    script = admin_web.client.get("/admin/onboarding-tickets.js")
    assert script.status_code == 200 and "AbCdEf0123456789" not in script.text


@pytest.mark.parametrize(
    "error,status", [(AuthenticationError(), 401), (RuntimeError("secret"), 500)]
)
def test_ticket_generation_error_does_not_echo_secret(
    admin_web: AdminWeb, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int
) -> None:
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)
    service = Mock()
    service.create.side_effect = error
    monkeypatch.setattr(admin_router, "OnboardingTicketService", lambda engine: service)
    response = _post(admin_web.client, "/admin/onboarding-tickets", data={"csrf_token": csrf})
    assert response.status_code == status
    assert "secret" not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
