from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from fastapi import FastAPI
from fastapi.testclient import TestClient
from retrieval_read.conftest import CALLER_ID
from sqlalchemy import Engine

from patchouli_lib.api.auth_routes import create_auth_router
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import PROBLEM_MEDIA_TYPE, install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import create_file_set_read_router
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
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
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import (
    SearchIndexUnavailableError,
    rebuild_search_index,
    require_ready_index,
)
from patchouli_lib.search.query_v2 import MAX_QUERY_BODY_BYTES

REQUEST_ID = "req_cccccccccccccccccccccccccccccccc"


@dataclass(frozen=True, slots=True)
class SearchV2Api:
    engine: Engine
    library_id: str
    token: str


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


def _post_v2(
    fixture: SearchV2Api,
    body: object = None,
    *,
    raw: bytes | None = None,
    token: str | None = None,
) -> Any:
    application = FastAPI()
    install_api_exception_handlers(application)
    application.add_middleware(RequestIDMiddleware, request_id_factory=lambda: REQUEST_ID)
    application.include_router(create_search_v2_router(fixture.engine, clock=lambda: 2_000_000))
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with TestClient(application, raise_server_exceptions=False) as client:
        if raw is not None:
            return client.post(
                "/api/v1/search",
                content=raw,
                headers={**headers, "Content-Type": "application/json"},
            )
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
    first = response.json()["items"][0]
    assert first["revision_number"] == 1
    assert first["revision_files_href"] == (
        f"/api/v1/libraries/{first['library_id']}/sections/{first['section_id']}"
        f"/pages/{first['page_id']}/revisions/{first['revision_id']}/files"
    )

    application = FastAPI()
    install_api_exception_handlers(application)
    application.include_router(
        create_file_set_read_router(search_v2_api.engine, clock=lambda: 2_000_000)
    )
    with TestClient(application) as client:
        denied = client.get(first["revision_files_href"])
        exact = client.get(
            first["revision_files_href"],
            headers={"Authorization": f"Bearer {search_v2_api.token}"},
        )
    assert denied.status_code == 401
    assert exact.status_code == 200
    assert exact.json()["revision_id"] == first["revision_id"]


def test_search_revision_link_stays_exact_and_rechecks_current_grant(
    search_v2_api: SearchV2Api,
) -> None:
    rebuild_search_index(search_v2_api.engine)
    found = _post_v2(
        search_v2_api,
        {"keywords": ["archive"]},
        token=search_v2_api.token,
    )
    assert found.status_code == 200
    item = found.json()["items"][0]
    old_revision = item["revision_id"]

    with immediate_transaction(search_v2_api.engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(item["library_id"], item["page_id"])
        assert page is not None
        new_revision = f"rev_{'f' * 32}"
        repository.add_file_set_revision(
            page,
            revision_id=new_revision,
            created_at=3_000_000,
            manifest=build_file_manifest((("updated.md", b"# Updated\n"),)),
        )
        assert (
            repository.advance_file_set_current_revision(
                page, revision_id=new_revision, updated_at=3_000_000
            )
            is not None
        )

    application = FastAPI()
    install_api_exception_handlers(application)
    application.include_router(
        create_file_set_read_router(search_v2_api.engine, clock=lambda: 2_000_000)
    )
    headers = {"Authorization": f"Bearer {search_v2_api.token}"}
    with TestClient(application) as client:
        exact = client.get(item["revision_files_href"], headers=headers)
        assert exact.status_code == 200
        assert exact.json()["revision_id"] == old_revision
        with immediate_transaction(search_v2_api.engine) as connection:
            assert AuthRepository(connection).remove_grant(
                item["library_id"], CALLER_ID, item["section_id"], SectionAction.PAGE_READ
            )
        revoked = client.get(item["revision_files_href"], headers=headers)
    assert revoked.status_code == 404


def test_v2_search_rejects_invalid_body_and_missing_credential(search_v2_api: SearchV2Api) -> None:
    missing = _post_v2(search_v2_api, {"keywords": ["archive"]})
    _problem(missing, 401, "authentication_required")
    invalid = _post_v2(search_v2_api, {"keywords": []}, token=search_v2_api.token)
    _problem(invalid, 422, "request_validation_failed")


def test_v2_search_authenticates_before_parsing_untrusted_body(
    search_v2_api: SearchV2Api,
) -> None:
    oversized = b"x" * (MAX_QUERY_BODY_BYTES + 1)
    _problem(_post_v2(search_v2_api, raw=oversized), 401, "authentication_required")
    _problem(
        _post_v2(search_v2_api, raw=oversized, token="invalid"),
        401,
        "invalid_token",
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"keywords":["secret"],"keywords":["archive"]}',
        b'{"keywords":["\xff"]}',
        b"x" * (MAX_QUERY_BODY_BYTES + 1),
    ],
    ids=["duplicate-key", "invalid-utf8", "oversized"],
)
def test_v2_search_rejects_ambiguous_or_oversized_body_without_echo(
    search_v2_api: SearchV2Api, raw: bytes
) -> None:
    response = _post_v2(search_v2_api, raw=raw, token=search_v2_api.token)
    _problem(response, 422, "request_validation_failed")
    assert "secret" not in response.text
    assert "archive" not in response.text


def test_v2_search_rejects_empty_or_unreadable_library_scope(
    search_v2_api: SearchV2Api,
) -> None:
    _problem(
        _post_v2(
            search_v2_api,
            {"keywords": ["archive"], "libraries": []},
            token=search_v2_api.token,
        ),
        422,
        "request_validation_failed",
    )
    _problem(
        _post_v2(
            search_v2_api,
            {"keywords": ["archive"], "libraries": ["f" * 32]},
            token=search_v2_api.token,
        ),
        404,
        "resource_not_found",
    )


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


def _problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert response.json()["code"] == code
    assert response.json()["details"] == {}
