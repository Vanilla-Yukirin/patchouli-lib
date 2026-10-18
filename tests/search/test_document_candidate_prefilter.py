"""Document-level FTS prefilter equivalence against independent literal fields.

Membership/sources use a full fixture scan, never candidate terms or scores.
The unfiltered SQL control additionally checks unchanged ranking, excerpts and
signed cursors; it is not used as the membership oracle. No timing threshold
or representative-corpus/performance acceptance is claimed here.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import seed_library_structure
from sqlalchemy import Connection, Engine
from test_service_v2 import CALLER, CLOCK, TAG_A, _agent, _grant, _opt_in

from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_create_core import FileSetPageCreateCore
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveSourceInput
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.retrieval.cursor import CursorCodec
from patchouli_lib.search import service_v2
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.search.literal_v2 import candidate_match_expression
from patchouli_lib.search.query_v2 import SearchQueryV2, TagIdentityV2
from patchouli_lib.search.service_v2 import SearchResultV2, SearchScopeError, search_pages_v2
from patchouli_lib.tags.repository import TagRepository

CODEC = CursorCodec(b"synthetic-document-prefilter-key-0001")
BASE_TIME = 1_700_000_000_000_000
OR_TERMS = tuple(f"needle-long-{number:02d}" for number in range(16))


@dataclass(frozen=True, slots=True)
class PageFixture:
    library_id: str
    page_id: str
    title: str
    files: tuple[tuple[str, bytes], ...]
    occurred_at: int
    visible: bool
    tagged: bool
    deleted: bool


@pytest.fixture(scope="module")
def corpus(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Engine, AuthenticatedRequestContext, tuple[PageFixture, ...]]]:
    directory = tmp_path_factory.mktemp("document-candidate-prefilter")
    database_url = f"sqlite:///{(directory / 'prefilter.sqlite').as_posix()}"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("PATCHOULI_DATABASE_URL", database_url)
        environment.setenv("PATCHOULI_ENVIRONMENT", "test")
        command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        home = seed_library_structure(engine)
        visible = seed_library_structure(engine, prefix="4", label="Visible")
        hidden = seed_library_structure(engine, prefix="7", label="Hidden")
        context = _agent(engine, home[0], home[1])
        _opt_in(engine, home[0])
        for ids in (home, visible):
            _grant(engine, home[0], ids[0], "read")
        _grant(engine, home[0], hidden[0], "write")  # WRITE is not READ.
        cases = (
            (
                home,
                "amber",
                (("indigo.bin", b"\xff opaque-body-only a\x00b"), ("part.md", b"amber")),
            ),
            (home, "Filename", (("amber.bin", b"\xff opaque-body-only"), ("part.md", b"indigo"))),
            (visible, "Texts", (("part.md", b"amber"), ("second.txt", b"indigo"))),
            (home, "Prefix only", (("part.md", b"amb wrong continuation; indi no full word"),)),
            (visible, "Split", (("part.md", b"abc"), ("second.txt", b"def"))),
            (home, "Straße 技术", (("part.md", "CAFÉ 🙂! 海洋研究备忘录".encode()),)),
            (
                visible,
                "Exact elsewhere",
                (
                    ("part.md", "海洋研 wrong suffix".encode()),
                    ("second.txt", "海洋研究备忘录".encode()),
                ),
            ),
            (home, "Old revision", (("part.md", b"current-only"),)),
            (home, "Deleted amber", (("part.md", b"amber indigo"),)),
            (
                hidden,
                "amber indigo Straße 技术",
                (("part.md", "CAFÉ 🙂! 海洋研究备忘录".encode()),),
            ),
        )
        records = []
        with immediate_transaction(engine) as connection:
            repository = ContentRepository(connection)
            tags = TagRepository(connection)
            for ids in (home, visible, hidden):
                tags.add_tag(library_id=ids[0], tag_id=TAG_A, name="fixture tag", created_at=0)
            for number, (ids, title, original_files) in enumerate(cases):
                # One long OR term in a text field; unrelated large fields are
                # deliberately present to expose Page-level scan amplification.
                files = tuple(
                    (
                        name,
                        content + (f" {OR_TERMS[number]}".encode() if name == "part.md" else b""),
                    )
                    for name, content in original_files
                )
                files += (("unrelated.txt", b"neutral content " * 600),)
                uid = (number + 1).to_bytes(16, "big")
                revision_id = f"rev_{number + 1:032x}"

                def source_id(number: int = number) -> str:
                    return f"{number + 1:032x}"

                def page_uid(uid: bytes = uid) -> bytes:
                    return uid

                def revision_identity(revision_id: str = revision_id) -> str:
                    return revision_id

                book = repository.get_book(ids[0], ids[2])
                assert book is not None
                is_history = number == 7
                page = FileSetPageCreateCore(
                    connection,
                    id_factory=source_id,
                    page_uid_factory=page_uid,
                    revision_id_factory=revision_identity,
                ).create_page(
                    book=book,
                    title=title,
                    occurred_at=BASE_TIME + number,
                    operation_at=2_000_000,
                    manifest=build_file_manifest(
                        (("old.md", b"legacy-only-needle"),) if is_history else files
                    ),
                    source=ArchiveSourceInput(kind="synthetic"),
                )
                if is_history:
                    new_id = "rev_" + "f" * 32
                    repository.add_file_set_revision(
                        page,
                        revision_id=new_id,
                        created_at=CLOCK,
                        manifest=build_file_manifest(files),
                    )
                    assert (
                        repository.advance_file_set_current_revision(
                            page, revision_id=new_id, updated_at=CLOCK
                        )
                        is not None
                    )
                tagged = number % 2 == 0
                if tagged:
                    tags.attach_page(library_id=ids[0], page_uid=uid, tag_id=TAG_A, created_at=0)
                deleted = number == 8
                if deleted:
                    assert (
                        repository.transition_page_lifecycle(
                            page,
                            action="delete",
                            actor_caller_id=CALLER,
                            actor_home_library_id=home[0],
                            request_id="req_" + "a" * 32,
                            changed_at=CLOCK,
                        )
                        is not None
                    )
                records.append(
                    PageFixture(
                        ids[0],
                        page.page_id,
                        title,
                        files,
                        BASE_TIME + number,
                        ids != hidden,
                        tagged,
                        deleted,
                    )
                )
        rebuild_search_index(engine, clock=lambda: CLOCK)
        yield engine, context, tuple(records)
    finally:
        engine.dispose()


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _literal_oracle(
    pages: tuple[PageFixture, ...],
    query: SearchQueryV2,
) -> dict[str, set[tuple[str, str | None]]]:
    needles = {_normalize(keyword) for keyword in query.keywords}
    expected: dict[str, set[tuple[str, str | None]]] = {}
    for page in pages:
        if not page.visible or page.deleted:
            continue
        if query.libraries is not None and page.library_id not in query.libraries:
            continue
        if query.occurred_from_us is not None and page.occurred_at < query.occurred_from_us:
            continue
        if query.occurred_before_us is not None and page.occurred_at >= query.occurred_before_us:
            continue
        if query.tags_any and not (
            page.tagged
            and any(
                tag.library_id == page.library_id and tag.tag_id == TAG_A for tag in query.tags_any
            )
        ):
            continue
        fields: list[tuple[str, str | None, str]] = [("title", None, page.title)]
        for name, content in page.files:
            fields.append(("file_name", name, name))
            if name.endswith((".md", ".txt")):
                fields.append(("file_text", name, content.decode("utf-8")))
        sources = {
            (kind, name)
            for kind, name, value in fields
            if any(needle in _normalize(value) for needle in needles)
        }
        if sources or not needles:
            expected[page.page_id] = sources
    return expected


def _query(keywords: tuple[str, ...]) -> SearchQueryV2:
    return SearchQueryV2(keywords, (), None, None, None, 100)


def _search(
    engine: Engine,
    context: AuthenticatedRequestContext,
    query: SearchQueryV2,
) -> SearchResultV2:
    return search_pages_v2(engine, context, query, clock=lambda: CLOCK, cursor_codec=CODEC)


def _unfiltered_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service_v2._rows_for_scope

    def scan(
        connection: Connection,
        generation: int,
        scope: service_v2._Scope,
        caller_id: str,
        query: SearchQueryV2,
        needles: tuple[str, ...],
        candidate_expression: str | None,
    ) -> list[tuple[object, ...]]:
        assert not (len(needles) == 1 and len(needles[0]) <= 3)
        return original(connection, generation, scope, caller_id, query, needles, None)

    monkeypatch.setattr(service_v2, "_rows_for_scope", scan)


@pytest.mark.parametrize(
    "keywords",
    [
        OR_TERMS,
        ("amber", "indigo"),
        ("amber", "AMBER", "indigo", "INDIGO"),
        ("技术", "CAFÉ", "Cafe\u0301", "ß", "🙂!", "海洋研究备忘录", "a\x00b"),
        ("abcdef",),
        ("ambiguous", "indispensable"),
        ("never-present",),
        ("legacy-only-needle",),
        ("opaque-body-only",),
    ],
    ids=[
        "wide-or",
        "cross-fields",
        "duplicates",
        "unicode-short-long",
        "split-files",
        "prefix-only",
        "no-hit",
        "old-revision",
        "binary-body",
    ],
)
def test_literal_oracle_and_unfiltered_reader_agree(
    corpus: tuple[Engine, AuthenticatedRequestContext, tuple[PageFixture, ...]],
    monkeypatch: pytest.MonkeyPatch,
    keywords: tuple[str, ...],
) -> None:
    engine, context, pages = corpus
    query = _query(keywords)
    actual = _search(engine, context, query)
    expected = _literal_oracle(pages, query)
    assert {
        item.page_id: {(source.kind, source.file_name) for source in item.match_sources}
        for item in actual.items
    } == expected
    if keywords == ("amber", "indigo"):
        # Independent field-quality judgment: title+name, name+text, then text+text.
        assert [item.page_id for item in actual.items] == [page.page_id for page in pages[:3]]
    with monkeypatch.context() as full_scan:
        _unfiltered_rows(full_scan)
        assert _search(engine, context, query) == actual


def test_tag_time_top_k_and_cursor_chain_match_full_scan(
    corpus: tuple[Engine, AuthenticatedRequestContext, tuple[PageFixture, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, context, pages = corpus
    tags = tuple(
        TagIdentityV2(library_id=library, tag_id=TAG_A)
        for library in sorted({page.library_id for page in pages if page.visible})
    )
    query = replace(
        _query(OR_TERMS),
        tags_any=tags,
        occurred_from_us=BASE_TIME + 2,
        occurred_before_us=BASE_TIME + 7,
        limit=1,
    )
    expected = _literal_oracle(pages, query)
    collected: list[str] = []
    while True:
        actual = _search(engine, context, query)
        with monkeypatch.context() as full_scan:
            _unfiltered_rows(full_scan)
            assert _search(engine, context, query) == actual  # Includes exact signed cursor.
        collected.extend(item.page_id for item in actual.items)
        if actual.next_cursor is None:
            break
        assert len(collected) <= len(expected)
        query = replace(query, cursor=actual.next_cursor)
    assert collected == [page.page_id for page in reversed(pages) if page.page_id in expected]
    assert len(collected) == len(set(collected))
    filter_only = replace(query, keywords=(), cursor=None, limit=100)
    filtered = _search(engine, context, filter_only)
    assert {item.page_id for item in filtered.items} == set(_literal_oracle(pages, filter_only))
    with pytest.raises(SearchScopeError):
        _search(engine, context, replace(query, cursor=None, libraries=(pages[-1].library_id,)))


def test_document_prefilter_avoids_unrelated_fields(
    corpus: tuple[Engine, AuthenticatedRequestContext, tuple[PageFixture, ...]],
) -> None:
    engine, context, pages = corpus
    query = _query(OR_TERMS)
    with engine.connect() as connection:
        generation = connection.exec_driver_sql(
            "SELECT active_generation FROM search_meta"
        ).scalar_one()
        for library_id in sorted({page.library_id for page in pages if page.visible}):
            scope = service_v2._Scope(library_id, False)
            rows = service_v2._rows_for_scope(
                connection,
                generation,
                scope,
                CALLER,
                query,
                OR_TERMS,
                candidate_match_expression(OR_TERMS),
            )
            unfiltered = service_v2._rows_for_scope(
                connection, generation, scope, CALLER, query, OR_TERMS, None
            )
            # Exactly one text field contains a candidate gram on each live Page.
            live = [page for page in pages if page.library_id == library_id and not page.deleted]
            assert len(rows) == len(live)
            assert len(rows) < len(unfiltered) / 3
    assert set(_literal_oracle(pages, query)) == {
        item.page_id for item in _search(engine, context, query).items
    }
