"""Database models for Library-scoped Tag definitions and Page associations."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.models import Base


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        PrimaryKeyConstraint("library_id", "id", name="pk_tags"),
        UniqueConstraint("library_id", "match_key", name="uq_tags_library_match_key"),
        ForeignKeyConstraint(
            ["library_id"], ["libraries.id"], name="fk_tags_library", ondelete="RESTRICT"
        ),
        CheckConstraint("length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'", name="ck_tags_id"),
        CheckConstraint(
            "typeof(display_name) = 'text' AND length(display_name) BETWEEN 1 AND 100 "
            "AND length(CAST(display_name AS BLOB)) <= 255 "
            "AND display_name = trim(display_name) "
            "AND instr(display_name, char(0)) = 0 "
            "AND instr(display_name, char(10)) = 0 "
            "AND instr(display_name, char(13)) = 0",
            name="ck_tags_display_name",
        ),
        CheckConstraint(
            "typeof(match_key) = 'text' AND length(match_key) BETWEEN 1 AND 100 "
            "AND length(CAST(match_key AS BLOB)) <= 255 "
            "AND match_key = trim(match_key) "
            "AND instr(match_key, char(0)) = 0 "
            "AND instr(match_key, char(10)) = 0 "
            "AND instr(match_key, char(13)) = 0",
            name="ck_tags_match_key",
        ),
        CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0", name="ck_tags_created_at"
        ),
    )

    library_id: Mapped[str] = mapped_column(String(32), nullable=False)
    id: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    match_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageTag(Base):
    __tablename__ = "page_tags"
    __table_args__ = (
        PrimaryKeyConstraint("library_id", "page_uid", "tag_id", name="pk_page_tags"),
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_tags_exact_page",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["library_id", "tag_id"],
            ["tags.library_id", "tags.id"],
            name="fk_page_tags_exact_tag",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "typeof(page_uid) = 'blob' AND length(page_uid) = 16", name="ck_page_tags_page_uid"
        ),
        CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0", name="ck_page_tags_created_at"
        ),
        Index("ix_page_tags_library_tag", "library_id", "tag_id"),
    )

    library_id: Mapped[str] = mapped_column(String(32), nullable=False)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    tag_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
