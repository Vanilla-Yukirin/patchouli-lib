"""Bounded operator parameters and honest descriptive HTTP latency statistics."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bench_search_v2_matrix import (  # noqa: E402
    latency_summary,
    run_http_matrix,
    validate_http_parameters,
)


@pytest.mark.parametrize("values", [(1, 1, 1), (100, 4, 5)])
def test_http_parameter_bounds_accept_reviewed_edges(values: tuple[int, int, int]) -> None:
    validate_http_parameters(*values)


@pytest.mark.parametrize(
    "values", [(0, 1, 1), (101, 1, 1), (1, 0, 1), (1, 5, 1), (1, 1, 0), (1, 1, 6)]
)
def test_http_parameter_bounds_reject_unbounded_work(values: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        validate_http_parameters(*values)


def test_nearest_rank_statistics_use_all_samples_and_real_batch_wall_time() -> None:
    samples = [float(value) for value in range(100, 0, -1)]
    result = latency_summary(samples, 2.0)
    assert result == {
        "sample_count": 100,
        "p50_ms": 50.0,
        "p95_ms": 95.0,
        "p99_ms": 99.0,
        "max_ms": 100.0,
        "batch_wall_seconds": 2.0,
        "throughput_rps": 50.0,
    }
    assert samples[0] == 100.0  # The caller's measurements are not sorted in place.


def test_one_sample_is_reported_as_one_not_a_fabricated_tail_distribution() -> None:
    result = latency_summary([3.0], 0.5)
    assert result["sample_count"] == 1
    assert result["p50_ms"] == result["p95_ms"] == result["p99_ms"] == result["max_ms"] == 3.0
    assert result["throughput_rps"] == 2.0


@pytest.mark.parametrize(
    ("samples", "wall_seconds"),
    [
        ([], 1.0),
        ([-1.0], 1.0),
        ([float("nan")], 1.0),
        ([float("inf")], 1.0),
        ([1.0], 0.0),
        ([1.0], -1.0),
        ([1.0], float("nan")),
        ([1.0], float("inf")),
    ],
)
def test_statistics_reject_missing_or_invalid_evidence(
    samples: list[float], wall_seconds: float
) -> None:
    with pytest.raises(ValueError):
        latency_summary(samples, wall_seconds)


def test_small_real_http_matrix_checks_or_multifile_and_signed_pagination() -> None:
    report = run_http_matrix(156, 512, 1, 1, 1)
    assert report["mode"] == "synthetic_real_http_search_v2_matrix"
    queries = {entry["name"]: entry for entry in report["queries"]}
    for name in (
        "wide_keyword_or",
        "multi_file_duplicate",
        "additional_text_file",
        "binary_file_name",
    ):
        assert queries[name]["oracle_match_count"] == 104
        assert queries[name]["returned"] == 20
        assert queries[name]["every_response_matches_independent_oracle"] is True
        assert queries[name]["warm_serial"]["sample_count"] == 1
    for name in ("binary_body_not_indexed", "no_cross_field_join"):
        assert queries[name]["oracle_match_count"] == queries[name]["returned"] == 0
    assert len(report["pagination"]) == 3
    for chain in report["pagination"]:
        assert chain["limit"] == 100
        assert chain["page_count"] == 2
        assert chain["returned"] == 104
        assert chain["complete_order_matches_independent_oracle"] is True
        assert chain["hidden_library_excluded"] is True
        assert chain["terminal_next_cursor_is_null"] is True
    assert report["visible_update_invalidates_cursor"] is True  # Actual HTTP 400 invalid_cursor.
    assert report["scope_rejections_checked"] == 2
    assert "NOT OS-cold" in report["cold_cache_note"]
