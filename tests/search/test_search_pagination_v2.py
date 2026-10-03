"""Synthetic, authorization-scoped search cursor and plain-text excerpt checks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import Engine

from patchouli_lib.api.authentication import AuthenticatedRequestContext
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
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.retrieval.cursor import CursorCodec, InvalidCursorError
from patchouli_lib.search.index_v2 import (
    dirty_sequence_at_transaction_start,
    flush_dirty_since,
    rebuild_search_index,
)
from patchouli_lib.search.query_v2 import InvalidSearchQueryV2, SearchQueryV2, parse_query_v2_json
from patchouli_lib.search.service_v2 import search_pages_v2

CALLER = "d" * 32
CREDENTIAL = "e" * 32
CLOCK = 3_000_000
CODEC = CursorCodec(b"synthetic-search-cursor-key-000001")


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
    cursor = fields.pop("cursor", None)
    assert cursor is None or isinstance(cursor, str)
    parsed = parse_query_v2_json(json.dumps(fields).encode())
    return replace(parsed, cursor=cursor)


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


def _page_values(ids: tuple[str, str, str], *, page_byte: int, title: str, content: bytes):  # type: ignore[no-untyped-def]
    return page_graph_values(
        library_id=ids[0],
        section_id=ids[1],
        book_id=ids[2],
        page_byte=page_byte,
        revision_hex=f"{page_byte:02x}",
        source_hex=f"{page_byte:x}"[-1],
        title=title,
        occurrence_wire="2026-08-13T10:00:00.123456Z",
        content_md=content,
    )


def _insert_page(
    engine: Engine,
    ids: tuple[str, str, str],
    *,
    page_byte: int,
    title: str,
    content: bytes,
    maintain_index: bool = False,
) -> str:
    values = _page_values(ids, page_byte=page_byte, title=title, content=content)
    with immediate_transaction(engine) as connection:
        start = dirty_sequence_at_transaction_start(connection) if maintain_index else None
        insert_page_graph(connection, values)
        if maintain_index:
            flush_dirty_since(connection, start)
    return cast(str, values[0].page_id)


def test_search_cursor_pages_the_complete_order_and_binds_query(engine: Engine) -> None:
    ids = seed_library_structure(engine)
    context = _agent(engine, ids[0], ids[1])
    expected = sorted(
        _insert_page(
            engine, ids, page_byte=byte, title=f"plain {byte}", content=b"needle " + b"x" * 400
        )
        for byte in (0x11, 0x12, 0x13)
    )
    rebuild_search_index(engine, clock=lambda: CLOCK)
    first = search_pages_v2(
        engine,
        context,
        _query(keywords=["needle"], limit=1),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert [item.page_id for item in first.items] == expected[:1]
    assert first.next_cursor is not None
    assert first.items[0].snippet is not None
    assert first.items[0].snippet.file_name == "content.md"
    assert first.items[0].snippet.matched is True
    assert "needle" in first.items[0].snippet.text
    assert len(first.items[0].snippet.text) <= 240

    second = search_pages_v2(
        engine,
        context,
        _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert [item.page_id for item in second.items] == expected[1:2]
    assert second.next_cursor is not None
    third = search_pages_v2(
        engine,
        context,
        _query(keywords=["needle"], limit=1, cursor=second.next_cursor),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert [item.page_id for item in third.items] == expected[2:]
    assert third.next_cursor is None

    for changed in (
        _query(keywords=["other"], limit=1, cursor=first.next_cursor),
        _query(keywords=["needle"], limit=2, cursor=first.next_cursor),
        _query(keywords=["needle"], limit=1, cursor=first.next_cursor + "x"),
    ):
        with pytest.raises(InvalidCursorError):
            search_pages_v2(engine, context, changed, clock=lambda: CLOCK, cursor_codec=CODEC)
    with pytest.raises(InvalidSearchQueryV2):
        search_pages_v2(
            engine,
            context,
            _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
            clock=lambda: CLOCK,
        )


def test_visible_write_invalidates_cursor_but_hidden_library_write_does_not(
    engine: Engine,
) -> None:
    visible = seed_library_structure(engine)
    hidden = seed_library_structure(engine, prefix="4", label="Hidden")
    context = _agent(engine, visible[0], visible[1])
    for byte in (0x11, 0x12):
        _insert_page(engine, visible, page_byte=byte, title=f"plain {byte}", content=b"needle")
    rebuild_search_index(engine, clock=lambda: CLOCK)
    first = search_pages_v2(
        engine,
        context,
        _query(keywords=["needle"], limit=1),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert first.next_cursor is not None
    _insert_page(
        engine,
        hidden,
        page_byte=0x13,
        title="needle hidden",
        content=b"needle",
        maintain_index=True,
    )
    assert (
        len(
            search_pages_v2(
                engine,
                context,
                _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
                clock=lambda: CLOCK,
                cursor_codec=CODEC,
            ).items
        )
        == 1
    )
    _insert_page(
        engine,
        visible,
        page_byte=0x14,
        title="needle visible",
        content=b"needle",
        maintain_index=True,
    )
    with pytest.raises(InvalidCursorError):
        search_pages_v2(
            engine,
            context,
            _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
            clock=lambda: CLOCK,
            cursor_codec=CODEC,
        )


def test_grant_and_index_generation_changes_invalidate_cursor(engine: Engine) -> None:
    ids = seed_library_structure(engine)
    context = _agent(engine, ids[0], ids[1])
    for byte in (0x11, 0x12):
        _insert_page(engine, ids, page_byte=byte, title=f"needle {byte}", content=b"plain")
    rebuild_search_index(engine, clock=lambda: CLOCK)
    first = search_pages_v2(
        engine,
        context,
        _query(keywords=["needle"], limit=1),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert first.next_cursor is not None
    assert first.items[0].snippet is not None
    assert first.items[0].snippet.matched is False
    assert first.items[0].snippet.text == "plain"
    other_token = generate_token()
    with immediate_transaction(engine) as connection:
        other_credential = AuthRepository(connection).add_credential(
            NewCredential(
                id="f" * 32,
                library_id=ids[0],
                caller_id=CALLER,
                selector=other_token.selector,
                token_version=other_token.version,
                verifier=other_token.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    other_context = AuthenticatedRequestContext(
        authenticated=AuthenticatedCaller(
            caller=context.authenticated.caller,
            credential=credential_metadata(other_credential),
        ),
        grants=(),
    )
    with pytest.raises(InvalidCursorError):
        search_pages_v2(
            engine,
            other_context,
            _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
            clock=lambda: CLOCK,
            cursor_codec=CODEC,
        )
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        assert repository.remove_grant(ids[0], CALLER, ids[1], SectionAction.PAGE_READ)
    with pytest.raises(InvalidCursorError):
        search_pages_v2(
            engine,
            context,
            _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
            clock=lambda: CLOCK,
            cursor_codec=CODEC,
        )
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=ids[0],
                caller_id=CALLER,
                section_id=ids[1],
                action=SectionAction.PAGE_READ,
                created_at=2_000_000,
            )
        )
    with pytest.raises(InvalidCursorError):
        search_pages_v2(
            engine,
            context,
            _query(keywords=["needle"], limit=1, cursor=first.next_cursor),
            clock=lambda: CLOCK,
            cursor_codec=CODEC,
        )
    next_first = search_pages_v2(
        engine,
        context,
        _query(keywords=["needle"], limit=1),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert next_first.next_cursor is not None
    rebuild_search_index(engine, clock=lambda: CLOCK)
    with pytest.raises(InvalidCursorError):
        search_pages_v2(
            engine,
            context,
            _query(keywords=["needle"], limit=1, cursor=next_first.next_cursor),
            clock=lambda: CLOCK,
            cursor_codec=CODEC,
        )


def test_binary_page_has_no_text_excerpt(engine: Engine) -> None:
    ids = seed_library_structure(engine)
    context = _agent(engine, ids[0], ids[1])
    page_id = _insert_page(engine, ids, page_byte=0x11, title="binary marker", content=b"old")
    manifest = build_file_manifest((("opaque.bin", b"\xff\xfe"),))
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(ids[0], page_id)
        assert page is not None
        repository.add_file_set_revision(
            page, revision_id=f"rev_{'c' * 32}", created_at=CLOCK, manifest=manifest
        )
        assert (
            repository.advance_file_set_current_revision(
                page, revision_id=f"rev_{'c' * 32}", updated_at=CLOCK
            )
            is not None
        )
    rebuild_search_index(engine, clock=lambda: CLOCK)
    found = search_pages_v2(
        engine,
        context,
        _query(keywords=["binary"], limit=1),
        clock=lambda: CLOCK,
        cursor_codec=CODEC,
    )
    assert len(found.items) == 1
    assert found.items[0].snippet is None


def test_cursor_wire_preserves_opaque_cursor_for_the_http_adapter() -> None:
    query = parse_query_v2_json(json.dumps({"keywords": ["needle"], "cursor": "x"}).encode())
    assert query.cursor == "x"
    with pytest.raises(InvalidSearchQueryV2):
        parse_query_v2_json(json.dumps({"keywords": ["needle"], "cursor": ""}).encode())
