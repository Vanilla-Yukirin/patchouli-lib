from __future__ import annotations

from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, insert, update

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AuditEvent, Caller, Credential, SectionGrant
from patchouli_lib.config import Settings
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.models import (
    Page,
    Revision,
    RevisionFile,
    RevisionFileSeal,
    RevisionFileSet,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.models import Section
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewLibrary
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.tags.models import Tag

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
        connection.execute(
            insert(RevisionFile).values(
                library_id=library_id,
                page_uid=page_uid,
                revision_id=revision_id,
                revision_number=1,
                filename="content.md",
                content_bytes=markdown,
                size_bytes=len(markdown),
                content_sha256=sha256(markdown).digest(),
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


def _append_file_set_revision(
    engine: Engine,
    library_id: str,
    page_id: str,
    *,
    number: int,
    marker: str,
    files: tuple[tuple[str, bytes], ...],
) -> None:
    manifest = build_file_manifest(files)
    revision_id = "rev_" + marker * 32
    created_at = (number + 1) * 1_000_000
    with immediate_transaction(engine) as connection:
        page = (
            connection.execute(
                Page.__table__.select().where(
                    Page.library_id == library_id, Page.page_id == page_id
                )
            )
            .mappings()
            .one()
        )
        connection.execute(
            insert(Revision).values(
                library_id=library_id,
                revision_id=revision_id,
                page_uid=page["page_uid"],
                revision_number=number,
                content_md=None,
                content_size_bytes=None,
                content_sha256=None,
                created_at=created_at,
            )
        )
        connection.execute(
            insert(RevisionFileSet).values(
                library_id=library_id,
                page_uid=page["page_uid"],
                revision_id=revision_id,
                revision_number=number,
                storage_format="file_set_v1",
                file_count=len(manifest.files),
                total_size_bytes=manifest.total_size_bytes,
                snapshot_sha256=manifest.snapshot_sha256,
            )
        )
        for entry in manifest.files:
            connection.execute(
                insert(RevisionFile).values(
                    library_id=library_id,
                    page_uid=page["page_uid"],
                    revision_id=revision_id,
                    revision_number=number,
                    filename=entry.name,
                    content_bytes=entry.content,
                    size_bytes=entry.content_size_bytes,
                    content_sha256=entry.content_sha256,
                )
            )
        connection.execute(
            insert(RevisionFileSeal).values(
                library_id=library_id,
                page_uid=page["page_uid"],
                revision_id=revision_id,
                revision_number=number,
            )
        )
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_id == page_id)
            .values(
                current_revision_id=revision_id,
                current_revision_number=number,
                updated_at=created_at,
            )
        )


def _seed_activity_actor(engine: Engine, library_id: str, marker: str) -> tuple[str, str]:
    caller_id = marker * 32
    credential_id = format(int(marker, 16) + 1, "x") * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Caller),
            {
                "id": caller_id,
                "library_id": library_id,
                "kind": "agent",
                "name": f"Actor {marker}",
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
                "library_id": library_id,
                "caller_id": caller_id,
                "selector": marker * 22,
                "token_version": 1,
                "verifier": b"v" * 32,
                "expires_at": 10_000_000,
                "created_at": 1,
                "updated_at": 1,
            },
        )
    return caller_id, credential_id


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
    assert "新签发的有效 Agent Token 可以通过主会话再次显示" in page.text
    assert "旧凭据无法从校验值还原" in page.text
    assert 'aria-current="page"' in page.text
    detail = client.get(f"/admin/libraries/{library_id}/callers/{caller_id}?lang=zh-CN")
    assert detail.status_code == 200
    assert "此身份暂无凭据。" in detail.text
    assert "此身份暂无分区授权。" in detail.text


