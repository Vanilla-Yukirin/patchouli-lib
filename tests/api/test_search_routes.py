from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from fastapi import FastAPI
from fastapi.testclient import TestClient
from retrieval_read.conftest import (
    CALLER_ID,
    HIDDEN_SECTION_ID,
    READ_SECTION_ID,
    RetrievalScope,
)
from retrieval_read.conftest import (
    retrieval_engine as retrieval_engine_fixture,
)
from retrieval_read.conftest import (
    retrieval_scope as retrieval_scope_fixture,
)
from sqlalchemy import Engine

from patchouli_lib.api import search_routes as search_routes_module
from patchouli_lib.api.auth_routes import create_auth_router
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import PROBLEM_MEDIA_TYPE, install_api_exception_handlers
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.api.search_routes import create_search_router
from patchouli_lib.api.search_routes_v2 import create_search_v2_router
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import (
    SearchIndexUnavailableError,
    rebuild_search_index,
    require_ready_index,
)

REQUEST_ID = "req_cccccccccccccccccccccccccccccccc"
WIRE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "api" / "agent_v1_wire.json"


@dataclass(frozen=True, slots=True)
class SearchApi:
    engine: Engine
    scope: RetrievalScope
    token: str
    credential_id: str


@dataclass(frozen=True, slots=True)
class SearchV2Api:
    engine: Engine
    library_id: str
    token: str


@pytest.fixture
def search_api(tmp_path: Path) -> Iterator[SearchApi]:
    engine_factory = cast(
        Callable[[Path], Iterator[Engine]],
        cast(Any, retrieval_engine_fixture).__wrapped__,
    )
    scope_factory = cast(
        Callable[[Engine], RetrievalScope],
        cast(Any, retrieval_scope_fixture).__wrapped__,
    )
    engine_iterator = engine_factory(tmp_path)
    engine = next(engine_iterator)
    scope = scope_factory(engine)
    issued = generate_token()
    credential_id = "d" * 32
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_credential(
            NewCredential(
                id=credential_id,
                library_id=scope.library_id,
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
        yield SearchApi(engine, scope, issued.value, credential_id)
    finally:
        with suppress(StopIteration):
            next(engine_iterator)


@pytest.fixture
def search_v2_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SearchV2Api]:
    database_url = f"sqlite:///{(tmp_path / 'search-v2.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    library_id, section_id, book_id = seed_library_structure(engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        title="Synthetic Archive",
        content_md=b"# Archive\n",
    )
    issued = generate_token()
    credential_id = "d" * 32
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id=CALLER_ID,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic search agent",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_credential(
            NewCredential(
                id=credential_id,
                library_id=library_id,
                caller_id=CALLER_ID,
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
                library_id=library_id,
                caller_id=CALLER_ID,
                section_id=section_id,
                action=SectionAction.PAGE_READ,
                created_at=1_000_000,
            )
        )
    try:
        yield SearchV2Api(engine, library_id, issued.value)
    finally:
        engine.dispose()


def _app(fixture: SearchApi) -> FastAPI:
    application = FastAPI()
    install_api_exception_handlers(application)
    application.add_middleware(
        RequestIDMiddleware,
        request_id_factory=lambda: REQUEST_ID,
    )
    application.include_router(create_search_router(fixture.engine, clock=lambda: 2_000_000))
    return application


def _post_v2(fixture: SearchV2Api, body: object, *, token: str | None = None) -> Any:
    application = FastAPI()
    install_api_exception_handlers(application)
    application.add_middleware(RequestIDMiddleware, request_id_factory=lambda: REQUEST_ID)
    application.include_router(create_search_v2_router(fixture.engine, clock=lambda: 2_000_000))
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with TestClient(application, raise_server_exceptions=False) as client:
        return client.post("/api/v1/search", json=body, headers=headers)


def test_v2_search_requires_ready_index_and_never_leaks_query(search_v2_api: SearchV2Api) -> None:
    phrase = "archive"
    unavailable = _post_v2(search_v2_api, {"keywords": [phrase]}, token=search_v2_api.token)
    _problem(unavailable, 503, "search_unavailable")
    assert phrase not in unavailable.text

    rebuild_search_index(search_v2_api.engine)
    response = _post_v2(search_v2_api, {"keywords": [phrase]}, token=search_v2_api.token)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert response.json()["items"]
    assert all(item["library_id"] == search_v2_api.library_id for item in response.json()["items"])
    assert response.json()["items"][0]["revision_number"] == 1


def test_v2_search_rejects_invalid_body_and_missing_credential(search_v2_api: SearchV2Api) -> None:
    missing = _post_v2(search_v2_api, {"keywords": ["archive"]})
    _problem(missing, 401, "authentication_required")
    invalid = _post_v2(search_v2_api, {"keywords": []}, token=search_v2_api.token)
    _problem(invalid, 422, "request_validation_failed")


