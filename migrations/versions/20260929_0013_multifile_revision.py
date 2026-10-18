"""Permit sealed, flat multi-file Revisions without placeholder Markdown.

Revision ID: 20260929_0013
Revises: 20260929_0012
Create Date: 2026-09-29

This is a storage-only step. Public writes and backup validation must be
updated separately before this schema is eligible for deployment.
"""

import re
import sqlite3
from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0013"
down_revision: str | None = "20260929_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
_MAX_FILES = 64
_MAX_PAGE_BYTES = 64 * 1024 * 1024
_REVISION_TRIGGERS = frozenset(
    {
        "trg_revisions_sequential_insert",
        "trg_revisions_create_append_guard",
        "trg_revisions_immutable_update",
        "trg_revisions_immutable_delete",
        "trg_revisions_mirror_content_file",
        "trg_revisions_require_file_seal",
    }
)
_UNCHANGED_REVISION_TRIGGERS = (
    "trg_revisions_sequential_insert",
    "trg_revisions_create_append_guard",
    "trg_revisions_immutable_update",
    "trg_revisions_immutable_delete",
    "trg_revisions_require_file_seal",
)


def _begin_sqlite_rebuild() -> None:
    """Use SQLite's documented FK-off table-rebuild path on a disposable connection.

    Earlier migrations may have opened an explicit transaction in the same
    Alembic command. Their version stamps are complete, so commit that prefix
    before disabling foreign keys. The rebuild itself remains one SQLite
    transaction through Alembic's 0013 version stamp. The migration engine is
    disposed by migrations/env.py after the command; application connections
    continue to enable FK enforcement at connect time.
    """

    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Multi-file Revision migration requires SQLite.")
    if driver.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("Cannot disable foreign keys for the SQLite table rebuild.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _check_foreign_keys() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Revision relationships are inconsistent.")


def _verify_legacy_sets() -> None:
    connection = op.get_bind()
    _check_foreign_keys()
    inconsistent = connection.execute(
        sa.text(
            "SELECT 1 FROM revisions AS r WHERE NOT EXISTS ("
            "SELECT 1 FROM revision_files AS f WHERE f.library_id = r.library_id "
            "AND f.page_uid = r.page_uid AND f.revision_id = r.revision_id "
            "AND f.revision_number = r.revision_number AND f.filename = 'content.md' "
            "AND f.content_bytes = r.content_md "
            "AND f.size_bytes = r.content_size_bytes "
            "AND f.content_sha256 = r.content_sha256) OR NOT EXISTS ("
            "SELECT 1 FROM revision_file_seals AS s WHERE s.library_id = r.library_id "
            "AND s.page_uid = r.page_uid AND s.revision_id = r.revision_id "
            "AND s.revision_number = r.revision_number) OR NOT EXISTS ("
            "SELECT 1 FROM revision_file_seal_guards AS g WHERE g.library_id = r.library_id "
            "AND g.page_uid = r.page_uid AND g.revision_id = r.revision_id "
            "AND g.revision_number = r.revision_number) LIMIT 1"
        )
    ).scalar_one_or_none()
    extra = connection.execute(
        sa.text("SELECT 1 FROM revision_files WHERE filename != 'content.md' LIMIT 1")
    ).scalar_one_or_none()
    if inconsistent is not None or extra is not None:
        raise RuntimeError("Existing Revision file sets are not exact legacy mirrors.")
    rows = connection.execute(
        sa.text("SELECT content_md, content_size_bytes, content_sha256 FROM revisions")
    )
    while batch := rows.fetchmany(8):
        for content, size, digest in batch:
            if (
                type(content) is not bytes
                or not 1 <= len(content) <= _MAX_MARKDOWN_BYTES
                or size != len(content)
                or type(digest) is not bytes
                or digest != sha256(content).digest()
            ):
                raise RuntimeError("Existing Revision content metadata is inconsistent.")


def _capture_revision_triggers() -> dict[str, str]:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' "
                "AND tbl_name = 'revisions'"
            )
        )
        .all()
    )
    captured = {name: sql for name, sql in rows}
    if set(captured) != _REVISION_TRIGGERS or any(not sql for sql in captured.values()):
        raise RuntimeError("Unexpected Revision triggers prevent a safe table rebuild.")
    return captured


