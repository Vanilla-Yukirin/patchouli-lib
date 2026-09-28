"""Session-protected Tag browsing stays within one Library and live Pages."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.tags.repository import TagRepository

_ORIGIN = "https://admin.example.invalid"
_PASSWORD = "synthetic browser password"
_PASSWORD_HASH = hash_password(_PASSWORD, salt_factory=lambda size: b"b" * size, iterations=300_000)


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'tag-browse.db').as_posix()}",
            "admin_password_hash": _PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
        }
    )
    app = create_app(settings)
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _login(client: TestClient) -> None:
    response = client.post(
        "/admin/login", data={"password": _PASSWORD}, headers={"Origin": _ORIGIN}
    )
    assert response.status_code == 303


def _seed_structure(engine: Engine, *, prefix: str) -> tuple[str, str, str]:
    ids = iter(
        (prefix * 32, format(int(prefix, 16) + 1, "x") * 32, format(int(prefix, 16) + 2, "x") * 32)
    )
    with immediate_transaction(engine) as connection:
        result = LibrarySeedService(
            LibraryRepository(connection), id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name=f"Synthetic {prefix} Library",
                section_name=f"Synthetic {prefix} Section",
                book_name=f"Synthetic {prefix} Book",
            )
        )
    return result.library.id, result.section.id, result.book.id


def _seed_page(
    engine: Engine,
    library_id: str,
    section_id: str,
    book_id: str,
    *,
    title: str,
    page_uid: bytes,
) -> str:
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, title)
    revision_id = "rev_" + page_uid.hex()
    content = MarkdownContent.from_bytes(b"# Synthetic preview\n")
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
    return identifier.value


def test_tag_browser_is_session_protected_library_scoped_and_escaped(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine, prefix="1")
    other_library, other_section, other_book = _seed_structure(engine, prefix="4")
    page_id = _seed_page(
        engine,
        library,
        section,
        book,
        title="Live <script>alert(1)</script>",
        page_uid=b"1" * 16,
    )
    deleted_page_id = _seed_page(
        engine,
        library,
        section,
        book,
        title="Deleted private page",
        page_uid=b"2" * 16,
    )
    _seed_page(
        engine,
        other_library,
        other_section,
        other_book,
        title="Other Library private page",
        page_uid=b"1" * 16,
    )
    tag_id, empty_tag_id, other_tag_id = "a" * 32, "b" * 32, "c" * 32
    with immediate_transaction(engine) as connection:
        tags = TagRepository(connection)
        tags.add_tag(
            library_id=library,
            tag_id=tag_id,
            name="<img src=x onerror=alert(1)>",
            created_at=3_000_000,
        )
        tags.add_tag(library_id=library, tag_id=empty_tag_id, name="Empty", created_at=3_000_000)
        tags.add_tag(
            library_id=other_library,
            tag_id=other_tag_id,
            name="Other private tag",
            created_at=3_000_000,
        )
        for page_uid in (b"1" * 16, b"2" * 16):
            tags.attach_page(
                library_id=library,
                page_uid=page_uid,
                tag_id=tag_id,
                created_at=3_000_000,
            )
        tags.attach_page(
            library_id=other_library,
            page_uid=b"1" * 16,
            tag_id=other_tag_id,
            created_at=3_000_000,
        )
        connection.execute(
            update(Page)
            .where(Page.library_id == library, Page.page_id == deleted_page_id)
            .values(deleted_at=4_000_000, updated_at=4_000_000)
        )

    base = f"/admin/libraries/{library}"
    for path in (f"{base}/tags", f"{base}/tags/{tag_id}"):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert response.headers["cache-control"] == "no-store, max-age=0"

    _login(client)
    library_page = client.get(base)
    assert f'href="{base}/tags"' in library_page.text
    directory = client.get(f"{base}/tags?lang=zh-CN")
    assert directory.status_code == 200
    assert directory.headers["content-language"] == "zh-CN"
    assert directory.headers["cache-control"] == "no-store, max-age=0"
    assert "标签" in directory.text
    assert "页面数: 1" in directory.text
    assert "页面数: 0" in directory.text
    assert "Other private tag" not in directory.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in directory.text
    assert "<img src=x onerror=alert(1)>" not in directory.text
    assert f'href="{base}/tags/{tag_id}"' in directory.text
    assert f'href="{base}/tags?lang=en"' in directory.text

    detail = client.get(f"{base}/tags/{tag_id}")
    page_path = f"{base}/sections/{section}/books/{book}/pages/{page_id}"
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store, max-age=0"
    assert f'href="{page_path}"' in detail.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail.text
    assert "<script>alert(1)</script>" not in detail.text
    assert "Deleted private page" not in detail.text
    assert "Other Library private page" not in detail.text
    assert client.get(page_path).status_code == 200

    empty = client.get(f"{base}/tags/{empty_tag_id}")
    assert empty.status_code == 200
    assert "暂无已标记的页面。" in empty.text
    for path in (
        f"{base}/tags/{other_tag_id}",
        f"/admin/libraries/{other_library}/tags/{tag_id}",
        f"/admin/libraries/{'9' * 32}/tags",
    ):
        missing = client.get(path)
        assert missing.status_code == 404
        assert "Other private tag" not in missing.text
        assert "Live &lt;script&gt;" not in missing.text
        assert missing.headers["cache-control"] == "no-store, max-age=0"
