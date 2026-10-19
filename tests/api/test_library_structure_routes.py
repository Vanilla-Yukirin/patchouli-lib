"""Small HTTP checks for empty-Library discovery, grants and safe README edits."""

from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from content.conftest import OPERATION_TIME
from content.conftest import content_engine as content_engine
from content.test_agent_page_move import _agent
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, update

from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.library_structure_routes import (
    MAX_STRUCTURE_BODY_BYTES,
    create_library_structure_router,
)
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.auth.models import (
    AuditEvent,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Section
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewLibrary


@dataclass(frozen=True)
class StructureStory:
    engine: Engine
    app: FastAPI
    library: str
    foreign_library: str
    headers: dict[str, str]
    credential_id: str

    @property
    def path(self) -> str:
        return f"/api/v1/libraries/{self.library}"


@pytest.fixture
def story(content_engine: Engine) -> StructureStory:
    library_id, foreign_id = "1" * 32, "2" * 32
    with immediate_transaction(content_engine) as connection:
        repository = LibraryRepository(connection)
        for identifier in (library_id, foreign_id):
            repository.add_library(
                NewLibrary(
                    id=identifier,
                    name=f"Synthetic {identifier[0]}",
                    description="Archive records",
                    readme="  # Guide\n\nKeep additions.\n",
                    created_at=OPERATION_TIME - 10,
                    updated_at=OPERATION_TIME - 10,
                )
            )
    agent = _agent(content_engine, library_id)
    app = FastAPI()
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(
        create_library_structure_router(content_engine, clock=lambda: OPERATION_TIME + 50)
    )
    return StructureStory(
        content_engine,
        app,
        library_id,
        foreign_id,
        {"Authorization": f"Bearer {agent.token}"},
        agent.credential_id,
    )


def test_empty_library_write_only_agent_initializes_hierarchy_and_readmes(
    story: StructureStory,
) -> None:
    with TestClient(story.app) as client:
        libraries = client.get("/api/v1/libraries", headers=story.headers)
        assert [item["id"] for item in libraries.json()["items"]] == [story.library]
        assert client.get(story.path + "/sections", headers=story.headers).json()["items"] == []
        library = client.get(story.path, headers=story.headers)
        assert library.json()["readme"] == "  # Guide\n\nKeep additions.\n"
        section = client.post(
            story.path + "/sections",
            headers=story.headers,
            json={
                "name": "Projects",
                "description": "Long-lived project records",
                "readme": "\n # Rules\n",
            },
        )
        assert section.status_code == 201
        section_path = story.path + "/sections/" + section.json()["id"]
        book = client.post(
            section_path + "/books",
            headers=story.headers,
            json={"name": "Delivery", "summary": "Delivery decisions", "readme": "  Book guide\n"},
        )
        assert book.status_code == 201
        book_path = section_path + "/books/" + book.json()["id"]
        assert client.get(book_path, headers=story.headers).json()["readme"] == "  Book guide\n"
        assert len(client.get(section_path + "/books", headers=story.headers).json()["items"]) == 1
        for path in (story.path, section_path, book_path):
            current = client.get(path, headers=story.headers)
            assert (
                client.get(
                    path, headers={**story.headers, "If-None-Match": current.headers["etag"]}
                ).status_code
                == 304
            )
            changed = client.put(
                path + "/readme",
                headers={**story.headers, "If-Match": current.headers["etag"]},
                json={"readme": "\n Updated\n"},
            )
            assert changed.status_code == 200 and changed.json()["readme"] == "\n Updated\n"
            assert changed.headers["etag"] != current.headers["etag"]
            assert client.get(path, headers=story.headers).json() == changed.json()
            unchanged = client.put(
                path + "/readme",
                headers={**story.headers, "If-Match": changed.headers["etag"]},
                json={"readme": "\n Updated\n"},
            )
            assert unchanged.headers["etag"] == changed.headers["etag"]
    with story.engine.connect() as connection:
        events = connection.execute(
            select(AuditEvent.action, AuditEvent.actor_credential_id, AuditEvent.resource_id).where(
                AuditEvent.action.like("structure.%")
            )
        ).all()
    assert len(events) == 5
    assert all(event.actor_credential_id == story.credential_id for event in events)
    assert {event.action for event in events} == {
        "structure.section.create",
        "structure.book.create",
        "structure.library.readme_update",
        "structure.section.readme_update",
        "structure.book.readme_update",
    }


def test_read_only_agent_reads_structure_but_cannot_write(story: StructureStory) -> None:
    with immediate_transaction(story.engine) as connection:
        connection.execute(update(CredentialLibraryGrant).values(action="read"))
    with TestClient(story.app) as client:
        assert client.get(story.path, headers=story.headers).status_code == 200
        assert (
            client.post(
                story.path + "/sections",
                headers=story.headers,
                json={"name": "No", "description": "No"},
            ).status_code
            == 403
        )
        assert (
            client.put(
                story.path + "/readme", headers=story.headers, json={"readme": "No"}
            ).status_code
            == 403
        )


def test_legacy_credentials_and_inactive_credentials_do_not_gain_structure_rights(
    story: StructureStory,
) -> None:
    legacy = _agent(
        story.engine, story.library, home_prefix="8", caller_id="b" * 32, opted_in=False
    )
    legacy_headers = {"Authorization": f"Bearer {legacy.token}"}
    with TestClient(story.app) as client:
        assert client.get("/api/v1/libraries", headers=legacy_headers).status_code == 403
        assert client.get(story.path, headers=legacy_headers).status_code == 403
        with immediate_transaction(story.engine) as connection:
            connection.execute(
                update(Credential).values(
                    revoked_at=OPERATION_TIME + 1, updated_at=OPERATION_TIME + 1
                )
            )
        assert client.get(story.path, headers=story.headers).status_code == 401
        assert client.get(story.path).status_code == 401


def test_cross_library_and_mismatched_parent_paths_are_denied(story: StructureStory) -> None:
    with TestClient(story.app) as client:
        foreign = f"/api/v1/libraries/{story.foreign_library}"
        assert client.get(foreign, headers=story.headers).status_code == 403
        assert (
            client.post(
                foreign + "/sections",
                headers=story.headers,
                json={"name": "No", "description": "No"},
            ).status_code
            == 403
        )
        missing = story.path + "/sections/" + "f" * 32
        assert client.get(missing, headers=story.headers).status_code == 404
        assert client.get(missing + "/books", headers=story.headers).status_code == 404
        assert (
            client.post(
                missing + "/books", headers=story.headers, json={"name": "No", "summary": "No"}
            ).status_code
            == 404
        )


def test_same_parent_name_reuses_only_identical_metadata(story: StructureStory) -> None:
    with TestClient(story.app) as client:
        body = {"name": "Projects", "description": "Project context", "readme": "  Preserve\n"}
        created = client.post(story.path + "/sections", headers=story.headers, json=body)
        replay = client.post(story.path + "/sections", headers=story.headers, json=body)
        assert created.status_code == 201 and replay.status_code == 200
        assert replay.json() == created.json()
        assert (
            client.post(
                story.path + "/sections", headers=story.headers, json={**body, "readme": "Changed"}
            ).status_code
            == 409
        )
        books = story.path + "/sections/" + created.json()["id"] + "/books"
        body2 = {"name": "Topic", "summary": "Topic records"}
        book = client.post(books, headers=story.headers, json=body2)
        replay2 = client.post(books, headers=story.headers, json=body2)
        assert book.status_code == 201 and replay2.status_code == 200
        with story.engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action.like("structure.%"))
                )
                == 2
            )
        assert book.json() == replay2.json()
        assert (
            client.post(
                books, headers=story.headers, json={**body2, "summary": "Different"}
            ).status_code
            == 409
        )


