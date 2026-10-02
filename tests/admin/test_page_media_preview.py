"""Sealed synthetic Revisions exercise the master-only derived media boundary."""

from __future__ import annotations

import sqlite3
from io import BytesIO
from time import time
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from sqlalchemy import Connection, Engine
from test_file_set_routes import (
    _ORIGIN,
    _SIGNING_SECRET,
    _TOKEN,
    Browser,
    _created,
    _current_etag,
    _master_session,
    _post,
)
from test_file_set_routes import browser as browser
from test_page_move_routes import _move, _target
from test_search_browser import _LEGACY
from test_search_browser import browser as _uninitialized_browser_fixture

from patchouli_lib.admin import router as router_module
from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.file_download import AdminFileDownloadService
from patchouli_lib.admin.markdown_preview import (
    MAX_MARKDOWN_PREVIEW_BYTES,
    render_markdown_preview,
)
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.raster_preview import build_raster_preview
from patchouli_lib.admin.read_model import AdminReadModel, PagePreviewRead
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.database import immediate_transaction
from patchouli_lib.retrieval.repository import RetrievalRepository, StoredRevisionFile

uninitialized_browser = _uninitialized_browser_fixture
_IMAGE_NAME = "雪 #%.png"


def _png(color: str = "red") -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "synthetic source metadata marker")
    with Image.new("RGB", (4, 3), color) as image, BytesIO() as output:
        image.save(output, format="PNG", pnginfo=info)
        return output.getvalue()


def _stable(browser: Browser, page_id: str) -> str:
    return f"/admin/libraries/{browser.library_id}/pages/{page_id}"


def _image_path(browser: Browser, page_id: str, number: int = 1) -> str:
    return (
        _stable(browser, page_id)
        + f"/revisions/{number}/preview-images/"
        + quote(_IMAGE_NAME, safe="")
    )


def _media_page(browser: Browser) -> dict[str, Any]:
    return _created(
        browser,
        files=(
            (
                "content.md",
                b"# Default document\n\n![same version](./"
                + quote(_IMAGE_NAME, safe="").encode()
                + b")\n\n[payload](payload.bin)",
            ),
            ("另一个.markdown", b"# Selected document\n\n<script>not executable</script>"),
            (_IMAGE_NAME, _png()),
            ("payload.bin", b"\x00\xfforiginal download"),
        ),
    )


def test_master_preview_is_reachable_selectable_and_keeps_downloads_raw(browser: Browser) -> None:
    created = _media_page(browser)
    page_id = created["page_id"]
    stable = _stable(browser, page_id)
    deep = browser.page_path(page_id)
    original = browser.client.get(deep)
    assert '<pre class="markdown-preview">' in original.text
    assert f'href="{stable}/revisions/1"' in original.text
    assert "Safe Markdown preview" in original.text
    default = browser.client.get(stable)
    assert default.status_code == 200
    assert "<h1>Default document</h1>" in default.text
    assert 'name="preview_file"' in default.text
    assert f'action="{stable}/revisions/1"' in default.text
    assert f'src="{_image_path(browser, page_id)}"' in default.text
    assert "img-src 'self'" in default.headers["content-security-policy"]
    assert "script-src" not in default.headers["content-security-policy"]
    selected = browser.client.get(stable, params={"preview_file": "另一个.markdown"})
    assert selected.status_code == 200
    assert "<h1>Selected document</h1>" in selected.text
    assert "<h1>Default document</h1>" not in selected.text
    assert "<script>not executable</script>" not in selected.text
    assert "&lt;script&gt;not executable&lt;/script&gt;" in selected.text
    raw = browser.client.get(deep + "/revisions/1/files/payload.bin")
    assert raw.content == b"\x00\xfforiginal download"
    assert raw.headers["content-type"] == "application/octet-stream"
    assert raw.headers["content-disposition"].startswith("attachment;")


def test_default_without_content_md_and_preview_only_budget_rejection(browser: Browser) -> None:
    created = _created(
        browser,
        files=(("B.markdown", b"# Second\n"), ("A.MD", b"# First\n")),
    )
    stable = _stable(browser, created["page_id"])
    assert "<h1>First</h1>" in browser.client.get(stable).text
    revised = _post(
        browser,
        browser.page_path(created["page_id"]) + "/file-revisions",
        {},
        (("content.md", b"x" * (MAX_MARKDOWN_PREVIEW_BYTES + 1)),),
        key="b" * 32,
        etag=created["etag"],
    )
    assert revised.status_code == 200
    unavailable = browser.client.get(stable)
    assert unavailable.status_code == 200
    assert "No safe Markdown preview is available" in unavailable.text
    assert len(unavailable.content) < 30_000
    raw = browser.client.get(
        browser.page_path(created["page_id"]) + "/revisions/2/files/content.md"
    )
    assert raw.status_code == 200 and len(raw.content) == MAX_MARKDOWN_PREVIEW_BYTES + 1


