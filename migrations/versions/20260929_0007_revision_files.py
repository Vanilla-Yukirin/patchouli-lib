"""Add flat file rows beside existing Markdown Revisions.

Revision ID: 20260929_0007
Revises: 20260813_0006
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0007"
down_revision: str | None = "20260813_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Intentionally local to this migration: changing runtime limits later must
# not change how a historical database upgrade is interpreted.
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_FILENAME_BYTES = 255
_MAX_LEGACY_MARKDOWN_BYTES = 2 * 1024 * 1024
_PREFLIGHT_BATCH_ROWS = 8

# This migration protects each row from UPDATE/DELETE. It does not yet seal
# the set of filenames belonging to a Revision; that needs a later write API.


def _begin_sqlite_transaction() -> None:
    """Make SQLite DDL and Alembic's version update one real transaction.

    SQLAlchemy may have an implicit transaction while Python's sqlite3 legacy
    mode has not actually sent BEGIN; SQLite DDL would otherwise auto-commit.
    Alembic owns the outer SQLAlchemy transaction and commits after stamping.
    """

    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Revision file migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _verify_legacy_revisions() -> None:
    """Fail before DDL rather than blessing corrupted legacy digest metadata."""

    connection = op.get_bind()
    # Check lengths inside SQLite before fetching payloads. Even if an old
    # CHECK constraint was bypassed, a single oversized row cannot be loaded
    # by this preflight. Eight valid rows contain at most 16 MiB of bodies.
    oversized = connection.execute(
        sa.text(
            "SELECT 1 FROM revisions WHERE typeof(content_md) != 'blob' "
            f"OR length(content_md) NOT BETWEEN 1 AND {_MAX_LEGACY_MARKDOWN_BYTES} LIMIT 1"
        )
    ).scalar_one_or_none()
    if oversized is not None:
        raise RuntimeError("Existing Revision content metadata is inconsistent.")

    result = connection.execute(
        sa.text("SELECT content_md, content_size_bytes, content_sha256 FROM revisions")
    )
    while rows := result.fetchmany(_PREFLIGHT_BATCH_ROWS):
        for content, size, digest in rows:
            if (
                not isinstance(content, bytes)
                or not 1 <= len(content) <= _MAX_FILE_BYTES
                or size != len(content)
                or not isinstance(digest, bytes)
                or digest != sha256(content).digest()
            ):
                raise RuntimeError("Existing Revision content metadata is inconsistent.")


def upgrade() -> None:
    _begin_sqlite_transaction()
    _verify_legacy_revisions()

    op.create_table(
        "revision_files",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_bytes", sa.LargeBinary(length=_MAX_FILE_BYTES), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.LargeBinary(length=32), nullable=False),
        sa.PrimaryKeyConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            "filename",
            name="pk_revision_files",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_files_exact_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "typeof(filename) = 'text' AND length(filename) >= 1 "
            f"AND length(CAST(filename AS BLOB)) <= {_MAX_FILENAME_BYTES} "
            "AND filename NOT IN ('.', '..') "
            "AND instr(filename, '/') = 0 AND instr(filename, char(92)) = 0 "
            "AND instr(filename, char(0)) = 0 "
            "AND filename = trim(filename, ' ') "
            "AND substr(filename, -1) != '.'",
            name="ck_revision_files_flat_filename",
        ),
        sa.CheckConstraint(
            "typeof(content_bytes) = 'blob' "
            f"AND length(content_bytes) BETWEEN 0 AND {_MAX_FILE_BYTES}",
            name="ck_revision_files_content_bytes",
        ),
        sa.CheckConstraint(
            "size_bytes = length(content_bytes)",
            name="ck_revision_files_size_bytes",
        ),
        sa.CheckConstraint(
            "typeof(content_sha256) = 'blob' AND length(content_sha256) = 32",
            name="ck_revision_files_content_sha256",
        ),
    )

    # Every historical Revision, not only the current Page pointer, is copied.
    # Exact bytes and trusted, prevalidated metadata are preserved unchanged.
    op.execute(
        sa.text(
            "INSERT INTO revision_files "
            "(library_id, page_uid, revision_id, revision_number, filename, "
            "content_bytes, size_bytes, content_sha256) "
            "SELECT library_id, page_uid, revision_id, revision_number, 'content.md', "
            "content_md, content_size_bytes, content_sha256 FROM revisions"
        )
    )

    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_no_update "
            "BEFORE UPDATE ON revision_files BEGIN "
            "SELECT RAISE(ABORT, 'Revision files are immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_no_delete "
            "BEFORE DELETE ON revision_files BEGIN "
            "SELECT RAISE(ABORT, 'Revision files are immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_no_replace "
            "BEFORE INSERT ON revision_files "
            "WHEN EXISTS (SELECT 1 FROM revision_files AS existing WHERE "
            "existing.library_id = NEW.library_id AND existing.page_uid = NEW.page_uid "
            "AND existing.revision_id = NEW.revision_id "
            "AND existing.revision_number = NEW.revision_number "
            "AND existing.filename = NEW.filename) "
            "BEGIN SELECT RAISE(ABORT, 'Revision files are immutable'); END"
        )
    )
    # Legacy callers still append only to revisions. Keep their future writes
    # represented in the new table in the same database transaction.
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_mirror_content_file "
            "AFTER INSERT ON revisions BEGIN "
            "INSERT INTO revision_files "
            "(library_id, page_uid, revision_id, revision_number, filename, "
            "content_bytes, size_bytes, content_sha256) "
            "VALUES (NEW.library_id, NEW.page_uid, NEW.revision_id, "
            "NEW.revision_number, 'content.md', NEW.content_md, "
            "NEW.content_size_bytes, NEW.content_sha256); END"
        )
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    # Downgrade is lossless only while this additive table contains solely the
    # legacy content.md mirror. New flat files have no representation in the
    # 0006 schema, so refuse before dropping any trigger or table.
    extra_file = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM revision_files WHERE filename != 'content.md' LIMIT 1"))
        .scalar_one_or_none()
    )
    if extra_file is not None:
        raise RuntimeError("Cannot downgrade while Revisions contain additional files.")

    op.execute(sa.text("DROP TRIGGER trg_revisions_mirror_content_file"))
    op.execute(sa.text("DROP TRIGGER trg_revision_files_no_replace"))
    op.execute(sa.text("DROP TRIGGER trg_revision_files_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_revision_files_no_update"))
    op.drop_table("revision_files")
