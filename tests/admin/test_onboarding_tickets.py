"""Ticket generation keeps master admission, Origin, CSRF and secret redaction."""

from unittest.mock import Mock

import pytest
from helpers import ORIGIN, AdminWeb
from helpers import admin_web as admin_web

import patchouli_lib.admin.json_api.actions as actions
from patchouli_lib.auth.service import AuthenticationError


def test_ticket_generation_guards_and_safe_errors(
    admin_web: AdminWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "/admin/api/onboarding-tickets"
    service = Mock()
    service.create.side_effect = AuthenticationError()
    monkeypatch.setattr(actions, "OnboardingTicketService", lambda engine: service)
    for headers in (
        {"Origin": ORIGIN},
        {**admin_web.headers, "Origin": "https://attacker.example.invalid"},
    ):
        assert admin_web.client.post(path, json={}, headers=headers).status_code == 403
    assert admin_web.post(path, {"read_mode": "exclude"}).status_code == 422
    service.create.assert_not_called()
    response = admin_web.post(path)
    assert response.status_code == 401
    assert "no-store" in response.headers["cache-control"]
    service.create.side_effect = RuntimeError("synthetic-secret-not-for-response")
    response = admin_web.post(path)
    assert response.status_code == 500
    assert "synthetic-secret-not-for-response" not in response.text
    admin_web.client.cookies.clear()
    assert admin_web.post(path).status_code == 401
    assert admin_web.client.get("/connect").status_code == 200
    assert admin_web.client.get("/api/v1/agent/skill/manifest").status_code == 401
