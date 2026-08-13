"""Strict application-facing values for bounded non-search retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchouli_lib.api.contracts import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Citation
from patchouli_lib.content.schemas import PageId, RevisionId, StrongPageETag
from patchouli_lib.identifiers import parse_occurrence_time
from patchouli_lib.library.schemas import OpaqueId, ResourceName

InternalPageKey = Annotated[str, Field(min_length=1, max_length=255)]


class RetrievalSchema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ReadWindow(RetrievalSchema):
    """A decoded internal keyset window, never a public cursor contract."""

    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    after_key: InternalPageKey | None = None


@dataclass(frozen=True, slots=True)
class KeysetPage[ItemT]:
    """A bounded service slice whose key must not be exposed as an API cursor.

    A router may consume ``next_key`` only after wrapping it in the integrity-
    protected, caller- and policy-bound cursor required by the accepted API
    contract.
    """

    items: tuple[ItemT, ...]
    next_key: str | None


class SectionView(RetrievalSchema):
    section_id: OpaqueId
    name: ResourceName


class BookView(RetrievalSchema):
    section_id: OpaqueId
    book_id: OpaqueId
    title: ResourceName


class PageView(RetrievalSchema):
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    title: Annotated[str, Field(min_length=1)] = Field(repr=False)
    type: Annotated[str, Field(min_length=1, max_length=32)]
    occurred_at: str
    current_revision_id: RevisionId
    current_revision_number: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]

    @field_validator("occurred_at")
    @classmethod
    def require_canonical_occurrence(cls, value: str) -> str:
        if parse_occurrence_time(value).canonical_utc != value:
            raise ValueError("Occurrence timestamp must be canonical UTC text.")
        return value


class RevisionView(RetrievalSchema):
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
        if parse_occurrence_time(value).canonical_utc != value:
            raise ValueError("Revision timestamp must be canonical UTC text.")
        return value


class PageMetadata(RetrievalSchema):
    """Current Page metadata and its exact current-Revision citation."""

    page: PageView
    citation: Citation

    @model_validator(mode="after")
    def require_current_citation(self) -> Self:
        if (
            self.citation.section_id != self.page.section_id
            or self.citation.page_id != self.page.page_id
            or self.citation.revision_id != self.page.current_revision_id
            or self.citation.revision_number != self.page.current_revision_number
        ):
            raise ValueError("Page metadata citation is not the current Revision.")
        return self


class PageDocument(RetrievalSchema):
    page: PageView
    revision: RevisionView
    citation: Citation

    @model_validator(mode="after")
    def require_exact_citation(self) -> Self:
        if (
            self.revision.page_id != self.page.page_id
            or self.citation.section_id != self.page.section_id
            or self.citation.page_id != self.page.page_id
            or self.citation.revision_id != self.revision.revision_id
            or self.citation.revision_number != self.revision.revision_number
        ):
            raise ValueError("Page, Revision, and citation identity is inconsistent.")
        return self


@dataclass(frozen=True, slots=True, repr=False)
class CurrentPageRead:
    """Current document body plus the strong ETag carried by the HTTP response."""

    document: PageDocument = field(repr=False)
    etag: StrongPageETag

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(page_id={self.document.page.page_id!r}, "
            f"revision_number={self.document.revision.revision_number!r}, "
            f"etag={self.etag!r})"
        )


__all__ = [
    "BookView",
    "CurrentPageRead",
    "InternalPageKey",
    "KeysetPage",
    "PageDocument",
    "PageMetadata",
    "PageView",
    "ReadWindow",
    "RevisionView",
    "SectionView",
]
