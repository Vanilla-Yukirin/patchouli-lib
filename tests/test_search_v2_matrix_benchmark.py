"""Small synthetic checks for the real search-v2 matrix operator script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bench_search_v2_matrix import (  # noqa: E402
    BINARY_BODY,
    DEFAULT_BYTES,
    DEFAULT_PAGES,
    OR_WORDS,
    SCALE_BYTES,
    SCALE_PAGES,
    SPLIT_LEFT,
    SPLIT_RIGHT,
    SyntheticPage,
    _check_pagination,
    _files,
    _oracle,
    _parsed,
    resolve_dimensions,
    run_matrix,
)


def test_explicit_scale_and_default_are_bounded() -> None:
    assert resolve_dimensions(scale=False, pages=None, bytes_per_page=None) == (
        DEFAULT_PAGES,
        DEFAULT_BYTES,
    )
    assert resolve_dimensions(scale=True, pages=None, bytes_per_page=None) == (
        SCALE_PAGES,
        SCALE_BYTES,
    )
    assert resolve_dimensions(scale=False, pages=SCALE_PAGES, bytes_per_page=None) == (
        SCALE_PAGES,
        SCALE_BYTES,
    )
    with pytest.raises(ValueError):
        resolve_dimensions(scale=True, pages=36, bytes_per_page=None)
    with pytest.raises(ValueError):
        resolve_dimensions(scale=False, pages=10_000, bytes_per_page=10_240)


def test_small_matrix_uses_real_search_and_independent_oracle() -> None:
    report = run_matrix(36, 512, 1)
    assert report["mode"] == "synthetic_real_search_v2_matrix"
    assert report["pages"] == 36
    assert report["library_count"] == 3
    assert report["readable_library_count"] == 2
    queries = {entry["name"]: entry for entry in report["queries"]}
    assert queries["common_default_scope"]["oracle_match_count"] == 24
    assert queries["common_default_scope"]["returned"] == 20
    assert queries["rare"]["oracle_match_count"] == 2
    assert queries["rare"]["returned"] == 2
    assert queries["no_hit"]["oracle_match_count"] == 0
    assert queries["no_hit"]["returned"] == 0
    for name in (
        "wide_keyword_or",
        "multi_file_duplicate",
        "additional_text_file",
        "binary_file_name",
    ):
        assert queries[name]["oracle_match_count"] == 24
        assert queries[name]["returned"] == 20
    for name in ("binary_body_not_indexed", "no_cross_field_join"):
        assert queries[name]["oracle_match_count"] == queries[name]["returned"] == 0
    assert all(entry["top20_matches_independent_oracle"] for entry in report["queries"])
    assert report["update_ms"] >= 0
    assert "operating-system file cache may be warm" in report["new_connection_note"]
    assert report["visible_update_invalidates_cursor"] is True


def test_complete_signed_cursor_chains_extend_beyond_top100_without_hidden_pages() -> None:
    report = run_matrix(156, 512, 1)
    assert len(report["pagination"]) == 3
    for chain in report["pagination"]:
        assert chain["limit"] == 100
        assert chain["page_count"] == 2
        assert chain["returned"] == 104  # 52 each in two readable Libraries; 52 hidden excluded.
        assert chain["complete_order_matches_independent_oracle"] is True
        assert chain["hidden_library_excluded"] is True
        assert chain["terminal_next_cursor_is_null"] is True


def test_fixture_budget_or_terms_and_literal_oracle_are_independent() -> None:
    records = []
    for number in range(16):
        files = _files(number, 512)
        assert sum(len(content) for _name, content in files) == 512
        assert len(files) == 3
        body = files[0][1].decode()
        assert [word for word in OR_WORDS if word in body] == [OR_WORDS[number]]
        records.append(
            SyntheticPage(
                number, "1" * 32, "2" * 32, str(number), "fixture", body, number, (), files
            )
        )
    pages = tuple(records)
    readable = frozenset({"1" * 32})
    expected = tuple(("1" * 32, str(number)) for number in reversed(range(16)))
    assert _oracle(pages, readable, _parsed({"keywords": list(OR_WORDS)})) == expected
    assert _oracle(pages, readable, _parsed({"keywords": [BINARY_BODY]})) == ()
    assert _oracle(pages, readable, _parsed({"keywords": [SPLIT_LEFT + SPLIT_RIGHT]})) == ()


@pytest.mark.parametrize(
    "failure", ["early_null", "repeat_cursor", "duplicate_page", "extra_cursor"]
)
def test_full_chain_checker_rejects_incomplete_duplicate_or_nonterminal_results(
    failure: str,
) -> None:
    expected = (("library", "one"), ("library", "two"), ("library", "three"))
    calls = 0

    def fetch(_payload: dict[str, object]) -> tuple[tuple[tuple[str, str], ...], str | None]:
        nonlocal calls
        index = calls
        calls += 1
        cursor = None if index == 2 else f"cursor-{index}"
        if failure == "early_null":
            cursor = None
        elif failure == "repeat_cursor":
            cursor = "same-cursor"
        elif failure == "duplicate_page":
            index = 0
        elif failure == "extra_cursor":
            cursor = f"cursor-{index}"
        return (expected[index],), cursor

    with pytest.raises(RuntimeError):
        _check_pagination(fetch, {}, expected, limit=1)
