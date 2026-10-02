"""Synthetic HTTP coverage for protected search continuation and snippets."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.api.search_routes_v2 import SearchResponse, create_search_v2_router
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.models import Page
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.retrieval.cursor import CursorCodec
from patchouli_lib.search.index_v2 import rebuild_search_index

_CODEC = CursorCodec(b"synthetic-http-search-cursor-key-0001")
_CALLER_ID = "a" * 32
_REQUEST_ID = "req_" + "b" * 32


@dataclass(frozen=True)
class SearchV2Api:
    engine: Engine
    library_id: str
    token: str


@pytest.fixture
def search_v2_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SearchV2Api]:
    database_url = f"sqlite:///{(tmp_path / 'search-cursor.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    library_id, section_id, book_id = seed_library_structure(engine)
    first = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        title="Synthetic Archive",
        content_md=b"# Archive\nA first full-text page.\n",
    )
    token = generate_token()
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, first)
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id=_CALLER_ID,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic search agent",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_credential(
            NewCredential(
                id="d" * 32,
                library_id=library_id,
                caller_id=_CALLER_ID,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_grant(
            NewSectionGrant(
                library_id=library_id,
                caller_id=_CALLER_ID,
                section_id=section_id,
                action=SectionAction.PAGE_READ,
                created_at=1_000_000,
            )
        )
    try:
        yield SearchV2Api(engine, library_id, token.value)
    finally:
        engine.dispose()


def _app(fixture: SearchV2Api) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: _REQUEST_ID)
    app.include_router(
        create_search_v2_router(fixture.engine, clock=lambda: 2_000_000, cursor_codec=_CODEC)
    )
    return app


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _prepare_two_pages(fixture: SearchV2Api) -> set[str]:
    with fixture.engine.connect() as connection:
        first = connection.execute(
            select(Page.page_id, Page.section_id, Page.book_id).where(
                Page.library_id == fixture.library_id
            )
        ).one()
    second = page_graph_values(
        library_id=fixture.library_id,
        section_id=first.section_id,
        book_id=first.book_id,
        page_byte=0x21,
        revision_hex="44",
        source_hex="5",
        title="Synthetic Archive Two",
        content_md=b"# Archive\nA second full-text page.\n",
    )
    with immediate_transaction(fixture.engine) as connection:
        insert_page_graph(connection, second)
    rebuild_search_index(fixture.engine)
    return {first.page_id, second[0].page_id}


def _first_page(client: TestClient, fixture: SearchV2Api) -> dict[str, Any]:
    response = client.post(
        "/api/v1/search",
        json={"keywords": ["archive"], "limit": 1},
        headers=_headers(fixture.token),
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def test_http_search_returns_two_pages_with_plain_text_snippets(
    search_v2_api: SearchV2Api,
) -> None:
    expected = _prepare_two_pages(search_v2_api)
    with TestClient(_app(search_v2_api)) as client:
        first = _first_page(client, search_v2_api)
        SearchResponse.model_validate(first)
        cursor = first["next_cursor"]
        assert isinstance(cursor, str) and cursor
        first_item = first["items"][0]
        snippet = first_item["snippet"]
        assert snippet["file_name"] == "content.md"
        assert isinstance(snippet["text"], str) and "archive" in snippet["text"]
        assert isinstance(snippet["matched"], bool)

        response = client.post(
            "/api/v1/search",
            json={"keywords": ["archive"], "limit": 1, "cursor": cursor},
            headers=_headers(search_v2_api.token),
        )
        assert response.status_code == 200
        second = response.json()
        SearchResponse.model_validate(second)
        assert second["next_cursor"] is None
        assert second["items"][0]["snippet"] is not None
        assert {first_item["page_id"], second["items"][0]["page_id"]} == expected


def test_http_search_rejects_tampered_or_rebound_cursor(search_v2_api: SearchV2Api) -> None:
    _prepare_two_pages(search_v2_api)
    with TestClient(_app(search_v2_api)) as client:
        cursor = _first_page(client, search_v2_api)["next_cursor"]
        assert isinstance(cursor, str)
        for query in (
            {"keywords": ["archive"], "limit": 1, "cursor": cursor + "x"},
            {"keywords": ["archive"], "limit": 2, "cursor": cursor},
            {"keywords": ["archive two"], "limit": 1, "cursor": cursor},
        ):
            response = client.post(
                "/api/v1/search", json=query, headers=_headers(search_v2_api.token)
            )
            assert response.status_code == 400
            assert response.json()["code"] == "invalid_cursor"
            assert "archive" not in response.text


def test_http_search_cursor_rejects_another_credential_of_same_caller(
    search_v2_api: SearchV2Api,
) -> None:
    _prepare_two_pages(search_v2_api)
    other = generate_token()
    with immediate_transaction(search_v2_api.engine) as connection:
        caller = AuthRepository(connection).get_caller(search_v2_api.library_id, _CALLER_ID)
        assert caller is not None
        AuthRepository(connection).add_credential(
            NewCredential(
                id="e" * 32,
                library_id=search_v2_api.library_id,
                caller_id=caller.id,
                selector=other.selector,
                token_version=other.version,
                verifier=other.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    with TestClient(_app(search_v2_api)) as client:
        cursor = _first_page(client, search_v2_api)["next_cursor"]
        assert isinstance(cursor, str)
        response = client.post(
            "/api/v1/search",
            json={"keywords": ["archive"], "limit": 1, "cursor": cursor},
            headers=_headers(other.value),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_cursor"


def test_http_search_rejects_valid_cursor_when_router_has_no_codec(
    search_v2_api: SearchV2Api,
) -> None:
    _prepare_two_pages(search_v2_api)
    with TestClient(_app(search_v2_api)) as client:
        cursor = _first_page(client, search_v2_api)["next_cursor"]
        assert isinstance(cursor, str)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: _REQUEST_ID)
    app.include_router(create_search_v2_router(search_v2_api.engine, clock=lambda: 2_000_000))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/search",
            json={"keywords": ["archive"], "limit": 1, "cursor": cursor},
            headers=_headers(search_v2_api.token),
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"
