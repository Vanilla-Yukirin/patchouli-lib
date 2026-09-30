from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import quote, urlsplit

from patchouli_client.errors import ProtocolError

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_CURSOR_LENGTH = 4_096
MAX_SEARCH_REQUEST_BYTES = 96 * 1024
MAX_SEARCH_KEYWORD_BYTES = 32 * 1024
MAX_SEARCH_ITEMS = 256
MAX_FILE_SET_FILES = 64
MAX_FILE_SET_FILE_BYTES = 16 * 1024 * 1024
MAX_FILE_SET_PAGE_BYTES = 64 * 1024 * 1024
_FILE_SET_DOMAIN = b"patchouli-page-file-snapshot-v1\x00"
_FILE_NAME_FORBIDDEN = frozenset('<>:"/\\|?*')
_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"})

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
class FileSetLimits:
    max_file_bytes: int
    max_page_bytes: int
    max_files_per_page: int

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FileSetLimits:
        values = tuple(
            _integer(data, key)
            for key in ("max_file_bytes", "max_page_bytes", "max_files_per_page")
        )
        if any(value < 1 for value in values):
            raise ProtocolError("file-set limits must be positive")
        return cls(*values)


@dataclass(frozen=True, slots=True)
class SearchLimits:
    max_request_bytes: int
    max_keywords_bytes: int
    max_keywords: int
    max_tags: int
    max_libraries: int

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SearchLimits:
        values = tuple(
            _integer(data, key)
            for key in (
                "max_request_bytes",
                "max_keywords_bytes",
                "max_keywords",
                "max_tags",
                "max_libraries",
            )
        )
        if any(value < 1 for value in values):
            raise ProtocolError("search limits must be positive")
        return cls(*values)


