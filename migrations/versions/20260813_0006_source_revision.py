"""Associate PageSource provenance with exact Revisions.

Revision ID: 20260813_0006
Revises: 20260813_0005
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0006"
down_revision: str | None = "20260813_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNRESOLVED_LEGACY_SOURCES = sa.text(
    "SELECT count(*) FROM page_sources AS source "
    "WHERE NOT EXISTS ("
    "SELECT 1 FROM pages AS page "
    "JOIN revisions AS revision "
    "ON revision.library_id = page.library_id "
    "AND revision.page_uid = page.page_uid "
    "AND revision.revision_id = page.current_revision_id "
    "AND revision.revision_number = page.current_revision_number "
    "WHERE page.library_id = source.library_id "
    "AND page.page_uid = source.page_uid)"
)

_UNRESOLVED_BACKFILLED_SOURCES = sa.text(
    "SELECT count(*) FROM page_sources AS source "
    "WHERE source.revision_id IS NULL OR source.revision_number IS NULL "
    "OR NOT EXISTS ("
    "SELECT 1 FROM revisions AS revision "
    "WHERE revision.library_id = source.library_id "
    "AND revision.page_uid = source.page_uid "
    "AND revision.revision_id = source.revision_id "
    "AND revision.revision_number = source.revision_number)"
)


def _require_resolved_sources(statement: sa.TextClause) -> None:
    unresolved = op.get_bind().execute(statement).scalar_one()
    if unresolved != 0:
        raise RuntimeError(
            "Existing PageSource rows cannot be associated with exact current Revisions."
        )


def upgrade() -> None:
    # Fail before the first DDL statement if any legacy row cannot be proven.
    _require_resolved_sources(_UNRESOLVED_LEGACY_SOURCES)

    op.add_column(
        "page_sources",
        sa.Column("revision_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "page_sources",
        sa.Column("revision_number", sa.BigInteger(), nullable=True),
    )

    # Legacy Source rows predate Revision association. Their owning Page is
    # already protected by a composite FK, and the Page current pointer is
    # protected by an exact Revision FK. Only after proving both joins for
    # every row do we deterministically associate legacy provenance with the
    # owning Page's current Revision. No identifier is invented or discarded.
    op.execute(
        sa.text(
            "UPDATE page_sources AS source SET "
            "revision_id = (SELECT page.current_revision_id FROM pages AS page "
            "WHERE page.library_id = source.library_id "
            "AND page.page_uid = source.page_uid), "
            "revision_number = (SELECT page.current_revision_number FROM pages AS page "
            "WHERE page.library_id = source.library_id "
            "AND page.page_uid = source.page_uid)"
        )
    )
    _require_resolved_sources(_UNRESOLVED_BACKFILLED_SOURCES)

    with op.batch_alter_table("page_sources", recreate="always") as batch_op:
        batch_op.alter_column(
            "revision_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.alter_column(
            "revision_number",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_page_sources_revision_id_wire",
            "typeof(revision_id) = 'text' AND length(revision_id) = 36 "
            "AND substr(revision_id, 1, 4) = 'rev_' "
            "AND substr(revision_id, 5) NOT GLOB '*[^0-9a-f]*'",
        )
        batch_op.create_check_constraint(
            "ck_page_sources_revision_number",
            "revision_number BETWEEN 1 AND 9223372036854775807",
        )
        batch_op.create_foreign_key(
            "fk_page_sources_library_page_revision_revisions",
            "revisions",
            ["library_id", "page_uid", "revision_id", "revision_number"],
            ["library_id", "page_uid", "revision_id", "revision_number"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    # Preserve every legacy Source field and row while intentionally removing
    # only the Revision association introduced by this migration.
    with op.batch_alter_table("page_sources", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_page_sources_library_page_revision_revisions",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_page_sources_revision_number",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_page_sources_revision_id_wire",
            type_="check",
        )
        batch_op.drop_column("revision_number")
        batch_op.drop_column("revision_id")
