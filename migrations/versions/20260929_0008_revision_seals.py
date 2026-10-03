"""Seal legacy Markdown Revision file sets without changing public writes.

Revision ID: 20260929_0008
Revises: 20260929_0007
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0008"
down_revision: str | None = "20260929_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_LEGACY_MARKDOWN_BYTES = 2 * 1024 * 1024
_PREFLIGHT_BATCH_ROWS = 8


def _begin_sqlite_transaction() -> None:
    """Make DDL, data backfill, and Alembic's version update atomic."""

    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Revision file seal migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _verify_legacy_file_sets() -> None:
    """Refuse anything other than an exact content.md mirror before DDL."""

    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing Revision file relationships are inconsistent.")

    extra = connection.execute(
        sa.text("SELECT 1 FROM revision_files WHERE filename != 'content.md' LIMIT 1")
    ).scalar_one_or_none()
    missing_or_mismatched = connection.execute(
        sa.text(
            "SELECT 1 FROM revisions AS revision WHERE NOT EXISTS ("
            "SELECT 1 FROM revision_files AS file WHERE "
            "file.library_id = revision.library_id "
            "AND file.page_uid = revision.page_uid "
            "AND file.revision_id = revision.revision_id "
            "AND file.revision_number = revision.revision_number "
            "AND file.filename = 'content.md' "
            "AND file.content_bytes = revision.content_md "
            "AND file.size_bytes = revision.content_size_bytes "
            "AND file.content_sha256 = revision.content_sha256) LIMIT 1"
        )
    ).scalar_one_or_none()
    if extra is not None or missing_or_mismatched is not None:
        raise RuntimeError("Existing Revision file sets are not exact legacy mirrors.")

    # SQLite cannot independently compute SHA-256. Keep the preflight bounded:
    # at most eight valid 2 MiB Markdown bodies are loaded at once.
    oversized = connection.execute(
        sa.text(
            "SELECT 1 FROM revisions WHERE typeof(content_md) != 'blob' "
            f"OR length(content_md) NOT BETWEEN 1 AND {_MAX_LEGACY_MARKDOWN_BYTES} LIMIT 1"
        )
    ).scalar_one_or_none()
    if oversized is not None:
        raise RuntimeError("Existing Revision content metadata is inconsistent.")
    rows = connection.execute(
        sa.text("SELECT content_md, content_size_bytes, content_sha256 FROM revisions")
    )
    while batch := rows.fetchmany(_PREFLIGHT_BATCH_ROWS):
        for content, size, digest in batch:
            if (
                not isinstance(content, bytes)
                or size != len(content)
                or not isinstance(digest, bytes)
                or digest != sha256(content).digest()
            ):
                raise RuntimeError("Existing Revision content metadata is inconsistent.")


def upgrade() -> None:
    _begin_sqlite_transaction()
    _verify_legacy_file_sets()

    # A seal is an immutable marker for the *whole* file set. This migration
    # supports exactly one legacy content.md; multi-file writes remain disabled.
    op.create_table(
        "revision_file_seals",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="pk_revision_file_seals",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_file_seals_exact_revision",
            ondelete="RESTRICT",
        ),
    )
    # A guard is inserted for every new Revision. Its deferred FK makes a
    # missing seal fail COMMIT irrespective of SQLite AFTER-trigger ordering.
    op.create_table(
        "revision_file_seal_guards",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="pk_revision_file_seal_guards",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_file_seal_guards_exact_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revision_file_seals.library_id",
                "revision_file_seals.page_uid",
                "revision_file_seals.revision_id",
                "revision_file_seals.revision_number",
            ],
            name="fk_revision_file_seal_guards_exact_seal",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    op.execute(
        sa.text(
            "INSERT INTO revision_file_seals "
            "(library_id, page_uid, revision_id, revision_number) "
            "SELECT library_id, page_uid, revision_id, revision_number FROM revisions"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO revision_file_seal_guards "
            "(library_id, page_uid, revision_id, revision_number) "
            "SELECT library_id, page_uid, revision_id, revision_number FROM revisions"
        )
    )

    op.execute(
        sa.text(
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
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_seals_no_update "
            "BEFORE UPDATE ON revision_file_seals BEGIN "
            "SELECT RAISE(ABORT, 'Revision file seals are immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_seals_no_delete "
            "BEFORE DELETE ON revision_file_seals BEGIN "
            "SELECT RAISE(ABORT, 'Revision file seals are immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_seal_guards_no_update "
            "BEFORE UPDATE ON revision_file_seal_guards BEGIN "
            "SELECT RAISE(ABORT, 'Revision file seal guards are immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_file_seal_guards_no_delete "
            "BEFORE DELETE ON revision_file_seal_guards BEGIN "
            "SELECT RAISE(ABORT, 'Revision file seal guards are immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_legacy_sealed_insert "
            "BEFORE INSERT ON revision_files WHEN NEW.filename != 'content.md' OR EXISTS ("
            "SELECT 1 FROM revision_file_seals AS seal WHERE "
            "seal.library_id = NEW.library_id AND seal.page_uid = NEW.page_uid "
            "AND seal.revision_id = NEW.revision_id "
            "AND seal.revision_number = NEW.revision_number) BEGIN "
            "SELECT RAISE(ABORT, 'Legacy Revision file set is sealed'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revision_files_auto_seal_legacy "
            "AFTER INSERT ON revision_files BEGIN "
            "INSERT INTO revision_file_seals "
            "(library_id, page_uid, revision_id, revision_number) VALUES "
            "(NEW.library_id, NEW.page_uid, NEW.revision_id, NEW.revision_number); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_require_file_seal "
            "AFTER INSERT ON revisions BEGIN "
            "INSERT INTO revision_file_seal_guards "
            "(library_id, page_uid, revision_id, revision_number) VALUES "
            "(NEW.library_id, NEW.page_uid, NEW.revision_id, NEW.revision_number); END"
        )
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    # The two new tables are derived markers only. 0007's immutable file rows
    # and the old Markdown Revision payloads are left untouched.
    op.execute(sa.text("DROP TRIGGER trg_revisions_require_file_seal"))
    op.execute(sa.text("DROP TRIGGER trg_revision_files_auto_seal_legacy"))
    op.execute(sa.text("DROP TRIGGER trg_revision_files_legacy_sealed_insert"))
    op.execute(sa.text("DROP TRIGGER trg_revision_file_seal_guards_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_revision_file_seal_guards_no_update"))
    op.execute(sa.text("DROP TRIGGER trg_revision_file_seals_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_revision_file_seals_no_update"))
    op.execute(sa.text("DROP TRIGGER trg_revision_file_seals_validate_insert"))
    op.drop_table("revision_file_seal_guards")
    op.drop_table("revision_file_seals")