@dataclass(frozen=True, slots=True)
class ApiLimits:
    max_content_bytes: int
    default_page_size: int
    max_page_size: int
    max_query_bytes: int  # Deprecated legacy field; search_pages uses search limits.
    file_set: FileSetLimits | None = None
    search: SearchLimits | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ApiLimits:
        return cls(
            max_content_bytes=_integer(data, "max_content_bytes"),
            default_page_size=_integer(data, "default_page_size"),
            max_page_size=_integer(data, "max_page_size"),
            max_query_bytes=_integer(data, "max_query_bytes"),
            file_set=(
                FileSetLimits.from_dict(_object(data["file_set"], context="file_set"))
                if data.get("file_set") is not None
                else None
            ),
            search=(
                SearchLimits.from_dict(_object(data["search"], context="search"))
                if data.get("search") is not None
                else None
            ),
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
class LibraryGrant:
    library_id: str
    actions: tuple[Literal["read", "write"], ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> LibraryGrant:
        if set(data) != {"library_id", "actions"}:
            raise ProtocolError("library grant must contain exactly library_id and actions")
        library_id = _string(data, "library_id")
        actions = _string_tuple(data, "actions")
        if not library_id or any(action not in {"read", "write"} for action in actions):
            raise ProtocolError("library grant contained an invalid permission")
        return cls(
            library_id=library_id, actions=cast(tuple[Literal["read", "write"], ...], actions)
        )


@dataclass(frozen=True, slots=True)
class WhoAmI:
    caller_id: str
    credential_id: str
    kind: str
    expires_at: datetime
    policy_version: int
    grants: tuple[Grant, ...]
    policy_mode: Literal["operator", "legacy_section", "library_grants"] | None = None
    library_grants: tuple[LibraryGrant, ...] | None = None
    name: str | None = field(default=None, repr=False)
    description: str | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WhoAmI:
        has_mode = "policy_mode" in data
        has_library_grants = "library_grants" in data
        if has_mode != has_library_grants:
            raise ProtocolError("policy_mode and library_grants must be supplied together")
        policy_mode: Literal["operator", "legacy_section", "library_grants"] | None = None
        library_grants: tuple[LibraryGrant, ...] | None = None
        if has_mode:
            value = data["policy_mode"]
            if not isinstance(value, str) or value not in {
                "operator",
                "legacy_section",
                "library_grants",
            }:
                raise ProtocolError("response field 'policy_mode' contained an unknown mode")
            policy_mode = cast(Literal["operator", "legacy_section", "library_grants"], value)
            library_grants = tuple(
                LibraryGrant.from_dict(_object(item, context="library grant"))
                for item in _object_list(data, "library_grants")
            )
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
            policy_mode=policy_mode,
            library_grants=library_grants,
            name=_optional_string(data, "name"),
            description=_optional_string(data, "description"),
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
class PageMetadata:
    page: Page
    citation: Citation

    def __post_init__(self) -> None:
        if (
            self.citation.page_id != self.page.page_id
            or self.citation.section_id != self.page.section_id
            or self.citation.revision_id != self.page.current_revision_id
            or self.citation.revision_number != self.page.current_revision_number
        ):
            raise ProtocolError("Page metadata citation did not identify the current Revision")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PageMetadata:
        if set(data) != {"page", "citation"}:
            raise ProtocolError("Page metadata item must contain exactly 'page' and 'citation'")
        return cls(
            page=Page.from_dict(_object(data.get("page"), context="page")),
            citation=Citation.from_dict(_object(data.get("citation"), context="citation")),
        )


@dataclass(frozen=True, slots=True)
class OccurrenceNotice:
    source: Literal["server_utc"]
    warning_code: Literal["occurred_at_defaulted"]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> OccurrenceNotice:
        if set(data) != {"source", "warning_code"}:
            raise ProtocolError("occurrence notice did not contain the expected fields")
        if data["source"] != "server_utc" or data["warning_code"] != "occurred_at_defaulted":
            raise ProtocolError("occurrence notice contained an unknown defaulting reason")
        return cls(source="server_utc", warning_code="occurred_at_defaulted")


@dataclass(frozen=True, slots=True)
class PageDocument:
    page: Page
    revision: Revision
    citation: Citation
    occurrence_notice: OccurrenceNotice | None = None

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
        notice = (
            OccurrenceNotice.from_dict(
                _object(data["occurrence_notice"], context="occurrence notice")
            )
            if "occurrence_notice" in data
            else None
        )
        return cls(
            page=Page.from_dict(_object(data.get("page"), context="page")),
            revision=Revision.from_dict(_object(data.get("revision"), context="revision")),
            citation=Citation.from_dict(_object(data.get("citation"), context="citation")),
            occurrence_notice=notice,
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


@dataclass(frozen=True, slots=True, init=False)
class ArchiveCreateMetadata:
    title: str = field(repr=False)
    occurred_at: datetime | None
    source: SourceInput

    def __init__(
        self,
        title: str,
        occurred_at: datetime | None = None,
        source: SourceInput | None = None,
    ) -> None:
        # Preserve the original positional order while allowing callers to omit
        # occurred_at with source=... .  An omitted time stays absent on the wire.
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "source", source)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("archive title must not be empty")
        if self.occurred_at is not None and not isinstance(self.occurred_at, datetime):
            raise ValueError("archive occurrence time must be a datetime")
        if not isinstance(self.source, SourceInput):
            raise ValueError("archive source must be SourceInput")
        if self.occurred_at is not None:
            format_rfc3339_utc(self.occurred_at)

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"title": self.title, "source": self.source.to_wire()}
        if self.occurred_at is not None:
            result["occurred_at"] = format_rfc3339_utc(self.occurred_at)
        return result


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


def require_file_set_name(value: str) -> str:
    if type(value) is not str or len(value) > 512:
        raise ValueError("file-set filename must be bounded text")
    name = unicodedata.normalize("NFC", value)
    if (
        not name
        or name in {".", ".."}
        or name[0] == " "
        or name[-1] == "."
        or any(part.endswith(" ") for part in name.split("."))
        or any(character in _FILE_NAME_FORBIDDEN for character in name)
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in name
        )
        or len(name.encode("utf-8")) > 255
    ):
        raise ValueError("file-set filename is not a safe flat filename")
    stem = name.split(".", 1)[0].upper()
    if stem in _DEVICE_NAMES or (
        len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "0123456789\u00b9\u00b2\u00b3"
    ):
        raise ValueError("file-set filename is reserved")
    return name


