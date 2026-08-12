"""Stable public Page identifier primitives."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Final

PAGE_ID_SCHEME: Final = "page-v1"
MAX_BASE_SLUG_BYTES: Final = 48
MAX_PAGE_ID_BYTES: Final = 80
MAX_COLLISION_ORDINAL: Final = 9_999_999_999

_MICROSECONDS_PER_SECOND: Final = 1_000_000
_MICROSECONDS_PER_DAY: Final = 86_400 * _MICROSECONDS_PER_SECOND
_EPOCH_ORDINAL: Final = date(1970, 1, 1).toordinal()
_MIN_UTC_MICROSECONDS: Final = (date.min.toordinal() - _EPOCH_ORDINAL) * _MICROSECONDS_PER_DAY
_MAX_UTC_MICROSECONDS: Final = (
    date.max.toordinal() - _EPOCH_ORDINAL + 1
) * _MICROSECONDS_PER_DAY - 1
_TIMESTAMP_PREFIX_BYTES: Final = 19
_REGISTRY_DOMAIN: Final = b"patchouli-page-id-v1\x00"

_RFC3339_PATTERN: Final = re.compile(
    r"\A"
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"[Tt](?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?"
    r"(?P<offset>[Zz]|[+-][0-9]{2}:[0-9]{2})"
    r"\Z"
)
_PREFIX_PATTERN: Final = re.compile(
    r"\A"
    r"(?P<year>[0-9]{4})(?P<month>[0-9]{2})(?P<day>[0-9]{2})"
    r"t(?P<hour>[0-9]{2})(?P<minute>[0-9]{2})(?P<second>[0-9]{2})"
    r"(?P<millisecond>[0-9]{3})z"
    r"\Z"
)
_SLUG_PATTERN: Final = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_ORDINAL_PATTERN: Final = re.compile(r"\A(?:[2-9]|[1-9][0-9]{1,9})\Z")

_INVALID_OCCURRENCE_TIME_MESSAGE: Final = "Invalid occurrence time."
_INVALID_TITLE_MESSAGE: Final = "Invalid Page title."
_INVALID_PAGE_ID_MESSAGE: Final = "Invalid Page identifier."
_INVALID_LIBRARY_SCOPE_MESSAGE: Final = "Invalid Library scope."


class InvalidOccurrenceTimeError(ValueError):
    """An occurrence time is outside the accepted RFC 3339 contract."""

    def __init__(self) -> None:
        super().__init__(_INVALID_OCCURRENCE_TIME_MESSAGE)


class InvalidPageTitleError(ValueError):
    """A creation title cannot be converted to a deterministic slug."""

    def __init__(self) -> None:
        super().__init__(_INVALID_TITLE_MESSAGE)


class InvalidPageIdError(ValueError):
    """A Page identifier is outside the public page-v1 syntax."""

    def __init__(self) -> None:
        super().__init__(_INVALID_PAGE_ID_MESSAGE)


class InvalidLibraryScopeError(ValueError):
    """A registry key was requested without an explicit Library scope."""

    def __init__(self) -> None:
        super().__init__(_INVALID_LIBRARY_SCOPE_MESSAGE)


@dataclass(frozen=True, slots=True)
class OccurrenceTime:
    """A validated instant represented in UTC without losing microseconds."""

    utc_microseconds: int
    canonical_utc: str


@dataclass(frozen=True, slots=True)
class GeneratedPageId:
    """A generated opaque Page ID plus its separately persisted components."""

    value: str
    timestamp_prefix: str
    base_slug: str
    collision_ordinal: int


@dataclass(frozen=True, slots=True)
class PageIdRegistryKey[LibraryScopeT]:
    """A Library-scoped registry key whose digest covers identifier text only."""

    library_scope: LibraryScopeT
    identifier_digest: bytes


def _require_int(value: object, error_type: type[ValueError]) -> int:
    if type(value) is not int:
        raise error_type
    return value


def _validate_civil_time(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
) -> date:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise InvalidOccurrenceTimeError
    try:
        return date(year, month, day)
    except ValueError:
        raise InvalidOccurrenceTimeError from None


def _format_utc_microseconds(utc_microseconds: int) -> str:
    days, day_microseconds = divmod(utc_microseconds, _MICROSECONDS_PER_DAY)
    utc_date = date.fromordinal(_EPOCH_ORDINAL + days)
    seconds, microseconds = divmod(day_microseconds, _MICROSECONDS_PER_SECOND)
    hour, seconds = divmod(seconds, 3600)
    minute, second = divmod(seconds, 60)
    return (
        f"{utc_date.year:04d}-{utc_date.month:02d}-{utc_date.day:02d}"
        f"T{hour:02d}:{minute:02d}:{second:02d}.{microseconds:06d}Z"
    )


def parse_occurrence_time(value: str) -> OccurrenceTime:
    """Parse strict RFC 3339 input and normalize it to signed UTC microseconds."""

    if not isinstance(value, str):
        raise InvalidOccurrenceTimeError
    match = _RFC3339_PATTERN.fullmatch(value)
    if match is None:
        raise InvalidOccurrenceTimeError

    year = int(match["year"])
    month = int(match["month"])
    day = int(match["day"])
    hour = int(match["hour"])
    minute = int(match["minute"])
    second = int(match["second"])
    local_date = _validate_civil_time(year, month, day, hour, minute, second)

    fraction = match["fraction"] or ""
    microseconds = int(fraction.ljust(6, "0")) if fraction else 0
    offset_text = match["offset"]
    offset_seconds = 0
    if offset_text not in {"Z", "z"}:
        sign = 1 if offset_text[0] == "+" else -1
        offset_hour = int(offset_text[1:3])
        offset_minute = int(offset_text[4:6])
        if offset_hour > 23 or offset_minute > 59 or offset_text == "-00:00":
            raise InvalidOccurrenceTimeError
        offset_seconds = sign * (offset_hour * 3600 + offset_minute * 60)

    local_days = local_date.toordinal() - _EPOCH_ORDINAL
    local_seconds = local_days * 86_400 + hour * 3600 + minute * 60 + second
    utc_microseconds = (local_seconds - offset_seconds) * _MICROSECONDS_PER_SECOND + microseconds
    if not _MIN_UTC_MICROSECONDS <= utc_microseconds <= _MAX_UTC_MICROSECONDS:
        raise InvalidOccurrenceTimeError
    return OccurrenceTime(
        utc_microseconds=utc_microseconds,
        canonical_utc=_format_utc_microseconds(utc_microseconds),
    )


def canonical_utc_wire(utc_microseconds: int) -> str:
    """Format an accepted signed UTC-microsecond value as canonical RFC 3339."""

    resolved = _require_int(utc_microseconds, InvalidOccurrenceTimeError)
    if not _MIN_UTC_MICROSECONDS <= resolved <= _MAX_UTC_MICROSECONDS:
        raise InvalidOccurrenceTimeError
    return _format_utc_microseconds(resolved)


def page_id_timestamp_prefix(utc_microseconds: int) -> str:
    """Floor signed microseconds to the earlier millisecond and format page-v1."""

    resolved = _require_int(utc_microseconds, InvalidOccurrenceTimeError)
    if not _MIN_UTC_MICROSECONDS <= resolved <= _MAX_UTC_MICROSECONDS:
        raise InvalidOccurrenceTimeError
    floored_microseconds = (resolved // 1000) * 1000
    canonical = _format_utc_microseconds(floored_microseconds)
    return (
        f"{canonical[0:4]}{canonical[5:7]}{canonical[8:10]}t"
        f"{canonical[11:13]}{canonical[14:16]}{canonical[17:19]}"
        f"{canonical[20:23]}z"
    )


def slugify_creation_title(stored_creation_title: str) -> str:
    """Create the deterministic model-free ASCII slug defined by page-v1."""

    if not isinstance(stored_creation_title, str):
        raise InvalidPageTitleError

    output: list[str] = []
    separator_pending = False
    ascii_alphanumeric_seen = False
    for character in stored_creation_title:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise InvalidPageTitleError
        if "A" <= character <= "Z":
            mapped = chr(codepoint + 32)
        elif "a" <= character <= "z" or "0" <= character <= "9":
            mapped = character
        else:
            separator_pending = True
            continue

        ascii_alphanumeric_seen = True
        if separator_pending and output and len(output) < MAX_BASE_SLUG_BYTES:
            output.append("-")
        separator_pending = False
        if len(output) < MAX_BASE_SLUG_BYTES:
            output.append(mapped)

    slug = "".join(output).rstrip("-")
    if ascii_alphanumeric_seen and slug:
        return slug

    encoded_title = stored_creation_title.encode("utf-8")
    fallback = hashlib.sha256(encoded_title).hexdigest()[:12]
    return f"page-{fallback}"


def _validate_collision_ordinal(value: object) -> int:
    ordinal = _require_int(value, InvalidPageIdError)
    if not 1 <= ordinal <= MAX_COLLISION_ORDINAL:
        raise InvalidPageIdError
    return ordinal


def generate_page_id(
    occurrence_time: OccurrenceTime,
    stored_creation_title: str,
    *,
    collision_ordinal: int = 1,
) -> GeneratedPageId:
    """Generate a page-v1 identifier and return its persisted components."""

    if not isinstance(occurrence_time, OccurrenceTime):
        raise InvalidOccurrenceTimeError
    canonical = canonical_utc_wire(occurrence_time.utc_microseconds)
    if occurrence_time.canonical_utc != canonical:
        raise InvalidOccurrenceTimeError

    ordinal = _validate_collision_ordinal(collision_ordinal)
    prefix = page_id_timestamp_prefix(occurrence_time.utc_microseconds)
    slug = slugify_creation_title(stored_creation_title)
    suffix = "" if ordinal == 1 else f"-{ordinal}"
    value = f"{prefix}-{slug}{suffix}"
    validate_page_id(value)
    return GeneratedPageId(
        value=value,
        timestamp_prefix=prefix,
        base_slug=slug,
        collision_ordinal=ordinal,
    )


def _is_valid_timestamp_prefix(value: str) -> bool:
    match = _PREFIX_PATTERN.fullmatch(value)
    if match is None:
        return False
    try:
        _validate_civil_time(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
        )
    except InvalidOccurrenceTimeError:
        return False
    return True


def _is_valid_base_slug(value: str) -> bool:
    return 1 <= len(value) <= MAX_BASE_SLUG_BYTES and _SLUG_PATTERN.fullmatch(value) is not None


def validate_page_id(value: str) -> str:
    """Validate opaque page-v1 syntax without exposing a component parser."""

    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PAGE_ID_BYTES:
        raise InvalidPageIdError
    if not value.isascii() or len(value) <= _TIMESTAMP_PREFIX_BYTES + 1:
        raise InvalidPageIdError

    prefix = value[:_TIMESTAMP_PREFIX_BYTES]
    if value[_TIMESTAMP_PREFIX_BYTES] != "-" or not _is_valid_timestamp_prefix(prefix):
        raise InvalidPageIdError

    slug_and_suffix = value[_TIMESTAMP_PREFIX_BYTES + 1 :]
    if _is_valid_base_slug(slug_and_suffix):
        return value

    base_slug, separator, ordinal_text = slug_and_suffix.rpartition("-")
    if (
        separator != "-"
        or not _is_valid_base_slug(base_slug)
        or _ORDINAL_PATTERN.fullmatch(ordinal_text) is None
        or int(ordinal_text) > MAX_COLLISION_ORDINAL
    ):
        raise InvalidPageIdError
    return value


def page_id_registry_digest(identifier_text: str) -> bytes:
    """Hash validated identifier text with the exact page-v1 domain separator."""

    validated = validate_page_id(identifier_text)
    return hashlib.sha256(_REGISTRY_DOMAIN + validated.encode("utf-8")).digest()


def page_id_registry_key[LibraryScopeT](
    library_scope: LibraryScopeT,
    identifier_text: str,
) -> PageIdRegistryKey[LibraryScopeT]:
    """Bind an opaque non-null Library scope to the identifier-text digest."""

    if library_scope is None:
        raise InvalidLibraryScopeError
    return PageIdRegistryKey(
        library_scope=library_scope,
        identifier_digest=page_id_registry_digest(identifier_text),
    )


__all__ = [
    "MAX_BASE_SLUG_BYTES",
    "MAX_COLLISION_ORDINAL",
    "MAX_PAGE_ID_BYTES",
    "PAGE_ID_SCHEME",
    "GeneratedPageId",
    "InvalidLibraryScopeError",
    "InvalidOccurrenceTimeError",
    "InvalidPageIdError",
    "InvalidPageTitleError",
    "OccurrenceTime",
    "PageIdRegistryKey",
    "canonical_utc_wire",
    "generate_page_id",
    "page_id_registry_digest",
    "page_id_registry_key",
    "page_id_timestamp_prefix",
    "parse_occurrence_time",
    "slugify_creation_title",
    "validate_page_id",
]
