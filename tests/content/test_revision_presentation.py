"""Version presentation and title extraction without duplicate browser journeys."""

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.legacy_presentation import legacy_presentation, resolve_presentations
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_service import FileSetRevisionService
from patchouli_lib.content.presentation import (
    extract_title,
    presentation_for,
    preview_text,
    read_presentation,
)
from patchouli_lib.database import immediate_transaction

from .helpers import insert_page_graph, page_graph_values
from .test_file_set_service import REVISION_TWO, _seed, _source


def test_shared_title_vectors() -> None:
    vectors = json.loads(
        (Path(__file__).parents[1] / "vectors/revision-titles.json").read_text("utf-8")
    )
    for vector in vectors:
        assert extract_title(vector["filename"], vector["content"].encode()) == vector["title"], (
            vector["name"]
        )
    # The Markdown rendering budget is not a text editing or entry selection limit.
    large = ("body " * 210_000 + "\n\n# Large source title\n").encode()
    assert len(large) > 1024 * 1024
    assert preview_text(large) == large.decode()
    selected = presentation_for(build_file_manifest((("large.md", large),)), "large.md")
    assert selected.primary_file == "large.md" and selected.display_title == "Large source title"
    for binary in (b"\xff", b"# Title\x00"):
        assert preview_text(binary) is None
        assert extract_title("binary.md", binary) == "无标题"
    for files, entry, title in (
        (
            (("note.md", b"# Legacy heading"), ("image.png", b"\x00\xff")),
            "note.md",
            "Legacy heading",
        ),
        ((("other.md", b"# Other"), ("README.MD", b"# Read me")), "README.MD", "Read me"),
        ((("a.md", b"# A"), ("b.md", b"# B")), None, "Old label"),
        ((("README.txt", b"Text"), ("image.png", b"\x00")), "README.txt", "Old label"),
        ((("note.md", b"No heading"),), "note.md", "Old label"),
        ((("note.md", b"\xff"),), None, "Old label"),
        ((("note.md", b"# Invalid\x00"),), None, "Old label"),
        ((("blob.bin", b"\x00"),), None, "Old label"),
    ):
        derived = legacy_presentation(build_file_manifest(files), "Old label")
        assert (derived.primary_file, derived.display_title) == (entry, title)
    assert (
        legacy_presentation(build_file_manifest((("note.md", b"Text"),)), "").display_title
        == "note.md"
    )


def test_primary_file_only_change_is_a_revision_and_is_immutable(content_engine: Engine) -> None:
    library_id, page_id, page_uid, legacy, etag = _seed(content_engine)
    with immediate_transaction(content_engine) as connection:
        original = connection.exec_driver_sql(
            "SELECT current_revision_id FROM pages LIMIT 1"
        ).scalar_one()
        old = read_presentation(connection, library_id, page_uid, original)
        assert old.primary_file is None and old.display_title == "无标题"
        key = (library_id, page_uid, original)
        statements: list[str] = []

        def capture(*args: object) -> None:
            statements.append(str(args[2]))

        # Batch resolution has the same query count for one or several legacy pages.
        page = connection.exec_driver_sql("SELECT section_id,book_id FROM pages LIMIT 1").one()
        keys = [key]
        for number in range(2, 6):
            values = page_graph_values(
                library_id=library_id,
                section_id=page[0],
                book_id=page[1],
                page_byte=number,
                revision_hex=f"{number:02x}",
                source_hex=f"{number + 9:x}",
                title=f"Old {number}",
                content_md=f"# Legacy {number}\n".encode(),
            )
            insert_page_graph(connection, values)
            keys.append((library_id, values[0].page_uid, values[1].revision_id))
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        before_changes = raw.total_changes
        event.listen(connection, "before_cursor_execute", capture)
        single = resolve_presentations(connection, [key])
        count = len(statements)
        statements.clear()
        multiple = resolve_presentations(connection, keys)
        assert len(statements) == count == 5
        event.remove(connection, "before_cursor_execute", capture)
        assert raw.total_changes == before_changes
        assert single[key].primary_file == "content.md"
        assert single[key].display_title == extract_title("content.md", legacy)
        assert multiple[key] == single[key]
        assert multiple[keys[-1]].display_title == "Legacy 5"
        result = FileSetRevisionService(connection).append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=etag,
            files=(("content.md", legacy),),
            revision_id=REVISION_TWO,
            revision_at=3_000_000,
            source=_source(library_id, page_uid, REVISION_TWO, 2),
            primary_file="content.md",
        )
        assert result.changed and result.page.current_revision_number == 2
        presentation = read_presentation(connection, library_id, page_uid, REVISION_TWO)
        assert presentation.primary_file == "content.md"
        assert presentation.display_title == extract_title("content.md", legacy)
        assert resolve_presentations(connection, [key])[key] == single[key]
        # An explicitly saved no-entry choice is not treated as legacy metadata.
        cleared = FileSetRevisionService(connection).append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=result.etag,
            files=(("content.md", b"# Current body differs from history\n"),),
            revision_id=f"rev_{'9' * 32}",
            revision_at=4_000_000,
            source=_source(library_id, page_uid, f"rev_{'9' * 32}", 3, source_id="a" * 32),
        )
        cleared_key = (library_id, page_uid, cleared.page.current_revision_id)
        explicit = resolve_presentations(connection, [cleared_key])[cleared_key]
        assert explicit.primary_file is None and explicit.display_title == "无标题"
        assert resolve_presentations(connection, [key])[key] == single[key]
    with content_engine.begin() as connection, pytest.raises(IntegrityError):
        connection.exec_driver_sql("UPDATE revision_presentations SET display_title='changed'")
