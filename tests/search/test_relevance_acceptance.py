"""Small Page relevance check against the real search service.

The synthetic expectations are declared independently of the ranker's score
formula. They are not human/user, representative-corpus, or latency acceptance.
All mutations finish before rebuild; only the resulting current snapshot is
checked, not the ordering or atomicity of online updates.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import seed_library_structure
from sqlalchemy import Engine
from test_service_v2 import (
    CALLER,
    CLOCK,
    TAG_A,
    TAG_B,
    _agent,
    _grant,
    _opt_in,
    _page,
    _search,
)

from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.tags.repository import TagRepository


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'relevance.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    value = build_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


def test_synthetic_mixed_page_relevance_and_filter_boundaries(engine: Engine) -> None:
    home = seed_library_structure(engine)
    visible = seed_library_structure(engine, prefix="4", label="Visible")
    hidden = seed_library_structure(engine, prefix="7", label="Hidden")
    context = _agent(engine, home[0], home[1])
    _opt_in(engine, home[0])
    _grant(engine, home[0], home[0], "read")
    _grant(engine, home[0], visible[0], "read")

    title = _page(
        engine,
        home,
        page_byte=0x21,
        title="月球🙂! orbital 轨道报告",
        content=b"neutral",
        occurrence="2026-08-13T10:00:03Z",
    )
    filename = _page(
        engine,
        home,
        page_byte=0x22,
        title="文件清单",
        content=b"old-version-only",
        occurrence="2026-08-13T10:00:04Z",
    )
    body = _page(
        engine,
        home,
        page_byte=0x23,
        title="观测摘要",
        content=b"old-version-only",
        occurrence="2026-08-13T10:00:05Z",
    )
    binary = _page(
        engine,
        home,
        page_byte=0x24,
        title="原始数据",
        content=b"old-version-only",
        occurrence="2026-08-13T10:00:06Z",
    )
    deleted = _page(
        engine,
        home,
        page_byte=0x25,
        title="月球🙂! orbital 已删除",
        content=b"neutral",
        occurrence="2026-08-13T10:00:07Z",
    )
    tagged_visible = _page(
        engine,
        visible,
        page_byte=0x26,
        title="跨库标签资料",
        content=b"neutral",
        occurrence="2026-08-13T10:00:08Z",
    )
    unseen = _page(
        engine,
        hidden,
        page_byte=0x27,
        title="月球🙂! orbital 月球🙂! orbital",
        content=b"neutral",
        occurrence="2026-08-13T10:00:09Z",
    )

    # The current file group replaces the legacy Markdown Revision. A binary
    # payload happens to contain "orbital" bytes, which must not be parsed.
    groups = (
        (filename, (("月球🙂!-orbital.md", b"neutral"),)),
        (body, (("part-a.md", "月球🙂! orbital".encode()), ("part-b.txt", b"neutral"))),
        (binary, (("月球🙂!-camera.bin", b"\xff\xfe orbital hidden text"),)),
    )
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        for index, (record, files) in enumerate(groups, start=1):
            page = repository.get_page(home[0], record[1])
            assert page is not None
            revision_id = f"rev_{index:032x}"
            repository.add_file_set_revision(
                page,
                revision_id=revision_id,
                created_at=CLOCK,
                manifest=build_file_manifest(files),
            )
            assert (
                repository.advance_file_set_current_revision(
                    page, revision_id=revision_id, updated_at=CLOCK
                )
                is not None
            )
        doomed = repository.get_page(home[0], deleted[1])
        assert doomed is not None
        repository.transition_page_lifecycle(
            doomed,
            action="delete",
            actor_caller_id=CALLER,
            actor_home_library_id=home[0],
            request_id="req_" + "a" * 32,
            changed_at=CLOCK,
        )
        tags = TagRepository(connection)
        for library_id, tag_id, page_uid in (
            (home[0], TAG_A, title[0]),
            (visible[0], TAG_B, tagged_visible[0]),
        ):
            tags.add_tag(library_id=library_id, tag_id=tag_id, name="合成标签", created_at=0)
            tags.attach_page(library_id=library_id, page_uid=page_uid, tag_id=tag_id, created_at=0)
    rebuild_search_index(engine, clock=lambda: CLOCK)

    # Independently declared synthetic expectation: a phrase in the title is best, then
    # the same two ideas in a filename, then in body text; the binary Page
    # matches only the Chinese/punctuation phrase in its filename.
    ranked = _search(engine, context, keywords=["月球🙂!", "orbital"])
    assert [item.page_id for item in ranked] == [title[1], filename[1], body[1], binary[1]]
    assert [item.revision_number for item in ranked] == [1, 2, 2, 2]
    assert unseen[1] not in {item.page_id for item in ranked}
    assert deleted[1] not in {item.page_id for item in ranked}
    punctuation = _search(engine, context, keywords=["🙂!"])
    assert {item.page_id for item in punctuation} == {title[1], filename[1], body[1], binary[1]}
    assert punctuation[0].page_id == title[1]
    assert [item.page_id for item in _search(engine, context, keywords=["orbital"])] == [
        title[1],
        filename[1],
        body[1],
    ]
    assert _search(engine, context, keywords=["old-version-only"]) == ()
    assert _search(engine, context, keywords=["hidden text"]) == ()

    # Tag OR spans two Libraries with distinct IDs; this does not test equal
    # raw Tag IDs across Libraries. [from, before) uses declared occurrence time.
    tags_any = [
        {"library_id": home[0], "tag_id": TAG_A},
        {"library_id": visible[0], "tag_id": TAG_B},
    ]
    assert [item.page_id for item in _search(engine, context, tags_any=tags_any)] == [
        tagged_visible[1],
        title[1],
    ]
    assert [
        item.page_id
        for item in _search(
            engine,
            context,
            tags_any=tags_any,
            occurred_from_us=title[2],
            occurred_before_us=tagged_visible[2],
        )
    ] == [title[1]]
