"""Persist immutable success receipts for master-session file-set writes.

Revision ID: 20261001_0026
Revises: 20261001_0025
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0026"
down_revision: str | None = "20261001_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "admin_master_file_set_receipts"


def _begin() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() not in {
        revision,
        down_revision,
    }:
        raise RuntimeError("Unexpected schema for master file-set receipts.")


def upgrade() -> None:
    _begin()
    op.create_table(
        _TABLE,
        sa.Column("identity_id", sa.String(32), primary_key=True),
        sa.Column("operation", sa.String(6), primary_key=True),
        sa.Column("key_digest", sa.LargeBinary(32), primary_key=True),
        sa.Column("request_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("library_id", sa.String(32), nullable=False),
        sa.Column("section_id", sa.String(32), nullable=False),
        sa.Column("book_id", sa.String(32), nullable=False),
        sa.Column("page_id", sa.String(80), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(16), nullable=False),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("changed", sa.BigInteger(), nullable=False),
        sa.Column("original_occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("original_page_updated_at", sa.BigInteger(), nullable=False),
        sa.Column("response_etag", sa.String(100), nullable=False),
        sa.Column("operation_at", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(32)),
        sa.Column("master_audit_event_id", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["book_id", "section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "source_id"],
            ["page_sources.library_id", "page_sources.source_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("library_id", "source_id"),
        sa.UniqueConstraint("master_audit_event_id"),
        sa.CheckConstraint("length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'"),
        sa.CheckConstraint("operation IN ('create', 'revise')"),
        sa.CheckConstraint("typeof(key_digest) = 'blob' AND length(key_digest) = 32"),
        sa.CheckConstraint(
            "typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32"
        ),
        sa.CheckConstraint("typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32"),
        sa.CheckConstraint("typeof(page_uid) = 'blob' AND length(page_uid) = 16"),
        sa.CheckConstraint("typeof(changed) = 'integer' AND changed IN (0, 1)"),
        sa.CheckConstraint("operation != 'create' OR (changed = 1 AND revision_number = 1)"),
        sa.CheckConstraint(
            "(changed = 1 AND source_id IS NOT NULL AND master_audit_event_id IS NOT NULL) OR "
            "(changed = 0 AND source_id IS NULL AND master_audit_event_id IS NULL)"
        ),
        sa.CheckConstraint(
            "typeof(operation_at) = 'integer' AND operation_at >= 0 AND "
            "typeof(original_page_updated_at) = 'integer' AND original_page_updated_at >= 0"
        ),
        sa.CheckConstraint(
            "typeof(original_occurred_at) = 'integer' AND "
            "original_occurred_at BETWEEN -62135596800000000 AND 253402300799999999"
        ),
    )
    for action in ("update", "delete"):
        op.execute(
            f"CREATE TRIGGER trg_master_file_set_receipts_no_{action} "
            f"BEFORE {action.upper()} ON {_TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Master file-set receipt is immutable'); END"
        )


def downgrade() -> None:
    _begin()
    connection = op.get_bind()
    if (
        connection.exec_driver_sql(f"SELECT 1 FROM {_TABLE} LIMIT 1").first()
        or connection.exec_driver_sql(
            "SELECT 1 FROM admin_master_audit_events WHERE action IN "
            "('content.page.file_set.create', 'content.page.file_set.revise') LIMIT 1"
        ).first()
    ):
        raise RuntimeError("Cannot discard master file-set write history.")
    for action in ("delete", "update"):
        op.execute(f"DROP TRIGGER trg_master_file_set_receipts_no_{action}")
    op.drop_table(_TABLE)
