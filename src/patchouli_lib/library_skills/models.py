"""Persistent Library Skill identities and immutable complete package versions."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.models import Base


class LibrarySkill(Base):
    __tablename__ = "library_skills"
    __table_args__ = (
        ForeignKeyConstraint(["library_id"], ["libraries.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["library_id", "id", "current_revision_id", "current_revision_number"],
            [
                "library_skill_revisions.library_id",
                "library_skill_revisions.skill_id",
                "library_skill_revisions.id",
                "library_skill_revisions.number",
            ],
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("library_id", "name"),
        CheckConstraint("length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'"),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND name NOT GLOB '*[^a-z0-9-]*' "
            "AND name NOT GLOB '-*' AND name NOT GLOB '*-' AND instr(name, '--') = 0"
        ),
        CheckConstraint("length(description) <= 4000"),
        CheckConstraint(
            "current_revision_number >= 1 AND created_at >= 0 AND updated_at >= created_at"
        ),
    )
    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    current_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LibrarySkillRevision(Base):
    __tablename__ = "library_skill_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "skill_id"],
            ["library_skills.library_id", "library_skills.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("library_id", "skill_id", "number"),
        UniqueConstraint("library_id", "skill_id", "id", "number"),
        CheckConstraint(
            "length(id) = 36 AND substr(id, 1, 4) = 'rev_' AND substr(id, 5) NOT GLOB '*[^0-9a-f]*'"
        ),
        CheckConstraint("number >= 1 AND created_at >= 0"),
        CheckConstraint("length(name) BETWEEN 1 AND 64 AND length(description) <= 4000"),
        CheckConstraint("file_count BETWEEN 1 AND 64 AND total_size_bytes BETWEEN 0 AND 67108864"),
        CheckConstraint("typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32"),
    )
    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    file_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LibrarySkillFile(Base):
    __tablename__ = "library_skill_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "skill_id", "revision_id"],
            [
                "library_skill_revisions.library_id",
                "library_skill_revisions.skill_id",
                "library_skill_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(path) BETWEEN 1 AND 1024 AND substr(path, 1, 1) != '/' "
            "AND instr(path, char(92)) = 0 AND instr(path, char(0)) = 0"
        ),
        CheckConstraint(
            "typeof(content_bytes) = 'blob' AND size_bytes BETWEEN 0 AND 16777216 "
            "AND length(content_bytes) = size_bytes"
        ),
        CheckConstraint("typeof(content_sha256) = 'blob' AND length(content_sha256) = 32"),
    )
    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    content_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
