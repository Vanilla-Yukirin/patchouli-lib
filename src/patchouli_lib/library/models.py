from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from patchouli_lib.models import Base

OPAQUE_ID_LENGTH = 32
NAME_MAX_LENGTH = 200
TEXT_MAX_LENGTH = 4_000


class Library(Base):
    __tablename__ = "libraries"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_libraries_id_lower_hex",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_libraries_name",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name="ck_libraries_timestamps",
        ),
        UniqueConstraint("name", name="uq_libraries_name"),
    )

    id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    sections: Mapped[list[Section]] = relationship(
        back_populates="library",
        passive_deletes=True,
    )


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_sections_library_id_libraries",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_sections_id_lower_hex",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_sections_name",
        ),
        CheckConstraint(
            "length(description) <= 4000",
            name="ck_sections_description",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name="ck_sections_timestamps",
        ),
        UniqueConstraint("id", "library_id", name="uq_sections_id_library_id"),
        UniqueConstraint("library_id", "name", name="uq_sections_library_id_name"),
    )

    id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    library: Mapped[Library] = relationship(back_populates="sections")
    books: Mapped[list[Book]] = relationship(
        back_populates="section",
        passive_deletes=True,
    )


class Book(Base):
    __tablename__ = "books"
    __table_args__ = (
        ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_books_section_library_sections",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_books_id_lower_hex",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_books_name",
        ),
        CheckConstraint(
            "length(summary) <= 4000",
            name="ck_books_summary",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name="ck_books_timestamps",
        ),
        UniqueConstraint(
            "id",
            "section_id",
            "library_id",
            name="uq_books_id_section_id_library_id",
        ),
        UniqueConstraint(
            "library_id",
            "section_id",
            "name",
            name="uq_books_library_id_section_id_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    section_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    section: Mapped[Section] = relationship(back_populates="books")