@pytest.mark.parametrize(
    "query",
    [
        "preview_file=missing.md",
        "preview_file=payload.bin",
        "preview_file=../content.md",
        "preview_file=",
        "preview_file=content.md&preview_file=content.md",
    ],
)
def test_unknown_or_ambiguous_selection_does_not_disclose_bytes(
    browser: Browser, query: str
) -> None:
    created = _media_page(browser)
    response = browser.client.get(_stable(browser, created["page_id"]) + "?" + query)
    assert response.status_code == 404
    assert "Default document" not in response.text


def test_exact_history_images_are_reencoded_and_metadata_stripped(browser: Browser) -> None:
    created = _media_page(browser)
    revised = _post(
        browser,
        browser.page_path(created["page_id"]) + "/file-revisions",
        {},
        (
            (
                "content.md",
                b"# Later document\n\n![later](" + quote(_IMAGE_NAME, safe="").encode() + b")",
            ),
            (_IMAGE_NAME, _png("blue")),
        ),
        key="b" * 32,
        etag=created["etag"],
    )
    assert revised.status_code == 200
    historical = browser.client.get(_stable(browser, created["page_id"]) + "/revisions/1")
    assert "<h1>Default document</h1>" in historical.text
    assert _image_path(browser, created["page_id"], 1) in historical.text
    assert _image_path(browser, created["page_id"], 2) not in historical.text
    for number, expected in ((1, (255, 0, 0)), (2, (0, 0, 255))):
        response = browser.client.get(_image_path(browser, created["page_id"], number))
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"] == "inline"
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert b"synthetic source metadata marker" not in response.content
        with Image.open(BytesIO(response.content)) as image:
            image.load()
            assert image.info == {} and image.getpixel((0, 0)) == expected
    raw = browser.client.get(
        browser.page_path(created["page_id"]) + "/revisions/1/files/" + quote(_IMAGE_NAME, safe="")
    )
    assert raw.content == _png()


def test_stable_media_survives_move_but_is_hidden_after_delete(browser: Browser) -> None:
    created = _media_page(browser)
    image_path = _image_path(browser, created["page_id"])
    moved = _move(browser, created, _target(browser.engine, browser.library_id))
    assert moved.status_code == 200
    assert browser.client.get(image_path).status_code == 200
    assert (
        "<h1>Default document</h1>" in browser.client.get(_stable(browser, created["page_id"])).text
    )
    AdminActionService(browser.engine).delete_page_as_master(
        browser.library_id,
        "5" * 32,
        "6" * 32,
        created["page_id"],
        MasterDeletePageFormInput(
            expected_etag=_current_etag(browser.engine, browser.library_id, created["page_id"]),
            confirm_delete="yes",
        ),
        master_session=_master_session(browser),
    )
    assert browser.client.get(image_path).status_code == 404
    assert (
        browser.client.get(_stable(browser, created["page_id"]) + "/revisions/1").status_code == 404
    )


