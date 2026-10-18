"""Synthetic end-to-end boundaries for master Page declared-time correction."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path
from re import search
from time import time

import pytest
from alembic import command as alembic
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.file_set_service import MasterFileSetService
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.pages import dashboard_page
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSessionCodec, MasterAdminSession
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AuditEvent, Caller, Credential, MasterAuditEvent
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.config import Settings
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.models import PageOccurrenceCorrection, PageSource, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput, PageRecord
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.identifiers import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.search import index_v2
from patchouli_lib.tags.models import Tag

_ORIGIN = "https://admin.example.invalid"
_MASTER = "synthetic occurrence master token material"
_LEGACY = "synthetic legacy password"
_SIGNING = "s" * 32
_OLD_TIME = "2026-08-13T10:00:00.123456Z"
_NEW_TIME = "2026-08-14T10:00:00.123456Z"
_CLOCK = parse_occurrence_time("2026-08-15T10:00:00.000000Z").utc_microseconds
_FILES = (
    (("content.md", b"# synthetic needle\n"),),
    (("content.md", b"# synthetic needle\n"), ("payload.bin", b"\x00\xff\x81")),
    (("payload.bin", b"\x00\xff\x81"),),
)


@dataclass(frozen=True, slots=True)
class Browser:
    client: TestClient
    engine: Engine
    library_id: str
    section_id: str
    book_id: str
    csrf: str
    session: MasterAdminSession

    def page_path(self, page_id: str) -> str:
        return (
            f"/admin/libraries/{self.library_id}/sections/{self.section_id}"
            f"/books/{self.book_id}/pages/{page_id}"
        )


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Browser]:
    database_url = f"sqlite:///{(tmp_path / 'master-occurrence.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    alembic.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "admin_password_hash": hash_password(
                _LEGACY, salt_factory=lambda size: b"l" * size, iterations=300_000
            ),
            "admin_session_signing_secret": _SIGNING,
        }
    )
    app = create_app(settings)
    engine: Engine = app.state.engine
    with immediate_transaction(engine) as connection:
        ids = iter(("1" * 32, "2" * 32, "3" * 32))
        structure = LibrarySeedService(
            LibraryRepository(connection), id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Library",
                section_name="Synthetic Section",
                book_name="Synthetic Book",
            )
        )
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER, now=1_000_000)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        assert (
            client.post(
                "/admin/login", data={"password": _MASTER}, headers={"Origin": _ORIGIN}
            ).status_code
            == 303
        )
        form = client.get("/admin")
        match = search(r'name="csrf_token" value="([^"]+)"', form.text)
        assert match is not None
        cookie = client.cookies.get("patchouli_admin_session")
        assert cookie is not None
        session = AdminSessionCodec(_SIGNING.encode(), ttl_seconds=600).verify_master(cookie)
        assert session is not None
        yield Browser(
            client,
            engine,
            structure.library.id,
            structure.section.id,
            structure.book.id,
            unescape(match.group(1)),
            session,
        )


def _key(value: str) -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(value))


def _create(
    browser: Browser, files: tuple[tuple[str, bytes], ...], *, title: str = "Synthetic needle"
) -> tuple[FileSetCreateCommand, str, str]:
    command = FileSetCreateCommand(
        library_id=browser.library_id,
        section_id=browser.section_id,
        book_id=browser.book_id,
        title=title,
        occurred_at=parse_occurrence_time(_OLD_TIME).utc_microseconds,
        files=files,
        source=ArchiveSourceInput(kind="synthetic"),
        request_id="req_" + "1" * 32,
    )
    receipt = (
        MasterFileSetService(browser.engine, clock=lambda: _CLOCK)
        .create_page(command, _key("create"), master_session=browser.session)
        .receipt
    )
    return command, receipt.page_id, receipt.response_etag


def _post(
    browser: Browser,
    page_id: str,
    etag: str,
    *,
    occurred_at: str | None = _NEW_TIME,
    origin: str | None = _ORIGIN,
    csrf: str | None = None,
    path: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    data = {"csrf_token": browser.csrf if csrf is None else csrf, "expected_etag": etag}
    if occurred_at is not None:
        data["occurred_at"] = occurred_at
    request_headers = {} if headers is None else dict(headers)
    if origin is not None:
        request_headers["Origin"] = origin
    return browser.client.post(
        path or browser.page_path(page_id) + "/occurrence",
        data=data,
        headers=request_headers,
    )


def _state(browser: Browser, page_id: str) -> tuple[PageRecord, str, tuple[tuple[str, bytes], ...]]:
    with browser.engine.connect() as connection:
        page = ContentRepository(connection).get_page(browser.library_id, page_id)
        assert page is not None
        manifest = ContentRepository(connection).get_current_file_manifest(page)
        etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        return page, etag, tuple((item.name, item.content) for item in manifest.files)


def _counts(browser: Browser) -> tuple[int, int, int, int]:
    with browser.engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (Revision, PageSource, MasterAuditEvent, PageOccurrenceCorrection)
        )  # type: ignore[return-value]


@pytest.mark.parametrize("files", _FILES)
def test_http_correction_preserves_every_file_and_records_immutable_master_audit(
    browser: Browser, files: tuple[tuple[str, bytes], ...]
) -> None:
    _command, page_id, old_etag = _create(browser, files)
    old, _, original_files = _state(browser, page_id)
    assert original_files == tuple(sorted(files))
    form = browser.client.get(browser.page_path(page_id))
    assert form.status_code == 200
    assert f'action="{browser.page_path(page_id)}/occurrence"' in form.text
    assert f'name="expected_etag" value="{escape(old_etag, quote=True)}"' in form.text
    response = _post(browser, page_id, old_etag)
    assert response.status_code == 200, response.text
    assert "Declared time saved." in response.text
    current, new_etag, current_files = _state(browser, page_id)
    assert new_etag != old_etag
    assert current_files == original_files
    assert current.page_id == old.page_id and current.page_uid == old.page_uid
    assert current.current_revision_id == old.current_revision_id
    assert current.current_revision_number == old.current_revision_number == 1
    assert current.occurred_at == parse_occurrence_time(_NEW_TIME).utc_microseconds
    assert current.updated_at > old.updated_at
    assert _counts(browser) == (1, 1, 2, 1)
    with browser.engine.connect() as connection:
        correction = connection.execute(select(PageOccurrenceCorrection.__table__)).mappings().one()
        assert correction["actor_caller_id"] is None
        assert correction["actor_home_library_id"] is None
        assert correction["master_audit_event_id"] is not None
        assert correction["old_occurred_at"] == old.occurred_at
        assert correction["new_occurred_at"] == current.occurred_at
        assert correction["at_revision_number"] == 1
        audit = (
            connection.execute(
                select(MasterAuditEvent.__table__).where(
                    MasterAuditEvent.id == correction["master_audit_event_id"]
                )
            )
            .mappings()
            .one()
        )
        assert audit["action"] == "content.page.occurrence.correct"
        assert audit["target_id"] == f"{browser.library_id}:{old.page_uid.hex()}"
        assert audit["occurred_at"] == correction["corrected_at"] == current.updated_at
    with pytest.raises(IntegrityError), immediate_transaction(browser.engine) as connection:
        connection.exec_driver_sql(
            "UPDATE page_occurrence_corrections SET corrected_at = corrected_at + 1 "
            "WHERE library_id = ? AND page_uid = ?",
            (browser.library_id, old.page_uid),
        )
    for name, content in files:
        download = browser.client.get(browser.page_path(page_id) + f"/revisions/1/files/{name}")
        assert download.status_code == 200 and download.content == content


def test_invalid_time_path_deleted_and_old_etag_do_not_write(browser: Browser) -> None:
    _command, page_id, etag = _create(browser, _FILES[0])
    before = _counts(browser)
    assert _post(browser, page_id, etag, occurred_at=None).status_code == 422
    assert _post(browser, page_id, etag, occurred_at="2026-08-14T10:00:00").status_code == 422
    wrong = browser.page_path(page_id).replace(f"/books/{browser.book_id}", f"/books/{'f' * 32}")
    assert _post(browser, page_id, etag, path=wrong + "/occurrence").status_code == 404
    assert _counts(browser) == before
    assert _post(browser, page_id, etag).status_code == 200
    changed = _counts(browser)
    assert _post(browser, page_id, etag, occurred_at=_OLD_TIME).status_code == 412
    assert _counts(browser) == changed
    current, current_etag, _ = _state(browser, page_id)
    assert _post(browser, page_id, current_etag).status_code == 200
    assert _counts(browser) == changed
    AdminActionService(browser.engine).delete_page_as_master(
        browser.library_id,
        browser.section_id,
        browser.book_id,
        page_id,
        MasterDeletePageFormInput(expected_etag=current_etag, confirm_delete="yes"),
        master_session=browser.session,
    )
    assert current.deleted_at is None
    assert _post(browser, page_id, current_etag, occurred_at=_OLD_TIME).status_code == 404
    assert _counts(browser) == (1, 1, 3, 1)


def test_origin_csrf_legacy_and_agent_bearer_cannot_correct(browser: Browser) -> None:
    _command, page_id, etag = _create(browser, _FILES[0])
    assert _post(browser, page_id, etag, origin=None).status_code == 403
    assert _post(browser, page_id, etag, origin="https://other.invalid").status_code == 403
    assert _post(browser, page_id, etag, csrf="wrong").status_code == 403
    before = _counts(browser)
    browser.client.cookies.clear()
    assert (
        _post(browser, page_id, etag, headers={"Authorization": "Bearer synthetic"}).status_code
        == 401
    )
    legacy_cookie, legacy_session = AdminSessionCodec(_SIGNING.encode(), ttl_seconds=600).issue()
    browser.client.cookies.set(
        "patchouli_admin_session", legacy_cookie, domain="admin.example.invalid", path="/admin"
    )
    assert _post(browser, page_id, etag, csrf=legacy_session.csrf_token).status_code == 401
    assert _counts(browser) == before


def test_rotated_and_expired_master_sessions_fail_inside_write_transaction(
    browser: Browser,
) -> None:
    _command, page_id, etag = _create(browser, _FILES[0])
    with immediate_transaction(browser.engine) as connection:
        rotated = MasterTokenRepository(connection).rotate(
            _MASTER, _MASTER + " rotated", now=_CLOCK
        )
        assert rotated is not None
    assert _post(browser, page_id, etag).status_code == 401
    service = AdminActionService(browser.engine, clock=lambda: _CLOCK + 1)
    from patchouli_lib.admin.contracts import MasterCorrectOccurrenceInput

    request = MasterCorrectOccurrenceInput(occurred_at=_NEW_TIME, expected_etag=etag)
    with pytest.raises(AuthenticationError):
        service.correct_page_occurrence_as_master(
            browser.library_id,
            browser.section_id,
            browser.book_id,
            page_id,
            request,
            master_session=browser.session,
        )
    expired = MasterAdminSession(
        expires_at=int(time()) - 1,
        csrf_token=browser.csrf,
        identity_id=browser.session.identity_id,
        session_generation=rotated.session_generation,
    )
    with pytest.raises(AuthenticationError):
        service.correct_page_occurrence_as_master(
            browser.library_id,
            browser.section_id,
            browser.book_id,
            page_id,
            request,
            master_session=expired,
        )
    assert _counts(browser) == (1, 1, 1, 0)


def test_audit_and_index_projection_failures_roll_back_with_safe_http_errors(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _command, page_id, etag = _create(browser, _FILES[0])
    index_v2.rebuild_search_index(browser.engine, clock=lambda: _CLOCK + 1)
    original_audit = MasterAuditRepository.add_success

    def broken_audit(self: MasterAuditRepository, **_kwargs: object) -> None:
        raise RuntimeError("synthetic internal audit secret")

    monkeypatch.setattr(MasterAuditRepository, "add_success", broken_audit)
    audit_failure = _post(browser, page_id, etag)
    assert audit_failure.status_code == 500
    assert "synthetic internal audit secret" not in audit_failure.text
    monkeypatch.setattr(MasterAuditRepository, "add_success", original_audit)
    assert _counts(browser) == (1, 1, 1, 0)

    def broken_projection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic internal index secret")

    monkeypatch.setattr(index_v2, "_project_page", broken_projection)
    index_failure = _post(browser, page_id, etag)
    assert index_failure.status_code == 500
    assert "synthetic internal index secret" not in index_failure.text
    assert _counts(browser) == (1, 1, 1, 0)
    page, still_etag, _ = _state(browser, page_id)
    assert still_etag == etag
    assert page.occurred_at == parse_occurrence_time(_OLD_TIME).utc_microseconds


def test_search_moves_to_new_half_open_time_window_and_old_receipt_still_replays(
    browser: Browser,
) -> None:
    command, page_id, etag = _create(browser, _FILES[1])
    service = MasterFileSetService(browser.engine, clock=lambda: _CLOCK + 5)
    original_receipt = service.create_page(
        command, _key("create"), master_session=browser.session
    ).receipt
    index_v2.rebuild_search_index(browser.engine, clock=lambda: _CLOCK + 1)

    def query(*, start: str, end: str) -> Response:
        return browser.client.post(
            "/admin/search",
            data={
                "csrf_token": browser.csrf,
                "keywords": "needle",
                "library_id": browser.library_id,
                "occurred_from": start,
                "occurred_before": end,
            },
            headers={"Origin": _ORIGIN},
        )

    old_window = ("2026-08-13T00:00:00", "2026-08-14T00:00:00")
    new_window = ("2026-08-14T00:00:00", "2026-08-15T00:00:00")
    assert page_id in query(start=old_window[0], end=old_window[1]).text
    assert page_id not in query(start=new_window[0], end=new_window[1]).text
    assert _post(browser, page_id, etag).status_code == 200
    moved_old = query(start=old_window[0], end=old_window[1])
    moved_new = query(start=new_window[0], end=new_window[1])
    assert moved_old.status_code == moved_new.status_code == 200
    assert page_id not in moved_old.text and page_id in moved_new.text
    replay = service.create_page(command, _key("create"), master_session=browser.session)
    assert replay.replayed and replay.receipt == original_receipt
    assert replay.receipt.response_etag == etag
    current, current_etag, _ = _state(browser, page_id)
    assert (
        current_etag != etag
        and current.occurred_at == parse_occurrence_time(_NEW_TIME).utc_microseconds
    )
    assert _counts(browser) == (1, 1, 2, 1)


def test_content_activity_chinese_link_and_agent_personal_filter(browser: Browser) -> None:
    _command, page_id, etag = _create(browser, _FILES[0])
    assert _post(browser, page_id, etag).status_code == 200
    activity = AdminReadModel(browser.engine).recent_content_activity()
    correction = [item for item in activity if item.action == "content.page.occurrence.correct"]
    assert len(correction) == 1
    assert correction[0].actor_id is None and correction[0].actor_name == "Administrator"
    assert correction[0].page_id == page_id and correction[0].revision_number is None
    rendered = dashboard_page(browser.csrf, activities=activity, locale="zh-CN")
    assert "更正了页面的发生时间" in rendered
    assert f'href="{browser.page_path(page_id)}"' in rendered
    assert "content.page.occurrence.correct" not in rendered
    caller_id, credential_id, tag_id = "a" * 32, "b" * 32, "c" * 32
    with immediate_transaction(browser.engine) as connection:
        connection.execute(
            insert(Caller),
            {
                "id": caller_id,
                "library_id": browser.library_id,
                "kind": "agent",
                "name": "Synthetic Agent",
                "description": "Synthetic activity actor",
                "policy_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        connection.execute(
            insert(Credential),
            {
                "id": credential_id,
                "library_id": browser.library_id,
                "caller_id": caller_id,
                "selector": "a" * 22,
                "token_version": 1,
                "verifier": b"v" * 32,
                "expires_at": _CLOCK + 1_000_000,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        connection.execute(
            insert(Tag),
            {
                "library_id": browser.library_id,
                "id": tag_id,
                "display_name": "Synthetic tag",
                "match_key": "synthetic tag",
                "created_at": 1,
            },
        )
        connection.execute(
            insert(AuditEvent),
            {
                "id": "d" * 32,
                "library_id": browser.library_id,
                "actor_home_library_id": browser.library_id,
                "actor_caller_id": caller_id,
                "actor_credential_id": credential_id,
                "action": "tag.create",
                "resource_type": "tag",
                "resource_id": tag_id,
                "outcome": "succeeded",
                "request_id": "synthetic-activity-request",
                "occurred_at": _CLOCK + 1,
            },
        )
    personal = AdminReadModel(browser.engine).content_activity_page(
        actor=(browser.library_id, caller_id)
    )
    assert len(personal.items) == 1 and personal.items[0].action == "tag.create"
    assert personal.items[0].actor_id == caller_id
