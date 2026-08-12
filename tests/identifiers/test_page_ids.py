from __future__ import annotations

import hashlib

import pytest

from patchouli_lib.identifiers import (
    MAX_COLLISION_ORDINAL,
    GeneratedPageId,
    InvalidLibraryScopeError,
    InvalidOccurrenceTimeError,
    InvalidPageIdError,
    InvalidPageTitleError,
    OccurrenceTime,
    canonical_utc_wire,
    generate_page_id,
    page_id_registry_digest,
    page_id_registry_key,
    page_id_timestamp_prefix,
    parse_occurrence_time,
    slugify_creation_title,
    validate_page_id,
)


@pytest.mark.parametrize(
    ("wire", "microseconds", "canonical"),
    [
        ("1970-01-01T00:00:00Z", 0, "1970-01-01T00:00:00.000000Z"),
        ("1970-01-01T00:00:00.1Z", 100_000, "1970-01-01T00:00:00.100000Z"),
        ("1970-01-01T00:00:00.12Z", 120_000, "1970-01-01T00:00:00.120000Z"),
        ("1970-01-01T00:00:00.123Z", 123_000, "1970-01-01T00:00:00.123000Z"),
        ("1970-01-01T00:00:00.123456Z", 123_456, "1970-01-01T00:00:00.123456Z"),
        ("1970-01-01t00:00:00z", 0, "1970-01-01T00:00:00.000000Z"),
        ("1970-01-01t00:00:00.25z", 250_000, "1970-01-01T00:00:00.250000Z"),
        ("1970-01-01T01:00:00+01:00", 0, "1970-01-01T00:00:00.000000Z"),
        ("1969-12-31T18:30:00-05:30", 0, "1970-01-01T00:00:00.000000Z"),
        (
            "0001-01-01T00:00:00Z",
            -62_135_596_800_000_000,
            "0001-01-01T00:00:00.000000Z",
        ),
        (
            "9999-12-31T23:59:59.999999Z",
            253_402_300_799_999_999,
            "9999-12-31T23:59:59.999999Z",
        ),
        (
            "0001-01-01T23:59:59+23:59",
            -62_135_596_741_000_000,
            "0001-01-01T00:00:59.000000Z",
        ),
        (
            "9999-12-31T00:00:00-23:59",
            253_402_300_740_000_000,
            "9999-12-31T23:59:00.000000Z",
        ),
    ],
)
def test_occurrence_time_exact_vectors(
    wire: str,
    microseconds: int,
    canonical: str,
) -> None:
    parsed = parse_occurrence_time(wire)

    assert parsed == OccurrenceTime(
        utc_microseconds=microseconds,
        canonical_utc=canonical,
    )
    assert canonical_utc_wire(microseconds) == canonical


@pytest.mark.parametrize(
    "wire",
    [
        "",
        "1970-01-01T00:00Z",
        "1970-01-01T00:00:00",
        "1970-01-01 00:00:00Z",
        "1970-01-01T00:00:00.Z",
        "1970-01-01T00:00:00.1234567Z",
        "1970-01-01T00:00:60Z",
        "1970-01-01T24:00:00Z",
        "1970-01-01T00:60:00Z",
        "1970-02-29T00:00:00Z",
        "2024-02-30T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "10000-01-01T00:00:00Z",
        "1970-01-01T00:00:00-00:00",
        "1970-01-01T00:00:00+24:00",
        "1970-01-01T00:00:00+00:60",
        "1970-01-01T00:00:00+0000",
        "1970-01-01T00:00:00+00:00:00",
    ],
)
def test_occurrence_time_rejects_malformed_and_invalid_civil_values(wire: str) -> None:
    with pytest.raises(InvalidOccurrenceTimeError) as exc_info:
        parse_occurrence_time(wire)

    assert wire not in str(exc_info.value) or not wire


@pytest.mark.parametrize(
    "wire",
    [
        "0001-01-01T00:00:00+00:01",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-00:01",
        "9999-12-31T23:59:59-23:59",
    ],
)
def test_occurrence_time_rejects_utc_boundary_overflow(wire: str) -> None:
    with pytest.raises(InvalidOccurrenceTimeError):
        parse_occurrence_time(wire)


@pytest.mark.parametrize(
    "value",
    [
        -62_135_596_800_000_001,
        253_402_300_800_000_000,
        True,
        0.0,
    ],
)
def test_canonical_wire_rejects_out_of_range_or_non_integer_values(value: object) -> None:
    with pytest.raises(InvalidOccurrenceTimeError):
        canonical_utc_wire(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("microseconds", "prefix"),
    [
        (0, "19700101t000000000z"),
        (999, "19700101t000000000z"),
        (1_000, "19700101t000000001z"),
        (-1, "19691231t235959999z"),
        (-999, "19691231t235959999z"),
        (-1_000, "19691231t235959999z"),
        (-1_001, "19691231t235959998z"),
        (-62_135_596_800_000_000, "00010101t000000000z"),
        (253_402_300_799_999_999, "99991231t235959999z"),
    ],
)
def test_timestamp_prefix_uses_mathematical_floor(
    microseconds: int,
    prefix: str,
) -> None:
    assert page_id_timestamp_prefix(microseconds) == prefix


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Example Session", "example-session"),
        ("  HELLO,___world--42  ", "hello-world-42"),
        ("Café notes", "caf-notes"),
        ("Cafe\u0301 notes", "cafe-notes"),
        ("Åland", "land"),
        ("A\u030aland", "a-land"),
        ("你好", "page-670d9743542c"),
        ("", "page-e3b0c44298fc"),
        ("a" * 80, "a" * 48),
        ("A" * 47 + " B", "a" * 47),
    ],
)
def test_slug_exact_vectors(title: str, slug: str) -> None:
    assert slugify_creation_title(title) == slug


