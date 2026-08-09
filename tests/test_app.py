import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from patchouli_lib.database import DatabaseNotReadyError


def test_service_info(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "PatchouliLib"
    assert payload["status"] == "design-stage bootstrap"
    assert payload["version"]


def test_liveness(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_readiness_checks_sqlite_and_fts5(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_hides_database_failure_details(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_readiness(_engine: Engine) -> None:
        raise DatabaseNotReadyError("private database detail")

    monkeypatch.setattr("patchouli_lib.app.check_database", fail_readiness)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "private database detail" not in response.text
