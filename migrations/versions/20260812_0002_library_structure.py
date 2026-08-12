"""Create Library, Section, and Book structure.

Revision ID: 20260812_0002
Revises: 20260810_0001
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0002"
down_revision: str | None = "20260810_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "libraries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_libraries_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_libraries_name",
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name="ck_libraries_timestamps",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_libraries"),
        sa.UniqueConstraint("name", name="uq_libraries_name"),
    )

    op.create_table(
        "sections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_sections_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_sections_name",
        ),
        sa.CheckConstraint(
            "length(description) <= 4000",
            name="ck_sections_description",
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name="ck_sections_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_sections_library_id_libraries",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sections"),
        sa.UniqueConstraint("id", "library_id", name="uq_sections_id_library_id"),
        sa.UniqueConstraint("library_id", "name", name="uq_sections_library_id_name"),
    )

    op.create_table(
        "books",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("section_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_books_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_books_name",
        ),
        sa.CheckConstraint(
            "length(summary) <= 4000",
            name="ck_books_summary",
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name="ck_books_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_books_section_library_sections",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_books"),
        sa.UniqueConstraint(
            "id",
            "section_id",
            "library_id",
            name="uq_books_id_section_id_library_id",
        ),
        sa.UniqueConstraint(
            "library_id",
            "section_id",
            "name",
            name="uq_books_library_id_section_id_name",
        ),
    )


def downgrade() -> None:
    op.drop_table("books")
    op.drop_table("sections")
    op.drop_table("libraries")
