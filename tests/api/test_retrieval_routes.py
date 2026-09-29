from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from retrieval_read.conftest import (
    CALLER_ID,
    HIDDEN_SECTION_ID,
    QUERY_SECTION_ID,
    SECOND_QUERY_SECTION_ID,
    RetrievalScope,
)
from retrieval_read.conftest import (
    retrieval_engine as retrieval_engine_fixture,
)
from retrieval_read.conftest import (
    retrieval_scope as retrieval_scope_fixture,
)
from sqlalchemy import Engine, insert, select
from starlette.requests import Request
from starlette.routing import Route

from patchouli_lib.api import retrieval_routes as retrieval_routes_module
from patchouli_lib.api.authentication import BearerAuthentication
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import PROBLEM_MEDIA_TYPE, install_api_exception_handlers
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.api.retrieval_routes import _perform_read, create_retrieval_router
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content import page_current_etag
from patchouli_lib.content.models import Page
from patchouli_lib.database import immediate_transaction
from patchouli_lib.retrieval.cursor import CursorCodec
from patchouli_lib.retrieval.repository import RetrievalRepository

REQUEST_ID = "req_1234567890abcdef1234567890abcdef"
CURSOR_SECRET = b"synthetic-retrieval-cursor-key!!"
type QueryValue = str | int | float | bool | None
type QueryParams = dict[str, QueryValue] | list[tuple[str, QueryValue]]


@dataclass(frozen=True, slots=True)
class RetrievalApi:
    engine: Engine
    scope: RetrievalScope
    token: str
    cursor_codec: CursorCodec


@pytest.fixture
def retrieval_api(
    tmp_path: Path,
) -> Iterator[RetrievalApi]:
    engine_factory = cast(
        Callable[[Path], Iterator[Engine]],
        cast(Any, retrieval_engine_fixture).__wrapped__,
    )
    scope_factory = cast(
        Callable[[Engine], RetrievalScope],
        cast(Any, retrieval_scope_fixture).__wrapped__,
    )
    engine_iterator = engine_factory(tmp_path)
    retrieval_engine = next(engine_iterator)
    retrieval_scope = scope_factory(retrieval_engine)
    # create_all() does not run Alembic's 0013 backfill for this synthetic fixture.
    with immediate_transaction(retrieval_engine) as connection:
        connection.exec_driver_sql(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "SELECT library_id, page_uid, revision_id, revision_number, "
            "'legacy_markdown', 1, content_size_bytes, NULL FROM revisions"
        )
    with retrieval_engine.connect() as connection:
        credential = RetrievalRepository(connection).get_credential(
            retrieval_scope.library_id,
            CALLER_ID,
            retrieval_scope.authenticated.credential.id,
        )
        assert credential is not None

    # The service fixture deliberately stores only public credential metadata.
    # Issue an additional route-test credential so its one-time token never
    # crosses fixture or response boundaries.
    issued = generate_token()
    credential_id = "d" * 32
    with immediate_transaction(retrieval_engine) as connection:
        AuthRepository(connection).add_credential(
            NewCredential(
                id=credential_id,
                library_id=retrieval_scope.library_id,
                caller_id=CALLER_ID,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )

    try:
        yield RetrievalApi(
            engine=retrieval_engine,
            scope=retrieval_scope,
            token=issued.value,
            cursor_codec=CursorCodec(CURSOR_SECRET),
        )
    finally:
        with suppress(StopIteration):
            next(engine_iterator)


def _app(fixture: RetrievalApi, *, clock: int = 2_000_000) -> FastAPI:
    application = FastAPI()
    install_api_exception_handlers(application)
    application.add_middleware(
        RequestIDMiddleware,
        request_id_factory=lambda: REQUEST_ID,
    )
    application.include_router(
        create_retrieval_router(
            fixture.engine,
            cursor_codec=fixture.cursor_codec,
            clock=lambda: clock,
        )
    )
    return application


def _auth(fixture: RetrievalApi) -> dict[str, str]:
    return {"Authorization": f"Bearer {fixture.token}"}


def _get(
    fixture: RetrievalApi,
    path: str,
    *,
    params: QueryParams | None = None,
) -> Any:
    with TestClient(_app(fixture), raise_server_exceptions=False) as client:
        return client.get(path, headers=_auth(fixture), params=params)


def _assert_protected(response: Any) -> None:
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID


def _assert_problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["code"] == code
    assert response.json()["details"] == {}
    _assert_protected(response)


def test_router_exposes_exactly_the_seven_non_search_get_routes(
    retrieval_api: RetrievalApi,
) -> None:
    router = create_retrieval_router(
        retrieval_api.engine,
        cursor_codec=retrieval_api.cursor_codec,
    )

    inventory = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
        if isinstance(route, Route)
    }

    assert inventory == {
        ("/api/v1/sections", ("GET",)),
        ("/api/v1/sections/{section_id}/books", ("GET",)),
        ("/api/v1/sections/{section_id}/pages", ("GET",)),
        ("/api/v1/sections/{section_id}/pages/{page_id}", ("GET",)),
        (
            "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}",
            ("GET",),
        ),
        (
            "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}/files",
            ("GET",),
        ),
        (
            "/api/v1/sections/{section_id}/pages/{page_id}/revisions/"
            "{revision_number}/files/{filename:path}",
            ("GET",),
        ),
    }


