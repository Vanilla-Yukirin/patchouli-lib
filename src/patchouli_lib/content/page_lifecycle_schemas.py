"""Library-explicit Caller lifecycle contract, independent of legacy Archive."""

from __future__ import annotations

import json
from typing import Literal

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

LIBRARY_DELETE_PAGE_ROUTE = "/api/v1/libraries/{library_id}/pages/{page_id}"
LIBRARY_RESTORE_PAGE_ROUTE = LIBRARY_DELETE_PAGE_ROUTE + "/restore"
LIBRARY_LIFECYCLE_ROUTES = frozenset({LIBRARY_DELETE_PAGE_ROUTE, LIBRARY_RESTORE_PAGE_ROUTE})
LIBRARY_LIFECYCLE_REVISION = "20261003_0030"


class PageLifecycleCommand(ContentSchema):
    library_id: OpaqueId
    page_id: PageId
    action: Literal["delete", "restore"]
    expected_etag: StrongPageETag = Field(repr=False)
    request_id: RequestId

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)


class LibraryPageLifecycleBody(ContentSchema):
    library_id: OpaqueId
    page_id: PageId
    section_id: OpaqueId
    book_id: OpaqueId
    state: Literal["active", "trashed"]
    deleted_at: str | None
    revision_id: RevisionId
    revision_number: int = Field(ge=1, le=(1 << 63) - 1)
    occurred_at: str
    original_updated_at: str
    updated_at: str
    request_etag: StrongPageETag = Field(repr=False)

    @field_validator("deleted_at", "occurred_at", "original_updated_at", "updated_at")
    @classmethod
    def require_timestamp(cls, value: str | None) -> str | None:
        if value is not None and parse_occurrence_time(value).canonical_utc != value:
            raise ValueError("Lifecycle timestamps must be canonical UTC.")
        return value

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)


def lifecycle_route(action: Literal["delete", "restore"]) -> tuple[str, str]:
    return (
        ("DELETE", LIBRARY_DELETE_PAGE_ROUTE)
        if action == "delete"
        else ("POST", LIBRARY_RESTORE_PAGE_ROUTE)
    )


def lifecycle_fingerprint(command: PageLifecycleCommand) -> bytes:
    return digest_request_fingerprint(
        b"caller-library-page-lifecycle-v1",
        json.dumps(
            command.model_dump(exclude={"request_id"}), sort_keys=True, separators=(",", ":")
        ).encode(),
    )


def lifecycle_key_digest(key_digest: bytes, home_library_id: str) -> bytes:
    return digest_request_fingerprint(
        b"caller-library-page-lifecycle-key-v1", key_digest, home_library_id.encode("ascii")
    )
