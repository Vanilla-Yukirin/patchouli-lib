"""Page lifecycle HTTP contracts against an Alembic-migrated database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from content.conftest import OPERATION_TIME, ArchiveScope
from content.conftest import archive_scope as archive_scope
from content.conftest import content_engine as content_engine
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from patchouli_lib.api.archive_routes import create_archive_router
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.api.retrieval_routes import create_retrieval_router
from patchouli_lib.auth.models import AuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewSectionGrant, SectionAction
from patchouli_lib.content.models import Page, PageLifecycleEvent, Revision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.retrieval.cursor import CursorCodec

REQUEST_ID = "req_" + "9" * 32
CURSOR_SECRET = b"synthetic-lifecycle-cursor-key!!"


@dataclass(frozen=True, slots=True)
class LifecycleApi:
    engine: Engine
    scope: ArchiveScope
    client: TestClient


@pytest.fixture
def lifecycle_api(content_engine: Engine, archive_scope: ArchiveScope) -> Iterator[LifecycleApi]:
    # The content fixtures upgrade through Alembic, including lifecycle triggers.
    with immediate_transaction(content_engine) as connection:
        auth = AuthRepository(connection)
        for action in (SectionAction.PAGE_READ, SectionAction.QUERY):
            auth.add_grant(
                NewSectionGrant(
                    library_id=archive_scope.library_id,
                    caller_id=archive_scope.caller_id,
                    section_id=archive_scope.section_id,
                    action=action,
                    created_at=1_000_000,
                )
            )
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(application)
    application.add_middleware(
        RequestIDMiddleware,
        request_id_factory=lambda: REQUEST_ID,
    )
    codec = CursorCodec(CURSOR_SECRET)
    application.include_router(
        create_archive_router(
            content_engine,
            clock=lambda: OPERATION_TIME,
            cursor_codec=codec,
        )
    )
    application.include_router(
        create_retrieval_router(
            content_engine,
            cursor_codec=codec,
            clock=lambda: OPERATION_TIME,
        )
    )
    with TestClient(application, raise_server_exceptions=False) as client:
        yield LifecycleApi(content_engine, archive_scope, client)


def _auth(api: LifecycleApi) -> tuple[str, str]:
    return "Authorization", f"Bearer {api.scope.token.value}"


def _create(api: LifecycleApi, *, title: str, key: str) -> Any:
    boundary = "synthetic-lifecycle-boundary"
    metadata = json.dumps(
        {
            "title": title,
            "occurred_at": "2026-08-13T10:00:00.123456Z",
            "source": {"kind": "synthetic"},
        },
        separators=(",", ":"),
    ).encode()
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode()
        + metadata
        + (
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="content"\r\n'
            "Content-Type: text/markdown;charset=utf-8\r\n\r\n"
            f"# {title}\n\r\n--{boundary}--\r\n"
        ).encode()
    )
    return api.client.post(
        f"/api/v1/sections/{api.scope.section_id}/books/{api.scope.book_id}/pages",
        headers=[
            _auth(api),
            ("Idempotency-Key", key),
            ("Content-Type", f"multipart/form-data; boundary={boundary}"),
        ],
        content=body,
    )


def _page_path(api: LifecycleApi, page_id: str) -> str:
    return f"/api/v1/sections/{api.scope.section_id}/pages/{page_id}"


def _lifecycle_headers(api: LifecycleApi, *, key: str, etag: str) -> list[tuple[str, str]]:
    return [_auth(api), ("Idempotency-Key", key), ("If-Match", etag)]


def _problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith("application/problem+json")
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.json()["code"] == code


def _mutation_counts(engine: Engine) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        return cast(
            tuple[int, int, int, int],
            tuple(
                connection.scalar(select(func.count()).select_from(table)) or 0
                for table in (PageLifecycleEvent, Revision, AuditEvent, IdempotencyRecord)
            ),
        )


def test_delete_restore_replay_and_read_visibility(lifecycle_api: LifecycleApi) -> None:
    api = lifecycle_api
    created = _create(api, title="Lifecycle One", key="lifecycle-create-one")
    assert created.status_code == 201
    page_id = created.json()["page"]["page_id"]
    path = _page_path(api, page_id)
    current = api.client.get(path, headers=[_auth(api)])
    assert current.status_code == 200
    assert current.headers["ETag"] == created.headers["ETag"]

    deleted = api.client.delete(
        path,
        headers=_lifecycle_headers(api, key="lifecycle-delete-one", etag=created.headers["ETag"]),
    )
    assert deleted.status_code == 200
    assert deleted.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert deleted.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert deleted.headers["ETag"] != created.headers["ETag"]
    assert deleted.json()["state"] == "trashed"
    assert deleted.json()["deleted_at"] is not None
    assert deleted.json()["current_revision_number"] == 1
    assert api.client.get(path, headers=[_auth(api)]).status_code == 404
    assert api.client.get(f"{path}/revisions/1", headers=[_auth(api)]).status_code == 404

    trash_path = f"/api/v1/sections/{api.scope.section_id}/trash"
    detail = api.client.get(f"{trash_path}/{page_id}", headers=[_auth(api)])
    assert detail.status_code == 200
    assert detail.json()["page_id"] == page_id
    assert detail.json()["state"] == "trashed"
    assert "title" not in detail.json()
    assert "content" not in detail.text
    assert detail.headers["ETag"] == deleted.headers["ETag"]
    assert [
        item["page_id"] for item in api.client.get(trash_path, headers=[_auth(api)]).json()["items"]
    ] == [page_id]

    before_replay = _mutation_counts(api.engine)
    delete_replay = api.client.delete(
        path,
        headers=_lifecycle_headers(api, key="lifecycle-delete-one", etag=created.headers["ETag"]),
    )
    assert delete_replay.status_code == 200
    assert delete_replay.headers["Idempotency-Replayed"] == "true"
    assert delete_replay.content == deleted.content
    assert delete_replay.headers["ETag"] == deleted.headers["ETag"]
    assert _mutation_counts(api.engine) == before_replay

    restored = api.client.post(
        f"{path}/restore",
        headers=_lifecycle_headers(api, key="lifecycle-restore-one", etag=deleted.headers["ETag"]),
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "active"
    assert restored.json()["deleted_at"] is None
    assert restored.headers["ETag"] != deleted.headers["ETag"]
    assert api.client.get(path, headers=[_auth(api)]).status_code == 200
    assert api.client.get(f"{path}/revisions/1", headers=[_auth(api)]).status_code == 200
    restored_counts = _mutation_counts(api.engine)
    assert restored_counts[0] == 2
    assert restored_counts[1] == 1
    restore_replay = api.client.post(
        f"{path}/restore",
        headers=_lifecycle_headers(api, key="lifecycle-restore-one", etag=deleted.headers["ETag"]),
    )
    assert restore_replay.status_code == 200
    assert restore_replay.headers["Idempotency-Replayed"] == "true"
    assert restore_replay.content == restored.content
    assert _mutation_counts(api.engine) == restored_counts
    _problem(
        api.client.get(f"{trash_path}/{page_id}", headers=[_auth(api)]), 404, "resource_not_found"
    )
    assert api.client.get(trash_path, headers=[_auth(api)]).json()["items"] == []

    # An old idempotent retry must not delete the restored Page again.
    after_restore = _mutation_counts(api.engine)
    old_delete_replay = api.client.delete(
        path,
        headers=_lifecycle_headers(api, key="lifecycle-delete-one", etag=created.headers["ETag"]),
    )
    assert old_delete_replay.status_code == 200
    assert old_delete_replay.headers["Idempotency-Replayed"] == "true"
    assert _mutation_counts(api.engine) == after_restore
    with api.engine.connect() as connection:
        assert connection.scalar(select(Page.deleted_at).where(Page.page_id == page_id)) is None


def test_deleted_page_replays_prior_occurrence_correction_but_rejects_fresh_one(
    lifecycle_api: LifecycleApi,
) -> None:
    api = lifecycle_api
    created = _create(api, title="Occurrence Before Delete", key="occurrence-delete-create")
    assert created.status_code == 201
    page_path = _page_path(api, created.json()["page"]["page_id"])
    occurrence_path = f"{page_path}/occurrence"
    body = {"occurred_at": "2026-08-14T10:00:00.123456Z"}
    prior_headers = _lifecycle_headers(
        api, key="occurrence-before-delete", etag=created.headers["ETag"]
    )
    corrected = api.client.patch(occurrence_path, headers=prior_headers, json=body)
    assert corrected.status_code == 200
    deleted = api.client.delete(
        page_path,
        headers=_lifecycle_headers(
            api, key="occurrence-then-delete", etag=corrected.headers["ETag"]
        ),
    )
    assert deleted.status_code == 200
    baseline = _mutation_counts(api.engine)
    replayed = api.client.patch(occurrence_path, headers=prior_headers, json=body)
    assert replayed.status_code == 200
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.content == corrected.content
    _problem(
        api.client.patch(
            occurrence_path,
            headers=_lifecycle_headers(
                api, key="occurrence-after-delete", etag=corrected.headers["ETag"]
            ),
            json={"occurred_at": "2026-08-15T10:00:00.123456Z"},
        ),
        404,
        "resource_not_found",
    )
    assert _mutation_counts(api.engine) == baseline


def test_lifecycle_rejects_stale_missing_and_noop_preconditions(
    lifecycle_api: LifecycleApi,
) -> None:
    api = lifecycle_api
    created = _create(api, title="Lifecycle Preconditions", key="lifecycle-pre-create")
    assert created.status_code == 201
    page_id = created.json()["page"]["page_id"]
    path = _page_path(api, page_id)
    _problem(
        api.client.post(
            f"{path}/restore",
            headers=_lifecycle_headers(
                api, key="lifecycle-premature-restore", etag=created.headers["ETag"]
            ),
        ),
        409,
        "page_state_unchanged",
    )
    _problem(
        api.client.delete(path, headers=[_auth(api), ("Idempotency-Key", "missing-match")]),
        428,
        "precondition_required",
    )
    _problem(
        api.client.delete(path, headers=[_auth(api), ("If-Match", created.headers["ETag"])]),
        422,
        "request_validation_failed",
    )
    _problem(
        api.client.request(
            "DELETE",
            path,
            headers=_lifecycle_headers(api, key="body-rejected", etag=created.headers["ETag"]),
            content=b"unexpected",
        ),
        422,
        "request_validation_failed",
    )
    deleted = api.client.delete(
        path,
        headers=_lifecycle_headers(api, key="lifecycle-pre-delete", etag=created.headers["ETag"]),
    )
    assert deleted.status_code == 200
    baseline = _mutation_counts(api.engine)
    _problem(
        api.client.delete(
            path,
            headers=_lifecycle_headers(
                api, key="lifecycle-pre-delete", etag=deleted.headers["ETag"]
            ),
        ),
        409,
        "idempotency_mismatch",
    )
    _problem(
        api.client.delete(
            path,
            headers=_lifecycle_headers(
                api, key="lifecycle-stale-delete", etag=created.headers["ETag"]
            ),
        ),
        412,
        "page_conflict",
    )
    _problem(
        api.client.delete(
            path,
            headers=_lifecycle_headers(
                api, key="lifecycle-noop-delete", etag=deleted.headers["ETag"]
            ),
        ),
        409,
        "page_state_unchanged",
    )
    _problem(
        api.client.post(
            f"{path}/restore",
            headers=_lifecycle_headers(
                api, key="lifecycle-stale-restore", etag=created.headers["ETag"]
            ),
        ),
        412,
        "page_conflict",
    )
    assert _mutation_counts(api.engine) == baseline


def test_trash_requires_write_permission_and_signed_cursor(lifecycle_api: LifecycleApi) -> None:
    api = lifecycle_api
    page_ids: set[str] = set()
    for index in range(3):
        created = _create(api, title=f"Pagination {index}", key=f"lifecycle-page-{index}")
        assert created.status_code == 201
        page_id = created.json()["page"]["page_id"]
        page_ids.add(page_id)
        deleted = api.client.delete(
            _page_path(api, page_id),
            headers=_lifecycle_headers(
                api, key=f"lifecycle-delete-{index}", etag=created.headers["ETag"]
            ),
        )
        assert deleted.status_code == 200
    trash_path = f"/api/v1/sections/{api.scope.section_id}/trash"
    first = api.client.get(f"{trash_path}?limit=1", headers=[_auth(api)])
    assert first.status_code == 200
    assert "title" not in first.json()["items"][0]
    assert "content" not in first.text
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str)
    second = api.client.get(f"{trash_path}?limit=1&cursor={cursor}", headers=[_auth(api)])
    assert second.status_code == 200
    third = api.client.get(
        f"{trash_path}?limit=1&cursor={second.json()['next_cursor']}", headers=[_auth(api)]
    )
    assert third.status_code == 200
    assert third.json()["next_cursor"] is None
    assert {
        item["page_id"] for page in (first, second, third) for item in page.json()["items"]
    } == page_ids
    _problem(
        api.client.get(f"{trash_path}?limit=2&cursor={cursor}", headers=[_auth(api)]),
        400,
        "invalid_cursor",
    )
    _problem(
        api.client.get(f"{trash_path}?limit=1&cursor={cursor}x", headers=[_auth(api)]),
        400,
        "invalid_cursor",
    )
    _problem(api.client.get(trash_path), 401, "authentication_required")
    _problem(
        api.client.get(f"{trash_path}/not-a-page-id", headers=[_auth(api)]),
        422,
        "request_validation_failed",
    )
    _problem(api.client.get(f"{trash_path}/{next(iter(page_ids))}"), 401, "authentication_required")
    _problem(
        api.client.get(
            f"/api/v1/sections/{'f' * 32}/trash",
            headers=[_auth(api)],
        ),
        404,
        "resource_not_found",
    )
    with immediate_transaction(api.engine) as connection:
        assert AuthRepository(connection).remove_grant(
            api.scope.library_id,
            api.scope.caller_id,
            api.scope.section_id,
            SectionAction.ARCHIVE_WRITE,
        )
    _problem(api.client.get(trash_path, headers=[_auth(api)]), 403, "insufficient_scope")
    _problem(
        api.client.get(f"{trash_path}/{next(iter(page_ids))}", headers=[_auth(api)]),
        403,
        "insufficient_scope",
    )


def test_writer_without_read_grants_cannot_enumerate_trashed_pages(
    lifecycle_api: LifecycleApi,
) -> None:
    api = lifecycle_api
    created = _create(api, title="Writer Only", key="writer-only-create")
    assert created.status_code == 201
    page_id = created.json()["page"]["page_id"]
    deleted = api.client.delete(
        _page_path(api, page_id),
        headers=_lifecycle_headers(api, key="writer-only-delete", etag=created.headers["ETag"]),
    )
    assert deleted.status_code == 200
    with immediate_transaction(api.engine) as connection:
        repository = AuthRepository(connection)
        for action in (SectionAction.PAGE_READ, SectionAction.QUERY):
            assert repository.remove_grant(
                api.scope.library_id,
                api.scope.caller_id,
                api.scope.section_id,
                action,
            )
    trash_path = f"/api/v1/sections/{api.scope.section_id}/trash"
    _problem(api.client.get(trash_path, headers=[_auth(api)]), 403, "insufficient_scope")
    _problem(
        api.client.get(f"{trash_path}/{page_id}", headers=[_auth(api)]),
        403,
        "insufficient_scope",
    )
