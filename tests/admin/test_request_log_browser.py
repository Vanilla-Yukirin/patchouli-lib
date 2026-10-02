from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from time import time_ns
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, select, update

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Library
from patchouli_lib.request_log import RequestLogRepository, RequestLogWrite
from patchouli_lib.request_log.middleware import cleanup_request_logs_once
from patchouli_lib.request_log.models import RequestLogRecord

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
            insert(Library).values(
                id=_LIBRARY_ID,
                name="Synthetic Library",
                description="",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        connection.execute(
            insert(Caller).values(
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


def _older_requests_url(page: str) -> str:
    match = re.search(r'<a href="([^"]+)">(?:Older requests|更早的请求)</a>', page)
    assert match is not None
    return unescape(match.group(1))


def _utc(micros: int) -> str:
    value = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


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
    older_url = _older_requests_url(first.text)
    cursor = parse_qs(urlsplit(older_url).query)["before"][0]
    assert cursor.startswith("rl1.")
    assert 'href="/admin/requests?lang=en"' in first.text

    second = client.get(older_url + "&lang=zh-CN")
    assert second.status_code == 200
    assert f"req_{2:032x}" in second.text
    assert f"req_{1:032x}" in second.text
    assert f"req_{3:032x}" not in second.text
    assert f'href="/admin/requests?before={cursor}&amp;lang=en"' in second.text
    assert client.get("/admin/requests?before=0").status_code == 400
    assert client.get("/admin/requests?before=1&before=2").status_code == 400
    assert str(recent_us) not in first.text


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


def test_request_log_combines_route_method_result_and_utc_range(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    recent_us = _seed(engine)
    _login(client, _MASTER_TOKEN)
    response = client.get(
        "/admin/requests",
        params={
            "route": "/api/v1/auth/whoami",
            "method": "POST",
            "status": "200",
            "since": _utc(recent_us + 10_000_000),
            "until": _utc(recent_us + 15_000_000),
        },
    )
    assert response.status_code == 200
    assert f"req_{11:032x}" in response.text
    assert f"req_{13:032x}" in response.text
    assert f"req_{9:032x}" not in response.text
    assert f"req_{15:032x}" not in response.text
    assert f"req_{12:032x}" not in response.text
    assert '<option value="/api/v1/auth/whoami" selected>' in response.text
    assert '<option value="POST" selected>' in response.text
    assert 'value="200"' in response.text
    assert (
        client.get(
            "/admin/requests", params={"route": "/api/v1/auth/whoami", "method": "GET"}
        ).status_code
        == 200
    )
    old_only = client.get("/admin/requests", params={"method": "GET"})
    assert f"req_{23:032x}" not in old_only.text
    assert "No API requests in the retained period." in old_only.text
    unknown = client.get("/admin/requests", params={"route": "/api/v1/removed"})
    assert unknown.status_code == 200
    assert "No API requests in the retained period." in unknown.text


def test_interrupted_filter_and_identity_scope_apply_together(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    recent_us = _seed(engine)
    with immediate_transaction(engine) as connection:
        repository = RequestLogRepository(connection)
        repository.add(
            RequestLogWrite(
                request_id=f"req_{24:032x}",
                method="GET",
                route_template="<unmatched>",
                status_code=200,
                completion="interrupted",
                occurred_at=recent_us + 24_000_000,
                duration_us=1_000,
                caller_id=_CALLER_ID,
                home_library_id=_LIBRARY_ID,
            )
        )
    _login(client, _MASTER_TOKEN)
    path = f"/admin/libraries/{_LIBRARY_ID}/callers/{_CALLER_ID}/requests"
    interrupted = client.get(path, params={"route": "<unmatched>", "status": "interrupted"})
    assert interrupted.status_code == 200
    assert f"req_{24:032x}" in interrupted.text
    assert f"req_{5:032x}" not in interrupted.text
    completed = client.get(path, params={"route": "<unmatched>", "status": "200"})
    assert f"req_{24:032x}" not in completed.text


def test_request_log_cursor_is_bound_to_filters_identity_and_session(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    _seed(engine)
    _login(client, _MASTER_TOKEN)
    first = client.get("/admin/requests", params={"route": "/api/v1/auth/whoami"})
    older_url = _older_requests_url(first.text)
    assert client.get(older_url).status_code == 200
    changed_filter = older_url + "&status=200"
    assert client.get(changed_filter).status_code == 400
    identity_path = f"/admin/libraries/{_LIBRARY_ID}/callers/{_CALLER_ID}/requests"
    before = parse_qs(urlsplit(older_url).query)["before"][0]
    assert client.get(identity_path, params={"before": before}).status_code == 400
    assert client.get(older_url.replace("rl1.", "rl1.x", 1)).status_code == 400
    _login(client, _MASTER_TOKEN)
    assert client.get(older_url).status_code == 400


def test_request_log_pagination_survives_retention_of_its_anchor(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    recent_us = _seed(engine)
    _login(client, _MASTER_TOKEN)
    first = client.get("/admin/requests")
    assert first.status_code == 200
    assert f"req_{3:032x}" in first.text
    older_url = _older_requests_url(first.text)

    # The next page is keyed by (occurred_at, id), not by looking up the anchor row.
    with immediate_transaction(engine) as connection:
        connection.execute(
            update(RequestLogRecord)
            .where(RequestLogRecord.request_id == f"req_{3:032x}")
            .values(occurred_at=recent_us - 31 * 86_400_000_000)
        )
    cleanup_request_logs_once(engine)
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(RequestLogRecord.id).where(RequestLogRecord.request_id == f"req_{3:032x}")
            ).first()
            is None
        )

    second = client.get(older_url)
    assert second.status_code == 200
    assert f"req_{2:032x}" in second.text
    assert f"req_{1:032x}" in second.text
    assert f"req_{3:032x}" not in second.text
    assert f"req_{4:032x}" not in second.text


def test_retained_obsolete_route_is_selectable_and_exactly_filterable(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    recent_us = _seed(engine)
    old_template = "/api/v1/retired/{legacy_id}"
    with immediate_transaction(engine) as connection:
        RequestLogRepository(connection).add(
            RequestLogWrite(
                request_id=f"req_{24:032x}",
                method="PATCH",
                route_template=old_template,
                status_code=204,
                completion="completed",
                occurred_at=recent_us + 500_000,
                duration_us=1_000,
            )
        )
    _login(client, _MASTER_TOKEN)
    listing = client.get("/admin/requests")
    assert listing.status_code == 200
    assert f'<option value="{old_template}">' in listing.text

    filtered = client.get("/admin/requests", params={"method": "PATCH", "route": old_template})
    assert filtered.status_code == 200
    assert f"req_{24:032x}" in filtered.text
    assert f"req_{1:032x}" not in filtered.text
    assert f"req_{22:032x}" not in filtered.text


@pytest.mark.parametrize(
    "query",
    [
        "route=https%3A%2F%2Fexample.invalid%2Fsecret-token",
        "route=%2Fapi%2Fv1%2Fprivate%3Ftoken%3Dsecret-token",
        "method=TRACE",
        "status=099",
        "status=600",
        "status=200&status=403",
        "since=2026-01-01T00%3A00%3A00",
        "since=2026-01-02T00%3A00%3A00Z&until=2026-01-01T00%3A00%3A00Z",
    ],
)
def test_bad_request_log_filters_are_rejected_without_echo(
    browser: tuple[TestClient, Engine], query: str
) -> None:
    client, engine = browser
    _seed(engine)
    _login(client, _MASTER_TOKEN)
    response = client.get(f"/admin/requests?{query}")
    assert response.status_code == 400
    assert "secret-token" not in response.text
    assert "req_" not in response.text
