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

from patchouli_lib.content.file_manifest import (
    MAX_FILE_BYTES,
    MAX_FILENAME_BYTES,
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
)
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


class PageOccurrenceCorrection(Base):
    """Immutable audit of one declared-time correction, created by SQLite."""

    __tablename__ = "page_occurrence_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_occurrence_corrections_page",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_page_occurrence_corrections_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name="ck_page_occurrence_corrections_sequence",
        ),
        CheckConstraint(
            f"old_occurred_at BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS} "
            f"AND new_occurred_at BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS} "
            "AND old_occurred_at != new_occurred_at",
            name="ck_page_occurrence_corrections_values",
        ),
        CheckConstraint(
            "at_revision_number BETWEEN 1 AND 9223372036854775807",
            name="ck_page_occurrence_corrections_revision",
        ),
        CheckConstraint("corrected_at >= 0", name="ck_page_occurrence_corrections_time"),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    old_occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    corrected_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageOccurrenceCorrectionGuard(Base):
    """Transient write authorization consumed by a Page UPDATE trigger."""

    __tablename__ = "page_occurrence_correction_guards"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_occurrence_correction_guards_page",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_page_occurrence_correction_guards_actor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["library_id", "page_uid", "sequence"],
            [
                "page_occurrence_corrections.library_id",
                "page_occurrence_corrections.page_uid",
                "page_occurrence_corrections.sequence",
            ],
            name="fk_page_occurrence_correction_guards_completed",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name="ck_page_occurrence_correction_guards_sequence",
        ),
        CheckConstraint(
            f"old_occurred_at BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS} "
            f"AND new_occurred_at BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS} "
            "AND old_occurred_at != new_occurred_at",
            name="ck_page_occurrence_correction_guards_values",
        ),
        CheckConstraint("corrected_at >= 0", name="ck_page_occurrence_correction_guards_time"),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    corrected_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PageLifecycleEvent(Base):
    """Immutable delete/restore event written by the Page transition trigger."""

    __tablename__ = "page_lifecycle_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_lifecycle_events_page",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_page_lifecycle_events_section",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_page_lifecycle_events_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name="ck_page_lifecycle_events_sequence",
        ),
        CheckConstraint(
            "(action = 'delete' AND old_deleted_at IS NULL) "
            "OR (action = 'restore' AND old_deleted_at IS NOT NULL)",
            name="ck_page_lifecycle_events_action",
        ),
        CheckConstraint(
            "old_updated_at >= 0 AND changed_at > old_updated_at "
            "AND changed_at <= 9223372036854775807 "
            "AND (old_deleted_at IS NULL OR "
            "(old_deleted_at >= 0 AND old_deleted_at <= old_updated_at))",
            name="ck_page_lifecycle_events_time",
        ),
        CheckConstraint(
            "at_revision_number BETWEEN 1 AND 9223372036854775807 "
            f"AND occurred_at_at_event BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS}",
            name="ck_page_lifecycle_events_snapshot",
        ),
        CheckConstraint(
            "length(request_id) = 36 AND substr(request_id, 1, 4) = 'req_' "
            "AND substr(request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_page_lifecycle_events_request_id",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    section_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    old_deleted_at: Mapped[int | None] = mapped_column(BigInteger)
    old_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at_at_event: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)


