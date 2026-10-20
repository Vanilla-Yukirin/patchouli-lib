from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.config import Settings
from patchouli_lib.database import DatabaseNotReadyError


def test_service_info(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "PatchouliLib"
    assert payload["status"] == "design-stage bootstrap"
    assert payload["version"]


@pytest.mark.parametrize(
    "base_url", ["https://library.example.invalid", "https://private.invalid:9443"]
)
def test_root_enters_enabled_console_without_changing_origin_or_authentication(
    tmp_path: Path,
    base_url: str,
) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'root-navigation.db').as_posix()}",
            "admin_session_signing_secret": "synthetic-session-secret-for-root-navigation",
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)
    with TestClient(application, base_url=base_url, follow_redirects=False) as admin_client:
        response = admin_client.get("/")
        assert response.status_code == 307
        assert response.headers["location"] == "/admin"
        assert response.headers["cache-control"] == "no-store, max-age=0"

        destination = admin_client.get("/", follow_redirects=True)
        assert destination.status_code == 200
        assert str(destination.url) == base_url + "/admin"
        assert destination.headers["content-type"].startswith("text/html")
        assert admin_client.get("/health/live").json() == {"status": "live"}


def test_liveness(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_unmigrated_database_is_not_ready_but_live(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert client.get("/health/live").json() == {"status": "live"}


def test_readiness_requires_current_alembic_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260929_0013")

    settings = Settings.model_validate({"environment": "test", "database_url": database_url})
    with TestClient(create_app(settings)) as migrated_client:
        assert migrated_client.get("/health/ready").status_code == 503
        assert migrated_client.get("/health/live").json() == {"status": "live"}

        command.upgrade(config, "head")

        ready = migrated_client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}


def test_readiness_hides_database_failure_details(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_readiness(_engine: Engine) -> None:
        raise DatabaseNotReadyError("private database detail")

    monkeypatch.setattr("patchouli_lib.app.check_database", fail_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == 503
    assert payload["code"] == "internal_error"
    assert payload["detail"] == "The server could not complete the request."
    assert payload["request_id"].startswith("req_")
    assert "private database detail" not in response.text
