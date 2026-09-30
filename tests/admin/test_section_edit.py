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
from patchouli_lib.auth.models import AdminStructureAuditEvent, Caller, MasterAuditEvent
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.models import Book, Section

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


def _csrf(client: TestClient, path: str = "/admin/libraries") -> str:
    response = client.get(path)
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


def _section(client: TestClient, csrf: str, *, name: str = "First") -> tuple[str, int]:
    library = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Library"})
    assert library.status_code == 303
    section = _post(
        client,
        library.headers["location"] + "/sections",
        {"csrf_token": csrf, "name": name, "description": "Original"},
    )
    assert section.status_code == 303
    path = section.headers["location"]
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
                ).where(MasterAuditEvent.action == "section.update")
            ).all()
        )


def test_master_section_edit_updates_metadata_only_with_one_redacted_audit(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _section(client, csrf)
    section_id = path.rsplit("/", 1)[-1]
    with engine.connect() as connection:
        original = connection.execute(select(Section.__table__)).mappings().one()
    name = '<img src=x onerror="synthetic()">'
    description = "<script>synthetic()</script>"
    result = _post(client, path, _edit(csrf, expected, name=name, description=description))
    assert result.status_code == 303
    assert result.headers["location"] == path
    detail = client.get(path)
    assert name not in detail.text and description not in detail.text
    assert "&lt;img src=x onerror=&quot;synthetic()&quot;&gt;" in detail.text
    assert "&lt;script&gt;synthetic()&lt;/script&gt;" in detail.text
    with engine.connect() as connection:
        updated = connection.execute(select(Section.__table__)).mappings().one()
        assert connection.scalar(select(func.count()).select_from(AdminStructureAuditEvent)) == 0
    assert updated["id"] == original["id"] == section_id
    assert updated["library_id"] == original["library_id"]
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_at"] > expected
    assert (updated["name"], updated["description"]) == (name, description)
    events = _update_events(engine)
    assert len(events) == 1
    assert events[0].target_type == "section" and events[0].target_id == section_id
    assert events[0].identity_id == "a" * 32
    assert len(events[0].session_fingerprint) == 32
    assert name not in repr(events) and description not in repr(events)


def test_section_edit_noop_stale_duplicate_and_wrong_scope(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _section(client, csrf)
    assert (
        _post(client, path, _edit(csrf, expected, name="First", description="Original")).status_code
        == 303
    )
    assert _update_events(engine) == []
    assert (
        _post(client, path, _edit(csrf, expected, name="Renamed", description="New")).status_code
        == 303
    )
    assert (
        _post(client, path, _edit(csrf, expected, name="Renamed", description="New")).status_code
        == 409
    )
    library_path = path.rsplit("/sections/", 1)[0]
    assert (
        _post(
            client,
            library_path + "/sections",
            {"csrf_token": csrf, "name": "Second"},
        ).status_code
        == 303
    )
    fresh = client.get(path)
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', fresh.text)
    assert match is not None
    current = int(match.group(1))
    assert (
        _post(client, path, _edit(csrf, current, name="Second", description="New")).status_code
        == 409
    )
    library_id = path.split("/libraries/", 1)[1].split("/", 1)[0]
    wrong_library_path = path.replace(f"/libraries/{library_id}/", f"/libraries/{'f' * 32}/")
    assert (
        _post(
            client, wrong_library_path, _edit(csrf, current, name="Wrong", description="New")
        ).status_code
        == 404
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(select(Section.name).where(Section.id == path.rsplit("/", 1)[-1]))
            == "Renamed"
        )
    assert len(_update_events(engine)) == 1


def test_section_edit_requires_master_origin_csrf_and_current_generation(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _section(client, csrf)
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
        assert connection.scalar(select(Section.name)) == "First"
    assert _update_events(engine) == []


def test_section_edit_audit_failure_rolls_back_and_legacy_session_cannot_edit(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    assert _post(client, "/admin/login", {"password": _PASSWORD}).status_code == 303
    legacy_csrf = _csrf(client)
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    assert client.get("/admin/libraries").status_code == 303
    assert (
        _post(
            client,
            "/admin/libraries/" + "f" * 32 + "/sections/" + "e" * 32,
            _edit(legacy_csrf, 1, name="No", description="No"),
        ).status_code
        == 401
    )
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    csrf = _csrf(client)
    path, expected = _section(client, csrf)

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    assert (
        _post(
            client, path, _edit(csrf, expected, name="Rollback", description="Rollback")
        ).status_code
        == 500
    )
    with engine.connect() as connection:
        assert connection.scalar(select(Section.name)) == "First"
    assert _update_events(engine) == []


def test_legacy_session_cannot_edit_section_before_master_setup(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    assert _post(client, "/admin/login", {"password": _PASSWORD}).status_code == 303
    csrf = _csrf(client)
    library = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Library"})
    section = _post(
        client, library.headers["location"] + "/sections", {"csrf_token": csrf, "name": "First"}
    )
    path = section.headers["location"]
    assert 'name="expected_updated_at"' not in client.get(path).text
    with engine.connect() as connection:
        expected = connection.scalar(select(Section.updated_at))
    assert expected is not None
    assert (
        _post(client, path, _edit(csrf, expected, name="Denied", description="Denied")).status_code
        == 403
    )
    assert _update_events(engine) == []


def test_section_edit_accepts_long_unicode_description_without_raising_other_form_limits(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _section(client, csrf)
    chinese_description = "中" * 2_000
    fields = _edit(csrf, expected, name="First", description=chinese_description)
    assert len(urlencode(fields).encode("ascii")) > 16_384
    assert _post(client, path, fields).status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(Section.description)) == chinese_description
        updated_at = connection.scalar(select(Section.updated_at))
    assert updated_at is not None
    largest_description = "𠮷" * 4_000
    fields = _edit(csrf, updated_at, name="First", description=largest_description)
    assert len(urlencode(fields).encode("ascii")) < 65_536
    assert _post(client, path, fields).status_code == 303
    with engine.connect() as connection:
        assert connection.scalar(select(Section.description)) == largest_description
        updated_at = connection.scalar(select(Section.updated_at))
    assert updated_at is not None
    fields = _edit(csrf, updated_at, name="First", description="𠮷" * 5_500)
    assert len(urlencode(fields).encode("ascii")) > 65_536
    assert _post(client, path, fields).status_code == 413
    with engine.connect() as connection:
        assert connection.scalar(select(Section.description)) == largest_description
    assert len(_update_events(engine)) == 2
    assert (
        _post(
            client,
            "/admin/libraries",
            {"csrf_token": csrf, "name": "Other", "description": chinese_description},
        ).status_code
        == 413
    )


def test_section_rename_preserves_book_page_revision_ownership(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _section(client, csrf)
    book = _post(client, path + "/books", {"csrf_token": csrf, "name": "Book"})
    assert book.status_code == 303
    with engine.connect() as connection:
        book_id = connection.scalar(select(Book.id))
        section_id = connection.scalar(select(Section.id))
        library_id = connection.scalar(select(Section.library_id))
    assert book_id is not None and section_id is not None and library_id is not None
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, "Synthetic archive")
    page_uid = b"1" * 16
    revision_id = "rev_" + "2" * 32
    markdown = b"# Synthetic archive\n"
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
                **MarkdownContent.from_bytes(markdown).model_dump(),
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
    assert (
        _post(client, path, _edit(csrf, expected, name="Renamed", description="New")).status_code
        == 303
    )
    with engine.connect() as connection:
        after_page = connection.execute(select(Page.__table__)).mappings().one()
        after_revision = connection.execute(select(Revision.__table__)).mappings().one()
        assert connection.scalar(select(Book.section_id)) == section_id
    assert after_page == before_page
    assert after_revision == before_revision
