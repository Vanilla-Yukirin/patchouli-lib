from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.config import Settings
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

_ORIGIN = "https://browser.example.invalid"
_PASSWORD = "synthetic browser password"


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Engine]]:
    database_url = f"sqlite:///{(tmp_path / 'trash.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "admin_password_hash": hash_password(
                _PASSWORD, salt_factory=lambda size: b"b" * size, iterations=300_000
            ),
            "admin_session_signing_secret": "s" * 32,
        }
    )
    app = create_app(settings)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _login(client: TestClient) -> None:
    response = client.post(
        "/admin/login", data={"password": _PASSWORD}, headers={"Origin": _ORIGIN}
    )
    assert response.status_code == 303


def _seed(engine: Engine, marker: str) -> tuple[str, str, str, str]:
    ids = iter(
        (marker * 32, format(int(marker, 16) + 1, "x") * 32, format(int(marker, 16) + 2, "x") * 32)
    )
    caller_id = format(int(marker, 16) + 3, "x") * 32
    with immediate_transaction(engine) as connection:
        structure = LibrarySeedService(
            LibraryRepository(connection), id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name=f"Synthetic {marker}",
                section_name=f"Synthetic Section {marker}",
                book_name=f"Synthetic Book {marker}",
            )
        )
        connection.execute(
            insert(Caller).values(
                id=caller_id,
                library_id=structure.library.id,
                kind="agent",
                name="Synthetic device",
                description="Test-only identity",
                policy_version=1,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return structure.library.id, structure.section.id, structure.book.id, caller_id


def _page(
    engine: Engine,
    scope: tuple[str, str, str, str],
    index: int,
    *,
    title: str | None = None,
    deleted_at: int | None = None,
    markdown: bytes = b"PRIVATE BODY MUST NOT APPEAR",
) -> str:
    library, section, book, caller = scope
    title = title or f"Synthetic Page {index:02d}"
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, title)
    page_uid = bytes([index]) * 16
    revision_id = "rev_" + f"{index:02x}" * 16
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        repository.add_page(
            NewPage(
                library_id=library,
                page_uid=page_uid,
                section_id=section,
                book_id=book,
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
                library_id=library,
                revision_id=revision_id,
                page_uid=page_uid,
                revision_number=1,
                created_at=2_000_000,
                **MarkdownContent.from_bytes(markdown).model_dump(),
            )
        )
        repository.add_identifier(
            NewPageIdentifier(
                library_id=library,
                identifier_digest=page_id_registry_digest(identifier.value),
                identifier_text=identifier.value,
                id_scheme=PAGE_ID_SCHEME,
                identifier_kind="canonical",
                page_uid=page_uid,
                created_at=2_000_000,
            )
        )
    if deleted_at is not None:
        with immediate_transaction(engine) as connection:
            repository = ContentRepository(connection)
            stored = repository.get_page(library, identifier.value)
            assert stored is not None
            repository.transition_page_lifecycle(
                stored,
                action="delete",
                actor_caller_id=caller,
                request_id="req_" + f"{index:032x}",
                changed_at=deleted_at,
            )
    return identifier.value


def _paths(scope: tuple[str, str, str, str], page_id: str) -> tuple[str, str, str]:
    library, section, _, _ = scope
    root = f"/admin/libraries/{library}"
    section_trash = f"{root}/sections/{section}/trash"
    return f"{root}/trash", section_trash, f"{section_trash}/{page_id}"


def test_trash_requires_session_and_has_empty_bilingual_view(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    scope = _seed(engine, "1")
    library_trash, section_trash, detail = _paths(scope, "missing")
    for path in (library_trash, section_trash, detail):
        anonymous = client.get(path)
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/admin/login"
        assert anonymous.headers["cache-control"] == "no-store, max-age=0"
    _login(client)
    empty = client.get(section_trash + "?lang=zh-CN")
    assert empty.status_code == 200
    assert "暂无已删除页面。" in empty.text
    assert "此处仅显示元数据" in empty.text
    assert empty.headers["content-language"] == "zh-CN"
    assert "default-src 'none'" in empty.headers["content-security-policy"]
    assert "回收站" in client.get(f"/admin/libraries/{scope[0]}").text
    assert "Trash" in client.get(section_trash + "?lang=en").text


def test_trash_scope_state_and_metadata_only(browser: tuple[TestClient, Engine]) -> None:
    client, engine = browser
    first = _seed(engine, "1")
    second = _seed(engine, "4")
    title = 'Deleted <img src=x onerror="alert(1)">'
    deleted_id = _page(engine, first, 1, title=title, deleted_at=3_000_000)
    live_id = _page(engine, first, 2)
    other_id = _page(engine, second, 3, deleted_at=3_000_000)
    library_trash, section_trash, detail = _paths(first, deleted_id)
    _login(client)
    listing = client.get(library_trash)
    assert listing.status_code == 200
    assert f'href="{detail}"' in listing.text
    assert "Deleted &lt;img" in listing.text
    assert "<img src=x" not in listing.text
    assert "PRIVATE BODY MUST NOT APPEAR" not in listing.text
    assert live_id not in listing.text
    assert other_id not in listing.text
    assert "operator_token" not in listing.text
    assert listing.headers["cache-control"] == "no-store, max-age=0"
    detail_response = client.get(detail)
    assert detail_response.status_code == 200
    assert deleted_id in detail_response.text
    assert "PRIVATE BODY MUST NOT APPEAR" not in detail_response.text
    assert "<img src=x" not in detail_response.text
    assert "<form" not in detail_response.text.replace(
        '<form method="post" action="/admin/logout">', ""
    )
    for path in (
        _paths(second, deleted_id)[2],
        f"/admin/libraries/{first[0]}/sections/{second[1]}/trash/{deleted_id}",
        f"{section_trash}/{live_id}",
        _paths(first, other_id)[2],
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert title not in response.text
        assert "PRIVATE BODY MUST NOT APPEAR" not in response.text

    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(first[0], deleted_id)
        assert page is not None
        repository.transition_page_lifecycle(
            page,
            action="restore",
            actor_caller_id=first[3],
            request_id="req_" + "f" * 32,
            changed_at=4_000_000,
        )
    assert client.get(detail).status_code == 404
    assert deleted_id not in client.get(library_trash).text


def test_trash_keyset_pagination_and_invalid_cursor(browser: tuple[TestClient, Engine]) -> None:
    client, engine = browser
    scope = _seed(engine, "1")
    identifiers = [_page(engine, scope, index, deleted_at=3_000_000) for index in range(1, 22)]
    library_trash, section_trash, _ = _paths(scope, identifiers[0])
    _login(client)
    first = client.get(section_trash)
    assert first.status_code == 200
    assert first.text.count("/trash/") == 20
    assert "Next page" in first.text
    assert identifiers[-1] not in first.text
    from html import unescape
    from re import search

    match = search(r'href="([^"]+\?before=[^"]+)"', first.text)
    assert match is not None
    second = client.get(unescape(match.group(1)))
    assert second.status_code == 200
    assert second.text.count("/trash/") == 1
    assert identifiers[-1] in second.text
    assert "Next page" not in second.text
    assert client.get(library_trash + "?before=bad").status_code == 404
    assert client.get(library_trash + "?before=1:a&before=2:b").status_code == 404
