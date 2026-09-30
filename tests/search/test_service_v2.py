"""Synthetic SQLite checks for the current-Page search read boundary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import Engine, insert

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuthenticatedCaller,
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
    credential_metadata,
)
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.models import Book, Section
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError, rebuild_search_index
from patchouli_lib.search.query_v2 import SearchQueryV2, parse_query_v2_json
from patchouli_lib.search.service_v2 import (
    SearchPageV2,
    SearchScopeError,
    search_pages_for_master,
    search_pages_v2,
)
from patchouli_lib.tags.repository import TagRepository

CALLER = "d" * 32
CREDENTIAL = "e" * 32
TAG_A = "a" * 32
TAG_B = "b" * 32
CLOCK = 3_000_000


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'reader.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    value = build_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


def _query(**fields: object) -> SearchQueryV2:
    import json

    return parse_query_v2_json(json.dumps(fields).encode())


def _agent(engine: Engine, library_id: str, section_id: str) -> AuthenticatedRequestContext:
    token = generate_token()
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.add_caller(
            NewCaller(
                id=CALLER,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic search agent",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        credential = repository.add_credential(
            NewCredential(
                id=CREDENTIAL,
                library_id=library_id,
                caller_id=CALLER,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_grant(
            NewSectionGrant(
                library_id=library_id,
                caller_id=CALLER,
                section_id=section_id,
                action=SectionAction.PAGE_READ,
                created_at=1_000_000,
            )
        )
    return AuthenticatedRequestContext(
        authenticated=AuthenticatedCaller(
            caller=caller, credential=credential_metadata(credential)
        ),
        grants=(),
    )


def _page(
    engine: Engine,
    ids: tuple[str, str, str],
    *,
    page_byte: int,
    title: str,
    content: bytes,
    occurrence: str = "2026-08-13T10:00:00.123456Z",
) -> tuple[bytes, str, int]:
    library_id, section_id, book_id = ids
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        page_byte=page_byte,
        revision_hex=f"{page_byte:02x}",
        source_hex=f"{page_byte:x}"[-1],
        title=title,
        occurrence_wire=occurrence,
        content_md=content,
    )
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    return values[0].page_uid, values[0].page_id, values[0].occurred_at


def _opt_in(engine: Engine, home: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": CREDENTIAL,
                "caller_id": CALLER,
                "home_library_id": home,
                "mode": "library_grants",
                "created_at": CLOCK,
            },
        )


def _grant(engine: Engine, home: str, target: str, action: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": CREDENTIAL,
                "caller_id": CALLER,
                "home_library_id": home,
                "target_library_id": target,
                "action": action,
                "created_at": CLOCK,
            },
        )


def _search(
    engine: Engine, context: AuthenticatedRequestContext, **fields: object
) -> tuple[SearchPageV2, ...]:
    return search_pages_v2(engine, context, _query(**fields), clock=lambda: CLOCK).items


def test_master_search_uses_signed_current_session_and_all_libraries(engine: Engine) -> None:
    home = seed_library_structure(engine)
    other = seed_library_structure(engine, prefix="4", label="Other")
    local = _page(engine, home, page_byte=0x11, title="needle local", content=b"needle")
    remote = _page(engine, other, page_byte=0x12, title="needle remote", content=b"needle")
    rebuild_search_index(engine, clock=lambda: CLOCK)
    with immediate_transaction(engine) as connection:
        state = MasterTokenRepository(connection).initialize_from_local_cli(
            "synthetic master search token material 0001", now=1_000
        )
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=3_600, clock=lambda: 1_000)
    cookie, _session = codec.issue_master(state.identity_id, state.session_generation)
    query = _query(keywords=["needle"])

    assert {
        item.page_id for item in search_pages_for_master(engine, cookie, codec, query).items
    } == {
        local[1],
        remote[1],
    }
    assert [
        item.page_id
        for item in search_pages_for_master(
            engine, cookie, codec, _query(keywords=["needle"], libraries=[other[0]])
        ).items
    ] == [remote[1]]
    with pytest.raises(SearchScopeError):
        search_pages_for_master(
            engine, cookie, codec, _query(keywords=["needle"], libraries=["f" * 32])
        )
    with pytest.raises(AuthenticationError):
        search_pages_for_master(engine, cookie + "x", codec, query)
    legacy_cookie, _legacy_session = codec.issue()
    with pytest.raises(AuthenticationError):
        search_pages_for_master(engine, legacy_cookie, codec, query)
    with immediate_transaction(engine) as connection:
        rotated = MasterTokenRepository(connection).rotate(
            "synthetic master search token material 0001",
            "synthetic master search token material 0002",
            now=1_001,
        )
        assert rotated is not None
    with pytest.raises(AuthenticationError):
        search_pages_for_master(engine, cookie, codec, query)


def test_legacy_section_scope_and_current_grant_revocation(engine: Engine) -> None:
    home = seed_library_structure(engine)
    other = seed_library_structure(engine, prefix="4", label="Other")
    context = _agent(engine, home[0], home[1])
    local = _page(engine, home, page_byte=0x11, title="中文 文件", content=b"needle local")
    hidden_section = (home[0], "7" * 32, "8" * 32)
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Section),
            {
                "id": hidden_section[1],
                "library_id": home[0],
                "name": "Hidden section",
                "description": "",
                "created_at": 1_000_000,
                "updated_at": 1_000_000,
            },
        )
        connection.execute(
            insert(Book),
            {
                "id": hidden_section[2],
                "library_id": home[0],
                "section_id": hidden_section[1],
                "name": "Hidden book",
                "summary": "",
                "created_at": 1_000_000,
                "updated_at": 1_000_000,
            },
        )
    _page(engine, hidden_section, page_byte=0x13, title="needle private", content=b"needle")
    _page(engine, other, page_byte=0x12, title="needle secret", content=b"needle hidden")
    rebuild_search_index(engine, clock=lambda: CLOCK)

    assert [item.page_id for item in _search(engine, context, keywords=["needle"])] == [local[1]]
    with pytest.raises(SearchScopeError) as denied:
        _search(engine, context, keywords=["needle"], libraries=[other[0]])
    with pytest.raises(SearchScopeError) as unknown:
        _search(engine, context, keywords=["needle"], libraries=["f" * 32])
    assert str(denied.value) == str(unknown.value)
    with immediate_transaction(engine) as connection:
        assert AuthRepository(connection).remove_grant(
            home[0], CALLER, home[1], SectionAction.PAGE_READ
        )
    assert _search(engine, context, keywords=["needle"]) == ()
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        stored = repository.get_credential(home[0], CALLER, CREDENTIAL)
        assert stored is not None
        repository.revoke_credential(stored, revoked_at=CLOCK)
    with pytest.raises(AuthenticationError):
        _search(engine, context, keywords=["needle"])


def test_library_read_is_explicit_and_cross_library_results_are_stable(engine: Engine) -> None:
    home = seed_library_structure(engine)
    other = seed_library_structure(engine, prefix="4", label="Other")
    context = _agent(engine, home[0], home[1])
    local = _page(engine, home, page_byte=0x11, title="中文 文件", content=b"needle local")
    remote = _page(engine, other, page_byte=0x12, title="needle remote", content=b"needle")
    rebuild_search_index(engine, clock=lambda: CLOCK)
    _opt_in(engine, home[0])
    _grant(engine, home[0], home[0], "read")
    _grant(engine, home[0], other[0], "write")

    assert [item.page_id for item in _search(engine, context, keywords=["needle"])] == [local[1]]
    with pytest.raises(SearchScopeError):
        _search(engine, context, keywords=["needle"], libraries=[other[0]])
    _grant(engine, home[0], other[0], "read")
    assert [item.page_id for item in _search(engine, context, keywords=["needle"])] == [
        remote[1],
        local[1],
    ]


def test_short_literal_matches_exactly_within_its_own_field(engine: Engine) -> None:
    home = seed_library_structure(engine)
    context = _agent(engine, home[0], home[1])
    title_hit = _page(engine, home, page_byte=0x11, title="技术笔记", content=b"plain")
    body_hit = _page(engine, home, page_byte=0x12, title="Other", content="这里有技术内容".encode())
    _page(engine, home, page_byte=0x13, title="技", content="术分开".encode())
    folded_hit = _page(engine, home, page_byte=0x14, title="Straße", content=b"plain")
    rebuild_search_index(engine, clock=lambda: CLOCK)

    hits = _search(engine, context, keywords=["技术"])
    assert [item.page_id for item in hits] == [title_hit[1], body_hit[1]]
    assert [source.kind for source in hits[0].match_sources] == ["title"]
    assert [source.kind for source in hits[1].match_sources] == ["file_text"]
    combined = _search(engine, context, keywords=["技术", "笔记"])
    assert [item.page_id for item in combined] == [title_hit[1], body_hit[1]]
    assert [source.kind for source in combined[0].match_sources] == ["title"]
    assert [source.kind for source in combined[1].match_sources] == ["file_text"]
    assert [item.page_id for item in _search(engine, context, keywords=["ß"])] == [folded_hit[1]]


def test_page_top_k_uses_stable_tie_breaker_without_candidate_truncation(engine: Engine) -> None:
    home = seed_library_structure(engine)
    context = _agent(engine, home[0], home[1])
    pages = [
        _page(engine, home, page_byte=0x11, title="needle zeta", content=b"plain"),
        _page(engine, home, page_byte=0x12, title="needle alpha", content=b"plain"),
        _page(engine, home, page_byte=0x13, title="needle beta", content=b"plain"),
    ]
    rebuild_search_index(engine, clock=lambda: CLOCK)

    all_hits = _search(engine, context, keywords=["needle"], limit=3)
    top_two = _search(engine, context, keywords=["needle"], limit=2)
    assert [item.page_id for item in all_hits] == sorted(page[1] for page in pages)
    assert [item.page_id for item in top_two] == [item.page_id for item in all_hits[:2]]
    assert len({item.page_id for item in top_two}) == 2
    assert all(item.revision_number == 1 for item in top_two)


def test_tag_time_unicode_binary_name_and_old_revision(engine: Engine) -> None:
    home = seed_library_structure(engine)
    context = _agent(engine, home[0], home[1])
    early = _page(
        engine,
        home,
        page_byte=0x11,
        title="Straße 中文",
        content=b"old revision only",
        occurrence="2026-08-13T10:00:00.123456Z",
    )
    later = _page(
        engine,
        home,
        page_byte=0x12,
        title="Opaque note",
        content=b"old only",
        occurrence="2026-08-14T10:00:00.123456Z",
    )
    manifest = build_file_manifest((("secret.bin", b"\xff\xfe hidden-byte-only"),))
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(home[0], later[1])
        assert page is not None
        repository.add_file_set_revision(
            page, revision_id=f"rev_{'c' * 32}", created_at=3_000_000, manifest=manifest
        )
        assert (
            repository.advance_file_set_current_revision(
                page, revision_id=f"rev_{'c' * 32}", updated_at=3_000_000
            )
            is not None
        )
        tags = TagRepository(connection)
        for tag_id in (TAG_A, TAG_B):
            tags.add_tag(library_id=home[0], tag_id=tag_id, name=tag_id, created_at=0)
        tags.attach_page(library_id=home[0], page_uid=early[0], tag_id=TAG_A, created_at=0)
        tags.attach_page(library_id=home[0], page_uid=later[0], tag_id=TAG_B, created_at=0)
    rebuild_search_index(engine, clock=lambda: CLOCK)

    assert [item.page_id for item in _search(engine, context, keywords=["STRASSE", "中文"])] == [
        early[1]
    ]
    hit = _search(engine, context, keywords=["secret.bin"])
    assert [item.page_id for item in hit] == [later[1]]
    assert hit[0].revision_id == f"rev_{'c' * 32}"
    assert hit[0].revision_number == 2
    assert [source.kind for source in hit[0].match_sources] == ["file_name"]
    assert _search(engine, context, keywords=["hidden-byte-only"]) == ()
    assert _search(engine, context, keywords=["old only"]) == ()
    requested_tags = [{"library_id": home[0], "tag_id": TAG_A}]
    assert [item.page_id for item in _search(engine, context, tags_any=requested_tags)] == [
        early[1]
    ]
    assert [item.page_id for item in _search(engine, context, occurred_from_us=later[2])] == [
        later[1]
    ]
    assert [item.page_id for item in _search(engine, context, occurred_before_us=later[2])] == [
        early[1]
    ]
    with pytest.raises(SearchScopeError):
        _search(engine, context, tags_any=[{"library_id": home[0], "tag_id": "f" * 32}])


def test_dirty_and_incomplete_selected_scope_fail_closed(engine: Engine) -> None:
    home = seed_library_structure(engine)
    context = _agent(engine, home[0], home[1])
    first = _page(engine, home, page_byte=0x11, title="Needle", content=b"text")
    generation = rebuild_search_index(engine, clock=lambda: CLOCK)
    assert len(_search(engine, context, keywords=["needle"])) == 1
    with engine.begin() as connection:
        tags = TagRepository(connection)
        tags.add_tag(library_id=home[0], tag_id=TAG_A, name="Synthetic", created_at=0)
        tags.attach_page(library_id=home[0], page_uid=first[0], tag_id=TAG_A, created_at=0)
    with pytest.raises(SearchIndexUnavailableError):
        _search(engine, context, keywords=["needle"])
    rebuild_search_index(engine, clock=lambda: CLOCK)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM search_terms WHERE rowid IN (SELECT id FROM search_documents "
            "WHERE generation = ? AND library_id = ? AND page_uid = ?)",
            (generation + 1, home[0], first[0]),
        )
    with pytest.raises(SearchIndexUnavailableError):
        _search(engine, context, keywords=["needle"])


def test_unready_generation_fails_closed(engine: Engine) -> None:
    home = seed_library_structure(engine)
    context = _agent(engine, home[0], home[1])
    with pytest.raises(SearchIndexUnavailableError):
        _search(engine, context, keywords=["nothing"])