def test_sections_use_deterministic_signed_pagination(retrieval_api: RetrievalApi) -> None:
    first = _get(retrieval_api, "/api/v1/sections", params={"limit": 1})
    assert first.status_code == 200
    assert [item["section_id"] for item in first.json()["items"]] == [QUERY_SECTION_ID]
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str) and cursor.startswith("plc1.")
    assert QUERY_SECTION_ID not in cursor
    _assert_protected(first)

    replay = _get(retrieval_api, "/api/v1/sections", params={"limit": 1})
    assert replay.json()["next_cursor"] == cursor

    second = _get(
        retrieval_api,
        "/api/v1/sections",
        params={"limit": 1, "cursor": cursor},
    )
    assert [item["section_id"] for item in second.json()["items"]] == [SECOND_QUERY_SECTION_ID]
    assert second.json()["next_cursor"] is None


def test_book_and_page_lists_are_current_only_and_have_exact_citations(
    retrieval_api: RetrievalApi,
) -> None:
    section_id = retrieval_api.scope.query_section_id
    books = _get(retrieval_api, f"/api/v1/sections/{section_id}/books", params={"limit": 1})
    assert books.status_code == 200
    assert len(books.json()["items"]) == 1
    assert "summary" not in books.text
    assert books.json()["next_cursor"].startswith("plc1.")

    pages = _get(retrieval_api, f"/api/v1/sections/{section_id}/pages")
    assert pages.status_code == 200
    item = pages.json()["items"][0]
    assert set(item) == {"page", "citation"}
    assert "revision" not in item and "content" not in pages.text
    assert item["citation"]["revision_id"] == item["page"]["current_revision_id"]
    assert item["citation"]["revision_number"] == item["page"]["current_revision_number"]


def test_current_and_history_reads_require_page_read_and_return_exact_body(
    retrieval_api: RetrievalApi,
) -> None:
    scope = retrieval_api.scope
    current_path = f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
    current = _get(retrieval_api, current_path)
    assert current.status_code == 200
    assert current.json()["revision"]["content"] == scope.current_content
    assert current.json()["citation"]["revision_number"] == 2
    with retrieval_api.engine.connect() as connection:
        stored = connection.execute(
            select(Page.occurred_at, Page.updated_at).where(Page.page_uid == scope.first_page_uid)
        ).one()
    assert current.headers["ETag"] == page_current_etag(
        scope.first_page_uid,
        scope.second_revision_id,
        2,
        stored[0],
        stored[1],
    )

    historical = _get(retrieval_api, f"{current_path}/revisions/1")
    assert historical.status_code == 200
    assert historical.json()["revision"]["content"] == scope.historical_content
    assert historical.json()["citation"]["revision_id"] == scope.first_revision_id
    assert historical.json()["page"]["current_revision_number"] == 2
    assert "ETag" not in historical.headers


def test_revision_file_manifest_and_download_are_exact_and_not_cacheable(
    retrieval_api: RetrievalApi,
) -> None:
    scope = retrieval_api.scope
    base = (
        f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}/revisions/1/files"
    )
    listing = _get(retrieval_api, base)
    assert listing.status_code == 200
    assert listing.json()["page_id"] == scope.first_page_id
    assert listing.json()["revision_id"] == scope.first_revision_id
    assert listing.json()["revision_number"] == 1
    assert listing.json()["files"] == [
        {
            "filename": "content.md",
            "size_bytes": len(scope.historical_content.encode()),
            "content_sha256": hashlib.sha256(scope.historical_content.encode()).hexdigest(),
        }
    ]
    assert scope.historical_content not in listing.text
    _assert_protected(listing)

    download = _get(retrieval_api, f"{base}/content.md")
    assert download.status_code == 200
    assert download.content == scope.historical_content.encode()
    assert download.headers["Content-Type"] == "application/octet-stream"
    assert download.headers["Content-Disposition"] == (
        "attachment; filename=\"download\"; filename*=UTF-8''content.md"
    )
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    _assert_protected(download)


