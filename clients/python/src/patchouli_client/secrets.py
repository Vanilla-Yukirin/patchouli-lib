from __future__ import annotations


def _require_header_value(value: str, *, label: str, max_length: int | None = None) -> str:
    if not value:
        raise ValueError(f"{label} must not be empty")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{label} is too long")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError(f"{label} must contain visible ASCII without whitespace")
    return value


class BearerToken:
    """A call-scoped secret whose normal string representations are redacted."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        self.__value = _require_header_value(value, label="bearer token")

    def __repr__(self) -> str:
        return "BearerToken(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def _authorization_value(self) -> str:
        return f"Bearer {self.__value}"


class IdempotencyKey:
    """An operation key kept out of reprs and transport error messages."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        self.__value = _require_header_value(value, label="idempotency key", max_length=200)

    def __repr__(self) -> str:
        return "IdempotencyKey(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def _header_value(self) -> str:
        return self.__value
