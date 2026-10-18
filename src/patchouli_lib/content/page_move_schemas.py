"""Candidate Agent movement contract; responses describe the original success."""

from __future__ import annotations

import json

from pydantic import Field, field_validator

from patchouli_lib.content.schemas import (
    ContentSchema,
    OpaqueId,
    PageId,
    RequestId,
    RevisionId,
    StrongPageETag,
)
from patchouli_lib.idempotency.schemas import digest_request_fingerprint
from patchouli_lib.identifiers import parse_occurrence_time, validate_page_id

PAGE_MOVE_ROUTE_TEMPLATE = "/api/v1/libraries/{library_id}/pages/{page_id}/move"


class PageMoveInput(ContentSchema):
    source_section_id: OpaqueId
    source_book_id: OpaqueId
    target_section_id: OpaqueId
    target_book_id: OpaqueId


class PageMoveCommand(PageMoveInput):
    library_id: OpaqueId
    page_id: PageId
    expected_etag: StrongPageETag = Field(repr=False)
    request_id: RequestId

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)


class PageMoveBody(PageMoveInput):
    changed: bool
    library_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: int = Field(ge=1, le=(1 << 63) - 1)
    occurred_at: str
    original_updated_at: str
    updated_at: str
    request_etag: StrongPageETag = Field(repr=False)

    @field_validator("occurred_at", "original_updated_at", "updated_at")
    @classmethod
    def require_timestamp(cls, value: str) -> str:
        if parse_occurrence_time(value).canonical_utc != value:
            raise ValueError("Movement timestamps must be canonical UTC.")
        return value

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)


def move_fingerprint(command: PageMoveCommand) -> bytes:
    return digest_request_fingerprint(
        b"caller-page-move-v1",
        json.dumps(
            command.model_dump(exclude={"request_id"}), sort_keys=True, separators=(",", ":")
        ).encode(),
    )


def caller_move_key_digest(key_digest: bytes, home_library_id: str) -> bytes:
    # Existing generic replay PK predates actor-home namespacing. Domain-scope
    # only this new operation, without changing any older request or wire key.
    return digest_request_fingerprint(
        b"caller-page-move-key-v1", key_digest, home_library_id.encode("ascii")
    )
