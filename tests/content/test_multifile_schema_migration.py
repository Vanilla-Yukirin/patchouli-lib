"""Synthetic-only migration coverage for sealed SQLite file-set Revisions."""

from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.models import (
    Page,
    Revision,
    RevisionFile,
    RevisionFileSeal,
    RevisionFileSet,
)
from patchouli_lib.content.schemas import MarkdownContent, NewRevision
from patchouli_lib.database import build_engine, immediate_transaction

from .helpers import insert_page_graph, page_graph_values, seed_library_structure

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NEW_SCHEMA = "20260929_0013"
OLD_SCHEMA = "20260929_0012"


def _database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "multifile.db"
) -> tuple[str, Config]:
    database_url = f"sqlite:///{(tmp_path / name).as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def _seed_legacy_page(database_url: str) -> tuple[str, bytes, str]:
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
            content_md=b"# Before\r\nExact legacy bytes.\n",
        )
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
        return library_id, values[0].page_uid, values[1].revision_id
    finally:
        engine.dispose()


def _append_legacy(database_url: str, library_id: str, page_uid: bytes) -> str:
    revision_id = "rev_" + "33" * 16
    content = MarkdownContent.from_bytes(b"# After\n")
    engine = build_engine(database_url)
    try:
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(Revision),
                NewRevision(
                    library_id=library_id,
                    revision_id=revision_id,
                    page_uid=page_uid,
                    revision_number=2,
                    created_at=3_000_000,
                    **content.model_dump(),
                ).model_dump(),
            )
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == page_uid)
                .values(
                    current_revision_id=revision_id,
                    current_revision_number=2,
                    updated_at=3_000_000,
                )
            )
        return revision_id
    finally:
        engine.dispose()


def _append_file_set(database_url: str, library_id: str, page_uid: bytes) -> str:
    revision_id = "rev_" + "44" * 16
    manifest = build_file_manifest(
        [("资料.bin", b"\x00\x01\xffpayload"), ("chart.png", b"\x89PNG\r\n\x1a\n")]
    )
    engine = build_engine(database_url)
    try:
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(Revision),
                {
                    "library_id": library_id,
                    "revision_id": revision_id,
                    "page_uid": page_uid,
                    "revision_number": 2,
                    "content_md": None,
                    "content_size_bytes": None,
                    "content_sha256": None,
                    "created_at": 3_000_000,
                },
            )
            connection.execute(
                insert(RevisionFileSet),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": revision_id,
                    "revision_number": 2,
                    "storage_format": "file_set_v1",
                    "file_count": len(manifest.files),
                    "total_size_bytes": manifest.total_size_bytes,
                    "snapshot_sha256": manifest.snapshot_sha256,
                },
            )
            for file in manifest.files:
                connection.execute(
                    insert(RevisionFile),
                    {
                        "library_id": library_id,
                        "page_uid": page_uid,
                        "revision_id": revision_id,
                        "revision_number": 2,
                        "filename": file.name,
                        "content_bytes": file.content,
                        "size_bytes": file.content_size_bytes,
                        "content_sha256": file.content_sha256,
                    },
                )
            connection.execute(
                insert(RevisionFileSeal),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": revision_id,
                    "revision_number": 2,
                },
            )
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == page_uid)
                .values(
                    current_revision_id=revision_id,
                    current_revision_number=2,
                    updated_at=3_000_000,
                ),
            )
        with engine.connect() as connection:
            saved = connection.execute(
                select(Revision).where(Revision.revision_id == revision_id)
            ).one()
            assert saved.content_md is None
            assert saved.content_size_bytes is None
            assert saved.content_sha256 is None
            saved_files = connection.execute(
                select(RevisionFile.filename, RevisionFile.content_bytes)
                .where(RevisionFile.revision_id == revision_id)
                .order_by(RevisionFile.filename)
            ).all()
            assert [tuple(row) for row in saved_files] == [
                (file.name, file.content) for file in manifest.files
            ]
            stored_manifest = connection.execute(
                select(RevisionFileSet).where(RevisionFileSet.revision_id == revision_id)
            ).one()
            assert stored_manifest.snapshot_sha256 == manifest.snapshot_sha256
            assert stored_manifest.total_size_bytes == manifest.total_size_bytes
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        return revision_id
    finally:
        engine.dispose()


def _legacy_schema_sql(database_url: str) -> dict[tuple[str, str], str]:
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
                    "AND (name = 'revisions' OR name LIKE 'trg_revisions_%' "
                    "OR name LIKE 'trg_revision_files_%' "
                    "OR name LIKE 'trg_revision_file_seals_%')"
                )
            ).all()
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            return {(kind, name): sql for kind, name, sql in rows}
    finally:
        engine.dispose()


