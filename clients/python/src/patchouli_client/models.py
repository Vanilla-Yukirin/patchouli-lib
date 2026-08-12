from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote, urlsplit

from patchouli_client.errors import ProtocolError

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_CURSOR_LENGTH = 4_096

_RFC3339_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$",
    re.ASCII,
)


def _object(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"{context} must be a JSON object")
    return cast(dict[str, object], value)


def _string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProtocolError(f"response field {key!r} must be a string")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"response field {key!r} must be a string or null")
    return value


def _required_nullable_string(data: Mapping[str, object], key: str) -> str | None:
    if key not in data:
        raise ProtocolError(f"response field {key!r} is required")
    return _optional_string(data, key)


def _integer(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"response field {key!r} must be an integer")
    return value


def _boolean(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ProtocolError(f"response field {key!r} must be a boolean")
    return value


def _string_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProtocolError(f"response field {key!r} must be an array of strings")
    return tuple(value)


def _object_list(data: Mapping[str, object], key: str) -> Sequence[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ProtocolError(f"response field {key!r} must be an array")
    return value


def parse_rfc3339(value: str) -> datetime:
    match = _RFC3339_PATTERN.fullmatch(value)
    if match is None or match.group("offset") == "-00:00":
        raise ProtocolError("response contained an invalid RFC 3339 timestamp")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ProtocolError("response contained an invalid RFC 3339 timestamp") from exc
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ProtocolError("timestamp cannot be represented in UTC") from exc


def format_rfc3339_utc(value: datetime) -> str:
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise ProtocolError("timestamp cannot be represented in UTC") from exc
    if value.tzinfo is None or offset is None:
        raise ValueError("timestamp must include a UTC offset")
    try:
        normalized = value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ProtocolError("timestamp cannot be represented in UTC") from exc
    return (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}T"
        f"{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}."
        f"{normalized.microsecond:06d}Z"
    )


def require_canonical_api_path(value: str, *, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise ProtocolError(f"{context} was not a canonical relative API resource path")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ProtocolError(f"{context} was not a canonical relative API resource path") from exc
    segments = value.split("/")
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != value
        or not value.startswith("/api/v1/")
        or "%" in value
        or "\\" in value
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
        or any(segment in {"", ".", ".."} for segment in segments[3:])
    ):
        raise ProtocolError(f"{context} was not a canonical relative API resource path")
    return value


@dataclass(frozen=True, slots=True)
class ApiLimits:
    max_content_bytes: int
    default_page_size: int
    max_page_size: int
    max_query_bytes: int

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ApiLimits:
        return cls(
            max_content_bytes=_integer(data, "max_content_bytes"),
            default_page_size=_integer(data, "default_page_size"),
            max_page_size=_integer(data, "max_page_size"),
            max_query_bytes=_integer(data, "max_query_bytes"),
        )


@dataclass(frozen=True, slots=True)
class IdempotencySupport:
    content_mutations: bool
    successful_replay_retention: str

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> IdempotencySupport:
        return cls(
            content_mutations=_boolean(data, "content_mutations"),
            successful_replay_retention=_string(data, "successful_replay_retention"),
        )


@dataclass(frozen=True, slots=True)
class Capabilities:
    api_versions: tuple[str, ...]
    features: tuple[str, ...]
    limits: ApiLimits
    idempotency: IdempotencySupport

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Capabilities:
        return cls(
            api_versions=_string_tuple(data, "api_versions"),
            features=_string_tuple(data, "features"),
            limits=ApiLimits.from_dict(_object(data.get("limits"), context="limits")),
            idempotency=IdempotencySupport.from_dict(
                _object(data.get("idempotency"), context="idempotency")
            ),
        )


@dataclass(frozen=True, slots=True)
class Grant:
    section_id: str
    actions: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Grant:
        return cls(section_id=_string(data, "section_id"), actions=_string_tuple(data, "actions"))


@dataclass(frozen=True, slots=True)
class WhoAmI:
    caller_id: str
    credential_id: str
    kind: str
    expires_at: datetime
    policy_version: int
    grants: tuple[Grant, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WhoAmI:
        return cls(
            caller_id=_string(data, "caller_id"),
            credential_id=_string(data, "credential_id"),
            kind=_string(data, "kind"),
            expires_at=parse_rfc3339(_string(data, "expires_at")),
            policy_version=_integer(data, "policy_version"),
            grants=tuple(
                Grant.from_dict(_object(item, context="grant"))
                for item in _object_list(data, "grants")
            ),
        )


@dataclass(frozen=True, slots=True)
class Section:
    section_id: str
    name: str = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Section:
        return cls(section_id=_string(data, "section_id"), name=_string(data, "name"))


@dataclass(frozen=True, slots=True)
class Book:
    section_id: str
    book_id: str
    title: str = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Book:
        return cls(
            section_id=_string(data, "section_id"),
            book_id=_string(data, "book_id"),
            title=_string(data, "title"),
        )


@dataclass(frozen=True, slots=True)
class Citation:
    section_id: str
    page_id: str
    revision_id: str
    revision_number: int
    href: str

    def __post_init__(self) -> None:
        if self.revision_number < 1:
            raise ProtocolError("citation did not contain a positive Revision")
        require_canonical_api_path(self.href, context="citation href")
        expected_href = (
            f"/api/v1/sections/{quote(self.section_id, safe='')}/pages/"
            f"{quote(self.page_id, safe='')}/revisions/{self.revision_number}"
        )
        if self.href != expected_href:
            raise ProtocolError("citation href did not identify its exact Page and Revision")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Citation:
        return cls(
            section_id=_string(data, "section_id"),
            page_id=_string(data, "page_id"),
            revision_id=_string(data, "revision_id"),
            revision_number=_integer(data, "revision_number"),
            href=_string(data, "href"),
        )


@dataclass(frozen=True, slots=True)
class Revision:
    page_id: str
    revision_id: str
    revision_number: int
    created_at: datetime
    content_type: str
    content_sha256: str
    content: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if self.revision_number < 1:
            raise ProtocolError("Revision number must be positive")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Revision:
        return cls(
            page_id=_string(data, "page_id"),
            revision_id=_string(data, "revision_id"),
            revision_number=_integer(data, "revision_number"),
            created_at=parse_rfc3339(_string(data, "created_at")),
            content_type=_string(data, "content_type"),
            content_sha256=_string(data, "content_sha256"),
            content=_optional_string(data, "content"),
        )


@dataclass(frozen=True, slots=True)
class Page:
    section_id: str
    book_id: str
    page_id: str
    title: str = field(repr=False)
    page_type: str
    occurred_at: datetime
    current_revision_id: str
    current_revision_number: int

    def __post_init__(self) -> None:
        if self.current_revision_number < 1:
            raise ProtocolError("current Revision number must be positive")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Page:
        return cls(
            section_id=_string(data, "section_id"),
            book_id=_string(data, "book_id"),
            page_id=_string(data, "page_id"),
            title=_string(data, "title"),
            page_type=_string(data, "type"),
            occurred_at=parse_rfc3339(_string(data, "occurred_at")),
            current_revision_id=_string(data, "current_revision_id"),
            current_revision_number=_integer(data, "current_revision_number"),
        )


@dataclass(frozen=True, slots=True)
class PageDocument:
    page: Page
    revision: Revision
    citation: Citation

    def __post_init__(self) -> None:
        if (
            self.revision.page_id != self.page.page_id
            or self.citation.page_id != self.page.page_id
            or self.citation.section_id != self.page.section_id
            or self.citation.revision_id != self.revision.revision_id
            or self.citation.revision_number != self.revision.revision_number
        ):
            raise ProtocolError("Page, Revision, and citation identifiers did not agree")

    def require_current_revision(self) -> None:
        if (
            self.page.current_revision_id != self.revision.revision_id
            or self.page.current_revision_number != self.revision.revision_number
        ):
            raise ProtocolError("response Revision did not match the current Page pointer")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PageDocument:
        return cls(
            page=Page.from_dict(_object(data.get("page"), context="page")),
            revision=Revision.from_dict(_object(data.get("revision"), context="revision")),
            citation=Citation.from_dict(_object(data.get("citation"), context="citation")),
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    page: Page
    citation: Citation
    snippet: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.citation.page_id != self.page.page_id
            or self.citation.section_id != self.page.section_id
            or self.citation.revision_id != self.page.current_revision_id
            or self.citation.revision_number != self.page.current_revision_number
        ):
            raise ProtocolError("search result citation did not identify the current Revision")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SearchHit:
        return cls(
            page=Page.from_dict(_object(data.get("page"), context="page")),
            citation=Citation.from_dict(_object(data.get("citation"), context="citation")),
            snippet=_string(data, "snippet"),
        )


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SourceInput:
    kind: str
    locator: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("source kind must not be empty")
        if self.locator is not None and not isinstance(self.locator, str):
            raise ValueError("source locator must be a string or null")

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind}
        if self.locator is not None:
            result["locator"] = self.locator
        return result


@dataclass(frozen=True, slots=True)
class ArchiveCreateMetadata:
    title: str = field(repr=False)
    occurred_at: datetime
    source: SourceInput

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("archive title must not be empty")
        if not isinstance(self.occurred_at, datetime):
            raise ValueError("archive occurrence time must be a datetime")
        if not isinstance(self.source, SourceInput):
            raise ValueError("archive source must be SourceInput")
        format_rfc3339_utc(self.occurred_at)

    def to_wire(self) -> dict[str, object]:
        return {
            "title": self.title,
            "occurred_at": format_rfc3339_utc(self.occurred_at),
            "source": self.source.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ArchiveRevisionMetadata:
    source: SourceInput

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceInput):
            raise ValueError("archive source must be SourceInput")

    def to_wire(self) -> dict[str, object]:
        return {"source": self.source.to_wire()}


@dataclass(frozen=True, slots=True)
class MarkdownContent:
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise ValueError("Markdown content must be bytes")
        if not self.body:
            raise ValueError("Markdown content must not be empty")
        if len(self.body) > MAX_ARCHIVE_BYTES:
            raise ValueError("Markdown content exceeds the alpha client ceiling")
        if b"\x00" in self.body:
            raise ValueError("Markdown content must not contain NUL")
        try:
            self.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown content must be valid UTF-8") from exc

    @classmethod
    def from_text(cls, value: str) -> MarkdownContent:
        return cls(value.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str = field(repr=False)
    limit: int = DEFAULT_PAGE_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query:
            raise ValueError("search query must not be empty")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("search limit must be an integer")
        if not 1 <= self.limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"search limit must be between 1 and {MAX_PAGE_LIMIT}")
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or not self.cursor
            or len(self.cursor) > MAX_CURSOR_LENGTH
        ):
            raise ValueError("search cursor must be a non-empty bounded string or null")

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"query": self.query, "limit": self.limit}
        if self.cursor is not None:
            result["cursor"] = self.cursor
        return result


