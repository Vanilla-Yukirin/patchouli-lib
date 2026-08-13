"""Strict storage-facing schemas for immutable Page and Revision content."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBytes, field_validator, model_validator

from patchouli_lib.api.contracts import validate_api_v1_path
from patchouli_lib.auth.schemas import AuditEventRecord
from patchouli_lib.content.models import (
    CONTENT_SHA256_BYTES,
    EXHAUSTED_COLLISION_ORDINAL,
    IDENTIFIER_DIGEST_BYTES,
    MAX_MARKDOWN_BYTES,
    MAX_OCCURRENCE_MICROSECONDS,
    MIN_OCCURRENCE_MICROSECONDS,
)
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.identifiers import (
    MAX_BASE_SLUG_BYTES,
    MAX_COLLISION_ORDINAL,
    PAGE_ID_SCHEME,
    OccurrenceTime,
    canonical_utc_wire,
    generate_page_id,
    page_id_registry_digest,
    validate_page_id,
    validate_page_uid,
    validate_revision_id,
    validate_revision_number,
)

OpaqueId = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
PageUid = Annotated[StrictBytes, Field(min_length=16, max_length=16)]
RevisionId = Annotated[str, Field(min_length=36, max_length=36, pattern=r"^rev_[0-9a-f]{32}$")]
PageId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")]
BaseSlug = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_BASE_SLUG_BYTES,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
OccurrenceMicros = Annotated[
    int,
    Field(ge=MIN_OCCURRENCE_MICROSECONDS, le=MAX_OCCURRENCE_MICROSECONDS),
]
StoredTimestamp = Annotated[int, Field(ge=0)]
CollisionOrdinal = Annotated[int, Field(ge=1, le=MAX_COLLISION_ORDINAL)]
CounterOrdinal = Annotated[int, Field(ge=2, le=EXHAUSTED_COLLISION_ORDINAL)]
Digest32 = Annotated[StrictBytes, Field(min_length=32, max_length=32)]
SourceKind = Annotated[str, Field(min_length=1, max_length=100)]
IdempotencyDigest = Annotated[StrictBytes, Field(min_length=32, max_length=32)]
RequestId = Annotated[str, Field(pattern=r"^req_[0-9a-f]{32}$")]
StrongPageETag = Annotated[
    str,
    Field(min_length=74, max_length=74, pattern=r'^"page-v1-[0-9a-f]{64}"$'),
]

_SOURCE_KIND_PATTERN = re.compile(r"\A\S(?:.*\S)?\Z", re.DOTALL)


class ContentSchema(BaseModel):
    """Strict immutable base for values crossing the storage boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class MarkdownContent(ContentSchema):
    """Exact accepted Markdown bytes plus verified storage metadata."""

    content_md: Annotated[
        StrictBytes,
        Field(min_length=1, max_length=MAX_MARKDOWN_BYTES, repr=False),
    ]
    content_size_bytes: Annotated[int, Field(ge=1, le=MAX_MARKDOWN_BYTES)]
    content_sha256: Annotated[
        StrictBytes,
        Field(min_length=CONTENT_SHA256_BYTES, max_length=CONTENT_SHA256_BYTES),
    ]

    @field_validator("content_md")
    @classmethod
    def require_utf8_markdown_without_nul(cls, value: bytes) -> bytes:
        if b"\x00" in value:
            raise ValueError("Markdown content must not contain NUL bytes.")
        try:
            value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown content must be valid UTF-8.") from exc
        return value

    @model_validator(mode="after")
    def require_exact_metadata(self) -> Self:
        if self.content_size_bytes != len(self.content_md):
            raise ValueError("Markdown content size metadata does not match the bytes.")
        if self.content_sha256 != hashlib.sha256(self.content_md).digest():
            raise ValueError("Markdown content digest metadata does not match the bytes.")
        return self

    @classmethod
    def from_bytes(cls, content_md: bytes) -> MarkdownContent:
        if type(content_md) is not bytes:
            raise ValueError("Markdown content must be supplied as exact bytes.")
        return cls(
            content_md=content_md,
            content_size_bytes=len(content_md),
            content_sha256=hashlib.sha256(content_md).digest(),
        )


