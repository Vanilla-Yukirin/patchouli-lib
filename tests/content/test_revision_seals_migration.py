"""Legacy Revision file-set sealing without changing the old Archive writer."""

from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from patchouli_lib.content.models import Page, PageSource, Revision, RevisionFile
from patchouli_lib.content.schemas import MarkdownContent, NewRevision
from patchouli_lib.database import build_engine, immediate_transaction

from .helpers import insert_page_graph, page_graph_values, seed_library_structure

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{(tmp_path / 'revision-seals.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def _append_revision(
    database_url: str,
    *,
    library_id: str,
    page_uid: bytes,
    revision_number: int,
    content: bytes,
) -> str:
    revision_id = f"rev_{revision_number:032x}"
    stored = MarkdownContent.from_bytes(content)
    engine = build_engine(database_url)
    try:
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(Revision),
                NewRevision(
                    library_id=library_id,
                    revision_id=revision_id,
                    page_uid=page_uid,
                    revision_number=revision_number,
                    created_at=2_000_000 + revision_number,
                    **stored.model_dump(),
                ).model_dump(),
            )
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == page_uid)
                .values(
                    current_revision_id=revision_id,
                    current_revision_number=revision_number,
                ),
            )
    finally:
        engine.dispose()
    return revision_id


def _row_count(database_url: str, table: str) -> int:
    assert table in {
        "revisions",
        "revision_files",
        "revision_file_seals",
        "revision_file_seal_guards",
    }
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            return int(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one())
    finally:
        engine.dispose()


def test_historical_backfill_and_future_legacy_writes_are_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0007")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        first = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
            content_md=b"# First\r\nExact bytes.\n",
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, first)
        page_uid = first[0].page_uid
    finally:
        engine.dispose()
    _append_revision(
        database_url,
        library_id=library_id,
        page_uid=page_uid,
        revision_number=2,
        content=b"# Second\n",
    )
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            old_revisions = connection.execute(
                select(Revision).order_by(Revision.revision_number)
            ).all()
            old_sources = connection.execute(select(PageSource)).all()
            old_files = connection.execute(
                select(RevisionFile).order_by(RevisionFile.revision_number)
            ).all()
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0008")
    assert _row_count(database_url, "revision_file_seals") == 2
    assert _row_count(database_url, "revision_file_seal_guards") == 2
    engine = build_engine(database_url)
    try:
        assert {"revision_file_seals", "revision_file_seal_guards"} <= set(
            inspect(engine).get_table_names()
        )
        with engine.connect() as connection:
            assert connection.execute(select(Revision)).all() == old_revisions
            assert connection.execute(select(PageSource)).all() == old_sources
            assert connection.execute(select(RevisionFile)).all() == old_files
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    third = b"# Third\n"
    third_id = _append_revision(
        database_url,
        library_id=library_id,
        page_uid=page_uid,
        revision_number=3,
        content=third,
    )
    assert _row_count(database_url, "revisions") == 3
    assert _row_count(database_url, "revision_files") == 3
    assert _row_count(database_url, "revision_file_seals") == 3
    assert _row_count(database_url, "revision_file_seal_guards") == 3

    # The original API also creates a first Revision for a newly created Page.
    engine = build_engine(database_url)
    try:
        another = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
            page_byte=0x44,
            revision_hex="55",
            source_hex="6",
            title="Another synthetic Page",
            content_md=b"# New Page\n",
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, another)
        with engine.connect() as connection:
            mirrored = connection.execute(
                select(
                    RevisionFile.filename,
                    RevisionFile.content_bytes,
                    RevisionFile.size_bytes,
                    RevisionFile.content_sha256,
                ).where(RevisionFile.revision_id == third_id)
            ).one()
            assert mirrored == ("content.md", third, len(third), sha256(third).digest())
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    assert _row_count(database_url, "revisions") == 4
    assert _row_count(database_url, "revision_files") == 4
    assert _row_count(database_url, "revision_file_seals") == 4
    assert _row_count(database_url, "revision_file_seal_guards") == 4


def test_sealed_set_rejects_late_file_replacement_and_marker_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0008")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
        params = {
            "library_id": library_id,
            "page_uid": values[0].page_uid,
            "revision_id": values[1].revision_id,
            "content": b"extra",
            "digest": sha256(b"extra").digest(),
        }
        file_insert = text(
            "INSERT OR REPLACE INTO revision_files "
            "(library_id, page_uid, revision_id, revision_number, filename, "
            "content_bytes, size_bytes, content_sha256) VALUES "
            "(:library_id, :page_uid, :revision_id, 1, :filename, "
            ":content, 5, :digest)"
        )
        for filename in ("extra.txt", "content.md"):
            with pytest.raises(IntegrityError, match="sealed"), engine.begin() as connection:
                connection.execute(file_insert, {**params, "filename": filename})
        for table in ("revision_file_seals", "revision_file_seal_guards"):
            with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
                connection.exec_driver_sql(f"DELETE FROM {table}")
            with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
                connection.exec_driver_sql(f"UPDATE {table} SET revision_number = 2")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    assert _row_count(database_url, "revision_files") == 1
    assert _row_count(database_url, "revision_file_seals") == 1
    assert _row_count(database_url, "revision_file_seal_guards") == 1