def test_caller_detail_scopes_safe_credential_metadata_and_existing_grants(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library_id, section_id, _ = _seed_structure(engine)
    other_library, _, _ = _seed_structure(engine, prefix="4", label="Other")
    second_section = "9" * 32
    caller_id = "8" * 32
    credential_ids = ("a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32)
    detail_path = f"/admin/libraries/{library_id}/callers/{caller_id}"
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Section),
            {
                "id": second_section,
                "library_id": library_id,
                "name": "Second <script> section",
                "description": "Synthetic section",
                "created_at": 1_000_000,
                "updated_at": 1_000_000,
            },
        )
        connection.execute(
            insert(Caller),
            {
                "id": caller_id,
                "library_id": library_id,
                "kind": "agent",
                "name": "Device <script>",
                "description": "Synthetic <img src=x onerror=alert(1)>",
                "policy_version": 1,
                "created_at": 1_000_000,
                "updated_at": 1_000_000,
            },
        )
        connection.execute(
            insert(Credential),
            [
                {
                    "id": cid,
                    "library_id": library_id,
                    "caller_id": caller_id,
                    "selector": marker * 22,
                    "token_version": 1,
                    "verifier": marker.encode() * 32,
                    "created_at": 1_000_000,
                    "updated_at": updated,
                    "expires_at": expiry,
                    "last_used_at": used,
                    "revoked_at": revoked,
                    "rotated_at": rotated,
                    "rotated_to_credential_id": target,
                }
                for cid, marker, updated, expiry, used, revoked, rotated, target in (
                    (
                        credential_ids[0],
                        "x",
                        2_000_000,
                        4_102_444_800_000_000,
                        2_000_000,
                        None,
                        None,
                        None,
                    ),
                    (credential_ids[1], "y", 1_000_000, 2_000_000, None, None, None, None),
                    (
                        credential_ids[2],
                        "z",
                        3_000_000,
                        4_102_444_800_000_000,
                        None,
                        3_000_000,
                        None,
                        None,
                    ),
                    (
                        credential_ids[3],
                        "w",
                        4_000_000,
                        4_102_444_800_000_000,
                        None,
                        4_000_000,
                        4_000_000,
                        credential_ids[0],
                    ),
                )
            ],
        )
        connection.execute(
            insert(Credential),
            {
                "id": credential_ids[4],
                "library_id": library_id,
                "caller_id": caller_id,
                "selector": "v" * 22,
                "token_version": 1,
                "verifier": b"v" * 32,
                "created_at": 4_000_000_000_000_000,
                "updated_at": 4_000_000_000_000_000,
                "expires_at": 4_102_444_800_000_000,
            },
        )
        connection.execute(
            insert(SectionGrant),
            [
                {
                    "library_id": library_id,
                    "caller_id": caller_id,
                    "section_id": grant_section,
                    "action": action,
                    "created_at": 1_000_000,
                }
                for grant_section, action in (
                    (section_id, "section:query"),
                    (section_id, "page:read"),
                    (second_section, "archive:write"),
                )
            ],
        )

    anonymous = client.get(detail_path)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/admin/login"
    assert anonymous.headers["cache-control"] == "no-store, max-age=0"
    _login(client)
    selected_sql: list[str] = []

    def capture_select(
        _connection: object, _cursor: object, statement: str, *_args: object
    ) -> None:
        if "FROM auth_credentials" in statement:
            selected_sql.append(statement)

    event.listen(engine, "before_cursor_execute", capture_select)
    try:
        english = client.get(f"{detail_path}?lang=en")
    finally:
        event.remove(engine, "before_cursor_execute", capture_select)
    assert english.status_code == 200
    assert english.headers["cache-control"] == "no-store, max-age=0"
    assert len(selected_sql) == 1
    assert "selector" not in selected_sql[0]
    assert "verifier" not in selected_sql[0]
    assert "Existing credentials" in english.text
    assert "Current Section grants" in english.text
    assert "single-Library, Section-level grants" in english.text
    for credential_id in credential_ids:
        assert credential_id in english.text
    credential_cards = [
        card.split("</li>", 1)[0] for card in english.text.split('<li class="credential-item">')[1:]
    ]
    assert len(credential_cards) == len(credential_ids)
    for card in credential_cards:
        summary, metadata = card.split("<details>", 1)
        assert 'class="credential-summary"' in summary
        assert "Expires <time datetime=" in summary
        assert "<code>" not in summary
        assert "<summary>Credential metadata</summary>" in metadata
        assert '<dl class="credential-metadata">' in metadata
        assert "Credential ID" in metadata
        assert "Created" in metadata
        assert "Last used" in metadata
        assert "Revoked" in metadata
        assert "Rotated" in metadata
        assert metadata.endswith(
            "</dl><p>Legacy Section grants; no automatic full-Library access.</p></details>"
        )
    assert "Credential active" in english.text
    assert "Credential not yet active" in english.text
    future_credential = next(
        card for card in credential_cards if f"<code>{credential_ids[4]}</code>" in card
    )
    assert "Credential not yet active" in future_credential
    assert "Credential active" not in future_credential
    assert "Credential expired" in english.text
    assert "Credential revoked" in english.text
    assert "Credential rotated" in english.text
    assert "Last used" in english.text
    assert "Never used" in english.text
    assert "Not revoked" in english.text
    assert english.text.count("<time datetime=") >= 7
    assert english.text.count("section:query") == 1
    assert english.text.count("page:read") == 1
    assert english.text.count("archive:write") == 1
    assert "Second &lt;script&gt; section" in english.text
    assert "Second <script> section" not in english.text
    assert "Synthetic &lt;img src=x onerror=alert(1)&gt;" in english.text
    assert "Synthetic <img src=x onerror=alert(1)>" not in english.text
    for secret in (
        "x" * 22,
        "y" * 22,
        "z" * 22,
        "w" * 22,
        "v" * 22,
        "x" * 32,
        "y" * 32,
        "z" * 32,
        "w" * 32,
        "v" * 32,
    ):
        assert secret not in english.text

    chinese = client.get(f"{detail_path}?lang=zh-CN")
    assert chinese.status_code == 200
    assert "现有凭据" in chinese.text
    assert "当前分区授权" in chinese.text
    assert "凭据有效" in chinese.text
    assert "凭据尚未生效" in chinese.text
    assert "凭据已过期" in chinese.text
    assert "凭据已撤销" in chinese.text
    assert "凭据已轮转" in chinese.text
    assert "最后使用时间" in chinese.text
    assert chinese.text.count("<summary>查看凭据元数据</summary>") == len(credential_ids)
    assert chinese.text.count('class="credential-summary"') == len(credential_ids)
    assert "单知识库、分区级授权" in chinese.text
    assert chinese.headers["cache-control"] == "no-store, max-age=0"

    foreign = client.get(f"/admin/libraries/{other_library}/callers/{caller_id}")
    assert foreign.status_code == 404
    assert all(credential_id not in foreign.text for credential_id in credential_ids)
    assert foreign.headers["cache-control"] == "no-store, max-age=0"

    with immediate_transaction(engine) as connection:
        connection.execute(
            update(Caller)
            .where(Caller.library_id == library_id, Caller.id == caller_id)
            .values(disabled_at=3_000_000, updated_at=3_000_000)
        )
    disabled = client.get(f"{detail_path}?lang=en")
    assert "Credential blocked by disabled identity" in disabled.text


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


