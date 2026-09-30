from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from time import time_ns

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Library
from patchouli_lib.request_log import RequestLogRepository, RequestLogWrite

_ORIGIN = "https://admin.example.invalid"
_LEGACY_PASSWORD = "synthetic legacy browser password"
_MASTER_TOKEN = "synthetic master token material for log browser"
_LIBRARY_ID = "a" * 32
_CALLER_ID = "b" * 32


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'request-log-browser.db').as_posix()}",
            "admin_password_hash": hash_password(
                _LEGACY_PASSWORD,
                salt_factory=lambda size: b"l" * size,
                iterations=300_000,
            ),
            "admin_session_signing_secret": "s" * 32,
        }
    )
    application = create_app(settings)
    Caller.metadata.create_all(application.state.engine)
    with TestClient(application, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, application.state.engine


def _login(client: TestClient, password: str) -> None:
    response = client.post("/admin/login", data={"password": password}, headers={"Origin": _ORIGIN})
    assert response.status_code == 303


def _seed(engine: Engine) -> int:
    recent_us = time_ns() // 1_000 - 23_000_000
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "c" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
        connection.execute(
            Library.__table__.insert().values(
                id=_LIBRARY_ID,
                name="Synthetic Library",
                description="",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        connection.execute(
            Caller.__table__.insert().values(
                id=_CALLER_ID,
                library_id=_LIBRARY_ID,
                kind="agent",
                name="Synthetic Agent",
                description="",
                policy_version=1,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository = RequestLogRepository(connection)
        for number in range(1, 23):
            repository.add(
                RequestLogWrite(
                    request_id=f"req_{number:032x}",
                    method="POST",
                    route_template="/api/v1/auth/whoami",
                    status_code=200 if number % 2 else 403,
                    completion="completed",
                    occurred_at=recent_us + number * 1_000_000,
                    duration_us=number * 1_000,
                    caller_id=_CALLER_ID if number <= 5 else None,
                    home_library_id=_LIBRARY_ID if number <= 5 else None,
                    credential_id="d" * 32 if number <= 5 else None,
                )
            )
        repository.add(
            RequestLogWrite(
                request_id=f"req_{23:032x}",
                method="GET",
                route_template="/api/v1/auth/whoami",
                status_code=200,
                completion="completed",
                occurred_at=recent_us - 31 * 86_400_000_000,
                duration_us=1_000,
            )
        )
    return recent_us


def test_request_log_page_is_master_only_and_bounded(browser: tuple[TestClient, Engine]) -> None:
    client, engine = browser
    assert client.get("/admin/requests").status_code == 303
    _login(client, _LEGACY_PASSWORD)
    assert 'href="/admin/requests"' not in client.get("/admin").text
    legacy = client.get("/admin/requests")
    assert legacy.status_code == 403
    assert "req_" not in legacy.text

    recent_us = _seed(engine)
    _login(client, _MASTER_TOKEN)
    assert 'href="/admin/requests"' in client.get("/admin").text
    first = client.get("/admin/requests?lang=zh-CN")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store, max-age=0"
    assert "接口请求记录" in first.text
    assert "POST /api/v1/auth/whoami" in first.text
    assert f"req_{22:032x}" in first.text
    assert f"req_{23:032x}" not in first.text
    assert f"req_{3:032x}" in first.text
    assert f"req_{2:032x}" not in first.text
    cursor = f"{recent_us + 3_000_000}:3"
    assert f'href="/admin/requests?before={cursor}"' in first.text
    assert 'href="/admin/requests?lang=en"' in first.text

    second = client.get(f"/admin/requests?before={cursor}&lang=zh-CN")
    assert second.status_code == 200
    assert f"req_{2:032x}" in second.text
    assert f"req_{1:032x}" in second.text
    assert f"req_{3:032x}" not in second.text
    assert f'href="/admin/requests?before={cursor.replace(":", "%3A")}&amp;lang=en"' in second.text
    assert client.get("/admin/requests?before=0").status_code == 404
    assert client.get("/admin/requests?before=1&before=2").status_code == 404


def test_request_log_identity_filter_does_not_show_other_rows(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    _seed(engine)
    _login(client, _MASTER_TOKEN)
    path = f"/admin/libraries/{_LIBRARY_ID}/callers/{_CALLER_ID}/requests"
    response = client.get(path)
    assert response.status_code == 200
    assert "API requests by this identity" in response.text
    assert f"req_{5:032x}" in response.text
    assert f"req_{6:032x}" not in response.text
    identity = client.get(path.removesuffix("/requests"))
    assert identity.status_code == 200
    assert f'href="{path}"' in identity.text
    assert client.get(path.replace(_CALLER_ID, "f" * 32)).status_code == 404


def test_request_log_orders_by_request_start_not_late_commit(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    recent_us = _seed(engine)
    with immediate_transaction(engine) as connection:
        RequestLogRepository(connection).add(
            RequestLogWrite(
                request_id=f"req_{24:032x}",
                method="GET",
                route_template="/api/v1/auth/whoami",
                status_code=200,
                completion="completed",
                occurred_at=recent_us + 10_500_000,
                duration_us=15_000_000,
            )
        )
    _login(client, _MASTER_TOKEN)
    response = client.get("/admin/requests")
    assert response.status_code == 200
    first, delayed, next_row = (
        f"req_{11:032x}",
        f"req_{24:032x}",
        f"req_{10:032x}",
    )
    assert response.text.index(first) < response.text.index(delayed) < response.text.index(next_row)