def test_missing_seal_cannot_commit_even_if_auto_seal_trigger_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0008")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
        next_content = MarkdownContent.from_bytes(b"# New\n")
        next_revision = NewRevision(
            library_id=library_id,
            revision_id=f"rev_{3:032x}",
            page_uid=values[0].page_uid,
            revision_number=2,
            created_at=2_000_002,
            **next_content.model_dump(),
        )
        # Trigger DDL and the new Revision share an explicit SQLite transaction.
        # The failed deferred FK COMMIT rolls both back and restores the trigger.
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.exec_driver_sql("DROP TRIGGER trg_revision_files_auto_seal_legacy")
            connection.execute(insert(Revision), next_revision.model_dump())
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == values[0].page_uid)
                .values(current_revision_id=next_revision.revision_id, current_revision_number=2)
            )
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260929_0008"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'trg_revision_files_auto_seal_legacy'"
                    )
                ).scalar_one()
                == 1
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    assert _row_count(database_url, "revisions") == 1
    assert _row_count(database_url, "revision_files") == 1
    assert _row_count(database_url, "revision_file_seals") == 1
    assert _row_count(database_url, "revision_file_seal_guards") == 1


@pytest.mark.parametrize("corruption", ["extra", "missing"])
def test_inconsistent_0007_file_set_refuses_upgrade_before_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0007")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
        if corruption == "extra":
            extra = b"fixture"
            with immediate_transaction(engine) as connection:
                connection.execute(
                    insert(RevisionFile),
                    {
                        "library_id": library_id,
                        "page_uid": values[0].page_uid,
                        "revision_id": values[1].revision_id,
                        "revision_number": 1,
                        "filename": "other.txt",
                        "content_bytes": extra,
                        "size_bytes": len(extra),
                        "content_sha256": sha256(extra).digest(),
                    },
                )
        else:
            with immediate_transaction(engine) as connection:
                original_trigger = connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'trg_revision_files_no_delete'"
                    )
                ).scalar_one()
                connection.exec_driver_sql("DROP TRIGGER trg_revision_files_no_delete")
                connection.exec_driver_sql("DELETE FROM revision_files")
                connection.exec_driver_sql(original_trigger)
        with engine.connect() as connection:
            files_before = connection.execute(select(RevisionFile)).all()
            revision_before = connection.execute(select(Revision)).all()
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="not exact legacy mirrors"):
        command.upgrade(config, "20260929_0008")
    engine = build_engine(database_url)
    try:
        assert "revision_file_seals" not in inspect(engine).get_table_names()
        assert "revision_file_seal_guards" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260929_0007"
            )
            assert connection.execute(select(RevisionFile)).all() == files_before
            assert connection.execute(select(Revision)).all() == revision_before
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_ddl_failure_rolls_back_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0007")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
        with immediate_transaction(engine) as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER trg_revision_file_seals_validate_insert "
                "BEFORE INSERT ON revisions BEGIN SELECT 1; END"
            )
        with engine.connect() as connection:
            revision_before = connection.execute(select(Revision)).all()
            file_before = connection.execute(select(RevisionFile)).all()
    finally:
        engine.dispose()

    with pytest.raises(OperationalError, match="already exists"):
        command.upgrade(config, "20260929_0008")
    engine = build_engine(database_url)
    try:
        assert "revision_file_seals" not in inspect(engine).get_table_names()
        assert "revision_file_seal_guards" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260929_0007"
            )
            assert connection.execute(select(Revision)).all() == revision_before
            assert connection.execute(select(RevisionFile)).all() == file_before
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with immediate_transaction(engine) as connection:
            connection.exec_driver_sql("DROP TRIGGER trg_revision_file_seals_validate_insert")
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0008")
    assert _row_count(database_url, "revision_file_seals") == 1
    assert _row_count(database_url, "revision_file_seal_guards") == 1


def test_empty_upgrade_downgrade_and_reupgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260929_0008")
    command.upgrade(config, "head")
    command.check(config)
    assert _row_count(database_url, "revision_file_seals") == 0
    assert _row_count(database_url, "revision_file_seal_guards") == 0
    command.downgrade(config, "20260929_0007")
    engine = build_engine(database_url)
    try:
        assert "revision_file_seals" not in inspect(engine).get_table_names()
        assert "revision_file_seal_guards" not in inspect(engine).get_table_names()
        assert "revision_files" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    command.upgrade(config, "20260929_0008")
    assert _row_count(database_url, "revision_file_seals") == 0
