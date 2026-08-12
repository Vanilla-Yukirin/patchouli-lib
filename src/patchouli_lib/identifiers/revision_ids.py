"""Internal Page UID and immutable Revision identifier primitives."""

from __future__ import annotations

import re
from collections.abc import Callable
from secrets import token_bytes
from typing import Final

RANDOM_IDENTIFIER_BYTES: Final = 16
MAX_REVISION_NUMBER: Final = (1 << 63) - 1
DEFAULT_COLLISION_ATTEMPTS: Final = 8

_REVISION_ID_PATTERN: Final = re.compile(r"\Arev_[0-9a-f]{32}\Z")
_GENERATION_ERROR_MESSAGE: Final = "Identifier generation failed."
_INVALID_PAGE_UID_MESSAGE: Final = "Invalid internal Page identifier."
_INVALID_REVISION_ID_MESSAGE: Final = "Invalid Revision identifier."
_INVALID_REVISION_NUMBER_MESSAGE: Final = "Invalid Revision number."

TryReservePageUid = Callable[[bytes], bool]
TryReserveRevisionId = Callable[[str], bool]


class IdentifierGenerationError(RuntimeError):
    """Secure entropy or bounded collision regeneration failed."""

    def __init__(self) -> None:
        super().__init__(_GENERATION_ERROR_MESSAGE)


class InvalidPageUidError(ValueError):
    """An internal Page UID is not exactly 128 bits."""

    def __init__(self) -> None:
        super().__init__(_INVALID_PAGE_UID_MESSAGE)


class InvalidRevisionIdError(ValueError):
    """A Revision ID is outside the strict public wire syntax."""

    def __init__(self) -> None:
        super().__init__(_INVALID_REVISION_ID_MESSAGE)


class InvalidRevisionNumberError(ValueError):
    """A Page-local Revision number is not a positive signed 64-bit integer."""

    def __init__(self) -> None:
        super().__init__(_INVALID_REVISION_NUMBER_MESSAGE)


def _random_identifier_bytes() -> bytes:
    candidate = token_bytes(RANDOM_IDENTIFIER_BYTES)
    if type(candidate) is not bytes or len(candidate) != RANDOM_IDENTIFIER_BYTES:
        raise IdentifierGenerationError
    return candidate


def _validate_max_attempts(max_attempts: object) -> int:
    if type(max_attempts) is not int or max_attempts < 1:
        raise IdentifierGenerationError
    return max_attempts


def generate_page_uid() -> bytes:
    """Generate one internal 128-bit Page relational identifier."""

    return _random_identifier_bytes()


def generate_unique_page_uid(
    try_reserve: TryReservePageUid,
    *,
    max_attempts: int = DEFAULT_COLLISION_ATTEMPTS,
) -> bytes:
    """Generate and atomically reserve a Page UID within an attempt bound.

    ``try_reserve`` must claim the candidate atomically and return ``True`` only
    when that claim succeeds. Returning ``False`` reports a uniqueness collision.
    """

    attempts = _validate_max_attempts(max_attempts)
    for _ in range(attempts):
        candidate = generate_page_uid()
        if try_reserve(candidate):
            return candidate
    raise IdentifierGenerationError


def generate_revision_id() -> str:
    """Generate one immutable Revision ID from independent 128-bit entropy."""

    return f"rev_{_random_identifier_bytes().hex()}"


def generate_unique_revision_id(
    try_reserve: TryReserveRevisionId,
    *,
    max_attempts: int = DEFAULT_COLLISION_ATTEMPTS,
) -> str:
    """Generate and atomically reserve a Revision ID within an attempt bound.

    ``try_reserve`` must claim the candidate atomically and return ``True`` only
    when that claim succeeds. Returning ``False`` reports a uniqueness collision.
    """

    attempts = _validate_max_attempts(max_attempts)
    for _ in range(attempts):
        candidate = generate_revision_id()
        if try_reserve(candidate):
            return candidate
    raise IdentifierGenerationError


def validate_page_uid(value: bytes) -> bytes:
    """Validate an internal Page UID without assigning it public semantics."""

    if type(value) is not bytes or len(value) != RANDOM_IDENTIFIER_BYTES:
        raise InvalidPageUidError
    return value


def validate_revision_id(value: str) -> str:
    """Validate the exact case-sensitive Revision ID wire representation."""

    if not isinstance(value, str) or _REVISION_ID_PATTERN.fullmatch(value) is None:
        raise InvalidRevisionIdError
    return value


def validate_revision_number(value: int) -> int:
    """Validate a positive signed 64-bit Page-local Revision number."""

    if type(value) is not int or not 1 <= value <= MAX_REVISION_NUMBER:
        raise InvalidRevisionNumberError
    return value


__all__ = [
    "DEFAULT_COLLISION_ATTEMPTS",
    "MAX_REVISION_NUMBER",
    "RANDOM_IDENTIFIER_BYTES",
    "IdentifierGenerationError",
    "InvalidPageUidError",
    "InvalidRevisionIdError",
    "InvalidRevisionNumberError",
    "generate_page_uid",
    "generate_revision_id",
    "generate_unique_page_uid",
    "generate_unique_revision_id",
    "validate_page_uid",
    "validate_revision_id",
    "validate_revision_number",
]
