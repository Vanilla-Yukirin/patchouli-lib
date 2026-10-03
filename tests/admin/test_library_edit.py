from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from starlette.concurrency import run_in_threadpool as starlette_run_in_threadpool

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import (
    AdminStructureAuditEvent,
    Caller,
    CredentialLibraryGrant,
    MasterAuditEvent,
)
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.models import Book, Library, Section

_ORIGIN = "https://admin.example.invalid"
_MASTER_TOKEN = "synthetic master token material 0001"
_PASSWORD = "synthetic admin password"


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": f"sqlite:///{(tmp_path / 'admin.db').as_posix()}",
                "admin_password_hash": hash_password(
                    _PASSWORD, salt_factory=lambda size: b"s" * size, iterations=300_000
                ),
                "admin_session_signing_secret": "s" * 32,
            }
        )
    )
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _post(client: TestClient, path: str, data: dict[str, str], *, origin: str = _ORIGIN) -> Any:
    return client.post(path, data=data, headers={"Origin": origin})


def _csrf(client: TestClient) -> str:
    response = client.get("/admin/libraries")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _setup_master(client: TestClient, engine: Engine) -> str:
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    return _csrf(client)


def _library(client: TestClient, csrf: str) -> tuple[str, int]:
    created = _post(
        client,
        "/admin/libraries",
        {"csrf_token": csrf, "name": "First", "description": "Original"},
    )
    assert created.status_code == 303
    path = created.headers["location"]
    detail = client.get(path)
    assert detail.status_code == 200
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', detail.text)
    assert match is not None
    return path, int(match.group(1))


def _edit(csrf: str, expected: int, *, name: str, description: str) -> dict[str, str]:
    return {
        "csrf_token": csrf,
        "expected_updated_at": str(expected),
        "name": name,
        "description": description,
    }


def _update_events(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(
                    MasterAuditEvent.action,
                    MasterAuditEvent.target_type,
                    MasterAuditEvent.target_id,
                    MasterAuditEvent.identity_id,
                    MasterAuditEvent.session_fingerprint,
                ).where(MasterAuditEvent.action == "library.update")
            ).all()
        )


