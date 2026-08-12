"""Strict storage-facing schemas for immutable Page and Revision content."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBytes, field_validator, model_validator

from patchouli_lib.content.models import (
    CONTENT_SHA256_BYTES,
    EXHAUSTED_COLLISION_ORDINAL,
    IDENTIFIER_DIGEST_BYTES,
    MAX_MARKDOWN_BYTES,
    MAX_OCCURRENCE_MICROSECONDS,
    MIN_OCCURRENCE_MICROSECONDS,
)
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

    content_md: Annotated[StrictBytes, Field(min_length=1, max_length=MAX_MARKDOWN_BYTES)]
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
    kind: SourceKind
    locator: str | None = None
    captured_at: OccurrenceMicros | None = None
    created_at: StoredTimestamp

    @field_validator("kind")
    @classmethod
    def require_trimmed_source_kind(cls, value: str) -> str:
        if _SOURCE_KIND_PATTERN.fullmatch(value) is None or "\x00" in value:
            raise ValueError("Source kind must be non-empty trimmed text.")
        return value

    @field_validator("locator")
    @classmethod
    def require_safe_locator_storage(cls, value: str | None) -> str | None:
        if value is not None and (not value or "\x00" in value):
            raise ValueError("Source locator must be non-empty text without NUL.")
        return value


class PageSourceRecord(NewPageSource):
    pass


__all__ = [
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
