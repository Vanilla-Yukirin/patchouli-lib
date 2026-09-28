from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, update

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AuditEvent, Caller, Credential
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewLibrary
from patchouli_lib.library.service import LibrarySeedService

_ORIGIN = "https://admin.example.invalid"
_PASSWORD = "synthetic browser password"
_PASSWORD_HASH = hash_password(_PASSWORD, salt_factory=lambda size: b"b" * size, iterations=300_000)


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'browse.db').as_posix()}",
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
        "/admin/login",
        data={"password": _PASSWORD},
        headers={"Origin": _ORIGIN},
    )
    assert response.status_code == 303


def _paths(library: str, section: str, book: str, page: str) -> tuple[str, ...]:
    library_path = f"/admin/libraries/{library}"
    section_path = f"{library_path}/sections/{section}"
    book_path = f"{section_path}/books/{book}"
    return library_path, section_path, book_path, f"{book_path}/pages/{page}"


def _seed_structure(
    engine: Engine, *, prefix: str = "1", label: str = "First"
) -> tuple[str, str, str]:
    ids = iter(
        (prefix * 32, format(int(prefix, 16) + 1, "x") * 32, format(int(prefix, 16) + 2, "x") * 32)
    )
    with immediate_transaction(engine) as connection:
        result = LibrarySeedService(
            LibraryRepository(connection), id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name=f"{label} Synthetic Library",
                section_name=f"{label} Synthetic Section",
                book_name=f"{label} Synthetic Book",
            )
        )
    return result.library.id, result.section.id, result.book.id


def _insert_page(
    engine: Engine,
    library_id: str,
    section_id: str,
    book_id: str,
    *,
    title: str = "Synthetic archive",
    markdown: bytes = b"# Synthetic archive\n",
) -> str:
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, title)
    page_uid = b"1" * 16
    revision_id = "rev_" + "2" * 32
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
    return identifier.value


def test_identity_browser_is_protected_and_shows_metadata_only(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    anonymous = client.get("/admin/agents")
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/admin/login"

    _login(client)
    empty = client.get("/admin/agents?lang=zh-CN")
    assert empty.status_code == 200
    assert "暂无身份。" in empty.text
    assert empty.headers["cache-control"] == "no-store, max-age=0"

    library_id, _, _ = _seed_structure(engine)
    caller_id = "d" * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Caller).values(
                id=caller_id,
                library_id=library_id,
                kind="agent",
                name="Reader <device>",
                description="Synthetic device",
                policy_version=1,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )

    page = client.get("/admin/agents?lang=zh-CN")
    assert page.status_code == 200
    assert "设备与身份" in page.text
    assert "Reader &lt;device&gt;" in page.text
    assert "Reader <device>" not in page.text
    assert f"/admin/libraries/{library_id}/callers/{caller_id}" in page.text
    assert "现有凭据无法从校验值还原" in page.text
    assert 'aria-current="page"' in page.text


