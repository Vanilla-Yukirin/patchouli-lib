"""Tag routes preserve exact Page and legacy Section-grant boundaries."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from fastapi import FastAPI
from fastapi.testclient import TestClient
from retrieval_read.conftest import (
    CALLER_ID,
    HIDDEN_SECTION_ID,
    QUERY_SECTION_ID,
    READ_SECTION_ID,
    RetrievalScope,
)
from retrieval_read.conftest import retrieval_engine as retrieval_engine_fixture
from retrieval_read.conftest import retrieval_scope as retrieval_scope_fixture
from sqlalchemy import Engine, delete, insert, select

from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.api.tag_routes import _perform, create_tag_router
from patchouli_lib.auth.models import AuditEvent, CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import immediate_transaction
from patchouli_lib.tags.repository import TagRepository

REQUEST_ID = "req_1234567890abcdef1234567890abcdef"
OPERATOR_ID = "e" * 32
OPERATOR_CREDENTIAL_ID = "f" * 32
AGENT_CREDENTIAL_ID = "d" * 32


@dataclass(frozen=True, slots=True)
class TagApi:
    engine: Engine
    scope: RetrievalScope
    agent_token: str
    operator_token: str


@pytest.fixture
def tag_api(tmp_path: Path) -> Iterator[TagApi]:
    engine_factory = cast(
        Callable[[Path], Iterator[Engine]], cast(Any, retrieval_engine_fixture).__wrapped__
    )
    scope_factory = cast(
        Callable[[Engine], RetrievalScope], cast(Any, retrieval_scope_fixture).__wrapped__
    )
    iterator = engine_factory(tmp_path)
    engine = next(iterator)
    scope = scope_factory(engine)
    agent = generate_token()
    operator = generate_token()
    with immediate_transaction(engine) as connection:
        auth = AuthRepository(connection)
        auth.add_credential(
            NewCredential(
                id=AGENT_CREDENTIAL_ID,
                library_id=scope.library_id,
                caller_id=CALLER_ID,
                selector=agent.selector,
                token_version=agent.version,
                verifier=agent.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        auth.add_grant(
            NewSectionGrant(
                library_id=scope.library_id,
                caller_id=CALLER_ID,
                section_id=QUERY_SECTION_ID,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=1_000_000,
            )
        )
        auth.add_caller(
            NewCaller(
                id=OPERATOR_ID,
                library_id=scope.library_id,
                kind=CallerKind.OPERATOR,
                name="Synthetic Operator",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        auth.add_credential(
            NewCredential(
                id=OPERATOR_CREDENTIAL_ID,
                library_id=scope.library_id,
                caller_id=OPERATOR_ID,
                selector=operator.selector,
                token_version=operator.version,
                verifier=operator.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    try:
        yield TagApi(engine, scope, agent.value, operator.value)
    finally:
        with suppress(StopIteration):
            next(iterator)


def _app(fixture: TagApi) -> FastAPI:
    app = FastAPI()
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: REQUEST_ID)
    app.include_router(create_tag_router(fixture.engine, clock=lambda: 2_000_000))
    return app


def _path(api: TagApi, suffix: str) -> str:
    return f"/api/v1/libraries/{api.scope.library_id}/{suffix}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_tag(api: TagApi, *, tag_id: str, name: str, page_ids: list[str]) -> None:
    with immediate_transaction(api.engine) as connection:
        repo = TagRepository(connection)
        repo.add_tag(
            library_id=api.scope.library_id, tag_id=tag_id, name=name, created_at=1_500_000
        )
        for page_id in page_ids:
            row = connection.exec_driver_sql(
                "SELECT page_uid FROM pages WHERE library_id = ? AND page_id = ?",
                (api.scope.library_id, page_id),
            ).one()
            repo.attach_page(
                library_id=api.scope.library_id,
                page_uid=row.page_uid,
                tag_id=tag_id,
                created_at=1_500_000,
            )


def _opt_in(api: TagApi) -> None:
    with immediate_transaction(api.engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": AGENT_CREDENTIAL_ID,
                "caller_id": CALLER_ID,
                "home_library_id": api.scope.library_id,
                "mode": "library_grants",
                "created_at": 1_500_000,
            },
        )


def _grant(api: TagApi, action: str) -> None:
    with immediate_transaction(api.engine) as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": AGENT_CREDENTIAL_ID,
                "caller_id": CALLER_ID,
                "home_library_id": api.scope.library_id,
                "target_library_id": api.scope.library_id,
                "action": action,
                "created_at": 1_500_000,
            },
        )


def _revoke(api: TagApi, action: str) -> None:
    with immediate_transaction(api.engine) as connection:
        connection.execute(
            delete(CredentialLibraryGrant).where(
                CredentialLibraryGrant.credential_id == AGENT_CREDENTIAL_ID,
                CredentialLibraryGrant.caller_id == CALLER_ID,
                CredentialLibraryGrant.home_library_id == api.scope.library_id,
                CredentialLibraryGrant.target_library_id == api.scope.library_id,
                CredentialLibraryGrant.action == action,
            )
        )


def _legacy_sibling_token(api: TagApi) -> str:
    sibling = generate_token()
    with immediate_transaction(api.engine) as connection:
        AuthRepository(connection).add_credential(
            NewCredential(
                id="0" * 32,
                library_id=api.scope.library_id,
                caller_id=CALLER_ID,
                selector=sibling.selector,
                token_version=sibling.version,
                verifier=sibling.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return sibling.value


def test_directory_hides_hidden_and_deleted_page_names_counts_and_pagination(
    tag_api: TagApi,
) -> None:
    api = tag_api
    _seed_tag(
        api,
        tag_id="1" * 32,
        name="Shared",
        page_ids=[api.scope.first_page_id, api.scope.hidden_page_id],
    )
    _seed_tag(api, tag_id="2" * 32, name="Private", page_ids=[api.scope.hidden_page_id])
    _seed_tag(api, tag_id="3" * 32, name="Deleted", page_ids=[api.scope.deleted_page_id])
    _seed_tag(api, tag_id="4" * 32, name="Read-only", page_ids=[api.scope.read_page_id])
    _seed_tag(api, tag_id="5" * 32, name="Unassociated", page_ids=[])

    with TestClient(_app(api), raise_server_exceptions=False) as client:
        agent = client.get(_path(api, "tags"), headers=_auth(api.agent_token))
        assert agent.status_code == 200
        assert agent.json() == {
            "items": [
                {
                    "tag_id": "1" * 32,
                    "name": "Shared",
                    "created_at": 1_500_000,
                    "page_count": 1,
                }
            ],
            "next_offset": None,
        }
        assert agent.headers["Cache-Control"] == "private, no-store"
        assert agent.headers["X-Request-ID"] == REQUEST_ID
        assert (
            client.get(
                _path(api, f"tags/{'2' * 32}/pages"), headers=_auth(api.agent_token)
            ).status_code
            == 404
        )
        visible_pages = client.get(
            _path(api, f"tags/{'1' * 32}/pages"), headers=_auth(api.agent_token)
        )
        assert [item["page_id"] for item in visible_pages.json()["items"]] == [
            api.scope.first_page_id
        ]
        read_only_page = _path(
            api, f"sections/{READ_SECTION_ID}/pages/{api.scope.read_page_id}/tags"
        )
        assert [
            item["name"]
            for item in client.get(read_only_page, headers=_auth(api.agent_token)).json()["items"]
        ] == ["Read-only"]
        operator = client.get(_path(api, "tags"), headers=_auth(api.operator_token))
        assert [item["name"] for item in operator.json()["items"]] == [
            "Deleted",
            "Private",
            "Read-only",
            "Shared",
            "Unassociated",
        ]
        query = client.get(
            _path(api, "tags"), headers=_auth(api.operator_token), params={"q": "SHAR"}
        )
        assert [item["name"] for item in query.json()["items"]] == ["Shared"]
        first = client.get(
            _path(api, "tags"), headers=_auth(api.operator_token), params={"limit": "1"}
        )
        assert first.json()["next_offset"] == 1
        second = client.get(
            _path(api, "tags"),
            headers=_auth(api.operator_token),
            params={"limit": "1", "offset": "1"},
        )
        assert second.json()["items"][0]["name"] == "Private"


def test_operator_creates_normalized_tag_and_agent_changes_exact_live_page(tag_api: TagApi) -> None:
    api = tag_api
    tag_path = _path(api, "tags")
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        denied = client.post(tag_path, headers=_auth(api.agent_token), json={"name": "Café"})
        assert denied.status_code == 403
        created = client.post(tag_path, headers=_auth(api.operator_token), json={"name": "Café"})
        assert created.status_code == 201
        tag_id = created.json()["tag_id"]
        with api.engine.connect() as connection:
            keys = connection.exec_driver_sql("SELECT match_key FROM tags").scalars().all()
            assert keys == ["café"], [ascii(key) for key in keys]
        replay = client.post(
            tag_path, headers=_auth(api.operator_token), json={"name": "CAFE\u0301"}
        )
        assert replay.status_code == 200, (created.json(), replay.json())
        assert replay.json()["tag_id"] == tag_id
        page_path = _path(
            api,
            f"sections/{QUERY_SECTION_ID}/pages/{api.scope.first_page_id}/tags/{tag_id}",
        )
        first = client.put(page_path, headers=_auth(api.agent_token))
        assert first.status_code == 200 and first.json() == {"changed": True}
        repeat = client.put(page_path, headers=_auth(api.agent_token))
        assert repeat.status_code == 200 and repeat.json() == {"changed": False}
        tags = client.get(page_path.rsplit("/", 1)[0], headers=_auth(api.agent_token))
        assert [item["name"] for item in tags.json()["items"]] == ["Café"]
        assert client.delete(page_path, headers=_auth(api.agent_token)).json() == {"changed": True}
        assert client.delete(page_path, headers=_auth(api.agent_token)).json() == {"changed": False}
    with api.engine.connect() as connection:
        actions = connection.execute(
            select(AuditEvent.action).order_by(AuditEvent.action)
        ).scalars()
        assert [action for action in actions if action.startswith("tag.")] == [
            "tag.create",
            "tag.page.attach",
            "tag.page.detach",
        ]


def test_cross_library_deleted_pages_and_grant_revocation_fail_closed(tag_api: TagApi) -> None:
    api = tag_api
    tag_id = "7" * 32
    _seed_tag(api, tag_id=tag_id, name="Visible", page_ids=[api.scope.first_page_id])
    visible_path = _path(
        api, f"sections/{QUERY_SECTION_ID}/pages/{api.scope.first_page_id}/tags/{tag_id}"
    )
    hidden_path = _path(
        api, f"sections/{HIDDEN_SECTION_ID}/pages/{api.scope.hidden_page_id}/tags/{tag_id}"
    )
    deleted_path = _path(
        api, f"sections/{QUERY_SECTION_ID}/pages/{api.scope.deleted_page_id}/tags/{tag_id}"
    )
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        assert (
            client.get(
                f"/api/v1/libraries/{'9' * 32}/tags", headers=_auth(api.agent_token)
            ).status_code
            == 404
        )
        assert client.put(hidden_path, headers=_auth(api.agent_token)).status_code == 404
        assert client.put(deleted_path, headers=_auth(api.agent_token)).status_code == 404
        assert client.put(visible_path, headers=_auth("not-a-token")).status_code == 401
        with immediate_transaction(api.engine) as connection:
            AuthRepository(connection).remove_grant(
                api.scope.library_id, CALLER_ID, QUERY_SECTION_ID, SectionAction.ARCHIVE_WRITE
            )
        assert client.delete(visible_path, headers=_auth(api.agent_token)).status_code == 403
        with immediate_transaction(api.engine) as connection:
            AuthRepository(connection).remove_grant(
                api.scope.library_id, CALLER_ID, QUERY_SECTION_ID, SectionAction.PAGE_READ
            )
        listed = client.get(_path(api, "tags"), headers=_auth(api.agent_token))
        assert listed.json()["items"] == []
        assert (
            client.get(visible_path.rsplit("/", 1)[0], headers=_auth(api.agent_token)).status_code
            == 403
        )


def test_opt_in_uses_exact_credential_read_and_write_without_legacy_fallback(
    tag_api: TagApi,
) -> None:
    api = tag_api
    _seed_tag(api, tag_id="1" * 32, name="Visible", page_ids=[api.scope.first_page_id])
    _seed_tag(api, tag_id="2" * 32, name="Hidden", page_ids=[api.scope.hidden_page_id])
    _seed_tag(api, tag_id="3" * 32, name="Unassociated", page_ids=[])
    sibling_token = _legacy_sibling_token(api)
    _opt_in(api)
    directory = _path(api, "tags")
    visible = _path(api, f"sections/{QUERY_SECTION_ID}/pages/{api.scope.first_page_id}/tags")
    hidden = _path(api, f"sections/{HIDDEN_SECTION_ID}/pages/{api.scope.hidden_page_id}/tags")
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        assert client.get(directory, headers=_auth(api.agent_token)).status_code == 403
        assert client.get(visible, headers=_auth(api.agent_token)).status_code == 403
        assert (
            client.put(f"{visible}/{'1' * 32}", headers=_auth(api.agent_token)).status_code == 403
        )
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Denied"}
            ).status_code
            == 403
        )
        sibling = client.get(directory, headers=_auth(sibling_token))
        assert [item["name"] for item in sibling.json()["items"]] == ["Visible"]

        _grant(api, "read")
        readable = client.get(directory, headers=_auth(api.agent_token))
        assert [(item["name"], item["page_count"]) for item in readable.json()["items"]] == [
            ("Hidden", 1),
            ("Unassociated", 0),
            ("Visible", 1),
        ]
        assert (
            client.get(hidden, headers=_auth(api.agent_token)).json()["items"][0]["name"]
            == "Hidden"
        )
        assert (
            client.get(_path(api, f"tags/{'2' * 32}/pages"), headers=_auth(api.agent_token)).json()[
                "items"
            ][0]["page_id"]
            == api.scope.hidden_page_id
        )
        assert client.put(f"{hidden}/{'1' * 32}", headers=_auth(api.agent_token)).status_code == 403

        _grant(api, "write")
        assert client.put(f"{hidden}/{'1' * 32}", headers=_auth(api.agent_token)).json() == {
            "changed": True
        }
        created = client.post(directory, headers=_auth(api.agent_token), json={"name": "New"})
        assert created.status_code == 201
        assert client.get(directory, headers=_auth(sibling_token)).json()["items"] == [
            {
                "tag_id": "1" * 32,
                "name": "Visible",
                "created_at": 1_500_000,
                "page_count": 1,
            }
        ]
    with api.engine.connect() as connection:
        tag_audits = connection.execute(
            select(AuditEvent.library_id, AuditEvent.actor_credential_id).where(
                AuditEvent.action.like("tag.%")
            )
        ).all()
        assert [tuple(row) for row in tag_audits] == [
            (api.scope.library_id, AGENT_CREDENTIAL_ID),
            (api.scope.library_id, AGENT_CREDENTIAL_ID),
        ]


def test_opt_in_write_only_does_not_grant_tag_read(tag_api: TagApi) -> None:
    api = tag_api
    _seed_tag(api, tag_id="1" * 32, name="Existing", page_ids=[])
    _opt_in(api)
    _grant(api, "write")
    directory = _path(api, "tags")
    hidden = _path(api, f"sections/{HIDDEN_SECTION_ID}/pages/{api.scope.hidden_page_id}/tags")
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        assert client.get(directory, headers=_auth(api.agent_token)).status_code == 403
        assert client.get(hidden, headers=_auth(api.agent_token)).status_code == 403
        assert (
            client.get(
                _path(api, f"tags/{'1' * 32}/pages"), headers=_auth(api.agent_token)
            ).status_code
            == 403
        )
        assert client.put(f"{hidden}/{'1' * 32}", headers=_auth(api.agent_token)).json() == {
            "changed": True
        }
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Created"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Existing"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Created"}
            ).status_code
            == 403
        )
        assert client.get(directory, headers=_auth(api.agent_token)).status_code == 403


def test_opt_in_tag_grant_revocation_takes_effect_immediately(tag_api: TagApi) -> None:
    api = tag_api
    _seed_tag(api, tag_id="1" * 32, name="Existing", page_ids=[])
    _opt_in(api)
    _grant(api, "read")
    _grant(api, "write")
    directory = _path(api, "tags")
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        assert client.get(directory, headers=_auth(api.agent_token)).status_code == 200
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Existing"}
            ).status_code
            == 200
        )
        _revoke(api, "read")
        assert client.get(directory, headers=_auth(api.agent_token)).status_code == 403
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Existing"}
            ).status_code
            == 403
        )
        assert (
            client.post(directory, headers=_auth(api.agent_token), json={"name": "New"}).status_code
            == 201
        )
        _revoke(api, "write")
        assert (
            client.post(
                directory, headers=_auth(api.agent_token), json={"name": "Other"}
            ).status_code
            == 403
        )


def test_opt_in_home_library_tag_counts_do_not_include_foreign_library(tag_api: TagApi) -> None:
    api = tag_api
    shared_id = "1" * 32
    _seed_tag(api, tag_id=shared_id, name="Home", page_ids=[api.scope.first_page_id])
    foreign_library, foreign_section, foreign_book = seed_library_structure(
        api.engine, prefix="b", label="Foreign"
    )
    foreign_page = page_graph_values(
        library_id=foreign_library,
        section_id=foreign_section,
        book_id=foreign_book,
        page_byte=0x77,
        revision_hex="88",
        source_hex="9",
    )
    with immediate_transaction(api.engine) as connection:
        insert_page_graph(connection, foreign_page)
        repository = TagRepository(connection)
        repository.add_tag(
            library_id=foreign_library, tag_id=shared_id, name="Foreign", created_at=1_500_000
        )
        repository.attach_page(
            library_id=foreign_library,
            page_uid=foreign_page[0].page_uid,
            tag_id=shared_id,
            created_at=1_500_000,
        )
    _opt_in(api)
    _grant(api, "read")
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        directory = client.get(_path(api, "tags"), headers=_auth(api.agent_token))
        assert [(item["name"], item["page_count"]) for item in directory.json()["items"]] == [
            ("Home", 1)
        ]
        pages = client.get(_path(api, f"tags/{shared_id}/pages"), headers=_auth(api.agent_token))
        assert [item["page_id"] for item in pages.json()["items"]] == [api.scope.first_page_id]
        assert (
            client.get(
                f"/api/v1/libraries/{foreign_library}/tags", headers=_auth(api.agent_token)
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    ("method", "suffix", "kwargs"),
    [
        ("get", "tags", {"params": {"limit": "101"}}),
        ("get", "tags", {"params": {"q": "a", "unexpected": "1"}}),
        ("get", "tags", {"params": [("q", "a"), ("q", "b")]}),
        ("post", "tags", {"json": {"name": " bad"}}),
        ("post", "tags", {"json": {"name": "x", "extra": 1}}),
    ],
)
def test_tag_input_validation(
    tag_api: TagApi, method: str, suffix: str, kwargs: dict[str, object]
) -> None:
    with TestClient(_app(tag_api), raise_server_exceptions=False) as client:
        response = getattr(client, method)(
            _path(tag_api, suffix), headers=_auth(tag_api.operator_token), **kwargs
        )
        assert response.status_code == 422
        assert response.json()["code"] == "request_validation_failed"


def test_create_rejects_oversized_and_ambiguous_json(tag_api: TagApi) -> None:
    path = _path(tag_api, "tags")
    headers = {**_auth(tag_api.operator_token), "Content-Type": "application/json"}
    with TestClient(_app(tag_api), raise_server_exceptions=False) as client:
        duplicate = client.post(path, headers=headers, content=b'{"name":"A","name":"B"}')
        assert duplicate.status_code == 422
        oversized = client.post(path, headers=headers, content=b"x" * 1_025)
        assert oversized.status_code == 413
    with tag_api.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM tags").scalar_one() == 0


def test_tag_read_does_not_hold_sqlite_writer_reservation(tag_api: TagApi) -> None:
    entered_read = Event()
    release_read = Event()

    def read_directory(service: object) -> None:
        from patchouli_lib.tags.service import TagService

        assert isinstance(service, TagService)
        service.list_tags(tag_api.agent_token, library_id=tag_api.scope.library_id)
        entered_read.set()
        assert release_read.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _perform,
            tag_api.engine,
            tag_api.agent_token,
            read_directory,
            clock=lambda: 7_000_000,
            read_only=True,
        )
        try:
            assert entered_read.wait(timeout=5)
            # BEGIN IMMEDIATE may coexist with a deferred read transaction.
            # The previous implementation reserved this writer slot for the
            # entire directory scan and this acquisition would time out.
            with tag_api.engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.rollback()
        finally:
            release_read.set()
        future.result(timeout=5)
