"""Small synthetic checks for the real search-v2 matrix operator script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bench_search_v2_matrix import (  # noqa: E402
    DEFAULT_BYTES,
    DEFAULT_PAGES,
    SCALE_BYTES,
    SCALE_PAGES,
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
    assert all(entry["top20_matches_independent_oracle"] for entry in report["queries"])
    assert report["update_ms"] >= 0
    assert "operating-system file cache may be warm" in report["new_connection_note"]