def test_slug_does_not_normalize_unicode_equivalents() -> None:
    assert slugify_creation_title("Åland") != slugify_creation_title("A\u030aland")
    assert slugify_creation_title("Café") != slugify_creation_title("Cafe\u0301")


@pytest.mark.parametrize("title", ["\ud800", "before\udfffafter"])
def test_slug_rejects_isolated_surrogates_without_echo(title: str) -> None:
    with pytest.raises(InvalidPageTitleError) as exc_info:
        slugify_creation_title(title)

    assert "before" not in str(exc_info.value)


def test_page_id_generation_returns_explicit_components() -> None:
    occurrence = parse_occurrence_time("2026-08-11T09:15:00.123999Z")

    generated = generate_page_id(occurrence, "Example Session")
    collided = generate_page_id(occurrence, "Example Session", collision_ordinal=2)

    assert generated == GeneratedPageId(
        value="20260811t091500123z-example-session",
        timestamp_prefix="20260811t091500123z",
        base_slug="example-session",
        collision_ordinal=1,
    )
    assert collided == GeneratedPageId(
        value="20260811t091500123z-example-session-2",
        timestamp_prefix="20260811t091500123z",
        base_slug="example-session",
        collision_ordinal=2,
    )


def test_page_id_generation_accepts_maximum_slug_and_ordinal() -> None:
    occurrence = parse_occurrence_time("2026-08-11T09:15:00Z")

    generated = generate_page_id(
        occurrence,
        "a" * 80,
        collision_ordinal=MAX_COLLISION_ORDINAL,
    )

    assert generated.base_slug == "a" * 48
    assert generated.value == f"20260811t091500000z-{'a' * 48}-9999999999"
    assert len(generated.value) == 79
    assert validate_page_id(generated.value) == generated.value


@pytest.mark.parametrize(
    "ordinal",
    [0, -1, MAX_COLLISION_ORDINAL + 1, True, 2.0],
)
def test_page_id_generation_rejects_invalid_or_exhausted_ordinal(ordinal: object) -> None:
    occurrence = parse_occurrence_time("2026-08-11T09:15:00Z")

    with pytest.raises(InvalidPageIdError):
        generate_page_id(
            occurrence,
            "Synthetic",
            collision_ordinal=ordinal,  # type: ignore[arg-type]
        )


def test_page_id_generation_rejects_inconsistent_occurrence_object() -> None:
    inconsistent = OccurrenceTime(
        utc_microseconds=0,
        canonical_utc="1970-01-01T00:00:00.000001Z",
    )

    with pytest.raises(InvalidOccurrenceTimeError):
        generate_page_id(inconsistent, "Synthetic")


@pytest.mark.parametrize(
    "value",
    [
        "20260811t091500123z-example-session",
        "20260811t091500123z-example-session-2",
        "20260811t091500123z-a-9999999999",
        "20260811t091500123z-a-0002",
        f"20260811t091500123z-{'a' * 48}",
        f"20260811t091500123z-{'a' * 48}-2",
    ],
)
def test_validate_page_id_accepts_supported_opaque_forms(value: str) -> None:
    assert validate_page_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "20260811t091500123z-",
        "20260811T091500123Z-example",
        "20260811t091500123z-Example",
        "20260811t091500123z-example_session",
        "20260811t091500123z-example--session",
        "20260811t091500123z--example",
        "20260811t091500123z-example-",
        "20260230t091500123z-example",
        "20260811t241500123z-example",
        "20260811t096000123z-example",
        "20260811t091560123z-example",
        "20260811t091500123z-é",
        "20260811t091500123z-example/other",
        "20260811t091500123z-example\\other",
        "20260811t091500123z-example%2fother",
        f"20260811t091500123z-{'a' * 49}",
        f"20260811t091500123z-{'a' * 49}-2",
        f"20260811t091500123z-{'a' * 48}-10000000000",
        "x" * 80,
        "x" * 81,
    ],
)
def test_validate_page_id_rejects_malformed_values_without_echo(value: str) -> None:
    with pytest.raises(InvalidPageIdError) as exc_info:
        validate_page_id(value)

    assert value not in str(exc_info.value) or not value


def test_registry_digest_exact_vector_and_library_scoping() -> None:
    identifier = "20260811t091500123z-example-session"
    expected_hex = "27144fe742ce8ef90f6d530836f177e737772065693659171a8dec7f6deb4551"

    digest = page_id_registry_digest(identifier)
    first = page_id_registry_key("library-a", identifier)
    second = page_id_registry_key("library-b", identifier)

    assert digest.hex() == expected_hex
    assert (
        digest == hashlib.sha256(b"patchouli-page-id-v1\x00" + identifier.encode("utf-8")).digest()
    )
    assert first.library_scope == "library-a"
    assert second.library_scope == "library-b"
    assert first.identifier_digest == second.identifier_digest == digest


def test_registry_key_requires_explicit_non_null_library_scope() -> None:
    with pytest.raises(InvalidLibraryScopeError):
        page_id_registry_key(None, "20260811t091500123z-example-session")