def _create_revisions_table(*, legacy_only: bool, table_name: str = "revisions_next") -> None:
    content_md_check = (
        "typeof(content_md) = 'blob' AND length(content_md) BETWEEN 1 AND 2097152 "
        "AND instr(content_md, x'00') = 0"
    )
    content_size_check = "content_size_bytes = length(content_md)"
    content_sha_check = "typeof(content_sha256) = 'blob' AND length(content_sha256) = 32"
    if not legacy_only:
        content_md_check = f"content_md IS NULL OR ({content_md_check})"
        content_size_check = (
            "(content_md IS NULL AND content_size_bytes IS NULL) OR "
            "(content_md IS NOT NULL AND content_size_bytes = length(content_md))"
        )
        content_sha_check = (
            "(content_md IS NULL AND content_sha256 IS NULL) OR "
            "(content_md IS NOT NULL AND typeof(content_sha256) = 'blob' "
            "AND length(content_sha256) = 32)"
        )
    op.create_table(
        table_name,
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column(
            "content_md", sa.LargeBinary(length=_MAX_MARKDOWN_BYTES), nullable=not legacy_only
        ),
        sa.Column("content_size_bytes", sa.BigInteger(), nullable=not legacy_only),
        sa.Column("content_sha256", sa.LargeBinary(length=32), nullable=not legacy_only),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(revision_id) = 'text' AND length(revision_id) = 36 "
            "AND substr(revision_id, 1, 4) = 'rev_' "
            "AND substr(revision_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_revisions_revision_id_wire",
        ),
        sa.CheckConstraint(
            "typeof(page_uid) = 'blob' AND length(page_uid) = 16",
            name="ck_revisions_page_uid_128_bit",
        ),
        sa.CheckConstraint(
            "revision_number BETWEEN 1 AND 9223372036854775807",
            name="ck_revisions_revision_number",
        ),
        sa.CheckConstraint(content_md_check, name="ck_revisions_content_md"),
        sa.CheckConstraint(content_size_check, name="ck_revisions_content_size_bytes"),
        sa.CheckConstraint(content_sha_check, name="ck_revisions_content_sha256"),
        sa.CheckConstraint("created_at >= 0", name="ck_revisions_created_at"),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_revisions_library_page_pages",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("library_id", "revision_id", name="pk_revisions"),
        sa.UniqueConstraint(
            "revision_id", "page_uid", "library_id", name="uq_revisions_revision_page_library"
        ),
        sa.UniqueConstraint(
            "library_id", "page_uid", "revision_number", name="uq_revisions_library_page_number"
        ),
        sa.UniqueConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="uq_revisions_library_page_id_number",
        ),
    )


def _rebuild_revisions(*, legacy_only: bool, old_triggers: dict[str, str]) -> None:
    # SQLite validates every stored trigger body during ALTER TABLE RENAME.
    # Child-table triggers referring to revisions would temporarily name a
    # missing table between DROP and RENAME, so preserve their exact SQL.
    related_triggers = [
        (name, sql)
        for name, sql in op.get_bind().execute(
            sa.text(
                "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' "
                "AND tbl_name != 'revisions'"
            )
        )
        if sql and re.search(r"\brevisions\b", sql, flags=re.IGNORECASE)
    ]
    preparer = op.get_bind().dialect.identifier_preparer
    for name, _sql in related_triggers:
        op.execute(sa.text(f"DROP TRIGGER {preparer.quote(name)}"))
    columns = (
        "(library_id, revision_id, page_uid, revision_number, content_md, "
        "content_size_bytes, content_sha256, created_at)"
    )
    if legacy_only:
        # A rename leaves CREATE TABLE "revisions" in sqlite_schema, while
        # the original 0004 table has CREATE TABLE revisions. The backup
        # verifier intentionally hashes that exact SQL. Copy through a TEMP
        # table and recreate the original name directly within this transaction.
        op.execute(sa.text("CREATE TEMP TABLE revisions_legacy_backup AS SELECT * FROM revisions"))
        op.drop_table("revisions")
        _create_revisions_table(legacy_only=True, table_name="revisions")
        op.execute(
            sa.text(f"INSERT INTO revisions {columns} SELECT * FROM revisions_legacy_backup")
        )
        op.execute(sa.text("DROP TABLE revisions_legacy_backup"))
    else:
        _create_revisions_table(legacy_only=False)
        op.execute(sa.text(f"INSERT INTO revisions_next {columns} SELECT * FROM revisions"))
        op.drop_table("revisions")
        op.rename_table("revisions_next", "revisions")
    for name in _UNCHANGED_REVISION_TRIGGERS:
        op.execute(sa.text(old_triggers[name]))
    for _name, sql in related_triggers:
        op.execute(sa.text(sql))