def test_browser_navigation_landmarks_and_narrow_grid(browser: tuple[TestClient, Engine]) -> None:
    client, engine = browser
    library_id, _, _ = _seed_structure(engine)
    _login(client)

    dashboard = client.get("/admin")
    assert '<nav aria-label="Administration">' in dashboard.text
    assert '<nav class="side-nav" aria-label="Administration sections">' in dashboard.text
    assert '<a href="/admin" aria-current="page">Home</a>' in dashboard.text

    detail = client.get(f"/admin/libraries/{library_id}")
    assert '<nav class="breadcrumb" aria-label="Breadcrumb">' in detail.text
    assert '<span aria-current="page">First Synthetic Library</span></nav>' in detail.text
    assert '<nav class="side-nav" aria-label="Administration sections">' in detail.text

    chinese = client.get(f"/admin/libraries/{library_id}?lang=zh-CN")
    assert '<nav class="breadcrumb" aria-label="当前位置">' in chinese.text
    assert '<nav class="side-nav" aria-label="管理栏目">' in chinese.text
    assert 'href="/admin/libraries"' in chinese.text

    stylesheet = client.get("/admin/style.css")
    assert stylesheet.status_code == 200
    assert "header nav { display: flex;" in stylesheet.text
    assert "\nnav { display: flex;" not in stylesheet.text
    assert "minmax(min(100%, 20rem), 1fr)" in stylesheet.text


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
    assert "Files in this version" in preview.text
    assert "content.md" in preview.text
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
    updated_markdown = b"# Updated\n<script>alert(1)</script>\n"
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
                **MarkdownContent.from_bytes(updated_markdown).model_dump(),
            )
        )
        connection.execute(
            insert(RevisionFile).values(
                library_id=library,
                page_uid=page["page_uid"],
                revision_id=revision_id,
                revision_number=2,
                filename="content.md",
                content_bytes=updated_markdown,
                size_bytes=len(updated_markdown),
                content_sha256=sha256(updated_markdown).digest(),
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
    assert sha256(updated_markdown).hexdigest() in current.text
    assert f'href="{history_path}"' in current.text
    assert '<ul class="item-list revision-files">' in current.text
    assert '<ul class="item-list revision-history">' in current.text
    assert (
        f'aria-current="true"><a href="{page_path}/revisions/2">'
        'Version 2</a> <span class="selected-marker">(Viewing)</span>'
    ) in current.text
    assert current.text.count('aria-current="true"') == 1
    historical = client.get(history_path)
    assert historical.status_code == 200
    assert "# Original" in historical.text
    assert sha256(b"# Original\n").hexdigest() in historical.text
    assert sha256(updated_markdown).hexdigest() not in historical.text
    assert "# Updated" not in historical.text
    assert "Back to current version" in historical.text
    assert (
        f'aria-current="true"><a href="{history_path}">'
        'Version 1</a> <span class="selected-marker">(Viewing)</span>'
    ) in historical.text
    assert historical.text.count('aria-current="true"') == 1
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
    assert "(正在查看)" in chinese.text


def test_browser_reads_mixed_and_binary_file_set_history_without_inline_binary(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine)
    page_id = _insert_page(engine, library, section, book, markdown=b"# Legacy\n")
    _, _, _, page_path = _paths(library, section, book, page_id)
    markdown = b"# Mixed\n<script>alert('text')</script>\n"
    binary = b"\x00<script>alert('binary')</script>\xff"
    _append_file_set_revision(
        engine,
        library,
        page_id,
        number=2,
        marker="3",
        files=(("content.md", markdown), ("payload.bin", binary)),
    )
    _login(client)

    mixed = client.get(page_path)
    assert mixed.status_code == 200
    assert "# Mixed" in mixed.text
    assert "&lt;script&gt;alert(&#x27;text&#x27;)&lt;/script&gt;" in mixed.text
    assert "<script>alert('text')</script>" not in mixed.text
    assert "<script>alert('binary')</script>" not in mixed.text
    assert "content.md" in mixed.text and "payload.bin" in mixed.text
    assert sha256(markdown).hexdigest() in mixed.text
    assert sha256(binary).hexdigest() in mixed.text
    assert f'href="{page_path}/revisions/1"' in mixed.text

    legacy = client.get(f"{page_path}/revisions/1")
    assert legacy.status_code == 200
    assert "# Legacy" in legacy.text
    assert "payload.bin" not in legacy.text

    _append_file_set_revision(
        engine,
        library,
        page_id,
        number=3,
        marker="4",
        files=(("payload.bin", binary),),
    )
    binary_only = client.get(page_path)
    assert binary_only.status_code == 200
    assert "No safe Markdown preview is available for this version." in binary_only.text
    assert '<pre class="markdown-preview">' not in binary_only.text
    assert "payload.bin" in binary_only.text
    assert "content.md" not in binary_only.text
    assert "<script>alert('binary')</script>" not in binary_only.text
    assert sha256(binary).hexdigest() in binary_only.text
    assert "此版本没有可安全预览的 Markdown 正文。" in client.get(page_path + "?lang=zh-CN").text

    historical_mixed = client.get(f"{page_path}/revisions/2")
    assert historical_mixed.status_code == 200
    assert "# Mixed" in historical_mixed.text
    assert "content.md" in historical_mixed.text
    assert "payload.bin" in historical_mixed.text
    assert sha256(markdown).hexdigest() in historical_mixed.text


def test_file_set_markdown_preview_rejects_invalid_or_oversized_bytes(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine)
    page_id = _insert_page(engine, library, section, book)
    _, _, _, page_path = _paths(library, section, book, page_id)
    invalid_utf8 = b"\xff<script>alert('invalid')</script>"
    _append_file_set_revision(
        engine,
        library,
        page_id,
        number=2,
        marker="3",
        files=(("content.md", invalid_utf8),),
    )
    _login(client)
    invalid = client.get(page_path)
    assert invalid.status_code == 200
    assert "No safe Markdown preview is available for this version." in invalid.text
    assert '<pre class="markdown-preview">' not in invalid.text
    assert "<script>alert('invalid')</script>" not in invalid.text
    assert sha256(invalid_utf8).hexdigest() in invalid.text

    oversized = b"x" * (64 * 1024 + 1)
    _append_file_set_revision(
        engine,
        library,
        page_id,
        number=3,
        marker="4",
        files=(("content.md", oversized),),
    )
    large = client.get(page_path)
    assert large.status_code == 200
    assert "No safe Markdown preview is available for this version." in large.text
    assert '<pre class="markdown-preview">' not in large.text
    assert sha256(oversized).hexdigest() in large.text
    assert len(large.content) < 10_000
    assert client.get(f"{page_path}/revisions/2").status_code == 200


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
    tag_id = "9" * 32
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
            "actor_home_library_id": library,
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
                {
                    **base,
                    "id": "0" * 32,
                    "action": "content.archive.correct_occurrence",
                    "outcome": "succeeded",
                    "occurred_at": 5_000_000,
                },
                {
                    **base,
                    "id": "1" * 32,
                    "action": "content.archive.delete",
                    "outcome": "succeeded",
                    "occurred_at": 6_000_000,
                },
                {
                    **base,
                    "id": "2" * 32,
                    "action": "content.archive.restore",
                    "outcome": "succeeded",
                    "occurred_at": 7_000_000,
                },
                {
                    **base,
                    "id": "3" * 32,
                    "action": "tag.create",
                    "resource_type": "tag",
                    "resource_id": tag_id,
                    "outcome": "succeeded",
                    "occurred_at": 8_000_000,
                },
                {
                    **base,
                    "id": "4" * 32,
                    "action": "tag.page.attach",
                    "resource_type": "page_tag",
                    "resource_id": f"{page_id}:{tag_id}",
                    "outcome": "succeeded",
                    "occurred_at": 9_000_000,
                },
                {
                    **base,
                    "id": "5" * 32,
                    "action": "tag.page.detach",
                    "resource_type": "page_tag",
                    "resource_id": f"{page_id}:{tag_id}",
                    "outcome": "succeeded",
                    "occurred_at": 10_000_000,
                },
            ],
        )
        connection.execute(
            insert(Tag),
            {
                "library_id": library,
                "id": tag_id,
                "display_name": "Tag <svg onload=alert(1)>",
                "match_key": "tag <svg onload=alert(1)>",
                "created_at": 8_000_000,
            },
        )

    assert client.get("/admin").status_code == 303
    assert client.get(f"/admin/libraries/{library}/callers/{actor_id}").status_code == 303
    _login(client)
    home = client.get("/admin?lang=zh-CN")
    assert home.status_code == 200
    assert "内容近况" in home.text
    assert home.text.count("更新了页面") == 1
    assert home.text.count("创建了页面") == 1
    assert home.text.count("更正了页面的发生时间") == 1
    assert home.text.count("删除了页面") == 1
    assert home.text.count("恢复了页面") == 1
    assert home.text.count("创建了标签") == 1
    assert home.text.count("关联了标签") == 1
    assert home.text.count("移除了标签") == 1
    assert home.text.index("更新了页面") < home.text.index("创建了页面")
    assert home.text.index("移除了标签") < home.text.index("关联了标签")
    assert f"/admin/libraries/{library}/callers/{actor_id}" in home.text
    assert f"/pages/{page_id}/revisions/2" in home.text
    assert (
        f'恢复了页面 <a href="/admin/libraries/{library}/sections/{section}'
        f'/books/{book}/pages/{page_id}">'
    ) in home.text
    assert (
        f'删除了页面 <a href="/admin/libraries/{library}/sections/{section}'
        f'/books/{book}/pages/{page_id}">'
    ) in home.text
    assert f"/admin/libraries/{library}/tags/{tag_id}" in home.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in home.text
    assert "&lt;svg onload=alert(1)&gt;" in home.text
    assert "<script>alert(1)</script>" not in home.text
    assert "<img src=x onerror=alert(1)>" not in home.text
    assert "<svg onload=alert(1)>" not in home.text
    assert 'title="1970-01-01 00:00:04 UTC"' in home.text
    assert 'title="1970-01-01 00:00:10 UTC"' in home.text
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


