"""Library-owned, immutable multi-file Skill packages.

Revision ID: 20261020_0036
Revises: 20261020_0035
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0036"
down_revision: str | None = "20261020_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin() -> None:
    driver = op.get_bind().connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Library Skill migration requires SQLite.")
    if not driver.in_transaction:
        op.get_bind().exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin()
    op.create_table(
        "library_skills",
        sa.Column("library_id", sa.String(32), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_revision_id", sa.String(36), nullable=False),
        sa.Column("current_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["library_id", "id", "current_revision_id", "current_revision_number"],
            [
                "library_skill_revisions.library_id",
                "library_skill_revisions.skill_id",
                "library_skill_revisions.id",
                "library_skill_revisions.number",
            ],
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("library_id", "name"),
        sa.CheckConstraint("length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'"),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND name NOT GLOB '*[^a-z0-9-]*' "
            "AND name NOT GLOB '-*' AND name NOT GLOB '*-' AND instr(name, '--') = 0"
        ),
        sa.CheckConstraint("length(description) <= 4000"),
        sa.CheckConstraint(
            "current_revision_number >= 1 AND created_at >= 0 AND updated_at >= created_at"
        ),
    )
    op.create_table(
        "library_skill_revisions",
        sa.Column("library_id", sa.String(32), primary_key=True),
        sa.Column("skill_id", sa.String(32), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("file_count", sa.BigInteger(), nullable=False),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["library_id", "skill_id"],
            ["library_skills.library_id", "library_skills.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("library_id", "skill_id", "number"),
        sa.UniqueConstraint("library_id", "skill_id", "id", "number"),
        sa.CheckConstraint(
            "length(id) = 36 AND substr(id, 1, 4) = 'rev_' AND substr(id, 5) NOT GLOB '*[^0-9a-f]*'"
        ),
        sa.CheckConstraint("number >= 1 AND created_at >= 0"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 64 AND length(description) <= 4000"),
        sa.CheckConstraint(
            "file_count BETWEEN 1 AND 64 AND total_size_bytes BETWEEN 0 AND 67108864"
        ),
        sa.CheckConstraint("typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32"),
    )
    op.create_table(
        "library_skill_files",
        sa.Column("library_id", sa.String(32), primary_key=True),
        sa.Column("skill_id", sa.String(32), primary_key=True),
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("content_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.LargeBinary(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["library_id", "skill_id", "revision_id"],
            [
                "library_skill_revisions.library_id",
                "library_skill_revisions.skill_id",
                "library_skill_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(path) BETWEEN 1 AND 1024 AND substr(path, 1, 1) != '/' "
            "AND instr(path, char(92)) = 0 AND instr(path, char(0)) = 0"
        ),
        sa.CheckConstraint(
            "typeof(content_bytes) = 'blob' AND size_bytes BETWEEN 0 AND 16777216 "
            "AND length(content_bytes) = size_bytes"
        ),
        sa.CheckConstraint("typeof(content_sha256) = 'blob' AND length(content_sha256) = 32"),
    )
    for table in ("library_skill_revisions", "library_skill_files"):
        for operation in ("UPDATE", "DELETE"):
            op.get_bind().exec_driver_sql(
                f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'Skill history is immutable'); END"
            )
    op.get_bind().exec_driver_sql(
        "CREATE TRIGGER library_skill_files_sealed BEFORE INSERT ON library_skill_files "
        "WHEN (SELECT count(*) FROM library_skill_files WHERE library_id=NEW.library_id "
        "AND skill_id=NEW.skill_id AND revision_id=NEW.revision_id) >= "
        "(SELECT file_count FROM library_skill_revisions WHERE library_id=NEW.library_id "
        "AND skill_id=NEW.skill_id AND id=NEW.revision_id) "
        "BEGIN SELECT RAISE(ABORT, 'Skill file snapshot is sealed'); END"
    )


def downgrade() -> None:
    _begin()
    if op.get_bind().exec_driver_sql("SELECT 1 FROM library_skills LIMIT 1").first():
        raise RuntimeError("Cannot downgrade while Library Skill data exists.")
    op.get_bind().exec_driver_sql("DROP TRIGGER library_skill_files_sealed")
    for table in ("library_skill_files", "library_skill_revisions"):
        for operation in ("delete", "update"):
            op.get_bind().exec_driver_sql(f"DROP TRIGGER {table}_no_{operation}")
        op.drop_table(table)
    op.drop_table("library_skills")