def test_media_denies_anonymous_legacy_and_rotated_sessions(
    uninitialized_browser: tuple[TestClient, Engine], browser: Browser
) -> None:
    client, _engine = uninitialized_browser
    path = "/admin/libraries/invalid/pages/invalid/revisions/01/preview-images/nested/file.png"
    anonymous = client.get(path)
    assert anonymous.status_code == 303 and anonymous.headers["location"] == "/admin/login"
    assert (
        client.post(
            "/admin/login", data={"password": _LEGACY}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )
    assert client.get(path).status_code == 403
    created = _media_page(browser)
    with immediate_transaction(browser.engine) as connection:
        MasterTokenRepository(connection).recover_from_local_cli(
            _TOKEN + " replaced", now=3_000_000
        )
    for url in (_image_path(browser, created["page_id"]), _stable(browser, created["page_id"])):
        rejected = browser.client.get(url)
        assert rejected.status_code == 303
        assert "Max-Age=0" in rejected.headers["set-cookie"]
        assert "Default document" not in rejected.text


def test_preview_admission_uses_real_begin_before_invalid_paths(browser: Browser) -> None:
    admitted: list[bool] = []

    def reject(connection: Connection) -> bool:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        admitted.append(raw.in_transaction)
        return False

    with pytest.raises(AuthenticationError):
        AdminReadModel(browser.engine).get_page_preview_by_id(
            "invalid", "invalid", 0, authorize=reject
        )
    with pytest.raises(AuthenticationError):
        AdminFileDownloadService(browser.engine).get_file_by_id(
            "invalid", "invalid", 0, "../bad.png", authorize=reject
        )
    assert admitted == [True, True]


def test_expired_cookie_cannot_read_markdown_or_image(browser: Browser) -> None:
    created = _media_page(browser)
    session = _master_session(browser)
    expired_cookie, _ = AdminSessionCodec(
        _SIGNING_SECRET.encode(), ttl_seconds=300, clock=lambda: time() - 600
    ).issue_master(session.identity_id, session.session_generation)
    for path in (_stable(browser, created["page_id"]), _image_path(browser, created["page_id"])):
        browser.client.cookies.clear()
        browser.client.cookies.set(
            "patchouli_admin_session", expired_cookie, domain="admin.example.invalid", path="/admin"
        )
        response = browser.client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert "Default document" not in response.text


@pytest.mark.parametrize("name", ["payload.bin", "missing.png", "nested/file.png", "../bad.png"])
def test_unknown_non_raster_or_nonflat_media_is_unavailable(browser: Browser, name: str) -> None:
    created = _media_page(browser)
    path = (
        _stable(browser, created["page_id"]) + "/revisions/1/preview-images/" + quote(name, safe="")
    )
    response = browser.client.get(path)
    assert response.status_code == 404
    assert "original download" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


@pytest.mark.parametrize("failure", ["seal", "other_file"])
def test_any_snapshot_failure_blocks_html_and_derived_image(
    browser: Browser, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    created = _media_page(browser)
    if failure == "seal":
        monkeypatch.setattr(RetrievalRepository, "has_revision_file_seal", lambda *_args: False)
    else:
        original = RetrievalRepository.list_revision_files

        def corrupt(
            repository: RetrievalRepository,
            library_id: str,
            page_uid: bytes,
            revision_id: str,
            revision_number: int,
        ) -> tuple[StoredRevisionFile, ...]:
            entries = original(repository, library_id, page_uid, revision_id, revision_number)
            return tuple(
                StoredRevisionFile(
                    entry.name,
                    entry.content,
                    entry.size_bytes,
                    b"x" * 32 if entry.name == "payload.bin" else entry.content_sha256,
                )
                for entry in entries
            )

        monkeypatch.setattr(RetrievalRepository, "list_revision_files", corrupt)
    for path in (_stable(browser, created["page_id"]), _image_path(browser, created["page_id"])):
        response = browser.client.get(path)
        assert response.status_code == 500
        assert "Default document" not in response.text and "Traceback" not in response.text
        assert response.headers["cache-control"] == "no-store, max-age=0"


def test_rendering_and_decoding_start_after_read_transactions_close(
    browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _media_page(browser)
    returned: list[str] = []
    original_read = AdminReadModel.get_page_preview_by_id
    original_file = AdminFileDownloadService.get_file_by_id
    original_render = render_markdown_preview
    original_decode = build_raster_preview

    def read(self: AdminReadModel, *args: Any, **kwargs: Any) -> PagePreviewRead | None:
        result = original_read(self, *args, **kwargs)
        returned.append("read_closed")
        return result

    def file(self: AdminFileDownloadService, *args: Any, **kwargs: Any) -> Any:
        result = original_file(self, *args, **kwargs)
        returned.append("file_closed")
        return result

    def render(*args: Any, **kwargs: Any) -> str:
        assert returned == ["read_closed"]
        return original_render(*args, **kwargs)

    def decode(*args: Any, **kwargs: Any) -> Any:
        assert returned == ["read_closed", "file_closed"]
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(AdminReadModel, "get_page_preview_by_id", read)
    monkeypatch.setattr(AdminFileDownloadService, "get_file_by_id", file)
    monkeypatch.setattr(router_module, "render_markdown_preview", render)
    monkeypatch.setattr(router_module, "build_raster_preview", decode)
    assert browser.client.get(_stable(browser, created["page_id"])).status_code == 200
    assert browser.client.get(_image_path(browser, created["page_id"])).status_code == 200