def test_home_shows_cross_library_file_set_activity_with_correct_links(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    home_library, _, _ = _seed_structure(engine)
    target_library, section, book = _seed_structure(engine, prefix="4", label="Target")
    actor_id, credential_id = _seed_activity_actor(engine, home_library, "a")
    page_id = _insert_page(engine, target_library, section, book, title="Target page")
    _append_file_set_revision(
        engine,
        target_library,
        page_id,
        number=2,
        marker="c",
        files=(("content.md", b"# Target page\n"), ("figure.png", b"png")),
    )
    revision_id = "rev_" + "c" * 32
    base = {
        "library_id": target_library,
        "actor_home_library_id": home_library,
        "actor_caller_id": actor_id,
        "actor_credential_id": credential_id,
        "outcome": "succeeded",
        "request_id": "synthetic-file-set-request",
    }
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(AuditEvent),
            [
                {
                    **base,
                    "id": "d" * 32,
                    "action": "content.page.file_set.create",
                    "resource_type": "page",
                    "resource_id": page_id,
                    "occurred_at": 3_000_000,
                },
                {
                    **base,
                    "id": "e" * 32,
                    "action": "content.page.file_set.revise",
                    "resource_type": "revision",
                    "resource_id": revision_id,
                    "occurred_at": 4_000_000,
                },
            ],
        )

    activity = AdminReadModel(engine).recent_content_activity()
    assert [item.action for item in activity] == [
        "content.page.file_set.revise",
        "content.page.file_set.create",
    ]
    assert all(item.library_id == target_library for item in activity)
    assert all(item.actor_home_library_id == home_library for item in activity)
    assert [item.revision_number for item in activity] == [2, 1]

    _login(client)
    response = client.get("/admin?lang=zh-CN")
    assert response.status_code == 200
    actor_path = f"/admin/libraries/{home_library}/callers/{actor_id}"
    wrong_actor_path = f"/admin/libraries/{target_library}/callers/{actor_id}"
    page_path = f"/admin/libraries/{target_library}/sections/{section}/books/{book}/pages/{page_id}"
    assert response.text.count(f'href="{actor_path}"') == 2
    assert wrong_actor_path not in response.text
    assert f'href="{page_path}/revisions/1"' in response.text
    assert f'href="{page_path}/revisions/2"' in response.text
    assert response.text.count("创建了页面") == 1
    assert response.text.count("更新了页面") == 1
    assert client.get(actor_path).status_code == 200
    assert client.get(f"{page_path}/revisions/1").status_code == 200
    assert client.get(f"{page_path}/revisions/2").status_code == 200


