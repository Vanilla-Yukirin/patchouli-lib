"""Additive historical Revision file backfill and legacy-writer compatibility."""

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
    database_url = f"sqlite:///{(tmp_path / 'revision-files.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def _append_revision(
    engine_url: str,
    *,
    library_id: str,
    page_uid: bytes,
    number: int,
    content: bytes,
) -> str:
    revision_id = f"rev_{number:032x}"
    engine = build_engine(engine_url)
    try:
        stored = MarkdownContent.from_bytes(content)
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(Revision),
                NewRevision(
                    library_id=library_id,
                    revision_id=revision_id,
                    page_uid=page_uid,
                    revision_number=number,
                    created_at=2_000_000 + number,
                    **stored.model_dump(),
                ).model_dump(),
            )
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == page_uid)
                .values(current_revision_id=revision_id, current_revision_number=number),
            )
        return revision_id
    finally:
        engine.dispose()


def test_backfills_all_revisions_preserves_sources_and_legacy_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260813_0006")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
            content_md=b"# First\r\n\nExact bytes.\n",
        )
        page_uid = values[0].page_uid
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
        second_id = _append_revision(
            database_url,
            library_id=library_id,
            page_uid=page_uid,
            number=2,
            content="# 第二版\n".encode(),
        )
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(PageSource),
                {
                    **values[4].model_dump(),
                    "source_id": "4" * 32,
                    "revision_id": second_id,
                    "revision_number": 2,
                },
            )
        with engine.connect() as connection:
            legacy_revisions = connection.execute(
                select(
                    Revision.library_id,
                    Revision.page_uid,
                    Revision.revision_id,
                    Revision.revision_number,
                    Revision.content_md,
                    Revision.content_size_bytes,
                    Revision.content_sha256,
                ).order_by(Revision.revision_number)
            ).all()
            legacy_sources = connection.execute(
                select(PageSource).order_by(PageSource.source_id)
            ).all()
            assert len(legacy_revisions) == len(legacy_sources) == 2
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0007")
    command.upgrade(config, "20260929_0007")  # Reapplying this revision is a no-op.
    engine = build_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "revision_files" in inspector.get_table_names()
        foreign_keys = {item["name"]: item for item in inspector.get_foreign_keys("revision_files")}
        assert foreign_keys["fk_revision_files_exact_revision"]["constrained_columns"] == [
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
        ]
        assert foreign_keys["fk_revision_files_exact_revision"]["referred_columns"] == [
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
        ]
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            files = [
                tuple(row)
                for row in connection.execute(
                    select(
                        RevisionFile.library_id,
                        RevisionFile.page_uid,
                        RevisionFile.revision_id,
                        RevisionFile.revision_number,
                        RevisionFile.filename,
                        RevisionFile.content_bytes,
                        RevisionFile.size_bytes,
                        RevisionFile.content_sha256,
                    ).order_by(RevisionFile.revision_number)
                )
            ]
            assert files == [(*old[:4], "content.md", *old[4:]) for old in legacy_revisions]
            assert (
                connection.execute(select(PageSource).order_by(PageSource.source_id)).all()
                == legacy_sources
            )
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260929_0007")

        third = b"# Third\n"
        third_id = _append_revision(
            database_url,
            library_id=library_id,
            page_uid=page_uid,
            number=3,
            content=third,
        )
        with engine.connect() as connection:
            mirrored = connection.execute(
                select(
                    RevisionFile.filename,
                    RevisionFile.content_bytes,
                    RevisionFile.size_bytes,
                    RevisionFile.content_sha256,
                ).where(
                    RevisionFile.library_id == library_id,
                    RevisionFile.page_uid == page_uid,
                    RevisionFile.revision_id == third_id,
                )
            ).one()
            assert mirrored[:3] == (
                "content.md",
                third,
                len(third),
            )
            assert mirrored[3] == sha256(third).digest()
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(
                update(RevisionFile)
                .where(RevisionFile.revision_id == third_id)
                .values(filename="changed.md")
            )
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text("DELETE FROM revision_files WHERE revision_id = :revision_id"),
                {"revision_id": third_id},
            )
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO revision_files "
                    "(library_id, page_uid, revision_id, revision_number, filename, "
                    "content_bytes, size_bytes, content_sha256) "
                    "VALUES (:library_id, :page_uid, :revision_id, 3, 'content.md', "
                    ":content, :size, :digest)"
                ),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": third_id,
                    "content": third,
                    "size": len(third),
                    "digest": sha256(third).digest(),
                },
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO revision_files "
                    "(library_id, page_uid, revision_id, revision_number, filename, "
                    "content_bytes, size_bytes, content_sha256) "
                    "VALUES (:library_id, :page_uid, :revision_id, 3, '../escape', "
                    ":content, :size, :digest)"
                ),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": third_id,
                    "content": third,
                    "size": len(third),
                    "digest": sha256(third).digest(),
                },
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260813_0006")
    engine = build_engine(database_url)
    try:
        assert "revision_files" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                connection.execute(select(PageSource).order_by(PageSource.source_id)).all()
                == legacy_sources
            )
            assert len(connection.execute(select(Revision)).all()) == 3
    finally:
        engine.dispose()
    command.upgrade(config, "head")


