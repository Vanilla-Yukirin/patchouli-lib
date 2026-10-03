"""Cookie-authenticated downloads return an exact, safe Revision snapshot."""

from __future__ import annotations

from collections.abc import Iterator
from html import unescape
from pathlib import Path
from re import search
from urllib.parse import quote

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_library_browser import (
    _ORIGIN,
    _PASSWORD,
    _PASSWORD_HASH,
    _append_file_set_revision,
    _insert_page,
    _paths,
    _seed_structure,
)

from patchouli_lib.admin.file_download import AdminFileDownloadService
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction

_MASTER = "synthetic master token for browser downloads"


@pytest.fixture
def download_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Engine]]:
    database_url = f"sqlite:///{(tmp_path / 'download-browser.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": database_url,
                "admin_password_hash": _PASSWORD_HASH,
                "admin_session_signing_secret": "s" * 32,
            }
        )
    )
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _login(client: TestClient, *, token: str = _PASSWORD) -> None:
    assert (
        client.post(
            "/admin/login", data={"password": token}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )


def _master_login(client: TestClient, engine: Engine) -> None:
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER, now=1_000)
    _login(client, token=_MASTER)


def _page(engine: Engine) -> tuple[tuple[str, str, str], str]:
    scope = _seed_structure(engine)
    page_id = _insert_page(engine, *scope, markdown=b"# Legacy download\n", sealed=True)
    return scope, _paths(*scope, page_id)[-1]


@pytest.mark.parametrize("mixed", [True, False])
def test_current_and_historical_downloads_use_exact_bytes_and_attachment_headers(
    download_browser: tuple[TestClient, Engine], mixed: bool
) -> None:
    client, engine = download_browser
    scope, page_path = _page(engine)
    page_id = page_path.rsplit("/", 1)[-1]
    name = "雪 & #%文档.txt"
    files: tuple[tuple[str, bytes], ...] = (
        (name, "中文原始内容".encode()),
        ("payload.html", b"\x00\xff<script>alert('binary')</script>"),
    )
    if mixed:
        files += (("content.md", b"# Current download\n"),)
    _append_file_set_revision(engine, scope[0], page_id, number=2, marker="3", files=files)
    _master_login(client, engine)
    preview = client.get(page_path + "?lang=zh-CN")
    assert preview.status_code == 200
    for filename, content in files:
        path = page_path + "/revisions/2/files/" + quote(filename, safe="")
        assert f'href="{path}"' in preview.text
        downloaded = client.get(path)
        assert downloaded.status_code == 200 and downloaded.content == content
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["content-disposition"] == (
            "attachment; filename=\"download\"; filename*=UTF-8''" + quote(filename, safe="")
        )
        assert downloaded.headers["cache-control"] == "no-store, max-age=0"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in downloaded.headers["content-security-policy"]
    historical = client.get(page_path + "/revisions/1")
    exact_old_path = page_path + "/revisions/1/files/content.md"
    assert f'href="{exact_old_path}"' in historical.text
    assert client.get(exact_old_path).content == b"# Legacy download\n"
    assert "<script>" not in preview.text


def test_legacy_cookie_downloads_stop_when_master_identity_is_created(
    download_browser: tuple[TestClient, Engine],
) -> None:
    client, engine = download_browser
    _, page_path = _page(engine)
    path = page_path + "/revisions/1/files/content.md"
    assert client.get(path).status_code == 303
    _login(client)
    assert client.get(path).content == b"# Legacy download\n"
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER, now=1_000)
    blocked = client.get(path)
    assert blocked.status_code == 303 and blocked.headers["location"] == "/admin/login"
    assert "Max-Age=0" in blocked.headers["set-cookie"]
    assert b"Legacy download" not in blocked.content


@pytest.mark.parametrize("master", [True, False])
def test_download_admission_uses_the_same_read_connection_after_a_credential_change(
    download_browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch, master: bool
) -> None:
    client, engine = download_browser
    _, page_path = _page(engine)
    if master:
        _master_login(client, engine)
    else:
        _login(client)
    original = AdminFileDownloadService.get_file

    def delayed(self: AdminFileDownloadService, *args: object, **kwargs: object) -> object:
        with immediate_transaction(engine) as connection:
            repository = MasterTokenRepository(connection)
            if master:
                repository.recover_from_local_cli(
                    "synthetic replacement master token for downloads", now=2_000
                )
            else:
                repository.initialize_from_local_cli(_MASTER, now=1_000)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AdminFileDownloadService, "get_file", delayed)
    response = client.get(page_path + "/revisions/1/files/content.md")
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert b"Legacy download" not in response.content


def test_download_rejects_bad_names_versions_and_full_path_mismatches(
    download_browser: tuple[TestClient, Engine],
) -> None:
    client, engine = download_browser
    scope, page_path = _page(engine)
    invalid_numbers = ("0", "01", "+1", "-1", "１", "bogus", str(1 << 63), "9" * 30)
    for value in invalid_numbers:
        assert client.get(page_path + f"/revisions/{value}/files/content.md").status_code == 303
    _master_login(client, engine)
    for value in (*invalid_numbers, "2"):
        assert client.get(page_path + f"/revisions/{value}/files/content.md").status_code == 404
    for name in ("absent.bin", "nested/content.md", "..\\content.md", "CON", "bad\nname.md"):
        assert (
            client.get(page_path + "/revisions/1/files/" + quote(name, safe="")).status_code == 404
        )
    for identifier in scope:
        assert (
            client.get(
                page_path.replace(identifier, "f" * 32) + "/revisions/1/files/content.md"
            ).status_code
            == 404
        )
    assert (
        client.get(
            page_path.rsplit("/", 1)[0] + "/unknown/revisions/1/files/content.md"
        ).status_code
        == 404
    )


def test_download_is_hidden_after_page_is_moved_to_trash(
    download_browser: tuple[TestClient, Engine],
) -> None:
    client, engine = download_browser
    _, page_path = _page(engine)
    _master_login(client, engine)
    preview = client.get(page_path)
    values = {"confirm_delete": "yes"}
    for field in ("csrf_token", "expected_etag"):
        match = search(rf'name="{field}" value="([^"]+)"', preview.text)
        assert match is not None
        values[field] = unescape(match.group(1))
    assert (
        client.post(page_path + "/delete", data=values, headers={"Origin": _ORIGIN}).status_code
        == 303
    )
    response = client.get(page_path + "/revisions/1/files/content.md")
    assert response.status_code == 404 and b"Legacy download" not in response.content


def test_download_storage_failure_returns_no_content_or_internal_exception(
    download_browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = download_browser
    _, page_path = _page(engine)
    _master_login(client, engine)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic internal credential material")

    monkeypatch.setattr(AdminFileDownloadService, "get_file", fail)
    response = client.get(page_path + "/revisions/1/files/content.md?lang=zh-CN")
    assert response.status_code == 500 and response.text == "无法下载此文件。"
    assert "synthetic" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