def test_deleted_activity_links_to_trash_without_cross_library_preview(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book = _seed_structure(engine)
    other_library, other_section, other_book = _seed_structure(engine, prefix="4", label="Other")
    page_id = _insert_page(engine, library, section, book, title="Deleted local page")
    foreign_id = _insert_page(
        engine, other_library, other_section, other_book, title="Foreign secret title"
    )
    caller_id, credential_id = _seed_activity_actor(engine, library, "a")
    with immediate_transaction(engine) as connection:
        connection.execute(
            update(Page)
            .where(Page.library_id == library, Page.page_id == page_id)
            .values(deleted_at=5_000_000, updated_at=5_000_000)
        )
        base = {
            "library_id": library,
            "actor_home_library_id": library,
            "actor_caller_id": caller_id,
            "actor_credential_id": credential_id,
            "resource_type": "page",
            "outcome": "succeeded",
            "request_id": "synthetic-request",
        }
        connection.execute(
            insert(AuditEvent),
            [
                {
                    **base,
                    "id": "1" * 32,
                    "action": "content.archive.delete",
                    "resource_id": page_id,
                    "occurred_at": 5_000_000,
                },
                {
                    **base,
                    "id": "2" * 32,
                    "action": "content.archive.create",
                    "resource_id": foreign_id,
                    "occurred_at": 6_000_000,
                },
                {
                    **base,
                    "id": "3" * 32,
                    "action": "tag.page.attach",
                    "resource_type": "page_tag",
                    "resource_id": "malformed",
                    "occurred_at": 7_000_000,
                },
            ],
        )

    _login(client)
    home = client.get("/admin?lang=zh-CN")
    assert home.status_code == 200
    assert f"/admin/libraries/{library}/sections/{section}/trash/{page_id}" in home.text
    assert f"/books/{book}/pages/{page_id}" not in home.text
    assert "Foreign secret title" not in home.text
    assert f"/pages/{foreign_id}" not in home.text
    assert "页面目前不可预览" in home.text
    assert "标签目前不可查看" in home.text
    assert home.headers["cache-control"] == "no-store, max-age=0"


def test_activity_limit_and_tie_breaker_are_stable(browser: tuple[TestClient, Engine]) -> None:
    client, engine = browser
    library, _, _ = _seed_structure(engine)
    caller_id, credential_id = _seed_activity_actor(engine, library, "a")
    tag_id = "9" * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Tag),
            {
                "library_id": library,
                "id": tag_id,
                "display_name": "Synthetic tag",
                "match_key": "synthetic tag",
                "created_at": 1,
            },
        )
        connection.execute(
            insert(AuditEvent),
            [
                {
                    "id": f"{index:032x}",
                    "library_id": library,
                    "actor_home_library_id": library,
                    "actor_caller_id": caller_id,
                    "actor_credential_id": credential_id,
                    "action": "tag.create",
                    "resource_type": "tag",
                    "resource_id": tag_id,
                    "outcome": "succeeded",
                    "request_id": "synthetic-request",
                    "occurred_at": index,
                }
                for index in range(1, 52)
            ],
        )
        connection.execute(
            insert(AuditEvent),
            {
                "id": "f" * 32,
                "library_id": library,
                "actor_home_library_id": library,
                "actor_caller_id": caller_id,
                "actor_credential_id": credential_id,
                "action": "content.archive.correct_occurrence",
                "resource_type": "page",
                "resource_id": "missing-page",
                "outcome": "succeeded",
                "request_id": "synthetic-request",
                "occurred_at": 51,
            },
        )
    model = AdminReadModel(engine)
    assert len(model.recent_content_activity()) == 50
    newest = model.recent_content_activity(limit=2)
    assert [item.action for item in newest] == [
        "content.archive.correct_occurrence",
        "tag.create",
    ]
    assert [item.occurred_at for item in newest] == [51, 51]
    with pytest.raises(ValueError, match="between 1 and 50"):
        model.recent_content_activity(limit=0)
    with pytest.raises(ValueError, match="between 1 and 50"):
        model.recent_content_activity(limit=51)
    _login(client)
    home = client.get("/admin?lang=en")
    assert home.status_code == 200
    assert home.text.count("Created a tag") == 49
    assert "Corrected a page's occurrence time" in home.text