@dataclass(frozen=True, slots=True)
class FileSetFile:
    filename: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        require_file_set_name(self.filename)
        if type(self.body) is not bytes or len(self.body) > MAX_FILE_SET_FILE_BYTES:
            raise ValueError("file-set content must be bounded bytes")


@dataclass(frozen=True, slots=True)
class FileSetSource:
    kind: str
    locator: str | None = field(default=None, repr=False)
    captured_at: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not str
            or not self.kind
            or self.kind.strip() != self.kind
            or len(self.kind) > 100
            or "\x00" in self.kind
        ):
            raise ValueError("file-set source kind must be non-empty without edge whitespace")
        if self.locator is not None and (
            type(self.locator) is not str or not self.locator or "\x00" in self.locator
        ):
            raise ValueError("file-set source locator must be non-empty text without NUL")
        if self.captured_at is not None and type(self.captured_at) is not int:
            raise ValueError("file-set captured_at must be UTC microseconds")

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind}
        if self.locator is not None:
            result["locator"] = self.locator
        if self.captured_at is not None:
            result["captured_at"] = self.captured_at
        return result


@dataclass(frozen=True, slots=True)
class FileSetCreateMetadata:
    title: str = field(repr=False)
    source: FileSetSource
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.title) is not str or not self.title.strip():
            raise ValueError("file-set title must be non-empty")
        if not isinstance(self.source, FileSetSource):
            raise ValueError("file-set source must be FileSetSource")
        if self.occurred_at is not None:
            if not isinstance(self.occurred_at, datetime):
                raise ValueError("file-set occurred_at must be a datetime")
            format_rfc3339_utc(self.occurred_at)

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {"title": self.title, "source": self.source.to_wire()}
        if self.occurred_at is not None:
            result["occurred_at"] = format_rfc3339_utc(self.occurred_at)
        return result


@dataclass(frozen=True, slots=True)
class FileSetRevisionMetadata:
    source: FileSetSource

    def __post_init__(self) -> None:
        if not isinstance(self.source, FileSetSource):
            raise ValueError("file-set source must be FileSetSource")

    def to_wire(self) -> dict[str, object]:
        return {"source": self.source.to_wire()}


