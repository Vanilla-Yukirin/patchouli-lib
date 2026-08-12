"""Library-scoped Page, Revision, identifier, and Source persistence models."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.identifiers import (
    MAX_BASE_SLUG_BYTES,
    MAX_COLLISION_ORDINAL,
    MAX_PAGE_ID_BYTES,
    PAGE_ID_SCHEME,
    RANDOM_IDENTIFIER_BYTES,
)
from patchouli_lib.library.models import OPAQUE_ID_LENGTH
from patchouli_lib.models import Base

REVISION_ID_LENGTH = 36
IDENTIFIER_DIGEST_BYTES = 32
CONTENT_SHA256_BYTES = 32
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
SOURCE_KIND_MAX_LENGTH = 100
PAGE_TYPE_MAX_LENGTH = 32
ID_SCHEME_MAX_LENGTH = 16
IDENTIFIER_KIND_MAX_LENGTH = 16
MIN_OCCURRENCE_MICROSECONDS = -62_135_596_800_000_000
MAX_OCCURRENCE_MICROSECONDS = 253_402_300_799_999_999
EXHAUSTED_COLLISION_ORDINAL = MAX_COLLISION_ORDINAL + 1

_PAGE_ID_CHECK = (
    "typeof(page_id) = 'text' AND length(page_id) BETWEEN 1 AND 80 "
    "AND page_id NOT GLOB '*[^a-z0-9-]*'"
)
_BASE_SLUG_CHECK = (
    "typeof(base_slug) = 'text' AND length(base_slug) BETWEEN 1 AND 48 "
    "AND base_slug NOT GLOB '*[^a-z0-9-]*' "
    "AND base_slug NOT GLOB '-*' AND base_slug NOT GLOB '*-' "
    "AND instr(base_slug, '--') = 0"
)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["book_id", "section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            name="fk_pages_book_section_library_books",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["library_id", "page_id", "page_uid"],
            [
                "page_identifier_registry.library_id",
                "page_identifier_registry.identifier_text",
                "page_identifier_registry.page_uid",
            ],
            name="fk_pages_canonical_identifier_registry",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            f"typeof(page_uid) = 'blob' AND length(page_uid) = {RANDOM_IDENTIFIER_BYTES}",
            name="ck_pages_page_uid_128_bit",
        ),
        CheckConstraint(_PAGE_ID_CHECK, name="ck_pages_page_id_wire"),
        CheckConstraint(
            f"id_scheme = '{PAGE_ID_SCHEME}'",
            name="ck_pages_id_scheme",
        ),
        CheckConstraint(
            f"id_timestamp_micros BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS} AND id_timestamp_micros % 1000 = 0",
            name="ck_pages_id_timestamp_micros",
        ),
        CheckConstraint(_BASE_SLUG_CHECK, name="ck_pages_base_slug"),
        CheckConstraint(
            f"collision_ordinal BETWEEN 1 AND {MAX_COLLISION_ORDINAL}",
            name="ck_pages_collision_ordinal",
        ),
        CheckConstraint(
            "typeof(title) = 'text' AND length(title) >= 1 AND instr(title, char(0)) = 0",
            name="ck_pages_title",
        ),
        CheckConstraint(
            f"length(page_type) BETWEEN 1 AND {PAGE_TYPE_MAX_LENGTH} "
            "AND page_type = trim(page_type) AND instr(page_type, char(0)) = 0",
            name="ck_pages_page_type",
        ),
        CheckConstraint(
            f"occurred_at BETWEEN {MIN_OCCURRENCE_MICROSECONDS} AND {MAX_OCCURRENCE_MICROSECONDS}",
            name="ck_pages_occurred_at",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at "
            "AND (deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name="ck_pages_lifecycle_timestamps",
        ),
        UniqueConstraint("library_id", "page_id", name="uq_pages_library_id_page_id"),
        UniqueConstraint(
            "library_id",
            "page_uid",
            "current_revision_id",
            "current_revision_number",
            name="uq_pages_library_page_current_revision",
        ),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    page_uid: Mapped[bytes] = mapped_column(
        LargeBinary(RANDOM_IDENTIFIER_BYTES),
        primary_key=True,
    )
    section_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    book_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    page_id: Mapped[str] = mapped_column(String(MAX_PAGE_ID_BYTES), nullable=False)
    id_scheme: Mapped[str] = mapped_column(String(ID_SCHEME_MAX_LENGTH), nullable=False)
    id_timestamp_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_slug: Mapped[str] = mapped_column(String(MAX_BASE_SLUG_BYTES), nullable=False)
    collision_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    page_type: Mapped[str] = mapped_column(String(PAGE_TYPE_MAX_LENGTH), nullable=False)
    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), nullable=False)
    current_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_revisions_library_page_pages",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "typeof(revision_id) = 'text' AND length(revision_id) = 36 "
            "AND substr(revision_id, 1, 4) = 'rev_' "
            "AND substr(revision_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_revisions_revision_id_wire",
        ),
        CheckConstraint(
            f"typeof(page_uid) = 'blob' AND length(page_uid) = {RANDOM_IDENTIFIER_BYTES}",
            name="ck_revisions_page_uid_128_bit",
        ),
        CheckConstraint(
            "revision_number BETWEEN 1 AND 9223372036854775807",
            name="ck_revisions_revision_number",
        ),
        CheckConstraint(
            f"typeof(content_md) = 'blob' AND length(content_md) BETWEEN 1 "
            f"AND {MAX_MARKDOWN_BYTES} AND instr(content_md, x'00') = 0",
            name="ck_revisions_content_md",
        ),
        CheckConstraint(
            "content_size_bytes = length(content_md)",
            name="ck_revisions_content_size_bytes",
        ),
        CheckConstraint(
            f"typeof(content_sha256) = 'blob' AND length(content_sha256) = {CONTENT_SHA256_BYTES}",
            name="ck_revisions_content_sha256",
        ),
        CheckConstraint("created_at >= 0", name="ck_revisions_created_at"),
        UniqueConstraint(
            "revision_id",
            "page_uid",
            "library_id",
            name="uq_revisions_revision_page_library",
        ),
        UniqueConstraint(
            "library_id",
            "page_uid",
            "revision_number",
            name="uq_revisions_library_page_number",
        ),
        UniqueConstraint(
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
            name="uq_revisions_library_page_id_number",
        ),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    revision_id: Mapped[str] = mapped_column(
        String(REVISION_ID_LENGTH),
        primary_key=True,
    )
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), nullable=False)
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_md: Mapped[bytes] = mapped_column(LargeBinary(MAX_MARKDOWN_BYTES), nullable=False)
    content_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[bytes] = mapped_column(
        LargeBinary(CONTENT_SHA256_BYTES),
        nullable=False,
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageRevisionAppendGuard(Base):
    """Internal deferred commit guard for one pending Page revision append."""

    __tablename__ = "page_revision_append_guards"
    __table_args__ = (
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "revision_number BETWEEN 2 AND 9223372036854775807",
            name="ck_page_revision_append_guards_revision_number",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(
        LargeBinary(RANDOM_IDENTIFIER_BYTES),
        primary_key=True,
    )
    revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), nullable=False)
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageIdentifier(Base):
    __tablename__ = "page_identifier_registry"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_identifier_registry_library_page_pages",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"typeof(identifier_digest) = 'blob' "
            f"AND length(identifier_digest) = {IDENTIFIER_DIGEST_BYTES}",
            name="ck_page_identifier_registry_digest",
        ),
        CheckConstraint(
            "typeof(identifier_text) = 'text' "
            "AND length(identifier_text) BETWEEN 1 AND 80 "
            "AND identifier_text NOT GLOB '*[^a-z0-9-]*'",
            name="ck_page_identifier_registry_text",
        ),
        CheckConstraint(
            f"id_scheme = '{PAGE_ID_SCHEME}'",
            name="ck_page_identifier_registry_scheme",
        ),
        CheckConstraint(
            "identifier_kind IN ('canonical', 'alias')",
            name="ck_page_identifier_registry_kind",
        ),
        CheckConstraint("created_at >= 0", name="ck_page_identifier_registry_created_at"),
        UniqueConstraint(
            "library_id",
            "identifier_text",
            name="uq_page_identifier_registry_library_text",
        ),
        UniqueConstraint(
            "library_id",
            "identifier_text",
            "page_uid",
            name="uq_page_identifier_registry_library_text_page",
        ),
        Index(
            "uq_page_identifier_registry_library_page_canonical",
            "library_id",
            "page_uid",
            unique=True,
            sqlite_where=text("identifier_kind = 'canonical'"),
        ),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    identifier_digest: Mapped[bytes] = mapped_column(
        LargeBinary(IDENTIFIER_DIGEST_BYTES),
        primary_key=True,
    )
    identifier_text: Mapped[str] = mapped_column(String(MAX_PAGE_ID_BYTES), nullable=False)
    id_scheme: Mapped[str] = mapped_column(String(ID_SCHEME_MAX_LENGTH), nullable=False)
    identifier_kind: Mapped[str] = mapped_column(
        String(IDENTIFIER_KIND_MAX_LENGTH),
        nullable=False,
    )
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageIdCollisionCounter(Base):
    __tablename__ = "page_id_collision_counters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_page_id_collision_counters_library_libraries",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"id_scheme = '{PAGE_ID_SCHEME}'",
            name="ck_page_id_collision_counters_scheme",
        ),
        CheckConstraint(
            f"id_timestamp_micros BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS} AND id_timestamp_micros % 1000 = 0",
            name="ck_page_id_collision_counters_timestamp",
        ),
        CheckConstraint(_BASE_SLUG_CHECK, name="ck_page_id_collision_counters_base_slug"),
        CheckConstraint(
            f"next_ordinal BETWEEN 2 AND {EXHAUSTED_COLLISION_ORDINAL}",
            name="ck_page_id_collision_counters_next_ordinal",
        ),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    id_scheme: Mapped[str] = mapped_column(
        String(ID_SCHEME_MAX_LENGTH),
        primary_key=True,
    )
    id_timestamp_micros: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    base_slug: Mapped[str] = mapped_column(
        String(MAX_BASE_SLUG_BYTES),
        primary_key=True,
    )
    next_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageSource(Base):
    __tablename__ = "page_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_sources_library_page_pages",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(source_id) = 32 AND source_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_page_sources_source_id_lower_hex",
        ),
        CheckConstraint(
            f"length(kind) BETWEEN 1 AND {SOURCE_KIND_MAX_LENGTH} "
            "AND kind = trim(kind) AND instr(kind, char(0)) = 0",
            name="ck_page_sources_kind",
        ),
        CheckConstraint(
            "locator IS NULL OR (length(locator) >= 1 AND instr(locator, char(0)) = 0)",
            name="ck_page_sources_locator",
        ),
        CheckConstraint(
            f"captured_at IS NULL OR captured_at BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS}",
            name="ck_page_sources_captured_at",
        ),
        CheckConstraint("created_at >= 0", name="ck_page_sources_created_at"),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    source_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), nullable=False)
    kind: Mapped[str] = mapped_column(String(SOURCE_KIND_MAX_LENGTH), nullable=False)
    locator: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = [
    "CONTENT_SHA256_BYTES",
    "EXHAUSTED_COLLISION_ORDINAL",
    "IDENTIFIER_DIGEST_BYTES",
    "MAX_MARKDOWN_BYTES",
    "MAX_OCCURRENCE_MICROSECONDS",
    "MIN_OCCURRENCE_MICROSECONDS",
    "Page",
    "PageIdCollisionCounter",
    "PageIdentifier",
    "PageSource",
    "Revision",
]