def _create_file_set_table() -> None:
    op.create_table(
        "revision_file_sets",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("storage_format", sa.String(length=16), nullable=False),
        sa.Column("file_count", sa.BigInteger(), nullable=False),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_sha256", sa.LargeBinary(length=32), nullable=True),
        sa.PrimaryKeyConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="pk_revision_file_sets",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_file_sets_exact_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(storage_format = 'legacy_markdown' AND file_count = 1 "
            f"AND total_size_bytes BETWEEN 1 AND {_MAX_MARKDOWN_BYTES} "
            "AND snapshot_sha256 IS NULL) OR "
            "(storage_format = 'file_set_v1' "
            f"AND file_count BETWEEN 1 AND {_MAX_FILES} "
            f"AND total_size_bytes BETWEEN 0 AND {_MAX_PAGE_BYTES} "
            "AND typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32)",
            name="ck_revision_file_sets_format_manifest",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "SELECT library_id, page_uid, revision_id, revision_number, "
            "'legacy_markdown', 1, content_size_bytes, NULL FROM revisions"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_sets_validate_insert "
            "BEFORE INSERT ON revision_file_sets WHEN NOT EXISTS ("
            "SELECT 1 FROM revisions AS r WHERE r.library_id = NEW.library_id "
            "AND r.page_uid = NEW.page_uid AND r.revision_id = NEW.revision_id "
            "AND r.revision_number = NEW.revision_number AND ("
            "(NEW.storage_format = 'legacy_markdown' AND r.content_md IS NOT NULL "
            "AND NEW.total_size_bytes = r.content_size_bytes) OR "
            "(NEW.storage_format = 'file_set_v1' AND r.content_md IS NULL))) "
            "BEGIN SELECT RAISE(ABORT, 'Revision file format mismatch'); END"
        )
    )
    # SQLite's REPLACE conflict action can delete and reinsert a sealed row
    # without running DELETE triggers when recursive_triggers is off. Reject
    # the conflicting INSERT itself before that implicit delete can happen.
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_sets_no_replace "
            "BEFORE INSERT ON revision_file_sets WHEN EXISTS ("
            "SELECT 1 FROM revision_file_sets AS existing WHERE "
            "existing.library_id = NEW.library_id AND existing.page_uid = NEW.page_uid "
            "AND existing.revision_id = NEW.revision_id "
            "AND existing.revision_number = NEW.revision_number) BEGIN "
            "SELECT RAISE(ABORT, 'Revision file sets are immutable'); END"
        )
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_revision_file_sets_no_{action.lower()} "
                f"BEFORE {action} ON revision_file_sets BEGIN "
                "SELECT RAISE(ABORT, 'Revision file sets are immutable'); END"
            )
        )