def _sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII) is None:
        raise ProtocolError("response contained an invalid SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class FileSetFileSummary:
    filename: str
    size_bytes: int
    content_sha256: str

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FileSetFileSummary:
        if set(data) != {"filename", "size_bytes", "content_sha256"}:
            raise ProtocolError("file-set entry did not contain the expected fields")
        name = _string(data, "filename")
        try:
            if require_file_set_name(name) != name:
                raise ValueError("noncanonical filename")
        except ValueError:
            raise ProtocolError("file-set response contained an unsafe filename") from None
        size = _integer(data, "size_bytes")
        if not 0 <= size <= MAX_FILE_SET_FILE_BYTES:
            raise ProtocolError("file-set response contained an invalid file size")
        return cls(name, size, _sha256(_string(data, "content_sha256")))


@dataclass(frozen=True, slots=True)
class FileSetManifest:
    page_id: str
    revision_id: str
    revision_number: int
    snapshot_sha256: str
    files: tuple[FileSetFileSummary, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FileSetManifest:
        if set(data) != {"page_id", "revision_id", "revision_number", "snapshot_sha256", "files"}:
            raise ProtocolError("file-set manifest did not contain the expected fields")
        page_id = _string(data, "page_id")
        revision_id = _string(data, "revision_id")
        number = _integer(data, "revision_number")
        files = tuple(
            FileSetFileSummary.from_dict(_object(item, context="file-set entry"))
            for item in _object_list(data, "files")
        )
        if (
            not page_id
            or not revision_id
            or number < 1
            or not 1 <= len(files) <= MAX_FILE_SET_FILES
        ):
            raise ProtocolError("file-set manifest contained invalid identifiers or file count")
        names = [item.filename for item in files]
        if names != sorted(names, key=lambda name: name.encode("utf-8")) or len(
            {unicodedata.normalize("NFC", name.casefold()) for name in names}
        ) != len(names):
            raise ProtocolError("file-set manifest was not canonically ordered")
        if sum(item.size_bytes for item in files) > MAX_FILE_SET_PAGE_BYTES:
            raise ProtocolError("file-set manifest exceeded the page size limit")
        digest = hashlib.sha256(_FILE_SET_DOMAIN)
        digest.update(len(files).to_bytes(8, "big"))
        for item in files:
            encoded = item.filename.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(item.size_bytes.to_bytes(8, "big"))
            digest.update(bytes.fromhex(item.content_sha256))
        snapshot = _sha256(_string(data, "snapshot_sha256"))
        if digest.hexdigest() != snapshot:
            raise ProtocolError("file-set manifest snapshot digest did not match its files")
        return cls(page_id, revision_id, number, snapshot, files)


def _manifest_fields(data: Mapping[str, object], extra: set[str]) -> FileSetManifest:
    if (
        set(data)
        != {"page_id", "revision_id", "revision_number", "snapshot_sha256", "files"} | extra
    ):
        raise ProtocolError("file-set response did not contain the expected fields")
    return FileSetManifest.from_dict(
        {key: value for key, value in data.items() if key not in extra}
    )


@dataclass(frozen=True, slots=True)
class FileSetCreateResult:
    section_id: str
    book_id: str
    occurred_at: datetime
    occurrence_defaulted: bool
    manifest: FileSetManifest

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FileSetCreateResult:
        extra = {"section_id", "book_id", "occurred_at", "occurrence_defaulted"}
        manifest = _manifest_fields(data, extra)
        if manifest.revision_number != 1:
            raise ProtocolError("new Page did not contain first Revision")
        section_id, book_id = _string(data, "section_id"), _string(data, "book_id")
        if not section_id or not book_id:
            raise ProtocolError("new Page response contained an empty scope identifier")
        return cls(
            section_id,
            book_id,
            parse_rfc3339(_string(data, "occurred_at")),
            _boolean(data, "occurrence_defaulted"),
            manifest,
        )


@dataclass(frozen=True, slots=True)
class FileSetRevisionResult:
    changed: bool
    section_id: str
    manifest: FileSetManifest

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FileSetRevisionResult:
        extra = {"changed", "section_id"}
        manifest = _manifest_fields(data, extra)
        section_id = _string(data, "section_id")
        if not section_id:
            raise ProtocolError("file-set revision contained an empty Section identifier")
        return cls(_boolean(data, "changed"), section_id, manifest)


@dataclass(frozen=True, slots=True)
class SearchTagRef:
    library_id: str
    tag_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.library_id, str) or not self.library_id:
            raise ValueError("search Tag Library identifier must not be empty")
        if not isinstance(self.tag_id, str) or not self.tag_id:
            raise ValueError("search Tag identifier must not be empty")

    def to_wire(self) -> dict[str, str]:
        return {"library_id": self.library_id, "tag_id": self.tag_id}


@dataclass(frozen=True, slots=True)
class CurrentPageSearchRequest:
    keywords: tuple[str, ...] = field(default=(), repr=False)
    tags_any: tuple[SearchTagRef, ...] = ()
    occurred_from_us: int | None = None
    occurred_before_us: int | None = None
    libraries: tuple[str, ...] | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CurrentPageSearchRequest:
        allowed = {
            "keywords",
            "tags_any",
            "occurred_from_us",
            "occurred_before_us",
            "libraries",
            "limit",
        }
        if set(data) - allowed:
            raise ValueError("search request has unsupported fields")
        keywords = data.get("keywords", [])
        tags = data.get("tags_any", [])
        libraries = data.get("libraries")
        if not isinstance(keywords, list) or not isinstance(tags, list):
            raise ValueError("search keywords and Tags must be arrays")
        if libraries is not None and not isinstance(libraries, list):
            raise ValueError("search Libraries must be an array or null")
        if any(not isinstance(tag, dict) or set(tag) != {"library_id", "tag_id"} for tag in tags):
            raise ValueError("search Tags must identify a Library and Tag")
        return cls(
            keywords=tuple(cast("list[str]", keywords)),
            tags_any=tuple(
                SearchTagRef(cast("str", tag["library_id"]), cast("str", tag["tag_id"]))
                for tag in tags
            ),
            occurred_from_us=cast("int | None", data.get("occurred_from_us")),
            occurred_before_us=cast("int | None", data.get("occurred_before_us")),
            libraries=None if libraries is None else tuple(cast("list[str]", libraries)),
            limit=cast("int", data.get("limit", DEFAULT_PAGE_LIMIT)),
        )

    def __post_init__(self) -> None:
        if not (
            self.keywords
            or self.tags_any
            or self.occurred_from_us is not None
            or self.occurred_before_us is not None
        ):
            raise ValueError("search requires at least one keyword, Tag, or time bound")
        if any(not isinstance(value, str) or not value for value in self.keywords):
            raise ValueError("search keywords must be non-empty strings")
        if len(self.keywords) > MAX_SEARCH_ITEMS:
            raise ValueError("search keyword count exceeds the supported limit")
        try:
            keyword_bytes = sum(len(value.encode("utf-8")) for value in self.keywords)
        except UnicodeError as exc:
            raise ValueError("search keywords must be valid UTF-8") from exc
        if keyword_bytes > MAX_SEARCH_KEYWORD_BYTES:
            raise ValueError("search keywords exceed the supported byte limit")
        if any(not isinstance(value, SearchTagRef) for value in self.tags_any):
            raise ValueError("search Tags must carry their Library identity")
        if len(self.tags_any) > MAX_SEARCH_ITEMS:
            raise ValueError("search Tag count exceeds the supported limit")
        if self.libraries is not None and not self.libraries:
            raise ValueError("search Libraries must not be an empty selection")
        if self.libraries is not None and any(
            not isinstance(value, str) or not value for value in self.libraries
        ):
            raise ValueError("search Libraries must be non-empty identifiers")
        if self.libraries is not None and len(self.libraries) > MAX_SEARCH_ITEMS:
            raise ValueError("search Library count exceeds the supported limit")
        for bound in (self.occurred_from_us, self.occurred_before_us):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
                raise ValueError("search time bounds must be integer UTC microseconds")
        if (
            self.occurred_from_us is not None
            and self.occurred_before_us is not None
            and self.occurred_from_us >= self.occurred_before_us
        ):
            raise ValueError("search time interval must be non-empty")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_PAGE_LIMIT
        ):
            raise ValueError("search limit must be within the supported page range")
        try:
            request_bytes = len(
                json.dumps(
                    self.to_wire(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            )
        except (UnicodeError, ValueError) as exc:
            raise ValueError("search request must be valid UTF-8 JSON") from exc
        if request_bytes > MAX_SEARCH_REQUEST_BYTES:
            raise ValueError("search request exceeds the supported byte limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "keywords": list(self.keywords),
            "tags_any": [tag.to_wire() for tag in self.tags_any],
            "occurred_from_us": self.occurred_from_us,
            "occurred_before_us": self.occurred_before_us,
            "libraries": None if self.libraries is None else list(self.libraries),
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class SearchMatchSource:
    kind: str
    file_name: str | None

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SearchMatchSource:
        kind = _string(data, "kind")
        if kind not in {"title", "file_name", "file_text"}:
            raise ProtocolError("search result contained an unknown match source")
        return cls(kind, _required_nullable_string(data, "file_name"))


@dataclass(frozen=True, slots=True)
class CurrentPageSearchItem:
    library_id: str
    section_id: str
    book_id: str
    page_id: str
    revision_id: str
    revision_number: int
    title: str
    occurred_at: int
    match_sources: tuple[SearchMatchSource, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CurrentPageSearchItem:
        return cls(
            library_id=_string(data, "library_id"),
            section_id=_string(data, "section_id"),
            book_id=_string(data, "book_id"),
            page_id=_string(data, "page_id"),
            revision_id=_string(data, "revision_id"),
            revision_number=_integer(data, "revision_number"),
            title=_string(data, "title"),
            occurred_at=_integer(data, "occurred_at"),
            match_sources=tuple(
                SearchMatchSource.from_dict(_object(value, context="match source"))
                for value in _object_list(data, "match_sources")
            ),
        )


@dataclass(frozen=True, slots=True)
class CurrentPageSearchResult:
    items: tuple[CurrentPageSearchItem, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CurrentPageSearchResult:
        return cls(
            tuple(
                CurrentPageSearchItem.from_dict(_object(value, context="search item"))
                for value in _object_list(data, "items")
            )
        )


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