def test_v2_capability_is_advertised_only_after_rebuild(search_v2_api: SearchV2Api) -> None:
    engine = search_v2_api.engine

    def ready() -> bool:
        try:
            with engine.connect() as connection:
                require_ready_index(connection)
        except SearchIndexUnavailableError:
            return False
        return True

    application = FastAPI()
    install_api_exception_handlers(application)
    application.include_router(
        create_auth_router(engine, search_ready=ready, clock=lambda: 2_000_000)
    )
    headers = {"Authorization": f"Bearer {search_v2_api.token}"}
    with TestClient(application) as client:
        assert (
            "search" not in client.get("/api/v1/capabilities", headers=headers).json()["features"]
        )
        rebuild_search_index(engine)
        assert "search" in client.get("/api/v1/capabilities", headers=headers).json()["features"]


def _post(
    fixture: SearchApi,
    section_id: str,
    *,
    token: str | None = None,
    json_body: object = None,
    content: bytes | None = None,
) -> Any:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    if content is not None:
        headers["Content-Type"] = "application/json"
    with TestClient(_app(fixture), raise_server_exceptions=False) as client:
        return client.post(
            f"/api/v1/sections/{section_id}/search",
            headers=headers,
            json=json_body if content is None else None,
            content=content,
        )


def _problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert response.json()["code"] == code
    assert response.json()["details"] == {}


def _database_dump(engine: Engine) -> tuple[str, ...]:
    connection = engine.raw_connection()
    try:
        sqlite = cast(sqlite3.Connection, connection.driver_connection)
        return tuple(sqlite.iterdump())
    finally:
        connection.close()


def test_authorized_search_matches_shared_unavailable_problem_without_mutation(
    search_api: SearchApi,
) -> None:
    fixture = json.loads(WIRE_FIXTURE.read_text(encoding="utf-8"))
    vector = fixture["problems"]["search_unavailable"]
    before = _database_dump(search_api.engine)

    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        json_body=fixture["responses"]["search"]["request"],
    )

    assert response.status_code == vector["status"]
    assert response.json() == vector["body"]
    for name, value in vector["headers"].items():
        assert response.headers[name] == value
    assert _database_dump(search_api.engine) == before
    assert "synthetic query" not in response.text


@pytest.mark.parametrize(
    "query",
    ["x" * 4_096, "文" * 1_365 + "x"],
)
def test_exact_query_byte_limit_is_accepted_without_mutation(
    search_api: SearchApi,
    query: str,
) -> None:
    assert len(query.encode("utf-8")) == 4_096
    before = _database_dump(search_api.engine)

    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        json_body={"query": query},
    )

    _problem(response, 503, "search_unavailable")
    assert query not in response.text
    assert _database_dump(search_api.engine) == before


@pytest.mark.parametrize(
    "query",
    ["x" * 4_097, "文" * 1_366],
)
def test_query_over_byte_limit_is_rejected_without_mutation(
    search_api: SearchApi,
    query: str,
) -> None:
    assert len(query.encode("utf-8")) > 4_096
    before = _database_dump(search_api.engine)

    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        json_body={"query": query},
    )

    _problem(response, 422, "request_validation_failed")
    assert query not in response.text
    assert _database_dump(search_api.engine) == before


def test_bounded_json_reader_rejects_oversized_wrapper_without_mutation(
    search_api: SearchApi,
) -> None:
    before = _database_dump(search_api.engine)
    private_padding = "private-padding-" * 3_000
    body = json.dumps({"query": "synthetic", "padding": private_padding}).encode()

    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        content=body,
    )

    _problem(response, 422, "request_validation_failed")
    assert private_padding not in response.text
    assert _database_dump(search_api.engine) == before


def test_largest_escaped_query_and_unicode_cursor_envelope_is_accepted(
    search_api: SearchApi,
) -> None:
    query = "\x00" * 4_096
    cursor = "\U0001f642" * 4_096
    body = json.dumps({"query": query, "limit": 20, "cursor": cursor}).encode()
    before = _database_dump(search_api.engine)

    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        content=body,
    )

    _problem(response, 503, "search_unavailable")
    assert query not in response.text
    assert cursor not in response.text
    assert _database_dump(search_api.engine) == before


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":"first","query":"private duplicate"}',
        b'{"query":"synthetic","limit":NaN}',
    ],
)
def test_noncanonical_json_is_rejected_without_private_echo(
    search_api: SearchApi,
    body: bytes,
) -> None:
    before = _database_dump(search_api.engine)
    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        content=body,
    )

    _problem(response, 422, "request_validation_failed")
    assert "private duplicate" not in response.text
    assert _database_dump(search_api.engine) == before


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":"synthetic","future":' + (b"[" * 1_100) + b"0" + (b"]" * 1_100) + b"}",
        b'{"query":"synthetic","limit":' + (b"1" * 5_000) + b"}",
    ],
)
def test_json_resource_parse_failures_are_safe_validation_problems(
    search_api: SearchApi,
    body: bytes,
) -> None:
    before = _database_dump(search_api.engine)
    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        content=body,
    )

    _problem(response, 422, "request_validation_failed")
    assert _database_dump(search_api.engine) == before


