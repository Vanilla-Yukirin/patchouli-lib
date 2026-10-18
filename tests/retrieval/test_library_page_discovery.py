"""Synthetic, exact-snapshot Agent discovery without legacy wire changes."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from content.conftest import OPERATION_TIME, ArchiveScope
from content.conftest import archive_scope as archive_scope
from content.conftest import content_engine as content_engine
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from content.test_page_move_caller_replay import TARGET, _destination, _move, _prepare
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, insert, update

from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import create_file_set_read_router
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.api.retrieval_routes import create_retrieval_router
from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewCredential, NewSectionGrant, SectionAction
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.file_set_create_service import (
    FileSetCreateCommand,
    FileSetCreateService,
    FileSetCreateSuccess,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    PageLifecycleCommand,
)
from patchouli_lib.content.service import ArchiveService, page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.retrieval.cursor import CursorCodec
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import ReadWindow
from patchouli_lib.retrieval.service import RetrievalNotFoundError, RetrievalService

NOW = OPERATION_TIME + 100


@dataclass(frozen=True)
class Discovery:
    library: str
    section: str
    book: str
    pages: tuple[str, ...]


def _app(engine: Engine) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: "req_" + "d" * 32)
    app.include_router(
        create_retrieval_router(
            engine,
            cursor_codec=CursorCodec(b"synthetic-discovery-cursor-secret"),
            clock=lambda: NOW,
        )
    )
    app.include_router(create_file_set_read_router(engine, clock=lambda: NOW))
    return app


def _headers(scope: ArchiveScope) -> dict[str, str]:
    return {"Authorization": f"Bearer {scope.token.value}"}


@pytest.fixture
def discovery(content_engine: Engine, archive_scope: ArchiveScope) -> Discovery:
    library, section, book = seed_library_structure(
        content_engine, prefix="6", label="Discovery target"
    )
    assert library != archive_scope.library_id
    pages: list[str] = []
    with immediate_transaction(content_engine) as connection:
        values = dict(
            credential_id=archive_scope.credential_id,
            caller_id=archive_scope.caller_id,
            home_library_id=archive_scope.library_id,
            created_at=OPERATION_TIME,
        )
        connection.execute(insert(CredentialLibraryPolicy), dict(values, mode="library_grants"))
        connection.execute(
            insert(CredentialLibraryGrant), dict(values, target_library_id=library, action="write")
        )
        for number in (1, 2):
            graph = page_graph_values(
                library_id=library,
                section_id=section,
                book_id=book,
                page_byte=number,
                revision_hex=f"{number:02x}",
                source_hex=str(number),
                title=f"Synthetic 中文 {number}",
            )
            insert_page_graph(connection, graph)
            pages.append(graph[0].page_id)
        binary = FileSetCreateService(connection, clock=lambda: OPERATION_TIME).create_page(
            archive_scope.token.value,
            FileSetCreateCommand(
                library_id=library,
                section_id=section,
                book_id=book,
                title="Synthetic binary",
                files=(("payload.bin", b"\x00\xff"),),
                source=ArchiveSourceInput(kind="synthetic"),
                request_id="req_" + "c" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("synthetic-discovery")),
        )
        assert isinstance(binary, FileSetCreateSuccess)
        pages.append(binary.page.page_id)
        connection.execute(
            delete(CredentialLibraryGrant).where(
                CredentialLibraryGrant.credential_id == archive_scope.credential_id
            )
        )
        connection.execute(
            insert(CredentialLibraryGrant), dict(values, target_library_id=library, action="read")
        )
    return Discovery(library, section, book, tuple(sorted(pages)))


def test_cross_library_pages_paginate_and_link_both_storage_formats(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    discovery: Discovery,
) -> None:
    base = f"/api/v1/libraries/{discovery.library}/sections/{discovery.section}/pages"
    with TestClient(_app(content_engine)) as client:
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get(base, headers=_headers(archive_scope), params=params)
            assert response.status_code == 200 and "etag" not in response.headers
            assert response.headers["cache-control"] == "private, no-store"
            item = response.json()["items"][0]
            page_id = item["page"]["page_id"]
            seen.append(page_id)
            stable = client.get(
                f"/api/v1/libraries/{discovery.library}/pages/{page_id}",
                headers=_headers(archive_scope),
            )
            assert stable.status_code == 200 and stable.json() == item
            assert "etag" not in stable.headers and "content" not in item
            current = client.get(item["current_files_href"], headers=_headers(archive_scope))
            exact = client.get(item["revision_files_href"], headers=_headers(archive_scope))
            assert current.status_code == exact.status_code == 200
            assert current.json() == exact.json()
            assert current.json()["revision_id"] == item["page"]["current_revision_id"]
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break
        assert seen == list(discovery.pages)
        hidden = client.get(
            f"/api/v1/libraries/{archive_scope.library_id}/pages/{seen[0]}",
            headers=_headers(archive_scope),
        )
        assert hidden.status_code == 404


def test_write_only_and_revoked_credentials_do_not_discover(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    discovery: Discovery,
) -> None:
    with immediate_transaction(content_engine) as connection:
        connection.execute(update(CredentialLibraryGrant).values(action="write"))
    list_path = f"/api/v1/libraries/{discovery.library}/sections/{discovery.section}/pages"
    stable_path = f"/api/v1/libraries/{discovery.library}/pages/{discovery.pages[0]}"
    with TestClient(_app(content_engine)) as client:
        for path in (list_path, stable_path):
            assert client.get(path, headers=_headers(archive_scope)).status_code == 404
        with immediate_transaction(content_engine) as connection:
            connection.execute(
                update(Credential)
                .where(Credential.id == archive_scope.credential_id)
                .values(revoked_at=NOW, updated_at=NOW)
            )
        for path in (list_path, stable_path):
            assert client.get(path, headers=_headers(archive_scope)).status_code == 401


def test_new_cursor_binds_target_credential_policy_and_route(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    discovery: Discovery,
) -> None:
    base = f"/api/v1/libraries/{discovery.library}/sections/{discovery.section}/pages"
    with TestClient(_app(content_engine)) as client:
        response = client.get(base, headers=_headers(archive_scope), params={"limit": 1})
        cursor = response.json()["next_cursor"]
        assert cursor is not None
        wrong_limit = client.get(
            base, headers=_headers(archive_scope), params={"limit": 2, "cursor": cursor}
        )
        assert wrong_limit.status_code == 400
        wrong_section = client.get(
            base.replace(discovery.section, "f" * 32),
            headers=_headers(archive_scope),
            params={"limit": 1, "cursor": cursor},
        )
        assert wrong_section.status_code == 400
        other = generate_token()
        with immediate_transaction(content_engine) as connection:
            credential_id = "e" * 32
            values = dict(
                credential_id=credential_id,
                caller_id=archive_scope.caller_id,
                home_library_id=archive_scope.library_id,
                created_at=OPERATION_TIME,
            )
            AuthRepository(connection).add_credential(
                NewCredential(
                    id=credential_id,
                    library_id=archive_scope.library_id,
                    caller_id=archive_scope.caller_id,
                    selector=other.selector,
                    token_version=other.version,
                    verifier=other.verifier,
                    created_at=OPERATION_TIME,
                    updated_at=OPERATION_TIME,
                    expires_at=OPERATION_TIME + 10_000_000,
                )
            )
            connection.execute(insert(CredentialLibraryPolicy), dict(values, mode="library_grants"))
            for target, cred in (
                (discovery.library, values),
                (archive_scope.library_id, dict(values, credential_id=archive_scope.credential_id)),
            ):
                connection.execute(
                    insert(CredentialLibraryGrant),
                    dict(cred, target_library_id=target, action="read"),
                )
        wrong_credential = client.get(
            base,
            headers={"Authorization": f"Bearer {other.value}"},
            params={"limit": 1, "cursor": cursor},
        )
        assert wrong_credential.status_code == 400
        wrong_target = client.get(
            f"/api/v1/libraries/{archive_scope.library_id}/sections/{archive_scope.section_id}/pages",
            headers=_headers(archive_scope),
            params={"limit": 1, "cursor": cursor},
        )
        assert wrong_target.status_code == 400
        old_route = client.get(
            f"/api/v1/sections/{discovery.section}/pages",
            headers=_headers(archive_scope),
            params={"limit": 1, "cursor": cursor},
        )
        assert old_route.status_code == 400
        with immediate_transaction(content_engine) as connection:
            connection.execute(
                update(Caller)
                .where(Caller.id == archive_scope.caller_id)
                .values(policy_version=Caller.policy_version + 1)
            )
        changed = client.get(
            base, headers=_headers(archive_scope), params={"limit": 1, "cursor": cursor}
        )
        assert changed.status_code == 400 and changed.json()["code"] == "invalid_cursor"


def test_deleted_page_is_absent_from_metadata_and_list(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    discovery: Discovery,
) -> None:
    page_id = discovery.pages[0]
    with immediate_transaction(content_engine) as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            dict(
                credential_id=archive_scope.credential_id,
                caller_id=archive_scope.caller_id,
                home_library_id=archive_scope.library_id,
                target_library_id=discovery.library,
                action="write",
                created_at=OPERATION_TIME,
            ),
        )
        page = ContentRepository(connection).get_page(discovery.library, page_id)
        assert page is not None
        ArchiveService(connection, clock=lambda: NOW).transition_page_lifecycle(
            archive_scope.token.value,
            PageLifecycleCommand(
                library_id=discovery.library,
                section_id=discovery.section,
                page_id=page_id,
                expected_etag=page_current_etag(
                    page.page_uid,
                    page.current_revision_id,
                    page.current_revision_number,
                    page.occurred_at,
                    page.updated_at,
                ),
                request_id="req_" + "f" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("synthetic-delete")),
            action="delete",
        )
    with TestClient(_app(content_engine)) as client:
        absent = client.get(
            f"/api/v1/libraries/{discovery.library}/pages/{page_id}",
            headers=_headers(archive_scope),
        )
        assert absent.status_code == 404
        listed = client.get(
            f"/api/v1/libraries/{discovery.library}/sections/{discovery.section}/pages",
            headers=_headers(archive_scope),
        )
        assert listed.status_code == 200
        assert {item["page"]["page_id"] for item in listed.json()["items"]} == set(
            discovery.pages
        ) - {page_id}


def test_stable_page_follows_move_without_expanding_legacy_sections(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    page, _, _ = _prepare(content_engine, archive_scope, "create")
    with immediate_transaction(content_engine) as connection:
        auth = AuthRepository(connection)
        for action in (SectionAction.PAGE_READ, SectionAction.QUERY):
            auth.add_grant(
                NewSectionGrant(
                    library_id=archive_scope.library_id,
                    caller_id=archive_scope.caller_id,
                    section_id=archive_scope.section_id,
                    action=action,
                    created_at=OPERATION_TIME,
                )
            )
    path = f"/api/v1/libraries/{page.library_id}/pages/{page.page_id}"
    with TestClient(_app(content_engine)) as client:
        assert client.get(path, headers=_headers(archive_scope)).status_code == 200
        assert (
            client.get(
                f"/api/v1/libraries/{'f' * 32}/sections/{archive_scope.section_id}/pages",
                headers=_headers(archive_scope),
            ).status_code
            == 404
        )
        # The older discovery routes retain their accepted 403 cross-Library denial.
        for old_path in ("/api/v1/sections", f"/api/v1/sections/{archive_scope.section_id}/books"):
            assert (
                client.get(
                    old_path,
                    headers=_headers(archive_scope),
                    params={"library_id": "f" * 32},
                ).status_code
                == 403
            )
        page = _move(
            content_engine,
            page,
            TARGET,
            "synthetic-discovery-move",
            _destination(content_engine, archive_scope),
        )
        assert client.get(path, headers=_headers(archive_scope)).status_code == 404
        with immediate_transaction(content_engine) as connection:
            AuthRepository(connection).add_grant(
                NewSectionGrant(
                    library_id=archive_scope.library_id,
                    caller_id=archive_scope.caller_id,
                    section_id=TARGET[0],
                    action=SectionAction.PAGE_READ,
                    created_at=OPERATION_TIME,
                )
            )
        moved = client.get(path, headers=_headers(archive_scope))
        assert moved.status_code == 200 and moved.json()["page"]["section_id"] == TARGET[0]
        assert moved.json()["page"]["book_id"] == TARGET[1]
        denied_list = client.get(
            f"/api/v1/libraries/{page.library_id}/sections/{TARGET[0]}/pages",
            headers=_headers(archive_scope),
        )
        assert denied_list.status_code == 404
        assert (
            client.get(
                f"/api/v1/libraries/{'f' * 32}/pages/{page.page_id}",
                headers=_headers(archive_scope),
            ).status_code
            == 404
        )


def test_page_list_policy_and_rows_share_real_begin_snapshot(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    discovery: Discovery,
) -> None:
    # WAL allows a synthetic concurrent policy change after the first snapshot read.
    with content_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    with content_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        authenticated = AuthRepository(connection).get_caller(
            archive_scope.library_id, archive_scope.caller_id
        )
        assert authenticated is not None
        from patchouli_lib.auth.service import AuthenticationService

        context = AuthenticationService(
            AuthRepository(connection), clock=lambda: NOW, last_used_coalesce_microseconds=-1
        ).authenticate(archive_scope.token.value)
        service = RetrievalService(RetrievalRepository(connection), context, clock=lambda: NOW)
        binding = service.library_page_cursor_binding(discovery.library, discovery.section, limit=1)
        with immediate_transaction(content_engine) as writer:
            writer.execute(
                delete(CredentialLibraryGrant).where(
                    CredentialLibraryGrant.credential_id == archive_scope.credential_id
                )
            )
        page = service.list_library_pages(discovery.library, discovery.section, ReadWindow(limit=1))
        assert len(page.items) == 1 and binding.section_id == discovery.section
        connection.rollback()
    with content_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        service = RetrievalService(RetrievalRepository(connection), context, clock=lambda: NOW)
        with pytest.raises(RetrievalNotFoundError):
            service.list_library_pages(discovery.library, discovery.section)
        connection.rollback()