def test_readme_preconditions_preserve_newer_content_and_allow_clear(story: StructureStory) -> None:
    with TestClient(story.app) as client:
        etag = client.get(story.path, headers=story.headers).headers["etag"]
        assert (
            client.put(
                story.path + "/readme", headers=story.headers, json={"readme": "First"}
            ).status_code
            == 428
        )
        first = client.put(
            story.path + "/readme",
            headers={**story.headers, "If-Match": etag},
            json={"readme": "First"},
        )
        assert first.status_code == 200
        assert (
            client.put(
                story.path + "/readme",
                headers={**story.headers, "If-Match": etag},
                json={"readme": "Stale"},
            ).status_code
            == 412
        )
        cleared = client.put(
            story.path + "/readme",
            headers={**story.headers, "If-Match": first.headers["etag"]},
            json={"readme": ""},
        )
        assert cleared.status_code == 200 and cleared.json()["readme"] == ""


def test_structure_json_is_bounded_without_markdown_length_limit(story: StructureStory) -> None:
    with TestClient(story.app) as client:
        for body in (
            {"name": "A"},
            {"name": "A", "description": " "},
            {"name": "A", "description": "Yes", "unknown": True},
        ):
            assert (
                client.post(story.path + "/sections", headers=story.headers, json=body).status_code
                == 422
            )
        assert (
            client.post(
                story.path + "/sections",
                headers={**story.headers, "Content-Type": "application/json"},
                content=b'{"name":"A","name":"B","description":"Yes"}',
            ).status_code
            == 422
        )
        assert (
            client.post(
                story.path + "/sections", headers=story.headers, content=b"not-json"
            ).status_code
            == 415
        )
        assert (
            client.post(
                story.path + "/sections",
                headers=story.headers,
                json={"name": "Long", "description": "Large guide", "readme": "x" * 5000},
            ).status_code
            == 201
        )
        assert (
            client.post(
                story.path + "/sections",
                headers={**story.headers, "Content-Type": "application/json"},
                content=b"x" * (MAX_STRUCTURE_BODY_BYTES + 1),
            ).status_code
            == 413
        )


