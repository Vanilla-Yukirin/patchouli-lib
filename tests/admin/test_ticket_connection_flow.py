"""Real application onboarding from master form to protected Skill installation."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest
from helpers import AdminWeb
from helpers import admin_web as admin_web
from sqlalchemy import select

from patchouli_lib.auth.models import Credential, OnboardingTicket
from patchouli_lib.request_log.models import RequestLogRecord


def test_ticket_to_custom_scope_and_verified_skill_bundle(
    admin_web: AdminWeb, caplog: pytest.LogCaptureFixture
) -> None:
    def create_library(name: str) -> str:
        response = admin_web.post("/admin/api/libraries", {"name": name})
        assert response.status_code == 201
        return str(response.json()["id"])

    first = create_library("Synthetic first collection")
    second = create_library("Synthetic editable collection")
    generated = admin_web.post("/admin/api/onboarding-tickets")
    assert generated.status_code == 201
    code = generated.json()["code"]
    assert isinstance(code, str) and len(code) == 16
    assert "no-store" in generated.headers["cache-control"]
    assert generated.headers["referrer-policy"] == "same-origin"

    discovered = admin_web.client.post("/api/v1/onboarding/libraries", json={"code": code})
    assert discovered.status_code == 200
    assert {item["id"] for item in discovered.json()["libraries"]} == {first, second}
    with admin_web.engine.connect() as connection:
        assert connection.scalar(select(OnboardingTicket.consumed_at)) is None

    name = "Synthetic ticket device"
    description = "private-description-marker-not-for-request-logs"
    payload: dict[str, object] = {
        "code": code,
        "name": name,
        "description": description,
        "never_expires": False,
        "credential_ttl_seconds": 3_600,
        "read_mode": "exclude",
        "read_libraries": [],
        "write_mode": "include",
        "write_libraries": [second],
    }
    exchanged = admin_web.client.post("/api/v1/onboarding/redeem", json=payload)
    assert exchanged.status_code == 201
    assert "no-store" in exchanged.headers["cache-control"]
    delivery = exchanged.json()
    token = delivery["token"]

    # The read exclusion mode includes future Libraries, while the independent
    # selected write scope remains restricted to exactly the chosen Library.
    future = create_library("Synthetic future collection")
    admin_web.client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    whoami = admin_web.client.get("/api/v1/auth/whoami", headers=headers)
    assert whoami.status_code == 200
    identity = whoami.json()
    assert identity["name"] == name and identity["description"] == description
    assert identity["credential_id"] == delivery["credential_id"]
    assert identity["caller_id"] == delivery["caller_id"]
    assert identity["expires_at"] is not None
    assert identity["library_scope"]["read_mode"] == "exclude"
    assert identity["library_scope"]["read_libraries"] == []
    assert identity["library_scope"]["write_mode"] == "include"
    assert identity["library_scope"]["write_libraries"] == [second]
    assert {item["library_id"]: set(item["actions"]) for item in identity["library_grants"]} == {
        first: {"read"},
        second: {"read", "write"},
        future: {"read"},
    }
    capabilities = admin_web.client.get("/api/v1/capabilities", headers=headers)
    assert capabilities.status_code == 200
    assert isinstance(capabilities.json()["features"], list)

    manifest = admin_web.client.get("/api/v1/agent/skill/manifest", headers=headers)
    assert manifest.status_code == 200
    assert "no-store" in manifest.headers["cache-control"]
    expected_paths = {
        "SKILL.md",
        "references/http.md",
        "references/local-token.md",
        "references/onboarding-ticket.md",
    }
    entries = manifest.json()["files"]
    assert {entry["path"] for entry in entries} == expected_paths
    for entry in entries:
        assert entry["href"] == "/api/v1/agent/skill/files/" + entry["path"]
        downloaded = admin_web.client.get(entry["href"], headers=headers)
        assert downloaded.status_code == 200
        assert len(downloaded.content) == entry["bytes"]
        assert sha256(downloaded.content).hexdigest() == entry["sha256"]
        assert "no-store" in downloaded.headers["cache-control"]
        assert token not in downloaded.text and code not in downloaded.text
        assert admin_web.client.get(entry["href"]).status_code == 401

    repeated = admin_web.client.post("/api/v1/onboarding/redeem", json=payload)
    assert repeated.status_code == 401
    assert repeated.json()["code"] == "invalid_onboarding_ticket"
    assert token not in repeated.text and code not in repeated.text
    assert (
        admin_web.client.post("/api/v1/onboarding/libraries", json={"code": code}).status_code
        == 401
    )
    with admin_web.engine.connect() as connection:
        credentials = connection.execute(select(Credential.id)).scalars().all()
        assert credentials == [delivery["credential_id"]]
        ticket = connection.execute(select(OnboardingTicket.__table__)).mappings().one()
        assert ticket["consumed_at"] is not None
        assert ticket["credential_id"] == delivery["credential_id"]
        assert ticket["code_hash"] == sha256(code.encode("ascii")).digest()
        records = connection.execute(select(RequestLogRecord.__table__)).mappings().all()

    assert records
    statuses = {(row["route_template"], row["status_code"]) for row in records}
    assert ("/api/v1/onboarding/redeem", 201) in statuses
    assert ("/api/v1/onboarding/redeem", 401) in statuses
    assert ("/api/v1/onboarding/libraries", 200) in statuses
    assert ("/api/v1/auth/whoami", 200) in statuses
    assert all(row["completion"] == "completed" for row in records)
    serialized_logs = json.dumps([dict(row) for row in records], sort_keys=True)
    for private_value in (code, token, name, description, json.dumps(payload)):
        assert private_value not in serialized_logs
        assert private_value not in caplog.text
