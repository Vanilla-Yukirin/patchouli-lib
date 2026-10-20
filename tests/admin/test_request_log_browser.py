"""Request-log JSON pagination retains start ordering, filters and actor boundaries."""

from time import time_ns

from helpers import AdminWeb, create_structure
from helpers import admin_web as admin_web
from sqlalchemy import insert, select, update

from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.request_log_filters import RequestLogFilters
from patchouli_lib.auth.models import Caller
from patchouli_lib.database import immediate_transaction
from patchouli_lib.request_log import RequestLogRepository, RequestLogWrite
from patchouli_lib.request_log.middleware import cleanup_request_logs_once
from patchouli_lib.request_log.models import RequestLogRecord


def seed(web: AdminWeb) -> tuple[int, str, str]:
    library, _, _ = create_structure(web)
    caller = "b" * 32
    recent = time_ns() // 1000 - 23_000_000
    with immediate_transaction(web.engine) as connection:
        connection.execute(
            insert(Caller),
            dict(
                id=caller,
                library_id=library,
                kind="agent",
                name="Synthetic",
                description="",
                policy_version=1,
                created_at=1,
                updated_at=1,
            ),
        )
        repository = RequestLogRepository(connection)
        for number in range(1, 24):
            repository.add(
                RequestLogWrite(
                    request_id=f"req_{number:032x}",
                    method="POST" if number < 23 else "GET",
                    route_template="/api/v1/auth/whoami",
                    status_code=200 if number % 2 else 403,
                    completion="completed",
                    occurred_at=recent + number * 1_000_000
                    if number < 23
                    else recent - 31 * 86_400_000_000,
                    duration_us=number * 1000,
                    caller_id=caller if number <= 5 else None,
                    home_library_id=library if number <= 5 else None,
                )
            )
    return recent, library, caller


def test_request_log_json_pagination_survives_retention_and_binds_session(
    admin_web: AdminWeb,
) -> None:
    web = admin_web
    recent, _, _ = seed(web)
    first = web.client.get("/admin/api/requests")
    assert first.status_code == 200 and "no-store" in first.headers["cache-control"]
    body = first.json()
    assert len(body["items"]) == 20 and body["next_cursor"].startswith("rl1.")
    assert body["items"][0]["request_id"] == f"req_{22:032x}"
    assert body["items"][-1]["request_id"] == f"req_{3:032x}"
    cursor = body["next_cursor"]
    with immediate_transaction(web.engine) as connection:
        connection.execute(
            update(RequestLogRecord)
            .where(RequestLogRecord.request_id == f"req_{3:032x}")
            .values(occurred_at=recent - 31 * 86_400_000_000)
        )
    cleanup_request_logs_once(web.engine)
    with web.engine.connect() as connection:
        assert (
            connection.scalar(
                select(RequestLogRecord.id).where(RequestLogRecord.request_id == f"req_{3:032x}")
            )
            is None
        )
    second = web.client.get("/admin/api/requests", params={"before": cursor})
    assert second.status_code == 200
    assert [item["request_id"] for item in second.json()["items"]] == [
        f"req_{2:032x}",
        f"req_{1:032x}",
    ]
    assert second.json()["next_cursor"] is None
    assert (
        web.client.get(
            "/admin/api/requests", params={"before": cursor, "status": "200"}
        ).status_code
        == 422
    )
    web.client.cookies.clear()
    assert web.client.get("/admin/api/requests").status_code == 401


def test_request_log_filters_actor_and_late_commit_order(admin_web: AdminWeb) -> None:
    web = admin_web
    recent, library, caller = seed(web)
    with immediate_transaction(web.engine) as connection:
        RequestLogRepository(connection).add(
            RequestLogWrite(
                request_id=f"req_{24:032x}",
                method="PATCH",
                route_template="/api/v1/retired/{legacy_id}",
                status_code=204,
                completion="completed",
                occurred_at=recent + 10_500_000,
                duration_us=15_000_000,
            )
        )
        RequestLogRepository(connection).add(
            RequestLogWrite(
                request_id=f"req_{25:032x}",
                method="GET",
                route_template="<unmatched>",
                status_code=200,
                completion="interrupted",
                occurred_at=recent + 24_000_000,
                duration_us=1000,
                caller_id=caller,
                home_library_id=library,
            )
        )
    model = AdminReadModel(web.engine)
    first = model.request_log_page(filters=RequestLogFilters())
    ids = [item.request_id for item in first.items]
    assert ids.index(f"req_{11:032x}") < ids.index(f"req_{24:032x}") < ids.index(f"req_{10:032x}")
    actor = model.request_log_page(
        filters=RequestLogFilters(status="interrupted"), actor=(library, caller)
    )
    assert [item.request_id for item in actor.items] == [f"req_{25:032x}"]
    filtered = model.request_log_page(
        filters=RequestLogFilters(
            method="POST", status="200", since_us=recent + 10_000_000, until_us=recent + 15_000_000
        )
    )
    assert [item.request_id for item in filtered.items] == [f"req_{13:032x}", f"req_{11:032x}"]
    retired = web.client.get(
        "/admin/api/requests", params={"method": "PATCH", "route": "/api/v1/retired/{legacy_id}"}
    )
    assert retired.status_code == 200 and len(retired.json()["items"]) == 1
    for query in (
        "route=https%3A%2F%2Finvalid%2Fsecret-token",
        "status=200&status=403",
        "since=2026-01-01T00%3A00%3A00",
    ):
        response = web.client.get("/admin/api/requests?" + query)
        assert response.status_code == 422 and "secret-token" not in response.text
