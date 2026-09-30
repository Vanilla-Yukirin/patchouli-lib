"""The human search form uses only a current master session and POSTed terms."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.tags.repository import TagRepository

_ORIGIN = "https://admin.example.invalid"
_MASTER = "synthetic browser master token material 0001"
_LEGACY = "synthetic legacy admin password"
_TAG_A = "a" * 32
_TAG_B = "b" * 32


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Engine]]:
    database_url = f"sqlite:///{(tmp_path / 'search-browser.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "admin_password_hash": hash_password(
                _LEGACY, salt_factory=lambda size: b"l" * size, iterations=300_000
            ),
            "admin_session_signing_secret": "s" * 32,
        }
    )
    app = create_app(settings)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _master_login(client: TestClient, engine: Engine) -> str:
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER, now=1_000)
    response = client.post("/admin/login", data={"password": _MASTER}, headers={"Origin": _ORIGIN})
    assert response.status_code == 303
    form = client.get("/admin/search")
    match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
    assert match is not None
    return match.group(1)


def _post(client: TestClient, csrf: str, **fields: str | list[str]) -> Response:
    data: dict[str, str | list[str]] = {
        "csrf_token": csrf,
        "keywords": "",
        "library_id": "",
        "occurred_from": "",
        "occurred_before": "",
    }
    data.update(fields)
    return client.post(
        "/admin/search",
        data=data,
        headers={"Origin": _ORIGIN},
    )


def _seed(engine: Engine) -> tuple[tuple[str, str, str, str], tuple[str, str, str, str]]:
    first = seed_library_structure(engine, prefix="1", label="First")
    second = seed_library_structure(engine, prefix="4", label="Second")
    local = page_graph_values(
        library_id=first[0],
        section_id=first[1],
        book_id=first[2],
        page_byte=0x11,
        revision_hex="12",
        title="中文 <script>alert(1)</script> needle",
        content_md=b"# needle local",
        occurrence_wire="2026-08-13T10:00:00.123456Z",
    )
    remote = page_graph_values(
        library_id=second[0],
        section_id=second[1],
        book_id=second[2],
        page_byte=0x13,
        revision_hex="14",
        title="needle remote",
        content_md=b"# needle remote",
        occurrence_wire="2026-08-14T10:00:00.123456Z",
    )
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, local)
        insert_page_graph(connection, remote)
        tags = TagRepository(connection)
        tags.add_tag(library_id=first[0], tag_id=_TAG_A, name="Local", created_at=0)
        tags.add_tag(library_id=second[0], tag_id=_TAG_B, name="Remote", created_at=0)
        tags.attach_page(
            library_id=first[0], page_uid=local[0].page_uid, tag_id=_TAG_A, created_at=0
        )
        tags.attach_page(
            library_id=second[0], page_uid=remote[0].page_uid, tag_id=_TAG_B, created_at=0
        )
    return (*first, local[0].page_id), (*second, remote[0].page_id)


def test_search_requires_master_session_same_origin_and_csrf(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    anonymous = client.get("/admin/search")
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/admin/login"
    assert client.post("/admin/search", data={"keywords": "needle"}).status_code == 403
    assert (
        client.post(
            "/admin/login", data={"password": _LEGACY}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )
    assert client.get("/admin/search").status_code == 403
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER, now=1_000)
    client.cookies.clear()
    csrf = _master_login_existing(client)
    form = client.get("/admin/search?lang=zh-CN&keywords=secret")
    assert form.status_code == 200
    assert 'action="/admin/search"' in form.text
    assert 'href="/admin/search" aria-current="page"' in form.text
    assert "搜索当前页面" in form.text
    assert "secret" not in form.text
    assert form.headers["cache-control"] == "no-store, max-age=0"
    assert _post(client, "wrong", keywords="needle").status_code == 403
    assert (
        client.post(
            "/admin/search",
            data={"csrf_token": csrf, "keywords": "needle"},
            headers={"Origin": "https://different.example.invalid"},
        ).status_code
        == 403
    )


def _master_login_existing(client: TestClient) -> str:
    response = client.post("/admin/login", data={"password": _MASTER}, headers={"Origin": _ORIGIN})
    assert response.status_code == 303
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/admin/search").text)
    assert match is not None
    return match.group(1)


def test_search_index_gate_filters_and_safe_preview_links(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    first, second = _seed(engine)
    csrf = _master_login(client, engine)
    unavailable = _post(client, csrf, keywords="needle")
    assert unavailable.status_code == 503
    assert "Search index is not ready. Rebuild it before searching." in unavailable.text
    rebuild_search_index(engine, clock=lambda: 3_000_000)

    all_hits = _post(client, csrf, keywords="needle")
    assert all_hits.status_code == 200
    assert first[3] in all_hits.text and second[3] in all_hits.text
    assert "<script>alert(1)</script>" not in all_hits.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in all_hits.text
    expected_link = (
        f"/admin/libraries/{first[0]}/sections/{first[1]}/books/{first[2]}/pages/{first[3]}"
    )
    assert f'href="{expected_link}"' in all_hits.text
    assert client.get(expected_link).status_code == 200
    only_first = _post(client, csrf, keywords="needle", library_id=first[0])
    assert first[3] in only_first.text and second[3] not in only_first.text
    only_second_tag = _post(client, csrf, tags=[f"{second[0]}:{_TAG_B}"])
    assert second[3] in only_second_tag.text and first[3] not in only_second_tag.text
    either_tag = _post(client, csrf, tags=[f"{first[0]}:{_TAG_A}", f"{second[0]}:{_TAG_B}"])
    assert first[3] in either_tag.text and second[3] in either_tag.text
    date_cutoff = _post(client, csrf, occurred_before="2026-08-14T10:00:00")
    assert first[3] in date_cutoff.text and second[3] not in date_cutoff.text


def test_search_invalid_inputs_and_master_rotation_fail_closed(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    first, second = _seed(engine)
    csrf = _master_login(client, engine)
    rebuild_search_index(engine, clock=lambda: 3_000_000)
    empty = _post(client, csrf)
    assert empty.status_code == 422
    assert "Choose a keyword, Tag, or time range before searching." in empty.text
    assert _post(client, csrf, keywords="needle", occurred_from="invalid").status_code == 422
    assert _post(client, csrf, keywords="needle", library_id="f" * 32).status_code == 422
    assert _post(client, csrf, keywords="needle", tags=["malformed"]).status_code == 422
    duplicate = client.post(
        "/admin/search",
        content=f"csrf_token={csrf}&keywords=needle&keywords=other".encode(),
        headers={"Origin": _ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 422
    oversized = client.post(
        "/admin/search",
        content=f"csrf_token={csrf}&keywords=".encode() + b"a" * 100_000,
        headers={"Origin": _ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert oversized.status_code == 413
    assert "a" * 100 not in oversized.text
    malformed_utf8 = client.post(
        "/admin/search",
        content=f"csrf_token={csrf}&keywords=".encode() + b"\xff",
        headers={"Origin": _ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert malformed_utf8.status_code == 400
    assert (
        _post(
            client,
            csrf,
            keywords="needle",
            library_id=first[0],
            tags=[f"{second[0]}:{_TAG_B}"],
        ).status_code
        == 422
    )
    with immediate_transaction(engine) as connection:
        rotated = MasterTokenRepository(connection).rotate(
            _MASTER, "synthetic browser master token material 0002", now=1_001
        )
        assert rotated is not None
    assert _post(client, csrf, keywords="needle").status_code == 401
