"""Synthetic master-session HTML search pagination without query-string disclosure."""

from __future__ import annotations

import re
from collections.abc import Iterator
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

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

_ORIGIN = "https://search.example.invalid"
_MASTER = "synthetic master token for paginated search 0001"
_TAG = "a" * 32
_NEXT_FORM = re.compile(
    r'<form method="post" action="/admin/search" class="search-next-page">(.*?)</form>',
    re.DOTALL,
)
_HIDDEN_FIELD = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')
_RESULT_LINK = re.compile(r'<li><a href="(/admin/libraries/[^"]+/pages/([^"/]+))">')


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Engine]]:
    database_url = f"sqlite:///{(tmp_path / 'search-pagination.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "admin_password_hash": hash_password(
                "synthetic legacy password",
                salt_factory=lambda size: b"l" * size,
                iterations=300_000,
            ),
            "admin_session_signing_secret": "s" * 32,
            "retrieval_cursor_signing_secret": "c" * 32,
        }
    )
    app = create_app(settings)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _seed(engine: Engine) -> tuple[str, set[str]]:
    library_id, section_id, book_id = seed_library_structure(engine)
    page_ids: set[str] = set()
    with immediate_transaction(engine) as connection:
        tags = TagRepository(connection)
        tags.add_tag(library_id=library_id, tag_id=_TAG, name="Synthetic", created_at=0)
        for number in range(1, 22):
            values = page_graph_values(
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_byte=number,
                revision_hex=f"{number:02x}",
                title=f"synthetic-page-{number:02d}",
                content_md=b"needle <script>alert(1)</script> plain text",
            )
            insert_page_graph(connection, values, include_source=False)
            tags.attach_page(
                library_id=library_id,
                page_uid=values[0].page_uid,
                tag_id=_TAG,
                created_at=0,
            )
            page_ids.add(values[0].page_id)
    rebuild_search_index(engine, clock=lambda: 3_000_000)
    return library_id, page_ids


def _login(client: TestClient, engine: Engine) -> str:
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER, now=1_000)
    response = client.post("/admin/login", data={"password": _MASTER}, headers={"Origin": _ORIGIN})
    assert response.status_code == 303
    form = client.get("/admin/search")
    match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
    assert match is not None
    return match.group(1)


def _post(client: TestClient, fields: list[tuple[str, str]]) -> Response:
    return client.post(
        "/admin/search",
        content=urlencode(fields).encode("utf-8"),
        headers={"Origin": _ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )


def _first_fields(csrf: str, library_id: str) -> list[tuple[str, str]]:
    return [
        ("csrf_token", csrf),
        ("keywords", "needle"),
        ("library_id", library_id),
        ("tags", f"{library_id}:{_TAG}"),
        ("occurred_from", "2026-08-13T09:00:00"),
        ("occurred_before", "2026-08-13T11:00:00"),
    ]


def _next_fields(body: str) -> list[tuple[str, str]]:
    match = _NEXT_FORM.search(body)
    assert match is not None
    return [(name, unescape(value)) for name, value in _HIDDEN_FIELD.findall(match.group(1))]


def _result_ids(body: str) -> set[str]:
    return {page_id for _, page_id in _RESULT_LINK.findall(body)}


def test_master_search_uses_hidden_post_for_second_page_and_escapes_snippet(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library_id, expected = _seed(engine)
    csrf = _login(client, engine)
    first = _post(client, _first_fields(csrf, library_id))
    assert first.status_code == 200
    assert len(_result_ids(first.text)) == 20
    assert str(first.request.url).endswith("/admin/search")
    assert "needle <script>" not in first.text
    assert "needle &lt;script&gt;alert(1)&lt;/script&gt;" in first.text

    next_fields = _next_fields(first.text)
    carried = dict(next_fields)
    cursor = carried.pop("cursor")
    assert carried == {
        "csrf_token": csrf,
        "keywords": "needle",
        "occurred_from": "2026-08-13T09:00:00",
        "occurred_before": "2026-08-13T11:00:00",
        "library_id": library_id,
        "tags": f"{library_id}:{_TAG}",
    }
    assert len(cursor) > 20
    second = _post(client, next_fields)
    assert second.status_code == 200
    assert len(_result_ids(second.text)) == 1
    assert _result_ids(first.text) | _result_ids(second.text) == expected
    assert _NEXT_FORM.search(second.text) is None
    assert str(second.request.url).endswith("/admin/search")


def test_invalid_master_search_cursor_requires_new_search_not_old_cursor(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library_id, _ = _seed(engine)
    csrf = _login(client, engine)
    first_fields = _first_fields(csrf, library_id)
    first = _post(client, first_fields)
    assert first.status_code == 200
    next_fields = _next_fields(first.text)
    corrupted = [(name, value + "x" if name == "cursor" else value) for name, value in next_fields]
    rejected = _post(client, corrupted)
    assert rejected.status_code == 400
    assert "Run a new search" in rejected.text
    assert _NEXT_FORM.search(rejected.text) is None
    restarted = _post(client, first_fields)
    assert restarted.status_code == 200
    assert len(_result_ids(restarted.text)) == 20
    assert _NEXT_FORM.search(restarted.text) is not None