def test_empty_upgrade_and_downgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    command.check(config)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.execute(select(RevisionFile)).all() == []
    finally:
        engine.dispose()
    command.downgrade(config, "20260813_0006")
    engine = build_engine(database_url)
    try:
        assert "revision_files" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_downgrade_refuses_additional_file_without_schema_or_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260813_0006")
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
    finally:
        engine.dispose()
    command.upgrade(config, "20260929_0007")

    dotfile = b"synthetic fixture\n"
    engine = build_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(RevisionFile),
                {
                    "library_id": library_id,
                    "page_uid": values[0].page_uid,
                    "revision_id": values[1].revision_id,
                    "revision_number": 1,
                    "filename": ".env",
                    "content_bytes": dotfile,
                    "size_bytes": len(dotfile),
                    "content_sha256": sha256(dotfile).digest(),
                },
            )
        with engine.connect() as connection:
            before_files = connection.execute(
                select(
                    RevisionFile.filename,
                    RevisionFile.content_bytes,
                    RevisionFile.size_bytes,
                    RevisionFile.content_sha256,
                ).order_by(RevisionFile.filename)
            ).all()
            before_revisions = connection.execute(select(Revision)).all()
            before_sources = connection.execute(select(PageSource)).all()
            before_triggers = set(
                connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND (tbl_name = 'revision_files' "
                        "OR name = 'trg_revisions_mirror_content_file')"
                    )
                ).scalars()
            )
            assert len(before_files) == 2
            assert before_files[0][0] == ".env"
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="additional files"):
        command.downgrade(config, "20260813_0006")

    engine = build_engine(database_url)
    try:
        assert "revision_files" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260929_0007")
            assert (
                connection.execute(
                    select(
                        RevisionFile.filename,
                        RevisionFile.content_bytes,
                        RevisionFile.size_bytes,
                        RevisionFile.content_sha256,
                    ).order_by(RevisionFile.filename)
                ).all()
                == before_files
            )
            assert connection.execute(select(Revision)).all() == before_revisions
            assert connection.execute(select(PageSource)).all() == before_sources
            assert (
                set(
                    connection.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                            "AND (tbl_name = 'revision_files' "
                            "OR name = 'trg_revisions_mirror_content_file')"
                        )
                    ).scalars()
                )
                == before_triggers
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_corrupt_legacy_digest_refuses_upgrade_before_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260813_0006")
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

        # Construct a synthetic pre-0007 corruption without permanently
        # weakening its immutable trigger or touching any real database.
        with engine.begin() as connection:
            trigger_ddl = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_revisions_immutable_update'"
                )
            ).scalar_one()
            connection.exec_driver_sql("DROP TRIGGER trg_revisions_immutable_update")
            connection.execute(
                text("UPDATE revisions SET content_sha256 = :wrong_digest"),
                {"wrong_digest": b"\x00" * 32},
            )
            connection.exec_driver_sql(trigger_ddl)
        with engine.connect() as connection:
            old_revision = connection.execute(
                text("SELECT content_md, content_size_bytes, content_sha256 FROM revisions")
            ).one()
            old_source = connection.execute(text("SELECT * FROM page_sources")).one()
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="content metadata is inconsistent"):
        command.upgrade(config, "20260929_0007")

    engine = build_engine(database_url)
    try:
        assert "revision_files" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260813_0006")
            assert (
                connection.execute(
                    text("SELECT content_md, content_size_bytes, content_sha256 FROM revisions")
                ).one()
                == old_revision
            )
            assert connection.execute(text("SELECT * FROM page_sources")).one() == old_source
            assert (
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'trg_revisions_immutable_update'"
                    )
                ).scalar_one()
                == trigger_ddl
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_failure_after_table_creation_rolls_back_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, "20260813_0006")
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
        # Occupy a future trigger name in this synthetic old database. The
        # upgrade will create and populate its table, then fail at trigger
        # creation. No production code path or real database is modified.
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER trg_revision_files_no_update "
                "BEFORE INSERT ON revisions BEGIN SELECT 1; END"
            )
        with engine.connect() as connection:
            old_revisions = connection.execute(select(Revision)).all()
            old_sources = connection.execute(select(PageSource)).all()
            injected_trigger = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                    "AND name = 'trg_revision_files_no_update'"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    with pytest.raises(OperationalError, match="already exists"):
        command.upgrade(config, "20260929_0007")

    engine = build_engine(database_url)
    try:
        assert "revision_files" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260813_0006")
            assert connection.execute(select(Revision)).all() == old_revisions
            assert connection.execute(select(PageSource)).all() == old_sources
            assert (
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'trg_revision_files_no_update'"
                    )
                ).scalar_one()
                == injected_trigger
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER trg_revision_files_no_update")
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0007")
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(select(RevisionFile)).all() != []
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
