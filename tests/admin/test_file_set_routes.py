"""Synthetic HTTP coverage for master-only complete file-set browser writes."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from re import search
from time import time
from typing import Any

import pytest
from alembic import command as alembic
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, select

from patchouli_lib.admin import file_set_routes
from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSessionCodec, MasterAdminSession
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, PageSource, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

_ORIGIN = "https://admin.example.invalid"
_TOKEN = "synthetic master file set token material"
_SIGNING_SECRET = "s" * 32
_FILES_MIXED = (("content.md", b"# Synthetic content\n"), ("payload.bin", b"\x00\xff\x81"))
_FILES_BINARY = (("payload.bin", b"\x00\xff\x81"),)
_FILES_MARKDOWN = (("content.md", b"# Only Markdown\n"),)
_TIME = "2026-08-13T10:00:00.123456Z"


@dataclass(frozen=True, slots=True)
class Browser:
    client: TestClient
    engine: Engine
    library_id: str
    section_id: str
    book_id: str
    csrf: str

    @property
    def book_path(self) -> str:
        return f"/admin/libraries/{self.library_id}/sections/{self.section_id}/books/{self.book_id}"

    @property
    def create_path(self) -> str:
        return self.book_path + "/pages"

    def page_path(self, page_id: str) -> str:
        return self.book_path + f"/pages/{page_id}"


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Browser]:
    database_url = f"sqlite:///{(tmp_path / 'browser-file-set.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    alembic.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "admin_password_hash": hash_password(
                "synthetic legacy password",
                salt_factory=lambda size: b"b" * size,
                iterations=300_000,
            ),
            "admin_session_signing_secret": _SIGNING_SECRET,
        }
    )
    app = create_app(settings)
    engine: Engine = app.state.engine
    with immediate_transaction(engine) as connection:
        identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
        structure = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Library",
                section_name="Synthetic Section",
                book_name="Synthetic Book",
            )
        )
        MasterTokenRepository(connection).initialize_from_local_cli(_TOKEN, now=1_000_000)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        login = client.post("/admin/login", data={"password": _TOKEN}, headers={"Origin": _ORIGIN})
        assert login.status_code == 303
        page = client.get("/admin")
        assert page.status_code == 200
        match = search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert match is not None
        yield Browser(
            client,
            engine,
            structure.library.id,
            structure.section.id,
            structure.book.id,
            unescape(match.group(1)),
        )


def _multipart(
    metadata: object,
    files: Sequence[tuple[str, bytes]],
    *,
    browser_string: bool = False,
    boundary: str = "synthetic-admin-file-set-boundary",
) -> tuple[str, bytes]:
    metadata_bytes = (
        metadata
        if isinstance(metadata, bytes)
        else json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="metadata"\r\n',
    ]
    if not browser_string:
        chunks.append(b"Content-Type: application/json; charset=utf-8\r\n")
    chunks.extend((b"\r\n", metadata_bytes, b"\r\n"))
    for name, content in files:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _post(
    browser: Browser,
    path: str,
    metadata: object,
    files: Sequence[tuple[str, bytes]],
    *,
    key: str = "a" * 32,
    csrf: str | None = None,
    etag: str | None = None,
    origin: str | None = _ORIGIN,
    browser_string: bool = False,
    extra_headers: Sequence[tuple[str, str]] = (),
) -> Response:
    media, body = _multipart(metadata, files, browser_string=browser_string)
    headers: list[tuple[str, str]] = [
        ("Content-Type", media),
        ("Idempotency-Key", key),
        ("X-CSRF-Token", browser.csrf if csrf is None else csrf),
    ]
    if origin is not None:
        headers.append(("Origin", origin))
    if etag is not None:
        headers.append(("If-Match", etag))
    headers.extend(extra_headers)
    return browser.client.post(path, headers=headers, content=body)


def _created(
    browser: Browser, *, files: Sequence[tuple[str, bytes]] = _FILES_MIXED
) -> dict[str, Any]:
    response = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        files,
    )
    assert response.status_code in (200, 201), response.text
    result: dict[str, Any] = response.json()
    assert result["changed"] is True and result["replayed"] is False
    return result


def _counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (Page, Revision, PageSource, MasterAuditEvent)
        )


def _current_etag(engine: Engine, library_id: str, page_id: str) -> str:
    with engine.connect() as connection:
        page = ContentRepository(connection).get_page(library_id, page_id)
        assert page is not None
        return page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )


def _master_session(browser: Browser) -> MasterAdminSession:
    cookie = browser.client.cookies.get("patchouli_admin_session")
    assert cookie is not None
    session = AdminSessionCodec(_SIGNING_SECRET.encode(), ttl_seconds=600).verify_master(cookie)
    assert session is not None
    return session


def _json_error(response: Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["content-type"].startswith("application/json")
    assert set(response.json()) == {"message"}
    assert _TOKEN not in response.text


def test_upload_forms_are_master_only_noscript_and_same_origin_csp(browser: Browser) -> None:
    book = browser.client.get(browser.book_path)
    assert book.status_code == 200
    assert f'href="{browser.book_path}/new-page"' in book.text
    creation = browser.client.get(browser.book_path + "/new-page")
    assert creation.status_code == 200
    assert 'id="file-set-upload"' in creation.text
    assert 'data-operation="create"' in creation.text
    assert "<noscript>" in creation.text
    assert 'id="upload-submit" type="submit" disabled' in creation.text
    assert "/admin/file-set-upload.js" in creation.text
    csp = creation.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp and "https:" not in csp
    assert creation.headers["cache-control"] == "no-store, max-age=0"
    script = browser.client.get("/admin/file-set-upload.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("application/javascript")
    assert "sessionStorage" in script.text and "FormData" in script.text

    created = _created(browser)
    page_path = browser.page_path(created["page_id"])
    detail = browser.client.get(page_path)
    assert detail.status_code == 200
    assert f'href="{page_path}/files/edit"' in detail.text
    revision = browser.client.get(page_path + "/files/edit")
    assert revision.status_code == 200
    assert 'data-operation="revise"' in revision.text
    assert "replace the whole set" in revision.text
    assert 'name="expected_etag"' in revision.text
    assert "payload.bin" in revision.text

    browser.client.cookies.clear()
    anonymous = browser.client.get(browser.book_path + "/new-page")
    assert anonymous.status_code == 401
    assert "file-set-upload" not in anonymous.text


@pytest.mark.parametrize("files", (_FILES_MIXED, _FILES_MARKDOWN, _FILES_BINARY))
def test_create_flat_file_variants_round_trip_exact_bytes(
    browser: Browser, files: tuple[tuple[str, bytes], ...]
) -> None:
    response = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        files,
        browser_string=True,
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert set(data) == {
        "page_id",
        "revision_id",
        "revision_number",
        "changed",
        "replayed",
        "etag",
        "snapshot_sha256",
        "files",
        "revision_url",
        "warnings",
    }
    assert data["revision_number"] == 1 and data["changed"] and not data["replayed"]
    assert data["warnings"] == []
    assert response.headers["etag"] == data["etag"]
    assert len(data["snapshot_sha256"]) == 64
    assert [(item["name"], item["size_bytes"]) for item in data["files"]] == [
        (name, len(content)) for name, content in sorted(files)
    ]
    assert data["revision_url"] == browser.page_path(data["page_id"]) + "/revisions/1"
    assert browser.client.get(data["revision_url"]).status_code == 200
    for name, content in files:
        download = browser.client.get(data["revision_url"] + "/files/" + name)
        assert download.status_code == 200
        assert download.content == content
        assert download.headers["content-type"].startswith("application/octet-stream")
        assert "attachment" in download.headers["content-disposition"]
    assert _counts(browser.engine) == (1, 1, 1, 1)


def test_create_replay_order_revision_noop_etag_and_deleted_replay(browser: Browser) -> None:
    created = _created(browser)
    first_counts = _counts(browser.engine)
    replay = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        tuple(reversed(_FILES_MIXED)),
    )
    assert replay.status_code == 201
    assert replay.json() == {**created, "replayed": True}
    assert _counts(browser.engine) == first_counts
    changed_key = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        _FILES_BINARY,
    )
    _json_error(changed_key, 409)
    page_path = browser.page_path(created["page_id"])
    revised = _post(
        browser,
        page_path + "/file-revisions",
        {},
        _FILES_MARKDOWN,
        key="b" * 32,
        etag=created["etag"],
    )
    assert revised.status_code == 200, revised.text
    second = revised.json()
    assert second["revision_number"] == 2 and second["changed"] and not second["replayed"]
    assert second["revision_url"] == page_path + "/revisions/2"
    no_op = _post(
        browser,
        page_path + "/file-revisions",
        {},
        _FILES_MARKDOWN,
        key="c" * 32,
        etag=second["etag"],
    )
    assert no_op.status_code == 200 and no_op.json()["changed"] is False
    assert no_op.json()["revision_number"] == 2
    after_noop = _counts(browser.engine)
    assert after_noop == (1, 2, 2, 2)
    same_revise = _post(
        browser,
        page_path + "/file-revisions",
        {},
        _FILES_MARKDOWN,
        key="b" * 32,
        etag=created["etag"],
    )
    assert same_revise.status_code == 200
    assert same_revise.json() == {**second, "replayed": True}
    assert _counts(browser.engine) == after_noop
    _json_error(
        _post(
            browser,
            page_path + "/file-revisions",
            {},
            _FILES_BINARY,
            key="d" * 32,
            etag=created["etag"],
        ),
        412,
    )
    wrong_path = page_path.replace(f"/books/{browser.book_id}", f"/books/{'f' * 32}")
    _json_error(
        _post(
            browser,
            wrong_path + "/file-revisions",
            {},
            _FILES_BINARY,
            key="e" * 32,
            etag=second["etag"],
        ),
        404,
    )

    AdminActionService(browser.engine).delete_page_as_master(
        browser.library_id,
        browser.section_id,
        browser.book_id,
        created["page_id"],
        MasterDeletePageFormInput(
            expected_etag=_current_etag(browser.engine, browser.library_id, created["page_id"]),
            confirm_delete="yes",
        ),
        master_session=_master_session(browser),
    )
    with browser.engine.connect() as connection:
        page = ContentRepository(connection).get_page(browser.library_id, created["page_id"])
        assert page is not None and page.deleted_at is not None
    deleted_replay = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        _FILES_MIXED,
    )
    assert deleted_replay.status_code == 201
    assert deleted_replay.json() == {**created, "replayed": True}
    assert browser.client.get(page_path).status_code == 404
    _json_error(
        _post(
            browser,
            page_path + "/file-revisions",
            {},
            _FILES_BINARY,
            key="f" * 32,
            etag=second["etag"],
        ),
        404,
    )


@pytest.mark.parametrize(
    "metadata",
    (
        b'{"title":"One","title":"Two","occurred_at":null}',
        {"title": "Synthetic", "occurred_at": _TIME, "unexpected": "value"},
        {"title": "Synthetic", "occurred_at": "2026-08-13T10:00:00-00:00"},
    ),
)
def test_create_rejects_duplicate_unknown_or_invalid_time_metadata(
    browser: Browser, metadata: object
) -> None:
    _json_error(_post(browser, browser.create_path, metadata, _FILES_MARKDOWN), 422)
    assert _counts(browser.engine) == (0, 0, 0, 0)


def test_browser_string_metadata_and_defaulted_time_warning(browser: Browser) -> None:
    response = _post(
        browser,
        browser.create_path,
        {"title": "No declared time", "occurred_at": None},
        _FILES_BINARY,
        browser_string=True,
    )
    assert response.status_code == 201, response.text
    assert response.json()["warnings"] == ["occurrence_defaulted"]
    assert response.json()["files"][0]["name"] == "payload.bin"


def test_origin_csrf_and_unique_operation_headers_precede_body_read(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _created(browser)
    page_path = browser.page_path(created["page_id"])
    reads = 0

    async def unexpected_body_read(*_args: object, **_kwargs: object) -> None:
        nonlocal reads
        reads += 1
        raise AssertionError("Unauthorized request read its body")

    monkeypatch.setattr(file_set_routes, "parse_file_set_multipart", unexpected_body_read)
    cases = (
        (_post(browser, browser.create_path, {}, _FILES_BINARY, origin=None), 403),
        (
            _post(browser, browser.create_path, {}, _FILES_BINARY, origin="https://other.invalid"),
            403,
        ),
        (_post(browser, browser.create_path, {}, _FILES_BINARY, csrf="wrong"), 403),
        (
            _post(
                browser,
                browser.create_path,
                {},
                _FILES_BINARY,
                extra_headers=(("X-CSRF-Token", browser.csrf),),
            ),
            403,
        ),
        (
            _post(
                browser,
                browser.create_path,
                {},
                _FILES_BINARY,
                extra_headers=(("Idempotency-Key", "b" * 32),),
            ),
            422,
        ),
        (
            _post(
                browser,
                page_path + "/file-revisions",
                {},
                _FILES_BINARY,
                etag=created["etag"],
                extra_headers=(("If-Match", created["etag"]),),
            ),
            422,
        ),
    )
    for response, status in cases:
        _json_error(response, status)
    assert reads == 0
    assert _counts(browser.engine) == (1, 1, 1, 1)


def test_revision_metadata_and_if_match_are_strict(browser: Browser) -> None:
    created = _created(browser)
    page_path = browser.page_path(created["page_id"])
    revision_path = page_path + "/file-revisions"
    _json_error(_post(browser, revision_path, {}, _FILES_BINARY, key="b" * 32), 428)
    _json_error(
        _post(
            browser,
            revision_path,
            {"title": "Unexpected"},
            _FILES_BINARY,
            key="c" * 32,
            etag=created["etag"],
        ),
        422,
    )
    _json_error(
        _post(browser, revision_path, {}, _FILES_BINARY, key="d" * 32, etag='W/"weak"'),
        422,
    )
    _json_error(
        _post(
            browser,
            browser.create_path,
            {"title": "Another"},
            _FILES_BINARY,
            key="e" * 32,
            etag=created["etag"],
        ),
        422,
    )
    assert _counts(browser.engine) == (1, 1, 1, 1)


def test_rotated_and_expired_cookies_cannot_write_or_replay(browser: Browser) -> None:
    created = _created(browser)
    old_session = _master_session(browser)
    new_token = _TOKEN + " rotated"
    with immediate_transaction(browser.engine) as connection:
        rotated = MasterTokenRepository(connection).rotate(_TOKEN, new_token, now=2_000_000)
        assert rotated is not None
    _json_error(
        _post(
            browser,
            browser.create_path,
            {"title": "Synthetic Page", "occurred_at": _TIME},
            _FILES_MIXED,
        ),
        401,
    )
    assert browser.client.get(browser.book_path + "/new-page").status_code == 401
    login = browser.client.post(
        "/admin/login", data={"password": new_token}, headers={"Origin": _ORIGIN}
    )
    assert login.status_code == 303
    page = browser.client.get(browser.book_path + "/new-page")
    assert page.status_code == 200
    match = search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    new_csrf = unescape(match.group(1))
    replay = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        _FILES_MIXED,
        csrf=new_csrf,
    )
    assert replay.status_code == 201
    assert replay.json() == {**created, "replayed": True}
    assert _counts(browser.engine) == (1, 1, 1, 1)

    expired_cookie, _ = AdminSessionCodec(
        _SIGNING_SECRET.encode(), ttl_seconds=300, clock=lambda: time() - 600
    ).issue_master(old_session.identity_id, rotated.session_generation)
    browser.client.cookies.clear()
    browser.client.cookies.set(
        "patchouli_admin_session",
        expired_cookie,
        domain="admin.example.invalid",
        path="/admin",
    )
    _json_error(
        _post(
            browser,
            browser.create_path,
            {"title": "Synthetic Page", "occurred_at": _TIME},
            _FILES_MIXED,
            csrf=new_csrf,
        ),
        401,
    )
    assert _counts(browser.engine) == (1, 1, 1, 1)


def test_audit_failure_rolls_back_and_returns_safe_500(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_audit(self: MasterAuditRepository, **_kwargs: object) -> None:
        raise RuntimeError("synthetic internal audit failure with payload.bin")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    response = _post(
        browser,
        browser.create_path,
        {"title": "Synthetic Page", "occurred_at": _TIME},
        _FILES_MIXED,
    )
    _json_error(response, 500)
    assert "synthetic internal" not in response.text
    assert "payload.bin" not in response.text
    assert _counts(browser.engine) == (0, 0, 0, 0)
