"""Strict request parsing for the registered current-Page search route."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from patchouli_lib.content.models import MAX_OCCURRENCE_MICROSECONDS, MIN_OCCURRENCE_MICROSECONDS
from patchouli_lib.search.literal_v2 import MAX_QUERY_BYTES, MAX_QUERY_KEYWORDS, find_page_hits
from patchouli_lib.search.query_v2 import (
    MAX_QUERY_BODY_BYTES,
    MAX_QUERY_LIBRARIES,
    MAX_QUERY_TAGS,
    InvalidSearchQueryV2,
    SearchQueryV2,
    SearchQueryV2Wire,
    TagIdentityV2,
    parse_query_v2_json,
)

LIBRARY_A = "a" * 32
LIBRARY_B = "b" * 32
TAG_A = "c" * 32
TAG_B = "d" * 32


def _parse(value: object) -> SearchQueryV2:
    return parse_query_v2_json(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def test_keyword_or_preserves_whole_field_literal_semantics_and_normalizes() -> None:
    query = _parse({"keywords": ["Straße", "STRASSE", "é", "e\u0301", "abc"]})
    assert query.keywords == ("strasse", "é", "abc")
    assert query.limit == 20
    assert query.libraries is None  # Caller scope must be resolved by authorization.
    assert find_page_hits(query.keywords, title="Straße", body="elsewhere")
    assert not find_page_hits(query.keywords, title="ab", body="c")
    assert "strasse" not in repr(query)


def test_tag_identity_is_composite_and_order_preserving() -> None:
    query = _parse(
        {
            "tags_any": [
                {"library_id": LIBRARY_A, "tag_id": TAG_A},
                {"library_id": LIBRARY_A, "tag_id": TAG_A},
                {"library_id": LIBRARY_B, "tag_id": TAG_A},
                {"library_id": LIBRARY_A, "tag_id": TAG_B},
            ],
            "libraries": [LIBRARY_B, LIBRARY_A, LIBRARY_B],
        }
    )
    assert query.tags_any == (
        TagIdentityV2(library_id=LIBRARY_A, tag_id=TAG_A),
        TagIdentityV2(library_id=LIBRARY_B, tag_id=TAG_A),
        TagIdentityV2(library_id=LIBRARY_A, tag_id=TAG_B),
    )
    assert query.libraries == (LIBRARY_B, LIBRARY_A)
    assert _parse({"tags_any": [{"library_id": LIBRARY_A, "tag_id": TAG_A}]}).keywords == ()


@pytest.mark.parametrize("tag", [TAG_A.upper(), "a", "g" * 32, 1, None])
def test_tag_ids_use_opaque_id_syntax_and_no_bare_tag_identity(tag: object) -> None:
    with pytest.raises(InvalidSearchQueryV2, match="Invalid search-v2 query"):
        _parse({"tags_any": [{"library_id": LIBRARY_A, "tag_id": tag}]})
    with pytest.raises(InvalidSearchQueryV2):
        _parse({"tags_any": [TAG_A]})


def test_utc_microsecond_half_open_window_and_filter_only_queries() -> None:
    lower = MIN_OCCURRENCE_MICROSECONDS
    upper = MAX_OCCURRENCE_MICROSECONDS + 1
    query = _parse({"occurred_from_us": lower, "occurred_before_us": upper})
    assert query.occurred_before_us == upper
    assert _parse({"occurred_before_us": 0}).occurred_from_us is None
    assert _parse({"occurred_from_us": 0}).occurred_before_us is None
    assert _parse({"occurred_from_us": 123, "occurred_before_us": 124}).occurred_from_us == 123


@pytest.mark.parametrize(
    "value",
    [
        {"occurred_from_us": 12, "occurred_before_us": 12},
        {"occurred_from_us": 13, "occurred_before_us": 12},
        {"occurred_from_us": MIN_OCCURRENCE_MICROSECONDS - 1},
        {"occurred_before_us": MAX_OCCURRENCE_MICROSECONDS + 2},
        {"occurred_from_us": "12"},
        {"occurred_before_us": 1.0},
        {"occurred_from_us": True},
    ],
)
def test_invalid_time_bounds_fail(value: object) -> None:
    with pytest.raises(InvalidSearchQueryV2):
        _parse(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"keywords": [], "tags_any": [], "libraries": [LIBRARY_A]},
        {"keywords": [], "tags_any": [], "occurred_from_us": None},
        {"keywords": None},
        {"keywords": "word"},
        {"keywords": [1]},
        {"keywords": [""]},
        {"keywords": ["x"], "limit": 0},
        {"keywords": ["x"], "limit": 101},
        {"keywords": ["x"], "limit": True},
        {"keywords": ["x"], "limit": "2"},
        {"keywords": ["x"], "unknown": "value"},
        {"keywords": ["x"], "libraries": ["A" * 32]},
        {"keywords": ["x"], "libraries": []},
        {"keywords": ["x"], "tags_any": [{"library_id": LIBRARY_A, "tag_id": TAG_A, "x": 1}]},
    ],
)
def test_strict_schema_and_nonempty_condition(value: object) -> None:
    with pytest.raises(InvalidSearchQueryV2):
        _parse(value)


def test_raw_and_collection_budgets_apply_before_deduplication() -> None:
    for body in (
        {"keywords": ["x"] * (MAX_QUERY_KEYWORDS + 1)},
        {"keywords": ["x" * (MAX_QUERY_BYTES + 1)]},
        {
            "keywords": ["x"],
            "tags_any": [{"library_id": LIBRARY_A, "tag_id": TAG_A}] * (MAX_QUERY_TAGS + 1),
        },
        {"keywords": ["x"], "libraries": [LIBRARY_A] * (MAX_QUERY_LIBRARIES + 1)},
    ):
        with pytest.raises(InvalidSearchQueryV2):
            _parse(body)
    assert len(_parse({"keywords": ["x"] * MAX_QUERY_KEYWORDS}).keywords) == 1
    assert _parse({"keywords": ["x"], "limit": 100}).limit == 100
    with pytest.raises(InvalidSearchQueryV2):
        parse_query_v2_json(b" " * (MAX_QUERY_BODY_BYTES + 1))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"keywords":["x"],"keywords":["y"]}',
        b'{"tags_any":[{"library_id":"a","library_id":"b"}]}',
        b'{"keywords":[NaN]}',
        b'{"keywords":["x"]}\xff',
        b'{"keywords":["x"],',
    ],
)
def test_invalid_json_or_duplicate_keys_fail_without_echo(raw: bytes) -> None:
    with pytest.raises(InvalidSearchQueryV2) as error:
        parse_query_v2_json(raw)
    assert "keywords" not in str(error.value)


def test_unrepresentable_unicode_and_error_text_are_not_echoed() -> None:
    secret = "private-query-\ud800"
    with pytest.raises(InvalidSearchQueryV2) as error:
        parse_query_v2_json(json.dumps({"keywords": [secret]}).encode("ascii"))
    assert "private-query" not in str(error.value)
    with pytest.raises(ValidationError) as direct_error:
        SearchQueryV2Wire.model_validate({"keywords": [secret]}, strict=True)
    assert "private-query" not in str(direct_error.value)
    wire = SearchQueryV2Wire.model_validate({"keywords": ["private-query"]}, strict=True)
    assert "private-query" not in repr(wire)


def test_raw_parser_requires_exact_bytes() -> None:
    with pytest.raises(InvalidSearchQueryV2):
        parse_query_v2_json("{}")  # type: ignore[arg-type]
