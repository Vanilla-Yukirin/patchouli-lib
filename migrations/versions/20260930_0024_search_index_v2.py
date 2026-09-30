"""Add a disabled, rebuildable current-Page search-v2 index skeleton.

Revision ID: 20260930_0024
Revises: 20260930_0023
Create Date: 2026-09-30

This migration does not enable search or classify any file as text. Authority
triggers only record affected Page identities; an application write coordinator
must build derived rows in the same transaction before enabling the index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0024"
down_revision: str | None = "20260930_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_VERSION = "search-v2-candidate-schema-1"


def _mark_dirty_trigger(
    name: str,
    table: str,
    event: str,
    row: str,
    *,
    current_only: bool = False,
    when: str | None = None,
) -> None:
    condition = when
    if current_only:
        current = (
            "EXISTS (SELECT 1 FROM pages AS p WHERE "
            f"p.library_id = {row}.library_id AND p.page_uid = {row}.page_uid "
            f"AND p.current_revision_id = {row}.revision_id "
            f"AND p.current_revision_number = {row}.revision_number)"
        )
        condition = current if condition is None else f"({condition}) AND {current}"
    clause = "" if condition is None else f" WHEN {condition}"
    op.execute(
        sa.text(
            f"CREATE TRIGGER {name} AFTER {event} ON {table}{clause} BEGIN "
            "UPDATE search_meta SET dirty_sequence = dirty_sequence + 1 WHERE singleton = 1; "
            "INSERT INTO search_dirty_pages (library_id, page_uid, seq) "
            f"SELECT {row}.library_id, {row}.page_uid, dirty_sequence "
            "FROM search_meta WHERE singleton = 1 "
            "ON CONFLICT(library_id, page_uid) DO UPDATE SET seq = excluded.seq; "
            "END"
        )
    )


def _create_dirty_triggers() -> None:
    _mark_dirty_trigger("trg_search_pages_insert", "pages", "INSERT", "NEW")
    _mark_dirty_trigger("trg_search_pages_delete", "pages", "DELETE", "OLD")
    _mark_dirty_trigger(
        "trg_search_pages_update_old",
        "pages",
        "UPDATE",
        "OLD",
        when=(
            "OLD.library_id IS NOT NEW.library_id OR OLD.page_uid IS NOT NEW.page_uid "
            "OR OLD.section_id IS NOT NEW.section_id OR OLD.book_id IS NOT NEW.book_id "
            "OR OLD.page_id IS NOT NEW.page_id OR OLD.title IS NOT NEW.title "
            "OR OLD.occurred_at IS NOT NEW.occurred_at "
            "OR OLD.current_revision_id IS NOT NEW.current_revision_id "
            "OR OLD.current_revision_number IS NOT NEW.current_revision_number "
            "OR OLD.deleted_at IS NOT NEW.deleted_at"
        ),
    )
    _mark_dirty_trigger(
        "trg_search_pages_update_new",
        "pages",
        "UPDATE",
        "NEW",
        when="OLD.library_id IS NOT NEW.library_id OR OLD.page_uid IS NOT NEW.page_uid",
    )
    for table in (
        "page_tags",
        "revisions",
        "revision_file_sets",
        "revision_files",
        "revision_file_seals",
    ):
        for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            _mark_dirty_trigger(
                f"trg_search_{table}_{event.lower()}",
                table,
                event,
                row,
                current_only=table != "page_tags",
            )
        # A changed association or Revision identity can stop applying to an
        # old Page even if the new identity is unrelated. Existing Revision
        # immutability guards reject normal updates; this is future-proofing.
        _mark_dirty_trigger(
            f"trg_search_{table}_update_old",
            table,
            "UPDATE",
            "OLD",
            current_only=table != "page_tags",
        )


def upgrade() -> None:
    op.create_table(
        "search_generations",
        sa.Column("generation", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("index_version", sa.String(120), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger()),
        sa.CheckConstraint("generation >= 1", name="ck_search_generations_generation"),
        sa.CheckConstraint(
            "state IN ('building', 'ready', 'retired')", name="ck_search_generations_state"
        ),
        sa.CheckConstraint(
            "length(index_version) BETWEEN 1 AND 120", name="ck_search_generations_version"
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND (completed_at IS NULL OR completed_at >= created_at)",
            name="ck_search_generations_times",
        ),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "search_meta",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("active_generation", sa.Integer()),
        sa.Column("ready", sa.Integer(), nullable=False),
        sa.Column("dirty_sequence", sa.BigInteger(), nullable=False),
        sa.Column("index_version", sa.String(120), nullable=False),
        sa.ForeignKeyConstraint(
            ["active_generation"],
            ["search_generations.generation"],
            name="fk_search_meta_active_generation",
        ),
        sa.CheckConstraint("singleton = 1", name="ck_search_meta_singleton"),
        sa.CheckConstraint(
            "ready IN (0, 1) AND (ready = 0 OR active_generation IS NOT NULL)",
            name="ck_search_meta_ready",
        ),
        sa.CheckConstraint(
            "typeof(dirty_sequence) = 'integer' AND dirty_sequence >= 0",
            name="ck_search_meta_dirty_sequence",
        ),
        sa.CheckConstraint(
            "length(index_version) BETWEEN 1 AND 120", name="ck_search_meta_version"
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO search_meta "
            "(singleton, active_generation, ready, dirty_sequence, index_version) "
            "VALUES (1, NULL, 0, 0, :index_version)"
        ).bindparams(index_version=INDEX_VERSION)
    )
    op.create_table(
        "search_dirty_pages",
        sa.Column("library_id", sa.String(32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(16), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "page_uid", name="pk_search_dirty_pages"),
        sa.CheckConstraint(
            "typeof(page_uid) = 'blob' AND length(page_uid) = 16", name="ck_search_dirty_pages_uid"
        ),
        sa.CheckConstraint(
            "typeof(seq) = 'integer' AND seq >= 1", name="ck_search_dirty_pages_seq"
        ),
    )
    op.create_index("ix_search_dirty_pages_seq", "search_dirty_pages", ["seq"])
    op.create_table(
        "search_page_state",
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.String(32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(16), nullable=False),
        sa.Column("section_id", sa.String(32), nullable=False),
        sa.Column("book_id", sa.String(32), nullable=False),
        sa.Column("page_id", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_sha256", sa.LargeBinary(32)),
        sa.Column("source_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "generation", "library_id", "page_uid", name="pk_search_page_state"
        ),
        sa.UniqueConstraint(
            "generation",
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="uq_search_page_state_exact_revision",
        ),
        sa.ForeignKeyConstraint(
            ["generation"],
            ["search_generations.generation"],
            name="fk_search_page_state_generation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "typeof(page_uid) = 'blob' AND length(page_uid) = 16", name="ck_search_page_state_uid"
        ),
        sa.CheckConstraint(
            "revision_number >= 1 AND document_count >= 1", name="ck_search_page_state_counts"
        ),
        sa.CheckConstraint(
            "snapshot_sha256 IS NULL OR "
            "(typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32)",
            name="ck_search_page_state_snapshot",
        ),
        sa.CheckConstraint(
            "typeof(source_sha256) = 'blob' AND length(source_sha256) = 32",
            name="ck_search_page_state_source",
        ),
    )
    op.create_index(
        "ix_search_page_state_scope_time",
        "search_page_state",
        ["generation", "library_id", "section_id", "occurred_at", "page_id"],
    )
    op.create_table(
        "search_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.String(32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(16), nullable=False),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("document_key", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(9), nullable=False),
        sa.Column("file_name", sa.Text()),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.LargeBinary(32), nullable=False),
        sa.UniqueConstraint(
            "generation",
            "library_id",
            "page_uid",
            "document_key",
            name="uq_search_documents_source",
        ),
        sa.ForeignKeyConstraint(
            ["generation", "library_id", "page_uid", "revision_id", "revision_number"],
            [
                "search_page_state.generation",
                "search_page_state.library_id",
                "search_page_state.page_uid",
                "search_page_state.revision_id",
                "search_page_state.revision_number",
            ],
            name="fk_search_documents_exact_page_state",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("id >= 1", name="ck_search_documents_id"),
        sa.CheckConstraint(
            "source_kind IN ('title', 'file_name', 'file_text')", name="ck_search_documents_kind"
        ),
        sa.CheckConstraint(
            "(source_kind = 'title' AND file_name IS NULL) OR "
            "(source_kind != 'title' AND file_name IS NOT NULL AND length(file_name) >= 1)",
            name="ck_search_documents_file_name",
        ),
        sa.CheckConstraint("length(document_key) >= 1", name="ck_search_documents_key"),
        sa.CheckConstraint(
            "typeof(source_sha256) = 'blob' AND length(source_sha256) = 32",
            name="ck_search_documents_source",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_search_documents_page", "search_documents", ["generation", "library_id", "page_uid"]
    )
    # FTS5 has no FK to search_documents. The future projector must insert and
    # delete equal rowids inside the same authority transaction, and rebuild
    # validation must detect missing/extra rows before activating a generation.
    op.execute(sa.text("CREATE VIRTUAL TABLE search_terms USING fts5(grams, tokenize='unicode61')"))
    _create_dirty_triggers()
    # Existing Pages are not silently considered indexed after this migration.
    op.execute(
        sa.text(
            "INSERT INTO search_dirty_pages (library_id, page_uid, seq) "
            "SELECT library_id, page_uid, 1 FROM pages"
        )
    )
    op.execute(
        sa.text(
            "UPDATE search_meta SET dirty_sequence = 1 WHERE "
            "EXISTS (SELECT 1 FROM search_dirty_pages)"
        )
    )


def downgrade() -> None:
    ready = (
        op.get_bind()
        .exec_driver_sql("SELECT ready FROM search_meta WHERE singleton = 1")
        .scalar_one()
    )
    if ready != 0:
        raise RuntimeError("Disable search before dropping its derived index.")
    for table in (
        "page_tags",
        "revisions",
        "revision_file_sets",
        "revision_files",
        "revision_file_seals",
    ):
        for event in ("insert", "update", "delete"):
            op.execute(sa.text(f"DROP TRIGGER trg_search_{table}_{event}"))
        op.execute(sa.text(f"DROP TRIGGER trg_search_{table}_update_old"))
    for name in (
        "trg_search_pages_insert",
        "trg_search_pages_delete",
        "trg_search_pages_update_old",
        "trg_search_pages_update_new",
    ):
        op.execute(sa.text(f"DROP TRIGGER {name}"))
    op.execute(sa.text("DROP TABLE search_terms"))
    op.drop_index("ix_search_documents_page", table_name="search_documents")
    op.drop_table("search_documents")
    op.drop_index("ix_search_page_state_scope_time", table_name="search_page_state")
    op.drop_table("search_page_state")
    op.drop_index("ix_search_dirty_pages_seq", table_name="search_dirty_pages")
    op.drop_table("search_dirty_pages")
    op.drop_table("search_meta")
    op.drop_table("search_generations")
