from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from patchouli_lib.api.contracts import (
    API_V1_PREFIX,
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    RFC3339UTC,
    Citation,
    OpaqueIdentifier,
    PaginatedResponse,
    PaginationParameters,
    WireModel,
    build_api_v1_path,
    format_rfc3339_utc,
    parse_rfc3339_utc,
)


class TimestampEnvelope(WireModel):
    occurred_at: RFC3339UTC


class SyntheticItem(WireModel):
    identifier: OpaqueIdentifier


def test_api_namespace_and_pagination_limits_are_stable() -> None:
    assert API_V1_PREFIX == "/api/v1"
    assert DEFAULT_PAGE_LIMIT == 20
    assert MAX_PAGE_LIMIT == 100


def test_rfc3339_timestamp_normalizes_to_canonical_utc() -> None:
    envelope = TimestampEnvelope.model_validate({"occurred_at": "2026-08-12T09:15:30.12+08:00"})

    assert envelope.occurred_at == datetime(
        2026,
        8,
        12,
        1,
        15,
        30,
        120_000,
        tzinfo=UTC,
    )
    assert envelope.model_dump(mode="json") == {"occurred_at": "2026-08-12T01:15:30.120000Z"}


def test_rfc3339_helpers_preserve_early_year_padding() -> None:
    value = datetime(1, 2, 3, 4, 5, 6, 7, tzinfo=UTC)

    assert format_rfc3339_utc(value) == "0001-02-03T04:05:06.000007Z"
    assert parse_rfc3339_utc("0001-02-03T04:05:06Z") == value.replace(microsecond=0)


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-12T01:02:03",
        "2026-08-12 01:02:03Z",
        "2026-08-12T01:02:03.1234567Z",
        "2026-08-12T01:02:03-00:00",
        "2026-08-12T01:02:60Z",
        datetime(2026, 8, 12, 1, 2, 3),
        1_786_499_323,
    ],
)
def test_rfc3339_timestamp_rejects_ambiguous_or_non_wire_values(value: object) -> None:
    with pytest.raises((ValueError, ValidationError)):
        TimestampEnvelope.model_validate({"occurred_at": value})


def test_rfc3339_helper_accepts_aware_datetime_and_normalizes_offset() -> None:
    source = datetime(2026, 8, 12, 9, 30, tzinfo=timezone(timedelta(hours=8)))

    assert parse_rfc3339_utc(source) == datetime(2026, 8, 12, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_rfc3339_utc_normalization_overflow_is_stable_validation_failure(value: str) -> None:
    with pytest.raises(ValidationError) as raised:
        TimestampEnvelope.model_validate({"occurred_at": value})

    assert "The timestamp cannot be represented in UTC." in str(raised.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0001-01-01T01:00:00+01:00", datetime(1, 1, 1, tzinfo=UTC)),
        (
            "9999-12-31T22:59:59-01:00",
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        ),
    ],
)
def test_rfc3339_utc_offset_boundaries_that_fit_are_accepted(
    value: str,
    expected: datetime,
) -> None:
    assert parse_rfc3339_utc(value) == expected


def test_citation_keeps_identifiers_opaque_and_href_relative() -> None:
    href = build_api_v1_path(
        "sections",
        "section-placeholder",
        "pages",
        "page-placeholder",
        "revisions",
        "3",
    )
    citation = Citation.model_validate(
        {
            "section_id": "section:opaque_1",
            "page_id": "Page-Value-Is-Not-Parsed",
            "revision_id": "rev.synthetic",
            "revision_number": 3,
            "href": href,
        }
    )

    assert citation.page_id == "Page-Value-Is-Not-Parsed"
    assert citation.revision_number == 3


@pytest.mark.parametrize("segment", ["", ".", "..", "raw space", "percent%20", "slash/"])
def test_api_path_builder_rejects_noncanonical_segments(segment: str) -> None:
    with pytest.raises(ValueError):
        build_api_v1_path("pages", segment)


@pytest.mark.parametrize(
    "href",
    [
        "https://service.example/api/v1/pages/example",
        "//service.example/api/v1/pages/example",
        "/api/v1/pages/example?private=query",
        "/api/v1/pages/example#fragment",
        "/api/v1/pages/%2e%2e/example",
        "/api/v1/pages/./example",
        "/api/v1/pages/../example",
        "/api/v1/pages//example",
        "/api/v1/pages/example/",
        "/api/v1",
        "/other/v1/pages/example",
        "/api/v1\\pages\\example",
        "\n/api/v1/pages/example",
        "/api/v1/pages/example\x7f",
        "/api/v1/pages/raw space",
    ],
)
def test_citation_rejects_non_relative_or_unversioned_href(href: str) -> None:
    with pytest.raises(ValidationError):
        Citation.model_validate(
            {
                "section_id": "section_placeholder",
                "page_id": "page_placeholder",
                "revision_id": "revision_placeholder",
                "revision_number": 1,
                "href": href,
            }
        )


def test_wire_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PaginationParameters.model_validate({"unexpected": "value"})


def test_pagination_defaults_and_bounds() -> None:
    assert PaginationParameters().model_dump() == {"limit": 20, "cursor": None}
    assert PaginationParameters(limit=100, cursor="opaque.cursor").limit == 100

    for invalid_limit in (0, 101):
        with pytest.raises(ValidationError):
            PaginationParameters(limit=invalid_limit)


def test_paginated_response_serializes_basic_collection_contract() -> None:
    response = PaginatedResponse[SyntheticItem](
        items=[SyntheticItem(identifier="opaque-item")],
        next_cursor="opaque-cursor",
    )

    assert response.model_dump(mode="json") == {
        "items": [{"identifier": "opaque-item"}],
        "next_cursor": "opaque-cursor",
    }


def test_paginated_response_enforces_item_ceiling() -> None:
    items = [SyntheticItem(identifier=f"opaque-{index}") for index in range(100)]
    assert len(PaginatedResponse[SyntheticItem](items=items).items) == 100

    with pytest.raises(ValidationError):
        PaginatedResponse[SyntheticItem](
            items=[*items, SyntheticItem(identifier="opaque-overflow")]
        )