def test_lossless_downgrade_restores_exact_0012_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct_url, direct_config = _database(tmp_path, monkeypatch, name="direct-0012.db")
    command.upgrade(direct_config, OLD_SCHEMA)
    _seed_legacy_page(direct_url)
    expected = _legacy_schema_sql(direct_url)

    migrated_url, migrated_config = _database(tmp_path, monkeypatch, name="roundtrip-0012.db")
    command.upgrade(migrated_config, OLD_SCHEMA)
    _seed_legacy_page(migrated_url)
    command.upgrade(migrated_config, NEW_SCHEMA)
    command.downgrade(migrated_config, OLD_SCHEMA)
    actual = _legacy_schema_sql(migrated_url)
    assert actual == expected
    assert {key: sha256(sql.encode()).digest() for key, sql in actual.items()} == {
        key: sha256(sql.encode()).digest() for key, sql in expected.items()
    }
    engine = build_engine(migrated_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(select(Revision.content_md)) == (
                b"# Before\r\nExact legacy bytes.\n"
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_legacy_rows_survive_and_legacy_writer_still_auto_seals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, OLD_SCHEMA)
    library_id, page_uid, first_revision_id = _seed_legacy_page(database_url)
    command.upgrade(config, NEW_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            first = connection.execute(
                select(Revision).where(Revision.revision_id == first_revision_id)
            ).one()
            assert first.content_md == b"# Before\r\nExact legacy bytes.\n"
            assert first.content_sha256 == sha256(first.content_md).digest()
            old_format = connection.execute(
                select(RevisionFileSet).where(RevisionFileSet.revision_id == first_revision_id)
            ).one()
            assert (old_format.storage_format, old_format.file_count) == ("legacy_markdown", 1)
            assert old_format.snapshot_sha256 is None
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    second_revision_id = _append_legacy(database_url, library_id, page_uid)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(RevisionFileSet.storage_format).where(
                        RevisionFileSet.revision_id == second_revision_id
                    )
                )
                == "legacy_markdown"
            )
            assert (
                connection.scalar(
                    select(RevisionFile.content_bytes).where(
                        RevisionFile.revision_id == second_revision_id
                    )
                )
                == b"# After\n"
            )
            assert (
                connection.scalar(
                    select(RevisionFileSeal.revision_id).where(
                        RevisionFileSeal.revision_id == second_revision_id
                    )
                )
                == second_revision_id
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_binary_only_file_set_is_sealed_and_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, NEW_SCHEMA)
    library_id, page_uid, _ = _seed_legacy_page(database_url)
    revision_id = _append_file_set(database_url, library_id, page_uid)
    engine = build_engine(database_url)
    try:
        for statement in (
            "UPDATE revision_file_sets SET file_count = 1 WHERE revision_id = :id",
            "DELETE FROM revision_file_sets WHERE revision_id = :id",
            "UPDATE revision_files SET filename = 'changed.bin' WHERE revision_id = :id",
            "DELETE FROM revision_files WHERE revision_id = :id",
        ):
            with (
                pytest.raises(IntegrityError, match="immutable"),
                immediate_transaction(engine) as connection,
            ):
                connection.execute(text(statement), {"id": revision_id})
        with (
            pytest.raises(IntegrityError, match="sealed"),
            immediate_transaction(engine) as connection,
        ):
            connection.execute(
                insert(RevisionFile),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": revision_id,
                    "revision_number": 2,
                    "filename": "late.bin",
                    "content_bytes": b"late",
                    "size_bytes": 4,
                    "content_sha256": sha256(b"late").digest(),
                },
            )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.scalar(select(Page.current_revision_id)) == revision_id
    finally:
        engine.dispose()


