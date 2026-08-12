from __future__ import annotations

from collections.abc import Callable

import pytest

from patchouli_lib.identifiers import (
    MAX_REVISION_NUMBER,
    RANDOM_IDENTIFIER_BYTES,
    IdentifierGenerationError,
    InvalidPageUidError,
    InvalidRevisionIdError,
    InvalidRevisionNumberError,
    generate_page_uid,
    generate_revision_id,
    generate_unique_page_uid,
    generate_unique_revision_id,
    validate_page_uid,
    validate_revision_id,
    validate_revision_number,
)
from patchouli_lib.identifiers import revision_ids as revision_ids_module


def _sequence_random(values: list[bytes]) -> Callable[[int], bytes]:
    iterator = iter(values)

    def random_bytes(length: int) -> bytes:
        assert length == RANDOM_IDENTIFIER_BYTES
        return next(iterator)

    return random_bytes


def test_page_uid_and_revision_id_use_independent_os_entropy_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = b"\x11" * RANDOM_IDENTIFIER_BYTES
    revision_bytes = b"\x22" * RANDOM_IDENTIFIER_BYTES
    monkeypatch.setattr(
        revision_ids_module,
        "token_bytes",
        _sequence_random([page_bytes, revision_bytes]),
    )

    page_uid = generate_page_uid()
    revision_id = generate_revision_id()

    assert page_uid == page_bytes
    assert revision_id == "rev_" + "22" * RANDOM_IDENTIFIER_BYTES
    assert page_uid.hex() not in revision_id


def test_revision_id_exact_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    random_value = bytes(range(RANDOM_IDENTIFIER_BYTES))
    monkeypatch.setattr(
        revision_ids_module,
        "token_bytes",
        _sequence_random([random_value]),
    )

    assert generate_revision_id() == "rev_000102030405060708090a0b0c0d0e0f"


def test_unique_page_uid_regenerates_injected_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = b"\xaa" * RANDOM_IDENTIFIER_BYTES
    accepted = b"\xbb" * RANDOM_IDENTIFIER_BYTES
    inspected: list[bytes] = []

    def try_reserve(candidate: bytes) -> bool:
        inspected.append(candidate)
        return candidate == accepted

    monkeypatch.setattr(
        revision_ids_module,
        "token_bytes",
        _sequence_random([duplicate, duplicate, accepted]),
    )
    result = generate_unique_page_uid(
        try_reserve,
        max_attempts=3,
    )

    assert result == accepted
    assert inspected == [duplicate, duplicate, accepted]


def test_unique_revision_id_regenerates_injected_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = b"\xcc" * RANDOM_IDENTIFIER_BYTES
    accepted = b"\xdd" * RANDOM_IDENTIFIER_BYTES
    inspected: list[str] = []

    def try_reserve(candidate: str) -> bool:
        inspected.append(candidate)
        return candidate == "rev_" + "dd" * RANDOM_IDENTIFIER_BYTES

    monkeypatch.setattr(
        revision_ids_module,
        "token_bytes",
        _sequence_random([duplicate, accepted]),
    )
    result = generate_unique_revision_id(
        try_reserve,
        max_attempts=2,
    )

    assert result == "rev_" + "dd" * RANDOM_IDENTIFIER_BYTES
    assert inspected == [
        "rev_" + "cc" * RANDOM_IDENTIFIER_BYTES,
        "rev_" + "dd" * RANDOM_IDENTIFIER_BYTES,
    ]


@pytest.mark.parametrize("kind", ["page", "revision"])
def test_bounded_collision_regeneration_fails_without_extra_attempt(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def duplicate_bytes(length: int) -> bytes:
        nonlocal calls
        calls += 1
        return b"\xee" * length

    monkeypatch.setattr(revision_ids_module, "token_bytes", duplicate_bytes)
    with pytest.raises(IdentifierGenerationError, match="^Identifier generation failed\\.$"):
        if kind == "page":
            generate_unique_page_uid(
                lambda _candidate: False,
                max_attempts=3,
            )
        else:
            generate_unique_revision_id(
                lambda _candidate: False,
                max_attempts=3,
            )

    assert calls == 3


@pytest.mark.parametrize("max_attempts", [0, -1, True, 1.0])
def test_collision_regeneration_rejects_invalid_attempt_bound(max_attempts: object) -> None:
    with pytest.raises(IdentifierGenerationError):
        generate_unique_page_uid(
            lambda _candidate: True,
            max_attempts=max_attempts,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"short",
        b"x" * (RANDOM_IDENTIFIER_BYTES + 1),
        bytearray(RANDOM_IDENTIFIER_BYTES),
        "synthetic-sensitive-random-value",
    ],
)
def test_generation_rejects_invalid_entropy_without_echo(
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_random(_length: int) -> bytes:
        return value  # type: ignore[return-value]

    monkeypatch.setattr(revision_ids_module, "token_bytes", invalid_random)
    with pytest.raises(IdentifierGenerationError) as exc_info:
        generate_page_uid()

    assert "synthetic-sensitive" not in str(exc_info.value)


def test_page_uid_validation_is_exact_and_type_strict() -> None:
    value = b"\x01" * RANDOM_IDENTIFIER_BYTES

    assert validate_page_uid(value) is value
    for invalid in (b"", b"x" * 15, b"x" * 17, bytearray(value)):
        with pytest.raises(InvalidPageUidError):
            validate_page_uid(invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "rev_",
        "rev_0" * 32,
        "rev_000102030405060708090a0b0c0d0e0",
        "rev_000102030405060708090a0b0c0d0e0f0",
        "REV_000102030405060708090a0b0c0d0e0f",
        "rev_000102030405060708090A0B0C0D0E0F",
        "rev_000102030405060708090a0b0c0d0e0g",
        "rev-000102030405060708090a0b0c0d0e0f",
        "rev_000102030405060708090a0b0c0d0e0f\n",
    ],
)
def test_revision_id_validation_rejects_malformed_wire_without_echo(value: str) -> None:
    with pytest.raises(InvalidRevisionIdError) as exc_info:
        validate_revision_id(value)

    assert value not in str(exc_info.value) or not value


def test_revision_id_validation_accepts_exact_wire() -> None:
    value = "rev_000102030405060708090a0b0c0d0e0f"
    assert validate_revision_id(value) == value


@pytest.mark.parametrize("value", [1, 2, MAX_REVISION_NUMBER])
def test_revision_number_accepts_positive_signed_64_bit_values(value: int) -> None:
    assert validate_revision_number(value) == value


@pytest.mark.parametrize(
    "value",
    [0, -1, MAX_REVISION_NUMBER + 1, True, False, 1.0, "1"],
)
def test_revision_number_rejects_invalid_values_without_echo(value: object) -> None:
    with pytest.raises(InvalidRevisionNumberError) as exc_info:
        validate_revision_number(value)  # type: ignore[arg-type]

    assert str(value) not in str(exc_info.value)
