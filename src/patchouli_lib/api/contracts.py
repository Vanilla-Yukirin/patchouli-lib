import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

API_V1_PREFIX = "/api/v1"
PROTECTED_CACHE_CONTROL = "private, no-store"
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_OPAQUE_IDENTIFIER_LENGTH = 255
MAX_CURSOR_LENGTH = 4_096

_RFC3339_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$",
    re.ASCII,
)
_API_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_~-][A-Za-z0-9._~-]*$", re.ASCII)


def parse_rfc3339_utc(value: object) -> datetime:
    """Parse an aware RFC 3339 value and normalize it to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        match = _RFC3339_PATTERN.fullmatch(value)
        if match is None or match.group("offset") == "-00:00":
            raise ValueError("A valid RFC 3339 timestamp with a known offset is required.")
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("A valid RFC 3339 timestamp is required.") from exc
    else:
        raise ValueError("An RFC 3339 timestamp string is required.")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A timezone-aware RFC 3339 timestamp is required.")
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("The timestamp cannot be represented in UTC.") from exc


def format_rfc3339_utc(value: datetime) -> str:
    """Serialize an aware datetime as canonical UTC with microsecond precision."""
    normalized = parse_rfc3339_utc(value)
    return (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}T"
        f"{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}."
        f"{normalized.microsecond:06d}Z"
    )


RFC3339UTC = Annotated[
    datetime,
    BeforeValidator(parse_rfc3339_utc),
    PlainSerializer(format_rfc3339_utc, return_type=str, when_used="json"),
]
OpaqueIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=MAX_OPAQUE_IDENTIFIER_LENGTH),
]
OpaqueCursor = Annotated[str, Field(min_length=1, max_length=MAX_CURSOR_LENGTH)]
PositiveRevisionNumber = Annotated[int, Field(ge=1)]
PageLimit = Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)]


class WireModel(BaseModel):
    """Strict base for shared HTTP wire models."""

    model_config = ConfigDict(extra="forbid")


class Citation(WireModel):
    """An exact, authorization-checked Page/Revision citation."""

    section_id: OpaqueIdentifier
    page_id: OpaqueIdentifier
    revision_id: OpaqueIdentifier
    revision_number: PositiveRevisionNumber
    href: Annotated[str, Field(min_length=1, max_length=2_048)]

    @field_validator("href")
    @classmethod
    def require_relative_api_href(cls, value: str) -> str:
        return validate_api_v1_path(value)


class PaginationParameters(WireModel):
    """Shared bounded collection input; cursor integrity is verified by its owner."""

    limit: PageLimit = DEFAULT_PAGE_LIMIT
    cursor: OpaqueCursor | None = None


class PaginatedResponse[ItemT](WireModel):
    """Provider-neutral response shape for a bounded collection."""

    items: Annotated[list[ItemT], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: OpaqueCursor | None = None


def build_api_v1_path(*segments: str) -> str:
    """Build one canonical, unescaped relative API v1 path."""
    if not segments:
        raise ValueError("At least one API path segment is required.")
    for segment in segments:
        if segment in {"", ".", ".."} or _API_PATH_SEGMENT_PATTERN.fullmatch(segment) is None:
            raise ValueError("API path segments must use canonical unescaped ASCII form.")
    return f"{API_V1_PREFIX}/{'/'.join(segments)}"


def validate_api_v1_path(value: str) -> str:
    """Validate one canonical relative API v1 path without decoding it."""
    prefix = f"{API_V1_PREFIX}/"
    if (
        len(value) > 2_048
        or not value.startswith(prefix)
        or "%" in value
        or "?" in value
        or "#" in value
        or "\\" in value
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("Citation href must be a canonical relative versioned API path.")

    segments = value.removeprefix(prefix).split("/")
    try:
        canonical = build_api_v1_path(*segments)
    except ValueError as exc:
        raise ValueError("Citation href must be a canonical relative versioned API path.") from exc
    if canonical != value:
        raise ValueError("Citation href must be a canonical relative versioned API path.")
    return value