def _replace_file_triggers(*, legacy_only: bool) -> None:
    for name in (
        "trg_revision_file_seals_validate_insert",
        "trg_revision_files_legacy_sealed_insert",
        "trg_revision_files_auto_seal_legacy",
    ):
        op.execute(sa.text(f"DROP TRIGGER {name}"))
    if legacy_only:
        # Exact 0008 behavior is restored so its downgrade remains sound.
        op.execute(sa.text(_LEGACY_SEAL_VALIDATE))
        op.execute(sa.text(_LEGACY_FILE_INSERT))
        op.execute(sa.text(_LEGACY_AUTO_SEAL))
        return
    # SQLite has no built-in SHA-256 aggregate: count and byte sum are sealed
    # here, while the file hashes and canonical snapshot digest require the
    # future write service and backup validator to recompute and compare them.
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_seals_validate_insert "
            "BEFORE INSERT ON revision_file_seals WHEN NOT EXISTS ("
            "SELECT 1 FROM revisions AS r JOIN revision_file_sets AS manifest "
            "ON manifest.library_id = r.library_id AND manifest.page_uid = r.page_uid "
            "AND manifest.revision_id = r.revision_id "
            "AND manifest.revision_number = r.revision_number "
            "WHERE r.library_id = NEW.library_id AND r.page_uid = NEW.page_uid "
            "AND r.revision_id = NEW.revision_id "
            "AND r.revision_number = NEW.revision_number AND ("
            "(manifest.storage_format = 'legacy_markdown' "
            "AND (SELECT count(*) FROM revision_files AS f WHERE "
            "f.library_id = r.library_id AND f.page_uid = r.page_uid "
            "AND f.revision_id = r.revision_id AND f.revision_number = r.revision_number) = 1 "
            "AND EXISTS (SELECT 1 FROM revision_files AS f WHERE "
            "f.library_id = r.library_id AND f.page_uid = r.page_uid "
            "AND f.revision_id = r.revision_id AND f.revision_number = r.revision_number "
            "AND f.filename = 'content.md' AND f.content_bytes = r.content_md "
            "AND f.size_bytes = r.content_size_bytes "
            "AND f.content_sha256 = r.content_sha256)) OR "
            "(manifest.storage_format = 'file_set_v1' AND r.content_md IS NULL "
            "AND (SELECT count(*) FROM revision_files AS f WHERE "
            "f.library_id = r.library_id AND f.page_uid = r.page_uid "
            "AND f.revision_id = r.revision_id "
            "AND f.revision_number = r.revision_number) = manifest.file_count "
            "AND (SELECT coalesce(sum(f.size_bytes), 0) FROM revision_files AS f WHERE "
            "f.library_id = r.library_id AND f.page_uid = r.page_uid "
            "AND f.revision_id = r.revision_id "
            "AND f.revision_number = r.revision_number) = manifest.total_size_bytes))) "
            "BEGIN SELECT RAISE(ABORT, 'Revision file set is incomplete'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_legacy_sealed_insert "
            "BEFORE INSERT ON revision_files WHEN EXISTS ("
            "SELECT 1 FROM revision_file_seals AS s WHERE "
            "s.library_id = NEW.library_id AND s.page_uid = NEW.page_uid "
            "AND s.revision_id = NEW.revision_id "
            "AND s.revision_number = NEW.revision_number) OR ("
            "NEW.filename != 'content.md' AND EXISTS ("
            "SELECT 1 FROM revisions AS r WHERE r.library_id = NEW.library_id "
            "AND r.page_uid = NEW.page_uid AND r.revision_id = NEW.revision_id "
            "AND r.revision_number = NEW.revision_number AND r.content_md IS NOT NULL)) "
            "BEGIN SELECT RAISE(ABORT, 'Revision file set is sealed'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_auto_seal_legacy "
            "AFTER INSERT ON revision_files WHEN EXISTS ("
            "SELECT 1 FROM revisions AS r WHERE r.library_id = NEW.library_id "
            "AND r.page_uid = NEW.page_uid AND r.revision_id = NEW.revision_id "
            "AND r.revision_number = NEW.revision_number AND r.content_md IS NOT NULL) "
            "BEGIN INSERT INTO revision_file_seals "
            "(library_id, page_uid, revision_id, revision_number) VALUES "
            "(NEW.library_id, NEW.page_uid, NEW.revision_id, NEW.revision_number); END"
        )
    )