class PageLifecycleGuard(Base):
    """Short-lived transition proof consumed during the Page UPDATE."""

    __tablename__ = "page_lifecycle_guards"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_lifecycle_guards_page",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_page_lifecycle_guards_section",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_page_lifecycle_guards_actor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["library_id", "page_uid", "sequence"],
            [
                "page_lifecycle_events.library_id",
                "page_lifecycle_events.page_uid",
                "page_lifecycle_events.sequence",
            ],
            name="fk_page_lifecycle_guards_completed",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name="ck_page_lifecycle_guards_sequence",
        ),
        CheckConstraint(
            "(action = 'delete' AND old_deleted_at IS NULL) "
            "OR (action = 'restore' AND old_deleted_at IS NOT NULL)",
            name="ck_page_lifecycle_guards_action",
        ),
        CheckConstraint(
            "old_updated_at >= 0 AND changed_at > old_updated_at "
            "AND changed_at <= 9223372036854775807 "
            "AND (old_deleted_at IS NULL OR "
            "(old_deleted_at >= 0 AND old_deleted_at <= old_updated_at))",
            name="ck_page_lifecycle_guards_time",
        ),
        CheckConstraint(
            "at_revision_number BETWEEN 1 AND 9223372036854775807 "
            f"AND occurred_at_at_event BETWEEN {MIN_OCCURRENCE_MICROSECONDS} "
            f"AND {MAX_OCCURRENCE_MICROSECONDS}",
            name="ck_page_lifecycle_guards_snapshot",
        ),
        CheckConstraint(
            "length(request_id) = 36 AND substr(request_id, 1, 4) = 'req_' "
            "AND substr(request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_page_lifecycle_guards_request_id",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    section_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    old_deleted_at: Mapped[int | None] = mapped_column(BigInteger)
    old_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at_at_event: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)


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
            "content_md IS NULL OR (typeof(content_md) = 'blob' "
            f"AND length(content_md) BETWEEN 1 AND {MAX_MARKDOWN_BYTES} "
            "AND instr(content_md, x'00') = 0)",
            name="ck_revisions_content_md",
        ),
        CheckConstraint(
            "(content_md IS NULL AND content_size_bytes IS NULL) OR "
            "(content_md IS NOT NULL AND content_size_bytes = length(content_md))",
            name="ck_revisions_content_size_bytes",
        ),
        CheckConstraint(
            "(content_md IS NULL AND content_sha256 IS NULL) OR "
            "(content_md IS NOT NULL AND typeof(content_sha256) = 'blob' "
            f"AND length(content_sha256) = {CONTENT_SHA256_BYTES})",
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
    content_md: Mapped[bytes | None] = mapped_column(LargeBinary(MAX_MARKDOWN_BYTES))
    content_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_sha256: Mapped[bytes | None] = mapped_column(
        LargeBinary(CONTENT_SHA256_BYTES),
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RevisionFileSet(Base):
    """Format and declared complete manifest for one immutable Revision."""

    __tablename__ = "revision_file_sets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_file_sets_exact_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(storage_format = 'legacy_markdown' AND file_count = 1 "
            f"AND total_size_bytes BETWEEN 1 AND {MAX_MARKDOWN_BYTES} "
            "AND snapshot_sha256 IS NULL) OR "
            "(storage_format = 'file_set_v1' "
            f"AND file_count BETWEEN 1 AND {MAX_FILES_PER_PAGE} "
            f"AND total_size_bytes BETWEEN 0 AND {MAX_PAGE_BYTES} "
            "AND typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32)",
            name="ck_revision_file_sets_format_manifest",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), primary_key=True)
    revision_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    storage_format: Mapped[str] = mapped_column(String(16), nullable=False)
    file_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(CONTENT_SHA256_BYTES))


class RevisionFile(Base):
    """One immutable flat file in a Revision's sealed snapshot."""

    __tablename__ = "revision_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_files_exact_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "typeof(filename) = 'text' AND length(filename) >= 1 "
            f"AND length(CAST(filename AS BLOB)) <= {MAX_FILENAME_BYTES} "
            "AND filename NOT IN ('.', '..') "
            "AND instr(filename, '/') = 0 AND instr(filename, char(92)) = 0 "
            "AND instr(filename, char(0)) = 0 "
            "AND filename = trim(filename, ' ') "
            "AND substr(filename, -1) != '.'",
            name="ck_revision_files_flat_filename",
        ),
        CheckConstraint(
            "typeof(content_bytes) = 'blob' "
            f"AND length(content_bytes) BETWEEN 0 AND {MAX_FILE_BYTES}",
            name="ck_revision_files_content_bytes",
        ),
        CheckConstraint("size_bytes = length(content_bytes)", name="ck_revision_files_size_bytes"),
        CheckConstraint(
            "typeof(content_sha256) = 'blob' AND length(content_sha256) = 32",
            name="ck_revision_files_content_sha256",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), primary_key=True)
    revision_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filename: Mapped[str] = mapped_column(Text, primary_key=True)
    content_bytes: Mapped[bytes] = mapped_column(LargeBinary(MAX_FILE_BYTES), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[bytes] = mapped_column(LargeBinary(CONTENT_SHA256_BYTES), nullable=False)


class RevisionFileSeal(Base):
    """An immutable marker for a complete Revision file set."""

    __tablename__ = "revision_file_seals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_file_seals_exact_revision",
            ondelete="RESTRICT",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), primary_key=True)
    revision_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class RevisionFileSealGuard(Base):
    """Require a matching seal by commit time for every Revision."""

    __tablename__ = "revision_file_seal_guards"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_revision_file_seal_guards_exact_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revision_file_seals.library_id",
                "revision_file_seals.page_uid",
                "revision_file_seals.revision_id",
                "revision_file_seals.revision_number",
            ],
            name="fk_revision_file_seal_guards_exact_seal",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(RANDOM_IDENTIFIER_BYTES), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), primary_key=True)
    revision_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)


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
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            name="fk_page_sources_library_page_revision_revisions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(source_id) = 32 AND source_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_page_sources_source_id_lower_hex",
        ),
        CheckConstraint(
            "typeof(revision_id) = 'text' AND length(revision_id) = 36 "
            "AND substr(revision_id, 1, 4) = 'rev_' "
            "AND substr(revision_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_page_sources_revision_id_wire",
        ),
        CheckConstraint(
            "revision_number BETWEEN 1 AND 9223372036854775807",
            name="ck_page_sources_revision_number",
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
    revision_id: Mapped[str] = mapped_column(String(REVISION_ID_LENGTH), nullable=False)
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    "PageOccurrenceCorrection",
    "PageOccurrenceCorrectionGuard",
    "PageLifecycleEvent",
    "PageLifecycleGuard",
    "PageIdCollisionCounter",
    "PageIdentifier",
    "PageSource",
    "Revision",
    "RevisionFile",
    "RevisionFileSet",
]