def test_master_library_edit_updates_only_metadata_and_redacted_audit(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _library(client, csrf)
    library_id = path.rsplit("/", 1)[-1]
    section = _post(client, path + "/sections", {"csrf_token": csrf, "name": "Section"})
    assert section.status_code == 303
    book = _post(
        client, section.headers["location"] + "/books", {"csrf_token": csrf, "name": "Book"}
    )
    assert book.status_code == 303
    with engine.connect() as connection:
        original = connection.execute(select(Library.__table__)).mappings().one()
        before_section = connection.execute(select(Section.__table__)).mappings().one()
        before_book = connection.execute(select(Book.__table__)).mappings().one()

    name = '<img src=x onerror="synthetic()">'
    description = "<script>synthetic()</script>"
    result = _post(client, path, _edit(csrf, expected, name=name, description=description))
    assert result.status_code == 303 and result.headers["location"] == path
    detail = client.get(path)
    grid = client.get("/admin/libraries")
    assert detail.status_code == 200 and grid.status_code == 200
    assert name not in detail.text and description not in detail.text
    assert name not in grid.text and description not in grid.text
    assert "&lt;img src=x onerror=&quot;synthetic()&quot;&gt;" in detail.text
    assert "&lt;script&gt;synthetic()&lt;/script&gt;" in grid.text
    with engine.connect() as connection:
        updated = connection.execute(select(Library.__table__)).mappings().one()
        assert connection.execute(select(Section.__table__)).mappings().one() == before_section
        assert connection.execute(select(Book.__table__)).mappings().one() == before_book
        assert connection.scalar(select(func.count()).select_from(AdminStructureAuditEvent)) == 0
    assert updated["id"] == original["id"] == library_id
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_at"] > expected
    assert (updated["name"], updated["description"]) == (name, description)
    events = _update_events(engine)
    assert len(events) == 1
    assert events[0].target_type == "library" and events[0].target_id == library_id
    assert events[0].identity_id == "a" * 32
    assert len(events[0].session_fingerprint) == 32
    assert name not in repr(events) and description not in repr(events)


def test_library_edit_noop_stale_global_duplicate_and_wrong_id(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _library(client, csrf)
    unchanged = _post(client, path, _edit(csrf, expected, name="First", description="Original"))
    assert unchanged.status_code == 303 and _update_events(engine) == []
    assert (
        _post(client, path, _edit(csrf, expected, name="Renamed", description="New")).status_code
        == 303
    )
    assert (
        _post(client, path, _edit(csrf, expected, name="Renamed", description="New")).status_code
        == 409
    )
    second = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Second"})
    assert second.status_code == 303
    fresh = client.get(path)
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', fresh.text)
    assert match is not None
    current = int(match.group(1))
    assert (
        _post(client, path, _edit(csrf, current, name="Second", description="New")).status_code
        == 409
    )
    assert (
        _post(
            client,
            "/admin/libraries/" + "f" * 32,
            _edit(csrf, current, name="Wrong", description="New"),
        ).status_code
        == 404
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(select(Library.name).where(Library.id == path.rsplit("/", 1)[-1]))
            == "Renamed"
        )
    assert len(_update_events(engine)) == 1


def test_library_edit_requires_master_origin_csrf_and_current_generation(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _library(client, csrf)
    fields = _edit(csrf, expected, name="Blocked", description="Blocked")
    assert _post(client, path, fields, origin="https://other.example.invalid").status_code == 403
    assert _post(client, path, {**fields, "csrf_token": "bad"}).status_code == 403
    assert _post(client, path, {**fields, "unknown": "x"}).status_code == 422
    assert _post(client, path, {**fields, "name": " "}).status_code == 422
    assert _post(client, path, {**fields, "description": "x" * 4_001}).status_code == 422
    assert _update_events(engine) == []

    async def rotate_before_action(action: Any, *args: Any, **kwargs: Any) -> Any:
        with immediate_transaction(engine) as connection:
            assert (
                MasterTokenRepository(connection).rotate(
                    _MASTER_TOKEN, "synthetic rotated master token material 0002", now=1_001
                )
                is not None
            )
        return await starlette_run_in_threadpool(action, *args, **kwargs)

    monkeypatch.setattr(admin_router, "run_in_threadpool", rotate_before_action)
    assert _post(client, path, fields).status_code == 401
    with engine.connect() as connection:
        assert connection.scalar(select(Library.name)) == "First"
    assert _update_events(engine) == []


def test_library_edit_audit_failure_rolls_back_and_legacy_session_is_denied(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    assert _post(client, "/admin/login", {"password": _PASSWORD}).status_code == 303
    legacy_csrf = _csrf(client)
    legacy_library = _post(
        client, "/admin/libraries", {"csrf_token": legacy_csrf, "name": "Legacy"}
    )
    assert legacy_library.status_code == 303
    legacy_path = legacy_library.headers["location"]
    assert 'name="expected_updated_at"' not in client.get(legacy_path).text
    with engine.connect() as connection:
        expected = connection.scalar(select(Library.updated_at))
    assert expected is not None
    assert (
        _post(
            client, legacy_path, _edit(legacy_csrf, expected, name="Denied", description="")
        ).status_code
        == 403
    )
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    assert (
        _post(
            client, legacy_path, _edit(legacy_csrf, expected, name="Denied", description="")
        ).status_code
        == 401
    )
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    csrf = _csrf(client)

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    assert (
        _post(
            client, legacy_path, _edit(csrf, expected, name="Rollback", description="Rollback")
        ).status_code
        == 500
    )
    with engine.connect() as connection:
        assert connection.scalar(select(Library.name)) == "Legacy"
    assert _update_events(engine) == []


def test_library_edit_accepts_long_unicode_without_raising_create_limit(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _library(client, csrf)
    chinese_description = "中" * 2_000
    fields = _edit(csrf, expected, name="First", description=chinese_description)
    assert len(urlencode(fields).encode("ascii")) > 16_384
    assert _post(client, path, fields).status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(Library.description)) == chinese_description
        updated_at = connection.scalar(select(Library.updated_at))
    assert updated_at is not None
    largest = "𠮷" * 4_000
    fields = _edit(csrf, updated_at, name="First", description=largest)
    assert len(urlencode(fields).encode("ascii")) < 65_536
    assert _post(client, path, fields).status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(Library.description)) == largest
        updated_at = connection.scalar(select(Library.updated_at))
    assert updated_at is not None
    assert (
        _post(
            client, path, _edit(csrf, updated_at, name="First", description="𠮷" * 5_500)
        ).status_code
        == 413
    )
    assert (
        _post(
            client,
            "/admin/libraries",
            {"csrf_token": csrf, "name": "Other", "description": chinese_description},
        ).status_code
        == 413
    )
    assert len(_update_events(engine)) == 2


def test_library_rename_preserves_page_revision_and_authorization_scope(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _library(client, csrf)
    section = _post(client, path + "/sections", {"csrf_token": csrf, "name": "Section"})
    book = _post(
        client, section.headers["location"] + "/books", {"csrf_token": csrf, "name": "Book"}
    )
    assert book.status_code == 303
    with engine.connect() as connection:
        library_id = connection.scalar(select(Library.id))
        section_id = connection.scalar(select(Section.id))
        book_id = connection.scalar(select(Book.id))
    assert library_id is not None and section_id is not None and book_id is not None
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, "Synthetic archive")
    page_uid = b"1" * 16
    revision_id = "rev_" + "2" * 32
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        repository.add_page(
            NewPage(
                library_id=library_id,
                page_uid=page_uid,
                section_id=section_id,
                book_id=book_id,
                page_id=identifier.value,
                id_scheme=PAGE_ID_SCHEME,
                id_timestamp_micros=(occurrence.utc_microseconds // 1_000) * 1_000,
                base_slug=identifier.base_slug,
                collision_ordinal=identifier.collision_ordinal,
                title="Synthetic archive",
                page_type="archive",
                occurred_at=occurrence.utc_microseconds,
                current_revision_id=revision_id,
                current_revision_number=1,
                created_at=2_000_000,
                updated_at=2_000_000,
            )
        )
        repository.add_revision(
            NewRevision(
                library_id=library_id,
                revision_id=revision_id,
                page_uid=page_uid,
                revision_number=1,
                created_at=2_000_000,
                **MarkdownContent.from_bytes(b"# Synthetic archive\n").model_dump(),
            )
        )
        repository.add_identifier(
            NewPageIdentifier(
                library_id=library_id,
                identifier_digest=page_id_registry_digest(identifier.value),
                identifier_text=identifier.value,
                id_scheme=PAGE_ID_SCHEME,
                identifier_kind="canonical",
                page_uid=page_uid,
                created_at=2_000_000,
            )
        )
    with engine.connect() as connection:
        before_page = connection.execute(select(Page.__table__)).mappings().one()
        before_revision = connection.execute(select(Revision.__table__)).mappings().one()
    issued = client.post(
        "/admin/agents/create",
        data={
            "csrf_token": csrf,
            "home_library_id": library_id,
            "agent_name": "Synthetic Agent",
            "agent_description": "Synthetic device",
            "credential_ttl_seconds": "3600",
            "grants": [f"{library_id}:read"],
        },
        headers={"Origin": _ORIGIN},
    )
    assert issued.status_code == 200
    with engine.connect() as connection:
        before_grants = (
            connection.execute(select(CredentialLibraryGrant.__table__)).mappings().all()
        )
    assert len(before_grants) == 1
    assert (
        _post(client, path, _edit(csrf, expected, name="Renamed", description="New")).status_code
        == 303
    )
    with engine.connect() as connection:
        assert connection.execute(select(Page.__table__)).mappings().one() == before_page
        assert connection.execute(select(Revision.__table__)).mappings().one() == before_revision
        assert (
            connection.execute(select(CredentialLibraryGrant.__table__)).mappings().all()
            == before_grants
        )
        assert connection.scalar(select(Section.library_id)) == library_id
        assert connection.scalar(select(Book.library_id)) == library_id
    assert client.get(book.headers["location"] + "/pages/" + identifier.value).status_code == 200
