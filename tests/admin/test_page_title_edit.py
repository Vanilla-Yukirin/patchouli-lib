"""Master-only Page display-title edits leave content and stable identity intact."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from time import time
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from starlette.concurrency import run_in_threadpool as starlette_run_in_threadpool

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.contracts import MasterUpdatePageTitleInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.config import Settings
from patchouli_lib.content.models import (
    Page,
    PageIdentifier,
    PageTitleEvent,
    Revision,
    RevisionFile,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    MarkdownContent,
    NewPage,
    NewPageIdentifier,
    NewRevision,
    PageRecord,
)
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time

_ORIGIN = "https://admin.example.invalid"
_MASTER_TOKEN = "synthetic master token material for Page title tests"
_PASSWORD = "synthetic admin password"


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Engine]]:
    database_url = f"sqlite:///{(tmp_path / 'page-title.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": database_url,
                "admin_password_hash": hash_password(
                    _PASSWORD, salt_factory=lambda size: b"s" * size, iterations=300_000
                ),
                "admin_session_signing_secret": "s" * 32,
            }
        )
    )
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


def _seed_page(
    client: TestClient,
    engine: Engine,
    csrf: str,
    *,
    library_name: str = "Library",
    title: str = "Original title",
    page_uid: bytes = b"p" * 16,
) -> tuple[str, int]:
    library = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": library_name})
    assert library.status_code == 303
    section = _post(
        client,
        library.headers["location"] + "/sections",
        {"csrf_token": csrf, "name": "Section"},
    )
    assert section.status_code == 303
    book = _post(
        client,
        section.headers["location"] + "/books",
        {"csrf_token": csrf, "name": "Book"},
    )
    assert book.status_code == 303
    library_id = library.headers["location"].rsplit("/", 1)[-1]
    section_id = section.headers["location"].rsplit("/", 1)[-1]
    book_id = book.headers["location"].rsplit("/", 1)[-1]
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, title)
    revision_id = "rev_" + page_uid.hex()
    content = MarkdownContent.from_bytes(b"# Synthetic content\n")
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
                title=title,
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
                **content.model_dump(),
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
    path = book.headers["location"] + f"/pages/{identifier.value}"
    detail = client.get(path)
    assert detail.status_code == 200
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', detail.text)
    return path, 2_000_000 if match is None else int(match.group(1))


def _edit(csrf: str, expected: int, title: str) -> dict[str, str]:
    return {"csrf_token": csrf, "expected_updated_at": str(expected), "title": title}


def _snapshot(engine: Engine, library_id: str, page_id: str) -> tuple[Any, ...]:
    with engine.connect() as connection:
        page = (
            connection.execute(
                select(Page.__table__).where(Page.library_id == library_id, Page.page_id == page_id)
            )
            .mappings()
            .one()
        )
        revision = (
            connection.execute(
                select(Revision.__table__).where(
                    Revision.library_id == library_id, Revision.page_uid == page["page_uid"]
                )
            )
            .mappings()
            .one()
        )
        identifier = (
            connection.execute(
                select(PageIdentifier.__table__).where(
                    PageIdentifier.library_id == library_id,
                    PageIdentifier.page_uid == page["page_uid"],
                )
            )
            .mappings()
            .one()
        )
    return page, revision, identifier


def _files(engine: Engine, library_id: str, page_uid: bytes) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(RevisionFile.__table__).where(
                    RevisionFile.library_id == library_id,
                    RevisionFile.page_uid == page_uid,
                )
            ).mappings()
        )


def _events(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(MasterAuditEvent.__table__).where(
                    MasterAuditEvent.action == "content.page.title.edit"
                )
            ).mappings()
        )


def _title_events(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(select(PageTitleEvent.__table__)).mappings())


def test_master_title_edit_changes_only_metadata_and_audits_without_title(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _seed_page(client, engine, csrf)
    library_id = path.split("/libraries/", 1)[1].split("/", 1)[0]
    page_id = path.rsplit("/", 1)[-1]
    original_page, original_revision, original_identifier = _snapshot(engine, library_id, page_id)
    original_files = _files(engine, library_id, original_page["page_uid"])
    original_etag = page_current_etag(
        original_page["page_uid"],
        original_page["current_revision_id"],
        original_page["current_revision_number"],
        original_page["occurred_at"],
        original_page["updated_at"],
    )
    title = '<img src=x onerror="synthetic()"> 页面'
    result = _post(client, path, _edit(csrf, expected, title))
    assert result.status_code == 303 and result.headers["location"] == path
    detail = client.get(path)
    assert detail.status_code == 200
    assert title not in detail.text
    assert "&lt;img src=x onerror=&quot;synthetic()&quot;&gt; 页面" in detail.text
    page, revision, identifier = _snapshot(engine, library_id, page_id)
    assert page["title"] == title
    assert page["updated_at"] > original_page["updated_at"]
    assert {key: value for key, value in page.items() if key not in {"title", "updated_at"}} == {
        key: value for key, value in original_page.items() if key not in {"title", "updated_at"}
    }
    assert revision == original_revision and identifier == original_identifier
    assert _files(engine, library_id, page["page_uid"]) == original_files
    assert (
        page_current_etag(
            page["page_uid"],
            page["current_revision_id"],
            page["current_revision_number"],
            page["occurred_at"],
            page["updated_at"],
        )
        != original_etag
    )
    events = _events(engine)
    assert len(events) == 1
    assert events[0]["target_type"] == "page"
    assert events[0]["target_id"] == f"{library_id}:{page['page_uid'].hex()}"
    assert events[0]["occurred_at"] == page["updated_at"]
    assert title not in repr(events)
    title_events = _title_events(engine)
    assert len(title_events) == 1
    assert title_events[0]["old_title"] == "Original title"
    assert title_events[0]["new_title"] == title
    assert title_events[0]["old_updated_at"] == expected
    assert title_events[0]["changed_at"] == page["updated_at"]
    assert title_events[0]["at_revision_number"] == 1
    assert title_events[0]["master_audit_event_id"] == events[0]["id"]
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "Changed a page title" in dashboard.text
    assert title not in dashboard.text
    assert f'href="{path}"' in dashboard.text
    assert "修改了页面标题" in client.get("/admin?lang=zh-CN").text


def test_title_activity_uses_target_library_trash_link_and_not_caller_view(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    first_path, _ = _seed_page(client, engine, csrf, library_name="First Library")
    target_path, expected = _seed_page(
        client,
        engine,
        csrf,
        library_name="Second Library",
        page_uid=b"q" * 16,
    )
    title = '<img src=x onerror="synthetic()"> 第二库'
    assert _post(client, target_path, _edit(csrf, expected, title)).status_code == 303
    library_id = target_path.split("/libraries/", 1)[1].split("/", 1)[0]
    section_id = target_path.split("/sections/", 1)[1].split("/", 1)[0]
    page_id = target_path.rsplit("/", 1)[-1]
    activity = AdminReadModel(engine).recent_content_activity()
    title_item = next(item for item in activity if item.action == "content.page.title.edit")
    assert title_item.library_id == library_id
    assert title_item.page_id == page_id
    assert title_item.page_title == title
    assert not title_item.page_deleted
    dashboard = client.get("/admin")
    assert f'href="{target_path}"' in dashboard.text
    assert f'href="{first_path}"' not in dashboard.text
    assert title not in dashboard.text
    assert "&lt;img src=x onerror=&quot;" in dashboard.text

    caller_id = "b" * 32
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_caller(
            NewCaller(
                id=caller_id,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic Agent",
                created_at=1_000,
                updated_at=1_000,
            )
        )
        content = ContentRepository(connection)
        page = content.get_page(library_id, page_id)
        assert page is not None
        content.transition_page_lifecycle(
            page,
            action="delete",
            actor_caller_id=caller_id,
            actor_home_library_id=library_id,
            request_id="req_" + "c" * 32,
            changed_at=page.updated_at + 1,
        )
    trash_path = f"/admin/libraries/{library_id}/sections/{section_id}/trash/{page_id}"
    dashboard = client.get("/admin")
    assert f'Administrator Changed a page title <a href="{trash_path}"' in dashboard.text
    assert title not in dashboard.text
    title_item = next(
        item
        for item in AdminReadModel(engine).recent_content_activity()
        if item.action == "content.page.title.edit"
    )
    assert title_item.page_deleted
    caller_activity = AdminReadModel(engine).recent_content_activity(actor=(library_id, caller_id))
    assert caller_activity == ()
    caller_detail = client.get(f"/admin/libraries/{library_id}/callers/{caller_id}")
    assert caller_detail.status_code == 200
    assert "Changed a page title" not in caller_detail.text


def test_noop_stale_and_wrong_path_do_not_write(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _seed_page(client, engine, csrf)
    assert _post(client, path, _edit(csrf, expected, "Original title")).status_code == 303
    assert _events(engine) == []
    assert _title_events(engine) == []
    assert "Changed a page title" not in client.get("/admin").text
    assert _post(client, path, _edit(csrf, expected, "New title")).status_code == 303
    assert _post(client, path, _edit(csrf, expected, "Stale title")).status_code == 409
    assert len(_events(engine)) == 1
    assert len(_title_events(engine)) == 1
    library_id = path.split("/libraries/", 1)[1].split("/", 1)[0]
    section_id = path.split("/sections/", 1)[1].split("/", 1)[0]
    book_id = path.split("/books/", 1)[1].split("/", 1)[0]
    fresh = client.get(path)
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', fresh.text)
    assert match is not None
    fields = _edit(csrf, int(match.group(1)), "Wrong path")
    for wrong in (
        path.replace(f"/libraries/{library_id}/", f"/libraries/{'f' * 32}/"),
        path.replace(f"/sections/{section_id}/", f"/sections/{'f' * 32}/"),
        path.replace(f"/books/{book_id}/", f"/books/{'f' * 32}/"),
    ):
        response = _post(client, wrong, fields)
        assert response.status_code == 404
        assert "New title" not in response.text
    assert len(_events(engine)) == 1
    assert len(_title_events(engine)) == 1


def test_title_edit_advances_clock_when_wall_clock_has_not_advanced(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _seed_page(client, engine, csrf)
    library_id = path.split("/libraries/", 1)[1].split("/", 1)[0]
    section_id = path.split("/sections/", 1)[1].split("/", 1)[0]
    book_id = path.split("/books/", 1)[1].split("/", 1)[0]
    page_id = path.rsplit("/", 1)[-1]
    session = MasterAdminSession(
        expires_at=int(time()) + 3_600,
        csrf_token=csrf,
        identity_id="a" * 32,
        session_generation=1,
    )
    AdminActionService(engine, clock=lambda: expected).update_page_title_as_master(
        library_id,
        section_id,
        book_id,
        page_id,
        MasterUpdatePageTitleInput(title="New title", expected_updated_at=expected),
        master_session=session,
    )
    page, _, _ = _snapshot(engine, library_id, page_id)
    assert page["updated_at"] == expected + 1
    assert _title_events(engine)[0]["changed_at"] == expected + 1


def test_title_edit_requires_master_origin_csrf_and_current_generation(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _seed_page(client, engine, csrf)
    fields = _edit(csrf, expected, "Denied")
    assert _post(client, path, fields, origin="https://other.example.invalid").status_code == 403
    assert _post(client, path, {**fields, "csrf_token": "bad"}).status_code == 403
    assert _post(client, path, {**fields, "unknown": "x"}).status_code == 422
    assert _post(client, path, {**fields, "title": " "}).status_code == 422
    assert _post(client, path, {**fields, "title": "a\x00b"}).status_code == 422
    assert _events(engine) == []
    assert _title_events(engine) == []

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
    assert _events(engine) == []
    assert _title_events(engine) == []


def test_audit_failure_rolls_back_and_legacy_session_cannot_edit(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    assert _post(client, "/admin/login", {"password": _PASSWORD}).status_code == 303
    legacy_csrf = _csrf(client)
    path, expected = _seed_page(client, engine, legacy_csrf)
    assert 'name="expected_updated_at"' not in client.get(path).text
    assert _post(client, path, _edit(legacy_csrf, expected, "Denied")).status_code == 403
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    assert client.get(path).status_code == 303
    assert _post(client, path, _edit(legacy_csrf, expected, "Denied")).status_code == 401
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    csrf = _csrf(client)

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    assert _post(client, path, _edit(csrf, expected, "Rollback")).status_code == 500
    with engine.connect() as connection:
        assert connection.scalar(select(Page.title)) == "Original title"
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
    assert _events(engine) == []
    assert _title_events(engine) == []
    assert "Changed a page title" not in client.get("/admin").text


def test_conditional_update_failure_rolls_back_preceding_audit(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    path, expected = _seed_page(client, engine, csrf)

    def refuse_update(
        self: ContentRepository, page: PageRecord, *, title: str, updated_at: int
    ) -> PageRecord | None:
        return None

    monkeypatch.setattr(ContentRepository, "update_page_title", refuse_update)
    assert _post(client, path, _edit(csrf, expected, "Not written")).status_code == 409
    with engine.connect() as connection:
        assert connection.scalar(select(Page.title)) == "Original title"
    assert _events(engine) == []
    assert _title_events(engine) == []
