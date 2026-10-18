"""Library-scoped Tag migration and repository behavior on synthetic data."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.tags.repository import TagRepository, normalize_tag_name


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{(tmp_path / 'tags.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    return database_url, config


def test_upgrade_preserves_legacy_page_and_exact_library_associations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0009")
    engine = build_engine(database_url)
    try:
        first_library, first_section, first_book = seed_library_structure(engine)
        second_library, second_section, second_book = seed_library_structure(
            engine, prefix="4", label="Second"
        )
        first = page_graph_values(
            library_id=first_library,
            section_id=first_section,
            book_id=first_book,
            page_byte=0x11,
        )
        second = page_graph_values(
            library_id=second_library,
            section_id=second_section,
            book_id=second_book,
            page_byte=0x44,
            revision_hex="55",
            source_hex="6",
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, first)
            insert_page_graph(connection, second)
        with engine.connect() as connection:
            old_count = connection.exec_driver_sql("SELECT count(*) FROM pages").scalar_one()
            old_revision_count = connection.exec_driver_sql(
                "SELECT count(*) FROM revisions"
            ).scalar_one()
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0010")
    engine = build_engine(database_url)
    try:
        assert {"tags", "page_tags"} <= set(inspect(engine).get_table_names())
        assert {"ix_page_tags_library_tag"} <= {
            item["name"] for item in inspect(engine).get_indexes("page_tags")
        }
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM pages").scalar_one() == old_count
            )
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM revisions").scalar_one()
                == old_revision_count
            )
            assert connection.exec_driver_sql("SELECT count(*) FROM tags").scalar_one() == 0

        with immediate_transaction(engine) as connection:
            repository = TagRepository(connection)
            tag_first = repository.add_tag(
                library_id=first_library,
                tag_id="a" * 32,
                name="Café",
                created_at=1_000_000,
            )
            tag_second = repository.add_tag(
                library_id=second_library,
                tag_id="b" * 32,
                name="CAFE\u0301",
                created_at=1_000_001,
            )
            assert tag_first.display_name == "Café"
            assert tag_second.display_name == "CAFÉ"
            assert tag_first.match_key == tag_second.match_key == "café"
            assert repository.find_tag(library_id=first_library, name="CAFÉ") == tag_first
            assert repository.find_tag(library_id=second_library, name="cafe\u0301") == tag_second
            assert repository.get_tag(library_id=first_library, tag_id="b" * 32) is None
            repository.attach_page(
                library_id=first_library,
                page_uid=first[0].page_uid,
                tag_id=tag_first.id,
                created_at=1_000_002,
            )
            repository.attach_page(
                library_id=second_library,
                page_uid=second[0].page_uid,
                tag_id=tag_second.id,
                created_at=1_000_003,
            )
            assert repository.list_page_tags(
                library_id=first_library, page_uid=first[0].page_uid
            ) == [tag_first]
            assert [
                item.page_uid
                for item in repository.list_tag_pages(
                    library_id=second_library, tag_id=tag_second.id
                )
            ] == [second[0].page_uid]
            assert [
                item.display_name for item in repository.list_tags(library_id=second_library)
            ] == ["CAFÉ"]

        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            TagRepository(connection).add_tag(
                library_id=first_library,
                tag_id="c" * 32,
                name="CAFE\u0301",
                created_at=1_000_004,
            )
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            TagRepository(connection).attach_page(
                library_id=first_library,
                page_uid=second[0].page_uid,
                tag_id=tag_first.id,
                created_at=1_000_004,
            )
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            TagRepository(connection).attach_page(
                library_id=second_library,
                page_uid=second[0].page_uid,
                tag_id=tag_first.id,
                created_at=1_000_004,
            )
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.execute(
                text("DELETE FROM tags WHERE library_id = :library_id AND id = :tag_id"),
                {"library_id": first_library, "tag_id": tag_first.id},
            )
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.execute(
                text("UPDATE tags SET created_at = :invalid WHERE library_id = :library_id"),
                {"invalid": "not-a-time", "library_id": first_library},
            )
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.execute(
                text("UPDATE page_tags SET created_at = :invalid WHERE library_id = :library_id"),
                {"invalid": 1.5, "library_id": first_library},
            )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("SELECT count(*) FROM page_tags").scalar_one() == 2
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="discard Tag"):
        command.downgrade(config, "20260929_0009")
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260929_0010")
            assert connection.exec_driver_sql("SELECT count(*) FROM page_tags").scalar_one() == 2
    finally:
        engine.dispose()


def test_empty_round_trip_and_detach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0010")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
            repository = TagRepository(connection)
            repository.add_tag(library_id=library_id, tag_id="a" * 32, name="合成", created_at=0)
            repository.attach_page(
                library_id=library_id,
                page_uid=values[0].page_uid,
                tag_id="a" * 32,
                created_at=0,
            )
        with immediate_transaction(engine) as connection:
            repository = TagRepository(connection)
            assert repository.detach_page(
                library_id=library_id, page_uid=values[0].page_uid, tag_id="a" * 32
            )
            assert not repository.detach_page(
                library_id=library_id, page_uid=values[0].page_uid, tag_id="a" * 32
            )
            connection.execute(
                text("DELETE FROM tags WHERE library_id = :library_id"), {"library_id": library_id}
            )
    finally:
        engine.dispose()
    command.downgrade(config, "20260929_0009")
    engine = build_engine(database_url)
    try:
        assert "tags" not in inspect(engine).get_table_names()
        assert "page_tags" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM pages").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    command.upgrade(config, "20260929_0010")


@pytest.mark.parametrize(
    "name",
    ["", " leading", "trailing ", "a\nline", "x\x00y", "x\u2028y", "x\u202ey", "x" * 101],
)
def test_invalid_tag_names_fail_before_write(name: str) -> None:
    with pytest.raises(ValueError):
        normalize_tag_name(name)


def test_unicode_casefold_is_normalized_after_folding() -> None:
    assert normalize_tag_name("Straße") == ("Straße", "strasse")
    assert normalize_tag_name("CAFE\u0301") == ("CAFÉ", "café")