@dataclass(frozen=True, slots=True)
class ProblemDetails:
    type: str
    title: str
    status: int
    detail: str = field(repr=False)
    code: str
    request_id: str
    instance: str | None = field(default=None, repr=False)
    details: Mapping[str, object] = field(default_factory=dict, repr=False)
    extensions: Mapping[str, object] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ProblemDetails:
        known = {"type", "title", "status", "detail", "code", "request_id", "instance", "details"}
        details_value = data.get("details", {})
        details = _object(details_value, context="problem details")
        extensions = {key: value for key, value in data.items() if key not in known}
        return cls(
            type=_string(data, "type"),
            title=_string(data, "title"),
            status=_integer(data, "status"),
            detail=_string(data, "detail"),
            code=_string(data, "code"),
            request_id=_string(data, "request_id"),
            instance=_optional_string(data, "instance"),
            details=details,
            extensions=extensions,
        )


def response_object(value: object) -> Mapping[str, object]:
    return _object(value, context="response")


def response_items(value: Mapping[str, object]) -> Sequence[object]:
    return _object_list(value, "items")


def response_cursor(value: Mapping[str, object]) -> str | None:
    cursor = _required_nullable_string(value, "next_cursor")
    if cursor is not None and (not cursor or len(cursor) > MAX_CURSOR_LENGTH):
        raise ProtocolError(
            "response field 'next_cursor' must be a non-empty bounded string or null"
        )
    return cursor