class _PageStorage(ContentSchema):
    library_id: OpaqueId
    page_uid: PageUid
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    id_scheme: Literal["page-v1"]
    id_timestamp_micros: OccurrenceMicros
    base_slug: BaseSlug
    collision_ordinal: CollisionOrdinal
    title: Annotated[str, Field(min_length=1)]
    page_type: Annotated[str, Field(min_length=1, max_length=32)]
    occurred_at: OccurrenceMicros
    current_revision_id: RevisionId
    current_revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    created_at: StoredTimestamp
    updated_at: StoredTimestamp
    deleted_at: StoredTimestamp | None = None

    @field_validator("page_uid")
    @classmethod
    def require_page_uid(cls, value: bytes) -> bytes:
        return validate_page_uid(value)

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("current_revision_id")
    @classmethod
    def require_revision_id(cls, value: str) -> str:
        return validate_revision_id(value)

    @field_validator("current_revision_number")
    @classmethod
    def require_current_revision_number(cls, value: int) -> int:
        return validate_revision_number(value)

    @field_validator("title")
    @classmethod
    def reject_nul_title(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Page title must not contain NUL characters.")
        return value

    @field_validator("page_type")
    @classmethod
    def require_trimmed_page_type(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value:
            raise ValueError("Page type must be non-empty trimmed text.")
        return value

    @model_validator(mode="after")
    def require_consistent_floor_and_lifecycle(self) -> Self:
        if self.id_timestamp_micros != (self.occurred_at // 1_000) * 1_000:
            raise ValueError("Page identifier timestamp is inconsistent.")
        if self.updated_at < self.created_at:
            raise ValueError("Page update time precedes creation time.")
        if (
            self.deleted_at is not None
            and not self.created_at <= self.deleted_at <= self.updated_at
        ):
            raise ValueError("Page deletion time is outside its lifecycle.")
        return self


class NewPage(_PageStorage):
    """Validate the initial Page row against its one-time creation title."""

    current_revision_number: Literal[1]

    @model_validator(mode="after")
    def require_consistent_generated_identity(self) -> Self:
        occurrence = OccurrenceTime(
            utc_microseconds=self.occurred_at,
            canonical_utc=canonical_utc_wire(self.occurred_at),
        )
        generated = generate_page_id(
            occurrence,
            self.title,
            collision_ordinal=self.collision_ordinal,
        )
        if (
            self.id_scheme != PAGE_ID_SCHEME
            or self.page_id != generated.value
            or self.base_slug != generated.base_slug
        ):
            raise ValueError("Page identifier metadata is inconsistent.")
        return self


class PageRecord(_PageStorage):
    """Validate a stored Page without recomputing identity after title changes."""

    pass


class NewRevision(MarkdownContent):
    library_id: OpaqueId
    revision_id: RevisionId
    page_uid: PageUid
    revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    created_at: StoredTimestamp

    @field_validator("revision_id")
    @classmethod
    def require_revision_id(cls, value: str) -> str:
        return validate_revision_id(value)

    @field_validator("revision_number")
    @classmethod
    def require_revision_number(cls, value: int) -> int:
        return validate_revision_number(value)

    @field_validator("page_uid")
    @classmethod
    def require_page_uid(cls, value: bytes) -> bytes:
        return validate_page_uid(value)


class RevisionRecord(NewRevision):
    pass


class NewPageIdentifier(ContentSchema):
    library_id: OpaqueId
    identifier_digest: Annotated[
        StrictBytes,
        Field(min_length=IDENTIFIER_DIGEST_BYTES, max_length=IDENTIFIER_DIGEST_BYTES),
    ]
    identifier_text: PageId
    id_scheme: Literal["page-v1"]
    identifier_kind: Literal["canonical", "alias"]
    page_uid: PageUid
    created_at: StoredTimestamp

    @field_validator("identifier_text")
    @classmethod
    def require_identifier_text(cls, value: str) -> str:
        return validate_page_id(value)

    @model_validator(mode="after")
    def require_exact_digest(self) -> Self:
        if self.identifier_digest != page_id_registry_digest(self.identifier_text):
            raise ValueError("Page identifier digest metadata does not match the text.")
        return self


class PageIdentifierRecord(NewPageIdentifier):
    pass


class NewPageIdCollisionCounter(ContentSchema):
    library_id: OpaqueId
    id_scheme: Literal["page-v1"]
    id_timestamp_micros: OccurrenceMicros
    base_slug: BaseSlug
    next_ordinal: CounterOrdinal

    @model_validator(mode="after")
    def require_millisecond_floor(self) -> Self:
        if self.id_timestamp_micros % 1_000 != 0:
            raise ValueError("Page identifier counter time must be a UTC millisecond floor.")
        return self


class PageIdCollisionCounterRecord(NewPageIdCollisionCounter):
    pass


class NewPageSource(ContentSchema):
    library_id: OpaqueId
    source_id: OpaqueId
    page_uid: PageUid
    revision_id: RevisionId
    revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    kind: SourceKind
    locator: str | None = Field(default=None, repr=False)
    captured_at: OccurrenceMicros | None = None
    created_at: StoredTimestamp

    @field_validator("revision_id")
    @classmethod
    def require_revision_id(cls, value: str) -> str:
        return validate_revision_id(value)

    @field_validator("revision_number")
    @classmethod
    def require_revision_number(cls, value: int) -> int:
        return validate_revision_number(value)

    @field_validator("kind")
    @classmethod
    def require_trimmed_source_kind(cls, value: str) -> str:
        if _SOURCE_KIND_PATTERN.fullmatch(value) is None or "\x00" in value:
            raise ValueError("Source kind must be non-empty trimmed text.")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Source kind must be valid Unicode text.") from exc
        return value

    @field_validator("locator")
    @classmethod
    def require_safe_locator_storage(cls, value: str | None) -> str | None:
        if value is not None and (not value or "\x00" in value):
            raise ValueError("Source locator must be non-empty text without NUL.")
        if value is not None:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("Source locator must be valid Unicode text.") from exc
        return value


class PageSourceRecord(NewPageSource):
    pass


class ArchiveSourceInput(ContentSchema):
    """Required provenance stored verbatim without identity or dedup semantics."""

    kind: SourceKind
    locator: str | None = Field(default=None, repr=False)
    captured_at: OccurrenceMicros | None = None

    @field_validator("kind")
    @classmethod
    def require_trimmed_source_kind(cls, value: str) -> str:
        if _SOURCE_KIND_PATTERN.fullmatch(value) is None or "\x00" in value:
            raise ValueError("Source kind must be non-empty trimmed text.")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Source kind must be valid Unicode text.") from exc
        return value

    @field_validator("locator")
    @classmethod
    def require_safe_locator(cls, value: str | None) -> str | None:
        if value is not None and (not value or "\x00" in value):
            raise ValueError("Source locator must be non-empty text without NUL.")
        if value is not None:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("Source locator must be valid Unicode text.") from exc
        return value


class CreateArchiveCommand(ContentSchema):
    """Semantic create request; bearer and raw idempotency key are kept separate."""

    library_id: OpaqueId
    section_id: OpaqueId
    book_id: OpaqueId
    title: Annotated[str, Field(min_length=1)]
    occurred_at: OccurrenceMicros
    content_md: Annotated[
        StrictBytes,
        Field(min_length=1, max_length=MAX_MARKDOWN_BYTES, repr=False),
    ]
    source: ArchiveSourceInput
    request_id: RequestId

    @field_validator("title")
    @classmethod
    def require_safe_title(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Page title must not contain NUL characters.")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Page title must be valid Unicode text.") from exc
        return value

    @field_validator("content_md")
    @classmethod
    def require_valid_markdown(cls, value: bytes) -> bytes:
        MarkdownContent.from_bytes(value)
        return value


class AppendArchiveRevisionCommand(ContentSchema):
    """Semantic append request with one optional strong current precondition."""

    library_id: OpaqueId
    section_id: OpaqueId
    page_id: PageId
    expected_etag: StrongPageETag | None = Field(default=None, repr=False)
    source: ArchiveSourceInput
    content_md: Annotated[
        StrictBytes,
        Field(min_length=1, max_length=MAX_MARKDOWN_BYTES, repr=False),
    ]
    request_id: RequestId

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("content_md")
    @classmethod
    def require_valid_markdown(cls, value: bytes) -> bytes:
        MarkdownContent.from_bytes(value)
        return value


class ArchiveIdempotencyKey(ContentSchema):
    """Already-digested key material safe to cross the domain boundary."""

    key_digest: IdempotencyDigest = Field(repr=False)


class ArchivePageView(ContentSchema):
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    title: Annotated[str, Field(min_length=1)]
    type: Literal["archive"]
    occurred_at: str
    current_revision_id: RevisionId
    current_revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]

    @field_validator("occurred_at")
    @classmethod
    def require_canonical_occurrence(cls, value: str) -> str:
        from patchouli_lib.identifiers import parse_occurrence_time

        if parse_occurrence_time(value).canonical_utc != value:
            raise ValueError("Occurrence timestamp must be canonical UTC text.")
        return value


class ArchiveRevisionView(ContentSchema):
    page_id: PageId
    revision_id: RevisionId
    revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    created_at: str
    content_type: Literal["text/markdown;charset=utf-8"] = "text/markdown;charset=utf-8"
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    content: Annotated[str, Field(min_length=1, repr=False)]

    @field_validator("created_at")
    @classmethod
    def require_canonical_created_at(cls, value: str) -> str:
        from patchouli_lib.identifiers import parse_occurrence_time

        if parse_occurrence_time(value).canonical_utc != value:
            raise ValueError("Creation timestamp must be canonical UTC text.")
        return value


class ArchiveCitation(ContentSchema):
    section_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    href: Annotated[str, Field(min_length=1, max_length=2_048)]

    @field_validator("href")
    @classmethod
    def require_relative_href(cls, value: str) -> str:
        return validate_api_v1_path(value)


class ArchiveResponseBody(ContentSchema):
    page: ArchivePageView
    revision: ArchiveRevisionView
    citation: ArchiveCitation

    @model_validator(mode="after")
    def require_consistent_identity(self) -> Self:
        if (
            self.revision.page_id != self.page.page_id
            or self.citation.page_id != self.page.page_id
            or self.citation.section_id != self.page.section_id
            or self.revision.revision_id != self.page.current_revision_id
            or self.citation.revision_id != self.page.current_revision_id
            or self.revision.revision_number != self.page.current_revision_number
            or self.citation.revision_number != self.page.current_revision_number
        ):
            raise ValueError("Archive response identity is inconsistent.")
        return self


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ArchiveMutationSuccess:
    page: PageRecord
    revision: RevisionRecord
    source: PageSourceRecord
    citation: ArchiveCitation
    audit_event: AuditEventRecord
    response: OriginalResponse = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(page_id={self.page.page_id!r}, "
            f"revision_id={self.revision.revision_id!r})"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ArchiveMutationReplay:
    body: ArchiveResponseBody = field(repr=False)
    response: ReplayResponse = field(repr=False)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(response=<redacted>)"


ArchiveMutationResult = ArchiveMutationSuccess | ArchiveMutationReplay


__all__ = [
    "AppendArchiveRevisionCommand",
    "ArchiveCitation",
    "ArchiveIdempotencyKey",
    "ArchiveMutationReplay",
    "ArchiveMutationResult",
    "ArchiveMutationSuccess",
    "ArchivePageView",
    "ArchiveResponseBody",
    "ArchiveRevisionView",
    "ArchiveSourceInput",
    "CreateArchiveCommand",
    "MarkdownContent",
    "NewPage",
    "NewPageIdCollisionCounter",
    "NewPageIdentifier",
    "NewPageSource",
    "NewRevision",
    "PageIdCollisionCounterRecord",
    "PageIdentifierRecord",
    "PageRecord",
    "PageSourceRecord",
    "RevisionRecord",
]
