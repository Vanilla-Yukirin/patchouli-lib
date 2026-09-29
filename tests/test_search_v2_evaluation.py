"""Small, deterministic checks for the offline-only search-v2 experiment."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = REPOSITORY_ROOT / "scripts" / "evaluate_search_v2.py"
SHORT_LITERAL_QUERIES = (
    "english_substring",
    "emoji",
    "punctuation",
    "symbol_boundary",
    "mixed_han_symbols",
)
_MODULE = runpy.run_path(str(EVALUATOR), run_name="search_v2_evaluation_for_test")
evaluate = cast(Callable[[int, int], dict[str, Any]], _MODULE["evaluate"])
generate_corpus = cast(Callable[[int, int], tuple[Any, ...]], _MODULE["generate_corpus"])
evaluation_queries = cast(Callable[[int], tuple[Any, ...]], _MODULE["evaluation_queries"])
literal_oracle = cast(Callable[[tuple[Any, ...], Any], frozenset[str]], _MODULE["literal_oracle"])


def _query(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in report["queries"] if item["name"] == name)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    # More than the obsolete 256-candidate Alpha cap, yet cheap for CI.
    return evaluate(330, 512)


def test_corpus_is_deterministic_diverse_and_keeps_history_outside_current_text() -> None:
    first = generate_corpus(96, 1_024)
    assert first == generate_corpus(96, 1_024)
    assert len(first) == 96
    assert all(len(doc.body.encode("utf-8")) == 1_024 for doc in first)
    assert {doc.library_id for doc in first} == {"library-0", "library-1", "library-2"}
    assert len({doc.section_id for doc in first}) == 12
    assert len({doc.book_id for doc in first}) == 24
    terms = {token for doc in first for token in doc.body.split() if token.startswith("topic")}
    assert len(terms) > 500
    assert any(doc.old_body is not None for doc in first)
    assert any(doc.deleted for doc in first)
    assert all("旧版独有标记" not in doc.body for doc in first)
    assert first[4].text_files == (("notes.txt", "多文件文本独有信号"),)
    assert first[4].binary_files[0][0] == "binary-needle.bin"
    assert first[4].binary_files[0][1].decode("utf-8") == "二进制隐匿密文"
    with pytest.raises(ValueError):
        generate_corpus(20_000, 10 * 1_024)
    with pytest.raises(ValueError):
        generate_corpus(8_000, 10 * 1_024)


def test_independent_oracle_uses_literal_page_membership_not_candidate_rank() -> None:
    documents = generate_corpus(96, 1_024)
    authorized = next(query for query in evaluation_queries(96) if query.name == "sentinel")
    assert literal_oracle(documents, authorized) == frozenset({"page-00000", "page-00001"})
    all_libraries = replace(authorized, libraries=("library-0", "library-1", "library-2"))
    assert literal_oracle(documents, all_libraries) == frozenset(
        {"page-00000", "page-00001", "page-00002"}
    )
    old_only = next(query for query in evaluation_queries(96) if query.name == "old_revision_only")
    assert literal_oracle(documents, old_only) == frozenset()
    disjoint = next(
        query for query in evaluation_queries(96) if query.name == "disjoint_keywords_or"
    )
    assert literal_oracle(documents, disjoint) == frozenset({"page-00000", "page-00004"})
    text_file = next(query for query in evaluation_queries(96) if query.name == "text_file")
    assert literal_oracle(documents, text_file) == frozenset({"page-00004"})
    binary_content = next(
        query for query in evaluation_queries(96) if query.name == "binary_bytes_not_text"
    )
    assert literal_oracle(documents, binary_content) == frozenset()
    tags = next(query for query in evaluation_queries(96) if query.name == "tags_any_only")
    assert literal_oracle(documents, tags) == literal_oracle(
        documents, replace(tags, tags_any=("tag-1",))
    ) | literal_oracle(documents, replace(tags, tags_any=("tag-2",)))
    before = next(query for query in evaluation_queries(96) if query.name == "time_before_only")
    assert "page-00060" not in literal_oracle(documents, before)
    assert "page-00058" in literal_oracle(documents, before)
    for name in SHORT_LITERAL_QUERIES:
        query = next(item for item in evaluation_queries(96) if item.name == name)
        assert literal_oracle(documents, query) == frozenset({"page-00003"})


def test_caller_text_is_encoded_before_fts_compilation() -> None:
    compile_keyword = cast(Callable[[tuple[str, ...]], str], _MODULE["_keyword_match"])
    compiled = compile_keyword(('NEAR("secret") OR title:admin*',))
    assert "NEAR" not in compiled
    assert "title:" not in compiled
    assert "*" not in compiled
    assert compiled.startswith('"')
    with pytest.raises(ValueError):
        compile_keyword(("",))


def test_all_queries_have_complete_membership_and_manual_sentinels(report: dict[str, Any]) -> None:
    assert report["mode"] == "offline_search_v2_candidate"
    assert report["evaluation_passed"] is True
    assert report["status"].startswith("Proposed; evaluation only")
    assert report["provenance"].startswith("CC0-1.0 original deterministic synthetic text")
    assert report["page_count"] == 330
    assert report["body_bytes_per_page"] == 512
    assert report["pages_with_extra_text_files"] > 10
    assert report["pages_with_binary_files"] > 10
    assert report["binary_payload_rows"] == report["pages_with_binary_files"]
    assert report["binary_payload_bytes"] > 0
    assert report["indexed_current_undeleted_pages"] == (
        report["page_count"] - report["deleted_count"]
    )
    assert report["occurred_at_unit"] == "synthetic microseconds since UTC epoch"
    assert all(query["complete_membership"] is True for query in report["queries"])
    assert all(query["new_connection_agrees"] is True for query in report["queries"])
    assert all(query["top_k_contains_no_duplicates"] is True for query in report["queries"])
    assert all(
        query["hand_labelled_top_k"] is True
        for query in report["queries"]
        if query["hand_labelled_top_k"] is not None
    )
    assert _query(report, "broad_all")["candidate_count"] > 256
    assert _query(report, "wide_keywords_or")["candidate_count"] > 256
    assert _query(report, "disjoint_keywords_or")["top_k_page_ids"] == [
        "page-00000",
        "page-00004",
    ]
    assert _query(report, "text_file")["top_k_page_ids"] == ["page-00004"]
    assert _query(report, "file_name")["top_k_page_ids"] == ["page-00004"]
    assert _query(report, "binary_bytes_not_text")["matched_count"] == 0
    assert (
        _query(report, "broad_all")["matched_count"]
        == report["page_count"] - report["deleted_count"]
    )
    assert _query(report, "tags_and_time_only")["matched_count"] > 0
    assert _query(report, "tags_any_only")["matched_count"] > 0
    assert _query(report, "time_before_only")["matched_count"] > 0
    assert _query(report, "old_revision_only")["matched_count"] == 0
    assert _query(report, "deleted_only")["matched_count"] == 0
    assert _query(report, "deleted_only")["candidate_count"] == 0
    assert _query(report, "no_match")["matched_count"] == 0
    for name in SHORT_LITERAL_QUERIES:
        query = _query(report, name)
        assert query["oracle_count"] == 1
        assert query["candidate_count"] >= 1
        assert query["top_k_page_ids"] == ["page-00003"]
    assert all(query["query_plan"] for query in report["queries"])


def test_auth_update_rebuild_and_measurement_caveats(report: dict[str, Any]) -> None:
    probe = report["unauthorized_high_relevance_probe"]
    assert probe["authorized_count"] > probe["top_k_size"] == 20
    assert probe["higher_ranked_unauthorized_count"] == 25
    assert probe["unfiltered_top_k_all_unauthorized"] is True
    assert probe["complete_set_stable"] is True
    assert probe["top_k_stable"] is True
    assert probe["candidate_count_stable"] is True
    assert probe["passed"] is True
    assert report["current_revision_update_replaces_old_match"] is True
    assert report["rebuild_matches_updated_projection"] is True
    assert report["build_ms"] > 0
    assert report["update_ms"] > 0
    assert report["rebuild_ms"] > 0
    assert report["build_measurement_scope"].startswith("Fetch authority rows")
    assert report["rebuild_measurement_scope"].startswith("Delete FTS rows")
    environment = report["measurement_environment"]
    assert environment["processes"] == 1
    assert environment["database"].startswith("disposable local SQLite")
    assert report["sizes_after_build"]["database_bytes"] > 0
    assert report["estimated_fts_growth_bytes"] > 0
    assert report["short_literal_terms_version"].startswith("non-Han-codepoint-1-2-3")
    assert "not an exact index-only size" in report["index_size_method"]
    for kind, samples in (("warm_connection_latency", 5), ("new_connection_latency", 1)):
        for percentile in ("p50_ms", "p95_ms", "p99_ms", "worst_ms"):
            assert report[kind][percentile] >= 0
        assert report[kind]["sample_count"] == len(report["queries"]) * samples
    assert "new connection is not an OS-cold measurement" in report["limitations"]
    assert "binary payload bytes are excluded" in report["limitations"]
    assert "not established" in report["limitations"]


def test_cli_json_contains_no_production_success_claim() -> None:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(EVALUATOR),
            "--pages",
            "48",
            "--body-bytes",
            "512",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    parsed = json.loads(result.stdout)
    assert parsed["page_count"] == 48
    assert parsed["evaluation_passed"] is True
    assert parsed["unauthorized_high_relevance_probe"]["authorized_count"] > 20
    assert parsed["status"] == (
        "Proposed; evaluation only; no production API, migration, or deployment"
    )