def test_browser_requires_session_and_empty_state(browser: tuple[TestClient, Engine]) -> None:
    client, engine = browser
    for path in ("/admin/libraries", "/admin/libraries/" + "a" * 32):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert response.headers["cache-control"] == "no-store, max-age=0"

    _login(client)
    dashboard = client.get("/admin")
    assert 'class="side-nav"' in dashboard.text
    assert 'href="/admin/libraries"' in dashboard.text

    empty = client.get("/admin/libraries")
    assert empty.status_code == 200
    assert "No libraries yet." in empty.text
    assert empty.headers["cache-control"] == "no-store, max-age=0"

    library_id = "a" * 32
    with immediate_transaction(engine) as connection:
        LibraryRepository(connection).add_library(
            NewLibrary(
                id=library_id,
                name="Empty Synthetic Library",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    library = client.get(f"/admin/libraries/{library_id}")
    assert library.status_code == 200
    assert "Page count: 0" in library.text
    assert "No sections yet." in library.text

    extreme_timestamp = (1 << 63) - 1
    with immediate_transaction(engine) as connection:
        LibraryRepository(connection).add_library(
            NewLibrary(
                id="b" * 32,
                name="Extreme Synthetic Library",
                created_at=extreme_timestamp,
                updated_at=extreme_timestamp,
            )
        )
    listing = client.get("/admin/libraries")
    assert listing.status_code == 200
    assert f"{extreme_timestamp} µs (UTC)" in listing.text
    assert "1970-01-01 00:00 UTC" in listing.text


def test_browser_reads_scoped_hierarchy_and_escapes_markdown(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(
        engine, prefix="1", label="Synthetic <script>alert(1)</script>"
    )
    second_library, second_section, second_book = _seed_structure(
        engine, prefix="4", label="Second"
    )
    page_id = _insert_page(
        engine,
        library,
        section,
        book,
        title="Synthetic <img src=x onerror=alert(1)>",
        markdown=b"# Synthetic preview\n\n<script>alert(1)</script>\n",
    )
    library_path, section_path, book_path, page_path = _paths(library, section, book, page_id)
    _login(client)

    listing = client.get("/admin/libraries")
    assert listing.status_code == 200
    assert '<ul class="item-list library-grid">' in listing.text
    assert f'href="{library_path}"' in listing.text
    assert "Page count: 1" in listing.text
    assert "Page count: 0" in listing.text
    assert "1970-01-01 00:00 UTC" in listing.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in listing.text
    assert "<script>alert(1)</script>" not in listing.text

    library_response = client.get(library_path)
    section_response = client.get(section_path)
    book_response = client.get(book_path)
    preview = client.get(page_path)
    assert f'href="{section_path}"' in library_response.text
    assert f'href="{book_path}"' in section_response.text
    assert f'href="{page_path}"' in book_response.text
    assert preview.status_code == 200
    assert '<pre class="markdown-preview"># Synthetic preview' in preview.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in preview.text
    assert "<script>alert(1)</script>" not in preview.text
    assert "<img src=x onerror=alert(1)>" not in preview.text
    for response in (listing, library_response, section_response, book_response, preview):
        assert response.headers["cache-control"] == "no-store, max-age=0"

    # A real sibling Library cannot be traversed by mixing its Section/Book IDs.
    wrong_scope = _paths(library, second_section, second_book, page_id)
    for path in wrong_scope[1:]:
        response = client.get(path)
        assert response.status_code == 404
        assert "Synthetic preview" not in response.text
        assert response.headers["cache-control"] == "no-store, max-age=0"
    assert client.get(f"/admin/libraries/{second_library}").status_code == 200


def test_browser_hides_deleted_pages_and_translates_labels(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine)
    page_id = _insert_page(engine, library, section, book)
    library_path, section_path, book_path, page_path = _paths(library, section, book, page_id)
    _login(client)
    chinese = client.get("/admin/libraries?lang=zh-CN")
    assert chinese.headers["content-language"] == "zh-CN"
    assert "知识库" in chinese.text
    assert "页面数: 1" in chinese.text
    assert 'href="/admin/libraries?lang=en"' in chinese.text
    assert "分区" in client.get(library_path).text
    assert "书籍" in client.get(section_path).text
    assert "页面" in client.get(book_path).text
    assert "当前 Markdown 正文" in client.get(page_path).text

    with immediate_transaction(engine) as connection:
        connection.execute(
            update(Page)
            .where(Page.library_id == library, Page.page_id == page_id)
            .values(deleted_at=3_000_000, updated_at=3_000_000)
        )
    listing = client.get("/admin/libraries")
    assert "页面数: 0" in listing.text
    assert "暂无页面。" in client.get(book_path).text
    missing = client.get(page_path)
    assert missing.status_code == 404
    assert "Synthetic archive" not in missing.text
    assert missing.headers["content-language"] == "zh-CN"
    assert client.get(f"{page_path}/revisions/1").status_code == 404

    english = client.get("/admin/libraries?lang=en")
    assert english.headers["content-language"] == "en"
    assert "Page count: 0" in english.text


def test_browser_reads_historical_revision_without_crossing_page_scope(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine)
    other_library, other_section, other_book = _seed_structure(engine, prefix="4", label="Other")
    page_id = _insert_page(engine, library, section, book, markdown=b"# Original\n")
    _, _, _, page_path = _paths(library, section, book, page_id)
    revision_id = "rev_" + "3" * 32
    with immediate_transaction(engine) as connection:
        page = (
            connection.execute(
                Page.__table__.select().where(Page.library_id == library, Page.page_id == page_id)
            )
            .mappings()
            .one()
        )
        ContentRepository(connection).add_revision(
            NewRevision(
                library_id=library,
                revision_id=revision_id,
                page_uid=page["page_uid"],
                revision_number=2,
                created_at=3_000_000,
                **MarkdownContent.from_bytes(
                    b"# Updated\n<script>alert(1)</script>\n"
                ).model_dump(),
            )
        )
        connection.execute(
            update(Page)
            .where(Page.library_id == library, Page.page_id == page_id)
            .values(
                current_revision_id=revision_id,
                current_revision_number=2,
                updated_at=3_000_000,
            )
        )

    history_path = f"{page_path}/revisions/1"
    assert client.get(history_path).status_code == 303
    _login(client)
    current = client.get(page_path)
    assert current.status_code == 200
    assert "# Updated" in current.text
    assert "Version history" in current.text
    assert f'href="{history_path}"' in current.text
    historical = client.get(history_path)
    assert historical.status_code == 200
    assert "# Original" in historical.text
    assert "# Updated" not in historical.text
    assert "Back to current version" in historical.text
    assert historical.headers["cache-control"] == "no-store, max-age=0"
    second = client.get(f"{page_path}/revisions/2")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in second.text
    assert "<script>alert(1)</script>" not in second.text
    for path in (
        f"{page_path}/revisions/0",
        f"{page_path}/revisions/3",
        f"{page_path}/revisions/{1 << 63}",
        f"/admin/libraries/{other_library}/sections/{other_section}/books/"
        f"{other_book}/pages/{page_id}/revisions/1",
    ):
        assert client.get(path).status_code == 404
    chinese = client.get(f"{history_path}?lang=zh-CN")
    assert "版本历史" in chinese.text
    assert "返回当前版本" in chinese.text


def test_home_shows_only_scoped_successful_content_activity(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine)
    other_library, _, _ = _seed_structure(engine, prefix="4", label="Other")
    page_id = _insert_page(
        engine,
        library,
        section,
        book,
        title="Title <script>alert(1)</script>",
    )
    actor_id = "a" * 32
    credential_id = "b" * 32
    revision_id = "rev_" + "3" * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Caller),
            {
                "id": actor_id,
                "library_id": library,
                "kind": "agent",
                "name": "Agent <img src=x onerror=alert(1)>",
                "description": "Synthetic caller",
                "policy_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        connection.execute(
            insert(Credential),
            {
                "id": credential_id,
                "library_id": library,
                "caller_id": actor_id,
                "selector": "s" * 22,
                "token_version": 1,
                "verifier": b"v" * 32,
                "expires_at": 10_000_000,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        page = (
            connection.execute(
                Page.__table__.select().where(Page.library_id == library, Page.page_id == page_id)
            )
            .mappings()
            .one()
        )
        connection.execute(
            insert(Revision),
            NewRevision(
                library_id=library,
                revision_id=revision_id,
                page_uid=page["page_uid"],
                revision_number=2,
                created_at=3_000_000,
                **MarkdownContent.from_bytes(b"# New\n").model_dump(),
            ).model_dump(),
        )
        connection.execute(
            update(Page)
            .where(Page.library_id == library, Page.page_id == page_id)
            .values(
                current_revision_id=revision_id,
                current_revision_number=2,
                updated_at=3_000_000,
            ),
        )
        base = {
            "library_id": library,
            "actor_caller_id": actor_id,
            "actor_credential_id": credential_id,
            "resource_type": "page",
            "resource_id": page_id,
            "request_id": "synthetic-request",
            "occurred_at": 3_000_000,
        }
        connection.execute(
            insert(AuditEvent),
            [
                {
                    **base,
                    "id": "c" * 32,
                    "action": "content.archive.create",
                    "outcome": "succeeded",
                },
                {
                    **base,
                    "id": "d" * 32,
                    "action": "content.archive.revise",
                    "resource_type": "revision",
                    "resource_id": revision_id,
                    "outcome": "succeeded",
                    "occurred_at": 4_000_000,
                },
                {**base, "id": "e" * 32, "action": "content.archive.create", "outcome": "failed"},
                {**base, "id": "f" * 32, "action": "auth.credential.issue", "outcome": "succeeded"},
            ],
        )

    assert client.get("/admin").status_code == 303
    assert client.get(f"/admin/libraries/{library}/callers/{actor_id}").status_code == 303
    _login(client)
    home = client.get("/admin?lang=zh-CN")
    assert home.status_code == 200
    assert "内容近况" in home.text
    assert home.text.count("更新了页面") == 1
    assert home.text.count("创建了页面") == 1
    assert home.text.index("更新了页面") < home.text.index("创建了页面")
    assert f"/admin/libraries/{library}/callers/{actor_id}" in home.text
    assert f"/pages/{page_id}/revisions/2" in home.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in home.text
    assert "<script>alert(1)</script>" not in home.text
    assert "<img src=x onerror=alert(1)>" not in home.text
    assert 'title="1970-01-01 00:00:04 UTC"' in home.text
    assert home.headers["cache-control"] == "no-store, max-age=0"

    rejected_form = client.post(
        "/admin/bootstrap", data={"csrf_token": "wrong"}, headers={"Origin": _ORIGIN}
    )
    assert rejected_form.status_code == 403
    assert "内容近况" not in rejected_form.text
    assert "暂无内容活动。" not in rejected_form.text

    caller = client.get(f"/admin/libraries/{library}/callers/{actor_id}")
    assert caller.status_code == 200
    assert "Synthetic caller" in caller.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in caller.text
    assert client.get(f"/admin/libraries/{other_library}/callers/{actor_id}").status_code == 404
