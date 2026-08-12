"""Create Page, Revision, identifier registry, counter, and Source storage.

Revision ID: 20260813_0004
Revises: 20260813_0003
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0004"
down_revision: str | None = "20260813_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pages",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("section_id", sa.String(length=32), nullable=False),
        sa.Column("book_id", sa.String(length=32), nullable=False),
        sa.Column("page_id", sa.String(length=80), nullable=False),
        sa.Column("id_scheme", sa.String(length=16), nullable=False),
        sa.Column("id_timestamp_micros", sa.BigInteger(), nullable=False),
        sa.Column("base_slug", sa.String(length=48), nullable=False),
        sa.Column("collision_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("page_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=False),
        sa.Column("current_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "typeof(page_uid) = 'blob' AND length(page_uid) = 16",
            name="ck_pages_page_uid_128_bit",
        ),
        sa.CheckConstraint(
            "typeof(page_id) = 'text' AND length(page_id) BETWEEN 1 AND 80 "
            "AND page_id NOT GLOB '*[^a-z0-9-]*'",
            name="ck_pages_page_id_wire",
        ),
        sa.CheckConstraint("id_scheme = 'page-v1'", name="ck_pages_id_scheme"),
        sa.CheckConstraint(
            "id_timestamp_micros BETWEEN -62135596800000000 AND 253402300799999999 "
            "AND id_timestamp_micros % 1000 = 0",
            name="ck_pages_id_timestamp_micros",
        ),
        sa.CheckConstraint(
            "typeof(base_slug) = 'text' AND length(base_slug) BETWEEN 1 AND 48 "
            "AND base_slug NOT GLOB '*[^a-z0-9-]*' "
            "AND base_slug NOT GLOB '-*' AND base_slug NOT GLOB '*-' "
            "AND instr(base_slug, '--') = 0",
            name="ck_pages_base_slug",
        ),
        sa.CheckConstraint(
            "collision_ordinal BETWEEN 1 AND 9999999999",
            name="ck_pages_collision_ordinal",
        ),
        sa.CheckConstraint(
            "typeof(title) = 'text' AND length(title) >= 1 AND instr(title, char(0)) = 0",
            name="ck_pages_title",
        ),
        sa.CheckConstraint(
            "length(page_type) BETWEEN 1 AND 32 AND page_type = trim(page_type) "
            "AND instr(page_type, char(0)) = 0",
            name="ck_pages_page_type",
        ),
        sa.CheckConstraint(
            "occurred_at BETWEEN -62135596800000000 AND 253402300799999999",
            name="ck_pages_occurred_at",
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at "
            "AND (deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name="ck_pages_lifecycle_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["book_id", "section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            name="fk_pages_book_section_library_books",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "current_revision_id", "current_revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_pages_current_revision_page_library_revisions",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_id", "page_uid"],
            [
                "page_identifier_registry.library_id",
                "page_identifier_registry.identifier_text",
                "page_identifier_registry.page_uid",
            ],
            name="fk_pages_canonical_identifier_registry",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("library_id", "page_uid", name="pk_pages"),
        sa.UniqueConstraint("library_id", "page_id", name="uq_pages_library_id_page_id"),
        sa.UniqueConstraint(
            "library_id",
            "page_uid",
            "current_revision_id",
            "current_revision_number",
            name="uq_pages_library_page_current_revision",
        ),
    )

    op.create_table(
        "revisions",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("content_md", sa.LargeBinary(length=2097152), nullable=False),
        sa.Column("content_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.LargeBinary(length=32), nullable=False),
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
        sa.CheckConstraint(
            "typeof(content_md) = 'blob' AND length(content_md) BETWEEN 1 AND 2097152 "
            "AND instr(content_md, x'00') = 0",
            name="ck_revisions_content_md",
        ),
        sa.CheckConstraint(
            "content_size_bytes = length(content_md)",
            name="ck_revisions_content_size_bytes",
        ),
        sa.CheckConstraint(
            "typeof(content_sha256) = 'blob' AND length(content_sha256) = 32",
            name="ck_revisions_content_sha256",
        ),
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
            "revision_id",
            "page_uid",
            "library_id",
            name="uq_revisions_revision_page_library",
        ),
        sa.UniqueConstraint(
            "library_id",
            "page_uid",
            "revision_number",
            name="uq_revisions_library_page_number",
        ),
        sa.UniqueConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="uq_revisions_library_page_id_number",
        ),
    )

    op.create_table(
        "page_revision_append_guards",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "revision_number BETWEEN 2 AND 9223372036854775807",
            name="ck_page_revision_append_guards_revision_number",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_page_revision_append_guards_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "pages.library_id",
                "pages.page_uid",
                "pages.current_revision_id",
                "pages.current_revision_number",
            ],
            name="fk_page_revision_append_guards_current_page",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint(
            "library_id",
            "page_uid",
            name="pk_page_revision_append_guards",
        ),
    )

    op.create_table(
        "page_identifier_registry",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("identifier_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("identifier_text", sa.String(length=80), nullable=False),
        sa.Column("id_scheme", sa.String(length=16), nullable=False),
        sa.Column("identifier_kind", sa.String(length=16), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(identifier_digest) = 'blob' AND length(identifier_digest) = 32",
            name="ck_page_identifier_registry_digest",
        ),
        sa.CheckConstraint(
            "typeof(identifier_text) = 'text' "
            "AND length(identifier_text) BETWEEN 1 AND 80 "
            "AND identifier_text NOT GLOB '*[^a-z0-9-]*'",
            name="ck_page_identifier_registry_text",
        ),
        sa.CheckConstraint(
            "id_scheme = 'page-v1'",
            name="ck_page_identifier_registry_scheme",
        ),
        sa.CheckConstraint(
            "identifier_kind IN ('canonical', 'alias')",
            name="ck_page_identifier_registry_kind",
        ),
        sa.CheckConstraint(
            "created_at >= 0",
            name="ck_page_identifier_registry_created_at",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_identifier_registry_library_page_pages",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "library_id",
            "identifier_digest",
            name="pk_page_identifier_registry",
        ),
        sa.UniqueConstraint(
            "library_id",
            "identifier_text",
            name="uq_page_identifier_registry_library_text",
        ),
        sa.UniqueConstraint(
            "library_id",
            "identifier_text",
            "page_uid",
            name="uq_page_identifier_registry_library_text_page",
        ),
    )
    op.create_index(
        "uq_page_identifier_registry_library_page_canonical",
        "page_identifier_registry",
        ["library_id", "page_uid"],
        unique=True,
        sqlite_where=sa.text("identifier_kind = 'canonical'"),
    )

    op.create_table(
        "page_id_collision_counters",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("id_scheme", sa.String(length=16), nullable=False),
        sa.Column("id_timestamp_micros", sa.BigInteger(), nullable=False),
        sa.Column("base_slug", sa.String(length=48), nullable=False),
        sa.Column("next_ordinal", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "id_scheme = 'page-v1'",
            name="ck_page_id_collision_counters_scheme",
        ),
        sa.CheckConstraint(
            "id_timestamp_micros BETWEEN -62135596800000000 AND 253402300799999999 "
            "AND id_timestamp_micros % 1000 = 0",
            name="ck_page_id_collision_counters_timestamp",
        ),
        sa.CheckConstraint(
            "typeof(base_slug) = 'text' AND length(base_slug) BETWEEN 1 AND 48 "
            "AND base_slug NOT GLOB '*[^a-z0-9-]*' "
            "AND base_slug NOT GLOB '-*' AND base_slug NOT GLOB '*-' "
            "AND instr(base_slug, '--') = 0",
            name="ck_page_id_collision_counters_base_slug",
        ),
        sa.CheckConstraint(
            "next_ordinal BETWEEN 2 AND 10000000000",
            name="ck_page_id_collision_counters_next_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_page_id_collision_counters_library_libraries",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "library_id",
            "id_scheme",
            "id_timestamp_micros",
            "base_slug",
            name="pk_page_id_collision_counters",
        ),
    )

    op.create_table(
        "page_sources",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(source_id) = 32 AND source_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_page_sources_source_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(kind) BETWEEN 1 AND 100 AND kind = trim(kind) AND instr(kind, char(0)) = 0",
            name="ck_page_sources_kind",
        ),
        sa.CheckConstraint(
            "locator IS NULL OR (length(locator) >= 1 AND instr(locator, char(0)) = 0)",
            name="ck_page_sources_locator",
        ),
        sa.CheckConstraint(
            "captured_at IS NULL OR captured_at BETWEEN -62135596800000000 AND 253402300799999999",
            name="ck_page_sources_captured_at",
        ),
        sa.CheckConstraint("created_at >= 0", name="ck_page_sources_created_at"),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_sources_library_page_pages",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("library_id", "source_id", name="pk_page_sources"),
    )

    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_initial_revision_number "
            "BEFORE INSERT ON pages WHEN NEW.current_revision_number != 1 BEGIN "
            "SELECT RAISE(ABORT, 'Initial Page revision number must be 1.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_sequential_insert "
            "BEFORE INSERT ON revisions WHEN NOT EXISTS ("
            "SELECT 1 FROM pages WHERE library_id = NEW.library_id "
            "AND page_uid = NEW.page_uid AND ((NEW.revision_number = 1 "
            "AND current_revision_number = 1 "
            "AND current_revision_id = NEW.revision_id "
            "AND NOT EXISTS (SELECT 1 FROM revisions existing "
            "WHERE existing.library_id = NEW.library_id "
            "AND existing.page_uid = NEW.page_uid)) OR "
            "(NEW.revision_number = current_revision_number + 1 "
            "AND EXISTS (SELECT 1 FROM revisions current_revision "
            "WHERE current_revision.library_id = NEW.library_id "
            "AND current_revision.page_uid = NEW.page_uid "
            "AND current_revision.revision_id = pages.current_revision_id "
            "AND current_revision.revision_number = pages.current_revision_number)))) BEGIN "
            "SELECT RAISE(ABORT, 'Revision number must append sequentially.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_create_append_guard "
            "AFTER INSERT ON revisions WHEN NEW.revision_number > 1 BEGIN "
            "INSERT INTO page_revision_append_guards "
            "(library_id, page_uid, revision_id, revision_number) VALUES "
            "(NEW.library_id, NEW.page_uid, NEW.revision_id, NEW.revision_number); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_current_revision_advance "
            "BEFORE UPDATE OF current_revision_id, current_revision_number ON pages "
            "WHEN NEW.current_revision_id IS NOT OLD.current_revision_id "
            "OR NEW.current_revision_number IS NOT OLD.current_revision_number BEGIN "
            "SELECT CASE WHEN NEW.current_revision_number != OLD.current_revision_number + 1 "
            "OR NOT EXISTS (SELECT 1 FROM page_revision_append_guards guard "
            "WHERE guard.library_id = OLD.library_id "
            "AND guard.page_uid = OLD.page_uid "
            "AND guard.revision_id = NEW.current_revision_id "
            "AND guard.revision_number = NEW.current_revision_number) "
            "THEN RAISE(ABORT, 'Page current Revision must advance sequentially.') END; END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_clear_append_guard "
            "AFTER UPDATE OF current_revision_id, current_revision_number ON pages "
            "WHEN NEW.current_revision_id IS NOT OLD.current_revision_id "
            "OR NEW.current_revision_number IS NOT OLD.current_revision_number BEGIN "
            "DELETE FROM page_revision_append_guards "
            "WHERE library_id = NEW.library_id AND page_uid = NEW.page_uid "
            "AND revision_id = NEW.current_revision_id "
            "AND revision_number = NEW.current_revision_number; END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_revision_append_guards_no_update "
            "BEFORE UPDATE ON page_revision_append_guards BEGIN "
            "SELECT RAISE(ABORT, 'Page Revision append guards are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_revision_append_guards_safe_delete "
            "BEFORE DELETE ON page_revision_append_guards WHEN NOT EXISTS ("
            "SELECT 1 FROM pages WHERE library_id = OLD.library_id "
            "AND page_uid = OLD.page_uid AND current_revision_id = OLD.revision_id "
            "AND current_revision_number = OLD.revision_number) BEGIN "
            "SELECT RAISE(ABORT, 'Pending Page Revision append cannot be discarded.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_immutable_update "
            "BEFORE UPDATE ON revisions BEGIN "
            "SELECT RAISE(ABORT, 'Revision rows are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_revisions_immutable_delete "
            "BEFORE DELETE ON revisions BEGIN "
            "SELECT RAISE(ABORT, 'Revision rows are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_id_collision_counters_monotonic "
            "BEFORE UPDATE ON page_id_collision_counters "
            "WHEN NEW.library_id IS NOT OLD.library_id "
            "OR NEW.id_scheme IS NOT OLD.id_scheme "
            "OR NEW.id_timestamp_micros IS NOT OLD.id_timestamp_micros "
            "OR NEW.base_slug IS NOT OLD.base_slug "
            "OR NEW.next_ordinal < OLD.next_ordinal BEGIN "
            "SELECT RAISE(ABORT, 'Page identifier counters are monotonic.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_id_collision_counters_no_delete "
            "BEFORE DELETE ON page_id_collision_counters BEGIN "
            "SELECT RAISE(ABORT, 'Page identifier counters are permanent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_canonical_identifier_on_insert "
            "BEFORE INSERT ON pages WHEN "
            "EXISTS (SELECT 1 FROM page_identifier_registry "
            "WHERE library_id = NEW.library_id AND page_uid = NEW.page_uid) "
            "AND NOT EXISTS (SELECT 1 FROM page_identifier_registry "
            "WHERE library_id = NEW.library_id AND page_uid = NEW.page_uid "
            "AND identifier_text = NEW.page_id AND identifier_kind = 'canonical') BEGIN "
            "SELECT RAISE(ABORT, 'Page canonical identifier is inconsistent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_identifier_registry_kind_on_insert "
            "BEFORE INSERT ON page_identifier_registry WHEN EXISTS ("
            "SELECT 1 FROM pages WHERE library_id = NEW.library_id "
            "AND page_uid = NEW.page_uid AND ((NEW.identifier_kind = 'canonical' "
            "AND page_id IS NOT NEW.identifier_text) OR "
            "(NEW.identifier_kind = 'alias' AND page_id IS NEW.identifier_text))) BEGIN "
            "SELECT RAISE(ABORT, 'Page identifier kind is inconsistent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_identifier_registry_stable "
            "BEFORE UPDATE ON page_identifier_registry WHEN "
            "NEW.library_id IS NOT OLD.library_id "
            "OR NEW.identifier_digest IS NOT OLD.identifier_digest "
            "OR NEW.identifier_text IS NOT OLD.identifier_text "
            "OR NEW.id_scheme IS NOT OLD.id_scheme "
            "OR NEW.identifier_kind IS NOT OLD.identifier_kind "
            "OR NEW.page_uid IS NOT OLD.page_uid "
            "OR NEW.created_at IS NOT OLD.created_at "
            "BEGIN SELECT RAISE(ABORT, 'Page identifier reservations are stable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_identifier_registry_no_delete "
            "BEFORE DELETE ON page_identifier_registry BEGIN "
            "SELECT RAISE(ABORT, 'Page identifier reservations are permanent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_stable_identity "
            "BEFORE UPDATE ON pages WHEN "
            "NEW.library_id IS NOT OLD.library_id "
            "OR NEW.page_uid IS NOT OLD.page_uid "
            "OR NEW.page_id IS NOT OLD.page_id "
            "OR NEW.id_scheme IS NOT OLD.id_scheme "
            "OR NEW.id_timestamp_micros IS NOT OLD.id_timestamp_micros "
            "OR NEW.base_slug IS NOT OLD.base_slug "
            "OR NEW.collision_ordinal IS NOT OLD.collision_ordinal "
            "OR NEW.occurred_at IS NOT OLD.occurred_at "
            "OR NEW.created_at IS NOT OLD.created_at BEGIN "
            "SELECT RAISE(ABORT, 'Page identity is stable.'); END"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_pages_stable_identity"))
    op.execute(sa.text("DROP TRIGGER trg_page_identifier_registry_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_page_identifier_registry_stable"))
    op.execute(sa.text("DROP TRIGGER trg_page_identifier_registry_kind_on_insert"))
    op.execute(sa.text("DROP TRIGGER trg_pages_canonical_identifier_on_insert"))
    op.execute(sa.text("DROP TRIGGER trg_page_id_collision_counters_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_page_id_collision_counters_monotonic"))
    op.execute(sa.text("DROP TRIGGER trg_page_revision_append_guards_safe_delete"))
    op.execute(sa.text("DROP TRIGGER trg_page_revision_append_guards_no_update"))
    op.execute(sa.text("DROP TRIGGER trg_pages_clear_append_guard"))
    op.execute(sa.text("DROP TRIGGER trg_pages_current_revision_advance"))
    op.execute(sa.text("DROP TRIGGER trg_revisions_create_append_guard"))
    op.execute(sa.text("DROP TRIGGER trg_revisions_sequential_insert"))
    op.execute(sa.text("DROP TRIGGER trg_pages_initial_revision_number"))
    op.execute(sa.text("DROP TRIGGER trg_revisions_immutable_delete"))
    op.execute(sa.text("DROP TRIGGER trg_revisions_immutable_update"))

    # Explicitly empty the deferred Page/Revision cycle so a populated downgrade
    # removes only this migration's data while foreign-key enforcement stays on.
    op.execute(sa.text("DELETE FROM page_revision_append_guards"))
    op.execute(sa.text("DELETE FROM page_sources"))
    op.execute(sa.text("DELETE FROM page_identifier_registry"))
    op.execute(sa.text("DELETE FROM revisions"))
    op.execute(sa.text("DELETE FROM pages"))

    op.drop_table("page_sources")
    op.drop_table("page_revision_append_guards")
    op.drop_table("page_id_collision_counters")
    op.drop_index(
        "uq_page_identifier_registry_library_page_canonical",
        table_name="page_identifier_registry",
    )
    op.drop_table("page_identifier_registry")
    op.drop_table("revisions")
    op.drop_table("pages")