@pytest.mark.parametrize(
    ("token", "expected_code"),
    [(None, "authentication_required"), ("invalid", "invalid_token")],
)
def test_authentication_precedes_section_and_body_validation(
    search_api: SearchApi,
    token: str | None,
    expected_code: str,
) -> None:
    response = _post(
        search_api,
        "not a section",
        token=token,
        content=b"{private malformed",
    )
    _problem(response, 401, expected_code)
    assert "private malformed" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"query": ""},
        {"query": "synthetic", "limit": 0},
        {"query": "synthetic", "limit": 101},
        {"query": "synthetic", "limit": True},
        {"query": "synthetic", "limit": "20"},
        {"query": "synthetic", "limit": 20.0},
        {"query": "synthetic", "cursor": ""},
        {"query": "synthetic", "future": True},
        ["synthetic"],
    ],
)
def test_authenticated_malformed_body_is_rejected_before_authorization(
    search_api: SearchApi,
    body: object,
) -> None:
    response = _post(
        search_api,
        HIDDEN_SECTION_ID,
        token=search_api.token,
        json_body=body,
    )
    _problem(response, 422, "request_validation_failed")


def test_hidden_absent_and_visible_insufficient_scope_are_distinct(
    search_api: SearchApi,
) -> None:
    hidden = _post(
        search_api,
        HIDDEN_SECTION_ID,
        token=search_api.token,
        json_body={"query": "private hidden query"},
    )
    absent = _post(
        search_api,
        "f" * 32,
        token=search_api.token,
        json_body={"query": "private absent query"},
    )
    insufficient = _post(
        search_api,
        READ_SECTION_ID,
        token=search_api.token,
        json_body={"query": "private visible query"},
    )

    _problem(hidden, 404, "resource_not_found")
    _problem(absent, 404, "resource_not_found")
    assert hidden.content == absent.content
    _problem(insufficient, 403, "insufficient_scope")
    assert "private" not in hidden.text + absent.text + insufficient.text


def test_current_grant_is_rechecked_after_authentication(
    search_api: SearchApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = search_routes_module._authenticate

    async def authenticate_then_remove(*args: Any, **kwargs: Any) -> Any:
        context = await original_authenticate(*args, **kwargs)
        with immediate_transaction(search_api.engine) as connection:
            repository = AuthRepository(connection)
            assert repository.remove_grant(
                search_api.scope.library_id,
                CALLER_ID,
                search_api.scope.query_section_id,
                SectionAction.QUERY,
            )
        return context

    monkeypatch.setattr(search_routes_module, "_authenticate", authenticate_then_remove)
    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        json_body={"query": "private query"},
    )
    _problem(response, 403, "insufficient_scope")


def test_current_new_grant_is_used_after_authentication(
    search_api: SearchApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = search_routes_module._authenticate

    async def authenticate_then_grant(*args: Any, **kwargs: Any) -> Any:
        context = await original_authenticate(*args, **kwargs)
        with immediate_transaction(search_api.engine) as connection:
            AuthRepository(connection).add_grant(
                NewSectionGrant(
                    library_id=search_api.scope.library_id,
                    caller_id=CALLER_ID,
                    section_id=READ_SECTION_ID,
                    action=SectionAction.QUERY,
                    created_at=2_000_000,
                )
            )
        return context

    monkeypatch.setattr(search_routes_module, "_authenticate", authenticate_then_grant)
    response = _post(
        search_api,
        READ_SECTION_ID,
        token=search_api.token,
        json_body={"query": "private query"},
    )
    _problem(response, 503, "search_unavailable")


def test_current_credential_revocation_is_rechecked_after_authentication(
    search_api: SearchApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = search_routes_module._authenticate

    async def authenticate_then_revoke(*args: Any, **kwargs: Any) -> Any:
        context = await original_authenticate(*args, **kwargs)
        with immediate_transaction(search_api.engine) as connection:
            repository = AuthRepository(connection)
            credential = repository.get_credential(
                search_api.scope.library_id,
                CALLER_ID,
                search_api.credential_id,
            )
            assert credential is not None
            assert repository.revoke_credential(credential, revoked_at=2_000_000) is not None
        return context

    monkeypatch.setattr(search_routes_module, "_authenticate", authenticate_then_revoke)
    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        json_body={"query": "private query"},
    )
    _problem(response, 401, "invalid_token")


def test_current_caller_disable_is_rechecked_after_authentication(
    search_api: SearchApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = search_routes_module._authenticate

    async def authenticate_then_disable(*args: Any, **kwargs: Any) -> Any:
        context = await original_authenticate(*args, **kwargs)
        with immediate_transaction(search_api.engine) as connection:
            assert (
                AuthRepository(connection).disable_caller(
                    search_api.scope.library_id,
                    CALLER_ID,
                    disabled_at=2_000_000,
                )
                is not None
            )
        return context

    monkeypatch.setattr(search_routes_module, "_authenticate", authenticate_then_disable)
    response = _post(
        search_api,
        search_api.scope.query_section_id,
        token=search_api.token,
        json_body={"query": "private query"},
    )
    _problem(response, 401, "invalid_token")
