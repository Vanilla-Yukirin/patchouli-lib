"""Bounded, pure candidate input contract for the proposed search-v2 query.

This module does not authorize Libraries, resolve Tags, query an index, or
register a route. A future HTTP adapter must use ``parse_query_v2_json`` (or
apply equivalent raw-body and duplicate-key checks) before using the query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from patchouli_lib.api.contracts import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from patchouli_lib.content.models import MAX_OCCURRENCE_MICROSECONDS, MIN_OCCURRENCE_MICROSECONDS
from patchouli_lib.library.schemas import OpaqueId
from patchouli_lib.search.literal_v2 import MAX_QUERY_KEYWORDS, normalize_keywords

MAX_QUERY_BODY_BYTES = 96 * 1024
MAX_QUERY_TAGS = 256
MAX_QUERY_LIBRARIES = 256

_FromMicros = Annotated[int, Field(ge=MIN_OCCURRENCE_MICROSECONDS, le=MAX_OCCURRENCE_MICROSECONDS)]
_BeforeMicros = Annotated[
    int, Field(ge=MIN_OCCURRENCE_MICROSECONDS, le=MAX_OCCURRENCE_MICROSECONDS + 1)
]


class InvalidSearchQueryV2(ValueError):
    """A deliberately non-echoing error for a future HTTP boundary."""

    def __init__(self) -> None:
        super().__init__("Invalid search-v2 query.")


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class TagIdentityV2(_StrictWireModel):
    """A Tag is scoped by Library; a bare tag ID is never sufficient."""

    library_id: OpaqueId
    tag_id: OpaqueId


@dataclass(frozen=True, slots=True)
class SearchQueryV2:
    """Normalized query only; permission and existence checks remain external."""

    keywords: tuple[str, ...] = field(repr=False)
    tags_any: tuple[TagIdentityV2, ...]
    occurred_from_us: int | None
    occurred_before_us: int | None
    libraries: tuple[str, ...] | None
    limit: int


class SearchQueryV2Wire(_StrictWireModel):
    """Provisional JSON shape, not an accepted public API or FastAPI route."""

    keywords: Annotated[list[str], Field(max_length=MAX_QUERY_KEYWORDS, repr=False)] = Field(
        default_factory=list, repr=False
    )
    tags_any: Annotated[list[TagIdentityV2], Field(max_length=MAX_QUERY_TAGS)] = Field(
        default_factory=list
    )
    occurred_from_us: _FromMicros | None = None
    occurred_before_us: _BeforeMicros | None = None
    libraries: Annotated[list[OpaqueId], Field(max_length=MAX_QUERY_LIBRARIES)] | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT

    @field_validator("keywords")
    @classmethod
    def normalize_bounded_keywords(cls, value: list[str]) -> list[str]:
        # literal_v2 checks the raw 32-KiB total and all 256 items *before*
        # normalization/deduplication; the result retains first-seen order.
        return list(normalize_keywords(tuple(value)))

    @model_validator(mode="after")
    def require_conditions_and_ordered_window(self) -> Self:
        if not (
            self.keywords
            or self.tags_any
            or self.occurred_from_us is not None
            or self.occurred_before_us is not None
        ):
            raise ValueError("Search requires a keyword, Tag, or time condition.")
        if (
            self.occurred_from_us is not None
            and self.occurred_before_us is not None
            and self.occurred_from_us >= self.occurred_before_us
        ):
            raise ValueError("Search time interval must be nonempty and ordered.")
        return self

    def to_query(self) -> SearchQueryV2:
        """Deduplicate stable identities without resolving access or Tag existence."""

        return SearchQueryV2(
            keywords=tuple(self.keywords),
            tags_any=tuple(dict.fromkeys(self.tags_any)),
            occurred_from_us=self.occurred_from_us,
            occurred_before_us=self.occurred_before_us,
            libraries=None if self.libraries is None else tuple(dict.fromkeys(self.libraries)),
            limit=self.limit,
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSearchQueryV2
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise InvalidSearchQueryV2


def parse_query_v2_json(raw_body: bytes) -> SearchQueryV2:
    """Parse one strictly bounded JSON body without echoing query text.

    The raw cap also limits whitespace, escapes, unknown fields, and JSON
    parser work. An HTTP adapter must still enforce the cap while streaming.
    """

    if type(raw_body) is not bytes or len(raw_body) > MAX_QUERY_BODY_BYTES:
        raise InvalidSearchQueryV2 from None
    try:
        document = json.loads(
            raw_body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        return SearchQueryV2Wire.model_validate(document, strict=True).to_query()
    except (UnicodeError, RecursionError, ValueError, TypeError, ValidationError):
        raise InvalidSearchQueryV2 from None


__all__ = [
    "InvalidSearchQueryV2",
    "MAX_QUERY_BODY_BYTES",
    "MAX_QUERY_LIBRARIES",
    "MAX_QUERY_TAGS",
    "SearchQueryV2",
    "SearchQueryV2Wire",
    "TagIdentityV2",
    "parse_query_v2_json",
]