def test_write_rechecks_grant_after_receiving_body(
    story: StructureStory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Request.stream

    async def revoke_after_body(self: Request) -> AsyncIterator[bytes]:
        async for chunk in original(self):
            yield chunk
        with immediate_transaction(story.engine) as connection:
            connection.execute(delete(CredentialLibraryGrant))

    monkeypatch.setattr(Request, "stream", revoke_after_body)
    with TestClient(story.app) as client:
        assert (
            client.post(
                story.path + "/sections",
                headers=story.headers,
                json={"name": "No", "description": "Revoked before write"},
            ).status_code
            == 403
        )
    with story.engine.connect() as connection:
        assert connection.execute(select(Section.id)).all() == []


def test_concurrent_identical_creation_returns_one_identity(story: StructureStory) -> None:
    def create() -> tuple[int, str]:
        with TestClient(story.app) as client:
            response = client.post(
                story.path + "/sections",
                headers=story.headers,
                json={"name": "Concurrent", "description": "One shared identity"},
            )
            return response.status_code, response.json()["id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(), range(2)))
    assert sorted(status for status, _ in results) == [200, 201]
    assert len({identifier for _, identifier in results}) == 1


def test_library_pagination_evaluates_exclusion_scope_once(story: StructureStory) -> None:
    with immediate_transaction(story.engine) as connection:
        connection.execute(
            update(CredentialLibraryPolicy).values(write_mode="exclude", scope_revision=1)
        )
    with TestClient(story.app) as client:
        first = client.get("/api/v1/libraries?limit=1", headers=story.headers)
        assert len(first.json()["items"]) == 1 and first.json()["next_offset"] == 1
        assert first.json()["items"][0]["id"] != story.library
        second = client.get("/api/v1/libraries?limit=1&offset=1", headers=story.headers)
        assert len(second.json()["items"]) == 1 and second.json()["next_offset"] is None
        for query in ("limit=101", "offset=-1", "limit=1&limit=2", "unknown=1"):
            assert (
                client.get("/api/v1/libraries?" + query, headers=story.headers).status_code == 422
            )