_LEGACY_SEAL_VALIDATE = (
    "CREATE TRIGGER trg_revision_file_seals_validate_insert "
    "BEFORE INSERT ON revision_file_seals WHEN NOT EXISTS ("
    "SELECT 1 FROM revisions AS revision WHERE "
    "revision.library_id = NEW.library_id AND revision.page_uid = NEW.page_uid "
    "AND revision.revision_id = NEW.revision_id "
    "AND revision.revision_number = NEW.revision_number "
    "AND (SELECT count(*) FROM revision_files AS file WHERE "
    "file.library_id = NEW.library_id AND file.page_uid = NEW.page_uid "
    "AND file.revision_id = NEW.revision_id "
    "AND file.revision_number = NEW.revision_number) = 1 "
    "AND EXISTS (SELECT 1 FROM revision_files AS file WHERE "
    "file.library_id = NEW.library_id AND file.page_uid = NEW.page_uid "
    "AND file.revision_id = NEW.revision_id "
    "AND file.revision_number = NEW.revision_number "
    "AND file.filename = 'content.md' "
    "AND file.content_bytes = revision.content_md "
    "AND file.size_bytes = revision.content_size_bytes "
    "AND file.content_sha256 = revision.content_sha256)) "
    "BEGIN SELECT RAISE(ABORT, 'Revision file set is not a legacy mirror'); END"
)
_LEGACY_FILE_INSERT = (
    "CREATE TRIGGER trg_revision_files_legacy_sealed_insert "
    "BEFORE INSERT ON revision_files WHEN NEW.filename != 'content.md' OR EXISTS ("
    "SELECT 1 FROM revision_file_seals AS seal WHERE "
    "seal.library_id = NEW.library_id AND seal.page_uid = NEW.page_uid "
    "AND seal.revision_id = NEW.revision_id "
    "AND seal.revision_number = NEW.revision_number) BEGIN "
    "SELECT RAISE(ABORT, 'Legacy Revision file set is sealed'); END"
)
_LEGACY_AUTO_SEAL = (
    "CREATE TRIGGER trg_revision_files_auto_seal_legacy "
    "AFTER INSERT ON revision_files BEGIN INSERT INTO revision_file_seals "
    "(library_id, page_uid, revision_id, revision_number) VALUES "
    "(NEW.library_id, NEW.page_uid, NEW.revision_id, NEW.revision_number); END"
)


def _create_legacy_mirror_trigger(*, with_file_set: bool) -> None:
    statements = ""
    if with_file_set:
        statements = (
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) VALUES "
            "(NEW.library_id, NEW.page_uid, NEW.revision_id, NEW.revision_number, "
            "'legacy_markdown', 1, NEW.content_size_bytes, NULL); "
        )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_mirror_content_file "
            "AFTER INSERT ON revisions "
            + ("WHEN NEW.content_md IS NOT NULL " if with_file_set else "")
            + "BEGIN "
            + statements
            + "INSERT INTO revision_files "
            "(library_id, page_uid, revision_id, revision_number, filename, "
            "content_bytes, size_bytes, content_sha256) VALUES "
            "(NEW.library_id, NEW.page_uid, NEW.revision_id, "
            "NEW.revision_number, 'content.md', NEW.content_md, "
            "NEW.content_size_bytes, NEW.content_sha256); END"
        )
    )


def upgrade() -> None:
    _begin_sqlite_rebuild()
    _verify_legacy_sets()
    old_triggers = _capture_revision_triggers()
    _rebuild_revisions(legacy_only=False, old_triggers=old_triggers)
    _create_file_set_table()
    _replace_file_triggers(legacy_only=False)
    _create_legacy_mirror_trigger(with_file_set=True)
    _check_foreign_keys()


def downgrade() -> None:
    _begin_sqlite_rebuild()
    _check_foreign_keys()
    incompatible = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM revision_file_sets WHERE storage_format != 'legacy_markdown' LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if incompatible is not None:
        raise RuntimeError("Cannot downgrade while multi-file Revisions exist.")
    _verify_legacy_sets()
    missing = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM revisions AS r WHERE r.content_md IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM revision_file_sets AS m WHERE m.library_id = r.library_id "
                "AND m.page_uid = r.page_uid AND m.revision_id = r.revision_id "
                "AND m.revision_number = r.revision_number "
                "AND m.storage_format = 'legacy_markdown' "
                "AND m.total_size_bytes = r.content_size_bytes) LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if missing is not None:
        raise RuntimeError("Cannot downgrade inconsistent Revision file sets.")
    old_triggers = _capture_revision_triggers()
    _replace_file_triggers(legacy_only=True)
    op.drop_table("revision_file_sets")
    _rebuild_revisions(legacy_only=True, old_triggers=old_triggers)
    _create_legacy_mirror_trigger(with_file_set=False)
    _check_foreign_keys()
