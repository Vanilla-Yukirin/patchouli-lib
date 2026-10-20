"""Shared migrated JSON administration fixture, without legacy HTML admission."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import AdminSessionCodec, MasterAdminSession
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction

ORIGIN = "https://admin.example.invalid"
MASTER_TOKEN = "synthetic migrated workspace master token"
SIGNING = "s" * 32


@dataclass(frozen=True)
class AdminWeb:
    client: TestClient
    engine: Engine
    csrf: str
    session: MasterAdminSession

    @property
    def headers(self) -> dict[str, str]:
        return {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}

    def post(self, path: str, data: Any = None, **kwargs: Any) -> Any:
        return self.client.post(
            path, json={} if data is None else data, headers=self.headers, **kwargs
        )


@pytest.fixture
def admin_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AdminWeb]:
    database_url = f"sqlite:///{(tmp_path / 'admin-json.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": database_url,
                "admin_session_signing_secret": SIGNING,
                "retrieval_cursor_signing_secret": "r" * 32,
            }
        )
    )
    with immediate_transaction(app.state.engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(MASTER_TOKEN, now=1_000)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        login = client.post(
            "/admin/api/session", json={"token": MASTER_TOKEN}, headers={"Origin": ORIGIN}
        )
        assert login.status_code == 200
        session = AdminSessionCodec(SIGNING.encode(), ttl_seconds=600).verify_master(
            client.cookies["patchouli_admin_session"]
        )
        assert session is not None
        yield AdminWeb(client, app.state.engine, login.json()["csrf_token"], session)
    app.state.engine.dispose()


def create_structure(web: AdminWeb) -> tuple[str, str, str]:
    library = web.post(
        "/admin/api/libraries", {"name": "Synthetic Library", "description": "Summary"}
    )
    assert library.status_code == 201, library.text
    library_id = library.json()["id"]
    section = web.post(
        f"/admin/api/libraries/{library_id}/sections",
        {"name": "Synthetic Section", "description": "Category"},
    )
    assert section.status_code == 201, section.text
    section_id = section.json()["id"]
    book = web.post(
        f"/admin/api/sections/{section_id}/books", {"name": "Synthetic Book", "summary": "Context"}
    )
    assert book.status_code == 201, book.text
    return library_id, section_id, book.json()["id"]