def test_replace_cannot_rewrite_a_sealed_file_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, NEW_SCHEMA)
    library_id, page_uid, legacy_id = _seed_legacy_page(database_url)
    file_set_id = _append_file_set(database_url, library_id, page_uid)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA recursive_triggers").scalar_one() == 0
            before = {
                revision_id: connection.execute(
                    select(RevisionFileSet).where(RevisionFileSet.revision_id == revision_id)
                ).one()
                for revision_id in (legacy_id, file_set_id)
            }
        for verb in ("INSERT OR REPLACE", "REPLACE"):
            for revision_id in (legacy_id, file_set_id):
                original = before[revision_id]
                with (
                    pytest.raises(IntegrityError, match="immutable"),
                    immediate_transaction(engine) as connection,
                ):
                    connection.execute(
                        text(
                            f"{verb} INTO revision_file_sets "
                            "(library_id, page_uid, revision_id, revision_number, "
                            "storage_format, file_count, total_size_bytes, snapshot_sha256) "
                            "VALUES (:library_id, :page_uid, :revision_id, :revision_number, "
                            ":storage_format, :file_count, :total_size_bytes, :snapshot_sha256)"
                        ),
                        {
                            "library_id": library_id,
                            "page_uid": page_uid,
                            "revision_id": revision_id,
                            "revision_number": original.revision_number,
                            "storage_format": original.storage_format,
                            "file_count": 1,
                            "total_size_bytes": original.total_size_bytes,
                            "snapshot_sha256": (None if revision_id == legacy_id else b"x" * 32),
                        },
                    )
        with engine.connect() as connection:
            assert {
                revision_id: connection.execute(
                    select(RevisionFileSet).where(RevisionFileSet.revision_id == revision_id)
                ).one()
                for revision_id in (legacy_id, file_set_id)
            } == before
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_incomplete_file_set_rolls_back_at_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, NEW_SCHEMA)
    library_id, page_uid, first_revision_id = _seed_legacy_page(database_url)
    engine = build_engine(database_url)
    try:
        second_id = "rev_" + "55" * 16
        content = b"\x00binary"
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.execute(
                insert(Revision),
                {
                    "library_id": library_id,
                    "revision_id": second_id,
                    "page_uid": page_uid,
                    "revision_number": 2,
                    "content_md": None,
                    "content_size_bytes": None,
                    "content_sha256": None,
                    "created_at": 3_000_000,
                },
            )
            connection.execute(
                insert(RevisionFileSet),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": second_id,
                    "revision_number": 2,
                    "storage_format": "file_set_v1",
                    "file_count": 2,
                    "total_size_bytes": len(content),
                    "snapshot_sha256": b"x" * 32,
                },
            )
            connection.execute(
                insert(RevisionFile),
                {
                    "library_id": library_id,
                    "page_uid": page_uid,
                    "revision_id": second_id,
                    "revision_number": 2,
                    "filename": "one.bin",
                    "content_bytes": content,
                    "size_bytes": len(content),
                    "content_sha256": sha256(content).digest(),
                },
            )
            with pytest.raises(IntegrityError, match="incomplete"):
                connection.execute(
                    insert(RevisionFileSeal),
                    {
                        "library_id": library_id,
                        "page_uid": page_uid,
                        "revision_id": second_id,
                        "revision_number": 2,
                    },
                )
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == page_uid)
                .values(current_revision_id=second_id, current_revision_number=2),
            )
        with engine.connect() as connection:
            assert connection.scalar(select(Page.current_revision_id)) == first_revision_id
            assert (
                connection.scalar(
                    select(Revision.revision_id).where(Revision.revision_id == second_id)
                )
                is None
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_failed_rebuild_rolls_back_and_empty_downgrade_is_lossless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, OLD_SCHEMA)
    library_id, page_uid, first_revision_id = _seed_legacy_page(database_url)
    engine = build_engine(database_url)
    try:
        with immediate_transaction(engine) as connection:
            connection.exec_driver_sql("CREATE TABLE revision_file_sets (collision INTEGER)")
    finally:
        engine.dispose()
    with pytest.raises(OperationalError, match="already exists"):
        command.upgrade(config, NEW_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == OLD_SCHEMA
            assert not inspect(engine).has_table("revisions_next")
            assert connection.exec_driver_sql("PRAGMA table_info(revisions)").all()[4][3] == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        with immediate_transaction(engine) as connection:
            connection.exec_driver_sql("DROP TABLE revision_file_sets")
    finally:
        engine.dispose()
    command.upgrade(config, NEW_SCHEMA)
    command.downgrade(config, OLD_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(Revision.content_md).where(Revision.revision_id == first_revision_id)
                )
                == b"# Before\r\nExact legacy bytes.\n"
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert not inspect(engine).has_table("revision_file_sets")
            assert connection.scalar(select(Page.current_revision_id)) == first_revision_id
    finally:
        engine.dispose()
    # A legacy append remains usable after a lossless downgrade.
    _append_legacy(database_url, library_id, page_uid)


def test_downgrade_refuses_binary_file_set_without_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch)
    command.upgrade(config, NEW_SCHEMA)
    library_id, page_uid, _ = _seed_legacy_page(database_url)
    revision_id = _append_file_set(database_url, library_id, page_uid)
    with pytest.raises(RuntimeError, match="multi-file Revisions exist"):
        command.downgrade(config, OLD_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW_SCHEMA
            assert (
                connection.scalar(
                    select(RevisionFileSet.storage_format).where(
                        RevisionFileSet.revision_id == revision_id
                    )
                )
                == "file_set_v1"
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
