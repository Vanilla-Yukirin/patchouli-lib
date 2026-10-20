"""First setup JSON admission uses the deployment proof and a separate signed cookie."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from patchouli_lib.admin.json_api import create_admin_json_router
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.config import Settings
from patchouli_lib.database import build_engine


def test_first_setup_origin_cookie_csrf_and_existing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'setup-json.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    proof = "synthetic deployment setup proof 0001"
    token = "synthetic first JSON setup master token"
    origin = "https://admin.example.invalid"
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "admin_setup_token": proof,
            "admin_session_signing_secret": "s" * 32,
        }
    )
    engine = build_engine(database_url)
    app = FastAPI()
    app.include_router(create_admin_json_router(engine, settings))
    with TestClient(app, base_url=origin) as client:
        state = client.get("/admin/api/session").json()
        assert state["setup_required"] and state["setup_available"]
        payload = {"setup_token": proof, "master_token": token, "confirmation": token}
        assert (
            client.post("/admin/api/setup", json=payload, headers={"Origin": origin}).status_code
            == 403
        )
        headers = {"Origin": origin, "X-CSRF-Token": state["setup_csrf_token"]}
        assert (
            client.post(
                "/admin/api/setup",
                json=payload,
                headers={**headers, "Origin": "https://other.invalid"},
            ).status_code
            == 403
        )
        invalid = client.post(
            "/admin/api/setup", json={**payload, "setup_token": "wrong"}, headers=headers
        )
        assert invalid.status_code == 403 and token not in invalid.text
        created = client.post("/admin/api/setup", json=payload, headers=headers)
        assert created.status_code == 200 and created.json()["authenticated"] is True
        assert token not in created.text and proof not in created.text
        cookie = created.headers.get_list("set-cookie")[0]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        with engine.connect() as connection:
            assert MasterTokenRepository(connection).authenticate(token) is not None
        client.cookies.clear()
        legacy, session = AdminSessionCodec(b"s" * 32, ttl_seconds=600).issue()
        client.cookies.set("patchouli_admin_session", legacy, path="/admin")
        assert client.get("/admin/api/libraries").status_code == 401
        assert (
            client.post(
                "/admin/api/setup",
                json=payload,
                headers={"Origin": origin, "X-CSRF-Token": session.csrf_token},
            ).status_code
            == 403
        )
    engine.dispose()