def test_file_set_is_rejected_by_legacy_routes_after_authorization(
    retrieval_api: RetrievalApi,
) -> None:
    scope = retrieval_api.scope
    binary = b"\x00\xff"
    with immediate_transaction(retrieval_api.engine) as connection:
        connection.exec_driver_sql(
            "DELETE FROM revision_file_sets WHERE revision_id = ?",
            (scope.second_revision_id,),
        )
        connection.exec_driver_sql(
            "UPDATE revisions SET content_md = NULL, content_size_bytes = NULL, "
            "content_sha256 = NULL WHERE revision_id = ?",
            (scope.second_revision_id,),
        )
        connection.exec_driver_sql(
            "DELETE FROM revision_files WHERE revision_id = ?",
            (scope.second_revision_id,),
        )
        connection.exec_driver_sql(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "VALUES (?, ?, ?, 2, 'file_set_v1', 1, ?, ?)",
            (
                scope.library_id,
                scope.first_page_uid,
                scope.second_revision_id,
                len(binary),
                b"s" * 32,
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO revision_files "
            "(library_id, page_uid, revision_id, revision_number, filename, "
            "content_bytes, size_bytes, content_sha256) VALUES (?, ?, ?, 2, ?, ?, ?, ?)",
            (
                scope.library_id,
                scope.first_page_uid,
                scope.second_revision_id,
                "artifact.bin",
                binary,
                len(binary),
                hashlib.sha256(binary).digest(),
            ),
        )

    page_path = f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
    for path in (
        page_path,
        f"{page_path}/revisions/2",
        f"{page_path}/revisions/2/files",
        f"{page_path}/revisions/2/files/artifact.bin",
    ):
        response = _get(retrieval_api, path)
        _assert_problem(response, 409, "revision_format_unsupported")
        assert "artifact.bin" not in response.text
        assert "ETag" not in response.headers

    historical = _get(retrieval_api, f"{page_path}/revisions/1")
    assert historical.status_code == 200
    assert historical.json()["revision"]["content"] == scope.historical_content

    with TestClient(_app(retrieval_api), raise_server_exceptions=False) as client:
        unauthenticated = client.get(page_path)
    _assert_problem(unauthenticated, 401, "authentication_required")
    forbidden = _get(
        retrieval_api,
        f"/api/v1/sections/{scope.second_query_section_id}/pages/{scope.first_page_id}",
    )
    _assert_problem(forbidden, 403, "insufficient_scope")
    hidden = _get(
        retrieval_api,
        f"/api/v1/sections/{scope.hidden_section_id}/pages/{scope.first_page_id}",
    )
    _assert_problem(hidden, 404, "resource_not_found")


def test_missing_revision_manifest_is_a_sanitized_persistence_failure(
    retrieval_api: RetrievalApi,
) -> None:
    scope = retrieval_api.scope
    with immediate_transaction(retrieval_api.engine) as connection:
        connection.exec_driver_sql(
            "DELETE FROM revision_file_sets WHERE revision_id = ?",
            (scope.second_revision_id,),
        )
    page_path = f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
    for path in (
        page_path,
        f"{page_path}/revisions/2",
        f"{page_path}/revisions/2/files",
        f"{page_path}/revisions/2/files/content.md",
    ):
        response = _get(retrieval_api, path)
        _assert_problem(response, 500, "internal_error")
        assert scope.current_content not in response.text
        assert "ETag" not in response.headers


@pytest.mark.parametrize("suffix", ["missing.md", "Content.md", "..%2Fsecret", "%0D%0Aevil", "CON"])
def test_revision_file_download_rejects_unknown_or_unsafe_name(
    retrieval_api: RetrievalApi,
    suffix: str,
) -> None:
    scope = retrieval_api.scope
    base = (
        f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
        f"/revisions/1/files/{suffix}"
    )
    response = _get(retrieval_api, base)
    if suffix in {"missing.md", "Content.md", "%0D%0Aevil"}:
        _assert_problem(response, 404, "resource_not_found")
    else:
        _assert_problem(response, 422, "request_validation_failed")
    assert scope.historical_content not in response.text


def test_revision_file_routes_preserve_auth_scope_and_tombstone_boundaries(
    retrieval_api: RetrievalApi,
) -> None:
    scope = retrieval_api.scope
    page_base = f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
    file_base = f"{page_base}/revisions/1/files"
    with TestClient(_app(retrieval_api), raise_server_exceptions=False) as client:
        unauthenticated = client.get(file_base)
        malformed = client.get(f"{file_base}/..%2Fsecret")
    _assert_problem(unauthenticated, 401, "authentication_required")
    _assert_problem(malformed, 401, "authentication_required")

    deleted = _get(
        retrieval_api,
        f"/api/v1/sections/{scope.query_section_id}/pages/"
        f"{scope.deleted_page_id}/revisions/1/files",
    )
    hidden = _get(
        retrieval_api,
        f"/api/v1/sections/{scope.hidden_section_id}/pages/"
        f"{scope.hidden_page_id}/revisions/1/files/content.md",
    )
    _assert_problem(deleted, 404, "resource_not_found")
    _assert_problem(hidden, 404, "resource_not_found")

    readable_without_query = _get(
        retrieval_api,
        f"/api/v1/sections/{scope.read_section_id}/pages/"
        f"{scope.read_page_id}/revisions/1/files/content.md",
    )
    assert readable_without_query.status_code == 200
    assert readable_without_query.content == b"# Read only\n"


@pytest.mark.parametrize(
    "revision_number",
    ["0", "01", "+1", "-1", "one", "1" * 100, str(1 << 63)],
)
def test_revision_path_authenticates_before_strict_number_validation(
    retrieval_api: RetrievalApi,
    revision_number: str,
) -> None:
    path = (
        f"/api/v1/sections/{retrieval_api.scope.query_section_id}/pages/"
        f"{retrieval_api.scope.first_page_id}/revisions/{revision_number}"
    )
    with TestClient(_app(retrieval_api), raise_server_exceptions=False) as client:
        missing_auth = client.get(path)
        invalid_auth = client.get(path, headers={"Authorization": "Bearer invalid"})
    _assert_problem(missing_auth, 401, "authentication_required")
    _assert_problem(invalid_auth, 401, "invalid_token")

    authenticated = _get(retrieval_api, path)
    _assert_problem(authenticated, 422, "request_validation_failed")


def test_authentication_authorization_hidden_and_absent_are_stable(
    retrieval_api: RetrievalApi,
) -> None:
    with TestClient(_app(retrieval_api), raise_server_exceptions=False) as client:
        missing_auth = client.get("/api/v1/sections")
    _assert_problem(missing_auth, 401, "authentication_required")

    invalid = _get(
        RetrievalApi(
            retrieval_api.engine,
            retrieval_api.scope,
            "plb1.unknown.invalid",
            retrieval_api.cursor_codec,
        ),
        "/api/v1/sections",
    )
    _assert_problem(invalid, 401, "invalid_token")

    insufficient = _get(
        retrieval_api,
        f"/api/v1/sections/{retrieval_api.scope.read_section_id}/books",
    )
    _assert_problem(insufficient, 403, "insufficient_scope")

    hidden = _get(
        retrieval_api,
        f"/api/v1/sections/{HIDDEN_SECTION_ID}/pages/{retrieval_api.scope.hidden_page_id}",
    )
    absent = _get(
        retrieval_api,
        f"/api/v1/sections/{HIDDEN_SECTION_ID}/pages/missing-page",
    )
    for response in (hidden, absent):
        _assert_problem(response, 404, "resource_not_found")
    assert hidden.content == absent.content
    assert retrieval_api.scope.hidden_page_id not in hidden.text


def test_cursor_rejects_tamper_limit_route_section_and_policy_changes(
    retrieval_api: RetrievalApi,
) -> None:
    first = _get(retrieval_api, "/api/v1/sections", params={"limit": 1})
    cursor = first.json()["next_cursor"]
    tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"

    cases: tuple[tuple[str, QueryParams], ...] = (
        ("/api/v1/sections", {"limit": 1, "cursor": tampered}),
        ("/api/v1/sections", {"limit": 2, "cursor": cursor}),
        (
            f"/api/v1/sections/{retrieval_api.scope.query_section_id}/books",
            {"limit": 1, "cursor": cursor},
        ),
    )
    for path, params in cases:
        _assert_problem(_get(retrieval_api, path, params=params), 400, "invalid_cursor")

    pages = _get(
        retrieval_api,
        f"/api/v1/sections/{retrieval_api.scope.query_section_id}/pages",
        params={"limit": 1},
    )
    page_cursor = pages.json()["next_cursor"]
    cross_section = _get(
        retrieval_api,
        f"/api/v1/sections/{SECOND_QUERY_SECTION_ID}/pages",
        params={"limit": 1, "cursor": page_cursor},
    )
    _assert_problem(cross_section, 400, "invalid_cursor")

    with immediate_transaction(retrieval_api.engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(retrieval_api.scope.library_id, CALLER_ID)
        assert caller is not None
        assert (
            repository.increment_policy_version(
                caller.library_id,
                caller.id,
                expected_version=caller.policy_version,
                updated_at=3_000_000,
            )
            is not None
        )

    stale = _get(
        retrieval_api,
        "/api/v1/sections",
        params={"limit": 1, "cursor": cursor},
    )
    _assert_problem(stale, 400, "invalid_cursor")


def test_cursor_cannot_cross_caller_binding(retrieval_api: RetrievalApi) -> None:
    first = _get(retrieval_api, "/api/v1/sections", params={"limit": 1})
    cursor = first.json()["next_cursor"]
    other_caller_id = "e" * 32
    issued = generate_token()
    with immediate_transaction(retrieval_api.engine) as connection:
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id=other_caller_id,
                library_id=retrieval_api.scope.library_id,
                kind=CallerKind.AGENT,
                name="Synthetic Other Agent",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_credential(
            NewCredential(
                id="f" * 32,
                library_id=retrieval_api.scope.library_id,
                caller_id=other_caller_id,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_grant(
            NewSectionGrant(
                library_id=retrieval_api.scope.library_id,
                caller_id=other_caller_id,
                section_id=retrieval_api.scope.query_section_id,
                action=SectionAction.QUERY,
                created_at=1_000_000,
            )
        )

    other = RetrievalApi(
        retrieval_api.engine,
        retrieval_api.scope,
        issued.value,
        retrieval_api.cursor_codec,
    )
    response = _get(
        other,
        "/api/v1/sections",
        params={"limit": 1, "cursor": cursor},
    )
    _assert_problem(response, 400, "invalid_cursor")


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1, "extra": "value"},
        [("limit", "1"), ("limit", "2")],
        [("cursor", "one"), ("cursor", "two")],
    ],
)
def test_collection_parameters_are_bounded_and_unambiguous(
    retrieval_api: RetrievalApi,
    params: QueryParams,
) -> None:
    with TestClient(_app(retrieval_api), raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/sections",
            headers=_auth(retrieval_api),
            params=params,
        )
    _assert_problem(response, 422, "request_validation_failed")


def test_runtime_database_failure_is_redacted(
    retrieval_api: RetrievalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_failure = "synthetic-private-storage-detail"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(private_failure)

    monkeypatch.setattr(RetrievalRepository, "list_queryable_sections", fail)
    response = _get(retrieval_api, "/api/v1/sections")

    _assert_problem(response, 500, "internal_error")
    assert private_failure not in response.text
    assert retrieval_api.token not in response.text


def test_cancellation_like_failure_propagates_and_closes_transaction(
    retrieval_api: RetrievalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticCancellation(BaseException):
        pass

    def cancel(*_args: object, **_kwargs: object) -> None:
        raise SyntheticCancellation

    monkeypatch.setattr(RetrievalRepository, "list_queryable_sections", cancel)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sections",
            "headers": [(b"authorization", f"Bearer {retrieval_api.token}".encode())],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
    )
    context = BearerAuthentication(retrieval_api.engine, clock=lambda: 2_000_000)(request)
    with pytest.raises(SyntheticCancellation):
        _perform_read(
            retrieval_api.engine,
            context,
            lambda service: service.list_sections(),
            clock=lambda: 2_000_000,
        )

    with retrieval_api.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1


def test_grant_removal_between_authentication_and_read_is_rechecked(
    retrieval_api: RetrievalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = retrieval_routes_module._authenticate

    async def authenticate_then_remove(*args: Any, **kwargs: Any) -> Any:
        context = await original_authenticate(*args, **kwargs)
        with immediate_transaction(retrieval_api.engine) as connection:
            repository = AuthRepository(connection)
            assert repository.remove_grant(
                retrieval_api.scope.library_id,
                CALLER_ID,
                retrieval_api.scope.query_section_id,
                SectionAction.QUERY,
            )
        return context

    monkeypatch.setattr(retrieval_routes_module, "_authenticate", authenticate_then_remove)

    response = _get(
        retrieval_api,
        f"/api/v1/sections/{retrieval_api.scope.query_section_id}/pages",
    )
    _assert_problem(response, 403, "insufficient_scope")


def test_read_uses_one_real_sqlite_snapshot_for_grant_and_page(
    retrieval_api: RetrievalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = retrieval_api.scope
    page_path = f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
    with retrieval_api.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one() == "wal"

    original_actions = RetrievalRepository.section_actions
    observed_transactions: list[bool] = []

    def actions_then_revoke_and_delete(
        repository: RetrievalRepository,
        library_id: str,
        caller_id: str,
        section_id: str,
    ) -> tuple[SectionAction, ...]:
        actions = original_actions(repository, library_id, caller_id, section_id)
        raw = repository._connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        observed_transactions.append(raw.in_transaction)
        with immediate_transaction(retrieval_api.engine) as writer:
            assert AuthRepository(writer).remove_grant(
                scope.library_id, CALLER_ID, scope.query_section_id, SectionAction.PAGE_READ
            )
            writer.exec_driver_sql(
                "UPDATE pages SET deleted_at = 4000000, updated_at = 4000000 "
                "WHERE library_id = ? AND page_uid = ?",
                (scope.library_id, scope.first_page_uid),
            )
        return actions

    with monkeypatch.context() as patcher:
        patcher.setattr(RetrievalRepository, "section_actions", actions_then_revoke_and_delete)
        response = _get(retrieval_api, page_path)
    assert observed_transactions == [True]
    assert response.status_code == 200
    assert response.json()["revision"]["content"] == scope.current_content
    _assert_problem(_get(retrieval_api, page_path), 403, "insufficient_scope")


def test_opted_in_library_grant_and_body_share_one_sqlite_snapshot(
    retrieval_api: RetrievalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = retrieval_api.scope
    credential_id = "d" * 32
    with immediate_transaction(retrieval_api.engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": credential_id,
                "caller_id": CALLER_ID,
                "home_library_id": scope.library_id,
                "mode": "library_grants",
                "created_at": 1_000_000,
            },
        )
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": credential_id,
                "caller_id": CALLER_ID,
                "home_library_id": scope.library_id,
                "target_library_id": scope.library_id,
                "action": "read",
                "created_at": 1_000_000,
            },
        )
    with retrieval_api.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one() == "wal"

    original_policy = RetrievalRepository.get_library_policy
    observed_transactions: list[bool] = []

    def policy_then_revoke_and_delete(repository: RetrievalRepository, **kwargs: Any) -> Any:
        policy = original_policy(repository, **kwargs)
        raw = repository._connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        observed_transactions.append(raw.in_transaction)
        with immediate_transaction(retrieval_api.engine) as writer:
            writer.exec_driver_sql(
                "DELETE FROM auth_credential_library_grants "
                "WHERE credential_id = ? AND target_library_id = ? AND action = 'read'",
                (credential_id, scope.library_id),
            )
            writer.exec_driver_sql(
                "UPDATE pages SET deleted_at = 4000000, updated_at = 4000000 "
                "WHERE library_id = ? AND page_uid = ?",
                (scope.library_id, scope.first_page_uid),
            )
        return policy

    page_path = f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
    with monkeypatch.context() as patcher:
        patcher.setattr(RetrievalRepository, "get_library_policy", policy_then_revoke_and_delete)
        response = _get(retrieval_api, page_path)
    assert observed_transactions == [True]
    assert response.status_code == 200
    assert response.json()["revision"]["content"] == scope.current_content
    _assert_problem(_get(retrieval_api, page_path), 403, "insufficient_scope")


def test_revision_file_download_rechecks_page_read_grant_after_authentication(
    retrieval_api: RetrievalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = retrieval_routes_module._authenticate

    async def authenticate_then_remove(*args: Any, **kwargs: Any) -> Any:
        context = await original_authenticate(*args, **kwargs)
        with immediate_transaction(retrieval_api.engine) as connection:
            assert AuthRepository(connection).remove_grant(
                retrieval_api.scope.library_id,
                CALLER_ID,
                retrieval_api.scope.query_section_id,
                SectionAction.PAGE_READ,
            )
        return context

    monkeypatch.setattr(retrieval_routes_module, "_authenticate", authenticate_then_remove)
    scope = retrieval_api.scope
    response = _get(
        retrieval_api,
        f"/api/v1/sections/{scope.query_section_id}/pages/{scope.first_page_id}"
        "/revisions/1/files/content.md",
    )
    _assert_problem(response, 403, "insufficient_scope")
