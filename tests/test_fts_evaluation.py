from __future__ import annotations

import json
import os
import platform
import runpy
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_SCRIPT = REPOSITORY_ROOT / "scripts" / "evaluate_fts.py"


def _run_evaluation(output_format: str) -> str:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, str(EVALUATION_SCRIPT), "--format", output_format],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result.stdout


def _candidate(report: dict[str, object], name: str) -> dict[str, object]:
    candidates = cast(list[dict[str, object]], report["candidates"])
    return next(candidate for candidate in candidates if candidate["name"] == name)


def _query(candidate: dict[str, object], query_id: str) -> dict[str, object]:
    queries = cast(list[dict[str, object]], candidate["query_results"])
    return next(query for query in queries if query["query_id"] == query_id)


def _hit_ids(query: dict[str, object]) -> list[str]:
    hits = cast(list[dict[str, object]], query["hits"])
    return [cast(str, hit["document_id"]) for hit in hits]


@pytest.fixture(scope="module")
def evaluation_report() -> dict[str, object]:
    parsed = json.loads(_run_evaluation("json"))
    return cast(dict[str, object], parsed)


def test_report_records_runtime_and_original_fixture_provenance(
    evaluation_report: dict[str, object],
) -> None:
    fixture = cast(dict[str, object], evaluation_report["fixture"])
    benchmark = cast(dict[str, object], evaluation_report["resource_benchmark"])

    assert evaluation_report["schema_version"] == 2
    assert evaluation_report["python_version"] == platform.python_version()
    assert evaluation_report["unicode_database_version"] == unicodedata.unidata_version
    assert evaluation_report["han_range_version"] == "Unicode 15.1"
    assert fixture["provenance"] == (
        "Original synthetic text authored for the PatchouliLib evaluation; "
        "not adapted from an external corpus."
    )
    assert benchmark["document_count"] == 384
    assert benchmark["timing_scope"] == (
        "Indicative wall-clock measurements from one local process; values are not "
        "release thresholds or cross-machine guarantees."
    )


def test_known_han_recall_boundaries(evaluation_report: dict[str, object]) -> None:
    unicode61 = _candidate(evaluation_report, "unicode61")
    trigram = _candidate(evaluation_report, "trigram")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    unicode_recall = cast(dict[str, float], unicode61["recall_by_dimension"])
    trigram_recall = cast(dict[str, float], trigram["recall_by_dimension"])
    application_recall = cast(dict[str, float], application["recall_by_dimension"])

    assert unicode_recall["han_1"] == 0.0
    assert unicode_recall["han_2"] == 0.0
    assert unicode_recall["han_3_plus"] == 0.0
    assert trigram_recall["han_1"] == 0.0
    assert trigram_recall["han_2"] == 0.0
    assert trigram_recall["han_3_plus"] == 1.0
    assert application_recall["han_1"] == 1.0
    assert application_recall["han_2"] == 1.0
    assert application_recall["han_3_plus"] == 1.0
    assert application_recall["mixed_language"] == 1.0


@pytest.mark.parametrize(
    "han_text",
    (
        "\U0002ebf0\U0002ebf1\U0002ebf2",  # Extension I
        "\U00030000\U00030001\U00030002",  # Extension G
        "\U00031350\U00031351\U00031352",  # Extension H
    ),
)
def test_unicode_15_1_extension_runs_emit_one_two_and_three_character_grams(
    han_text: str,
) -> None:
    module = runpy.run_path(str(EVALUATION_SCRIPT), run_name="fts_evaluation_for_test")
    application_tokens = cast(object, module["application_tokens"])
    assert callable(application_tokens)
    tokens = cast(tuple[str, ...], application_tokens(han_text))

    expected = (
        {f"h1{character.encode('utf-8').hex()}" for character in han_text}
        | {f"h2{han_text[offset : offset + 2].encode('utf-8').hex()}" for offset in range(2)}
        | {f"h3{han_text.encode('utf-8').hex()}"}
    )
    assert set(tokens) == expected


def test_section_filtering_precedes_rank_and_limit(
    evaluation_report: dict[str, object],
) -> None:
    candidates = cast(list[dict[str, object]], evaluation_report["candidates"])

    for candidate in candidates:
        isolation = cast(dict[str, object], candidate["cross_section_isolation"])
        expected_order = ["section-authorized-title", "section-authorized-body"]
        assert isolation["authorized_rank_order_before"] == expected_order
        assert isolation["authorized_rank_order_after"] == expected_order
        assert isolation["authorized_rank_scores_before"] == [10.0, 1.0]
        assert isolation["authorized_rank_scores_after"] == [10.0, 1.0]
        assert isolation["authorized_result_count_before"] == 2
        assert isolation["authorized_result_count_after"] == 2
        assert isolation["authorized_limit_one_hit_ids"] == ["section-authorized-title"]
        assert isolation["passed"] is True
        assert cast(list[str], isolation["unauthorized_probe_hit_ids"])[0].startswith(
            "section-unauthorized"
        )


def test_candidates_replace_old_current_content_and_rank_deterministically(
    evaluation_report: dict[str, object],
) -> None:
    candidates = cast(list[dict[str, object]], evaluation_report["candidates"])

    for candidate in candidates:
        current_only = cast(dict[str, bool], candidate["current_only_update"])
        assert current_only == {
            "legacy_found_before_update": True,
            "legacy_absent_after_update": True,
            "current_found_after_update": True,
            "passed": True,
        }
        assert candidate["deterministic_ranking"] is True
        assert candidate["ranking_tie_break"] == {
            "order": ["ranking-a", "ranking-z"],
            "passed": True,
        }


def test_plain_text_compilers_do_not_expose_caller_fts_syntax(
    evaluation_report: dict[str, object],
) -> None:
    unicode61 = _candidate(evaluation_report, "unicode61")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    assert _query(unicode61, "literal-and")["compiled_query"] == '"AND"'
    assert _query(unicode61, "literal-quotes")["compiled_query"] == '"""quoted"""'
    assert _query(unicode61, "literal-wildcard")["compiled_query"] == '"prefix*"'
    assert _query(unicode61, "literal-column-selector")["compiled_query"] == '"title:demo"'
    assert _query(unicode61, "literal-unmatched-quote")["compiled_query"] == '"unmatched""quote"'
    assert _query(unicode61, "literal-or")["compiled_query"] == '"alpha OR beta"'
    assert _query(unicode61, "literal-near-parentheses")["compiled_query"] == '"NEAR(alpha beta)"'
    assert (
        _query(unicode61, "literal-grouped-or-near")["compiled_query"]
        == '"(alpha OR beta) NEAR(gamma delta)"'
    )

    for query_id in (
        "literal-wildcard",
        "literal-unmatched-quote",
        "literal-or",
        "literal-near-parentheses",
        "literal-grouped-or-near",
    ):
        compiled = cast(str, _query(application, query_id)["compiled_query"])
        assert "prefix" not in compiled
        assert "unmatched" not in compiled
        assert "OR" not in compiled
        assert "NEAR" not in compiled
        assert "*" not in compiled
        assert "(" not in compiled
        assert ")" not in compiled
        assert all(part.startswith('"') and part.endswith('"') for part in compiled.split(" AND "))


def test_literal_operator_combinations_do_not_expand_match_grammar(
    evaluation_report: dict[str, object],
) -> None:
    candidates = cast(list[dict[str, object]], evaluation_report["candidates"])

    for candidate in candidates:
        for query_id in (
            "literal-unmatched-quote",
            "literal-or",
            "literal-near-parentheses",
            "literal-grouped-or-near",
        ):
            assert _hit_ids(_query(candidate, query_id)) == ["literal-syntax"]


def test_encoded_punctuation_keeps_literal_wildcard_from_matching_decoy(
    evaluation_report: dict[str, object],
) -> None:
    unicode61 = _candidate(evaluation_report, "unicode61")
    trigram = _candidate(evaluation_report, "trigram")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    assert _query(unicode61, "literal-wildcard")["recall"] == 1.0
    assert _query(unicode61, "literal-wildcard")["precision"] == 0.5
    assert _query(trigram, "literal-wildcard")["precision"] == 1.0
    assert _query(application, "literal-wildcard")["precision"] == 1.0
    assert _hit_ids(_query(application, "literal-wildcard")) == ["literal-syntax"]


def test_report_includes_precision_and_non_normative_resource_measurements(
    evaluation_report: dict[str, object],
) -> None:
    baseline = cast(dict[str, object], evaluation_report["resource_benchmark"])
    candidates = cast(list[dict[str, object]], evaluation_report["candidates"])

    assert cast(int, baseline["baseline_database_bytes"]) > 0
    for candidate in candidates:
        precision_by_dimension = cast(dict[str, float], candidate["precision_by_dimension"])
        metrics = cast(dict[str, object], candidate["resource_metrics"])
        assert precision_by_dimension["literal_fts_syntax"] > 0.0
        assert cast(int, metrics["index_overhead_bytes"]) > 0
        assert cast(float, metrics["index_overhead_bytes_per_document"]) > 0.0
        for metric_name, expected_samples in (
            ("build_latency", 3),
            ("update_latency", 24),
            ("query_latency", 120),
        ):
            latency = cast(dict[str, object], metrics[metric_name])
            assert latency["samples"] == expected_samples
            assert cast(float, latency["median_ms"]) >= 0.0
            assert cast(float, latency["p95_ms"]) >= cast(float, latency["median_ms"])


def test_proposed_candidate_has_bounded_query_time_resource_guardrails(
    evaluation_report: dict[str, object],
) -> None:
    application = _candidate(evaluation_report, "application_cjk_ngrams")
    guardrails = cast(dict[str, object], application["resource_guardrails"])
    max_body = cast(dict[str, object], guardrails["max_body_query"])
    high_entropy = cast(dict[str, object], guardrails["high_entropy_index"])
    broad_match = cast(dict[str, object], guardrails["broad_match_at_limit"])
    query_plan = cast(dict[str, object], guardrails["recall_query_plan"])
    late_intersection = cast(
        dict[str, object],
        guardrails["same_section_late_intersection"],
    )
    scoped_postings = cast(dict[str, object], guardrails["section_scoped_postings"])

    assert guardrails["max_content_bytes"] == 2 * 1024 * 1024
    assert guardrails["max_derived_tokens_per_document"] == 65_536
    assert guardrails["max_query_codepoints"] == 2048
    assert guardrails["max_query_utf8_bytes"] == 4096
    assert guardrails["max_query_tokens"] == 64
    assert guardrails["max_candidates"] == 256
    assert guardrails["max_searchable_pages_per_section"] == 1024
    assert guardrails["query_time_body_tokenization"] is False
    assert max_body["body_bytes"] == 2 * 1024 * 1024
    assert max_body["hit_ids"] == ["max-body-authorized"]
    assert max_body["passed"] is True
    assert high_entropy["body_bytes"] == 2 * 1024 * 1024
    assert high_entropy["failed_closed"] is True
    assert high_entropy["durable_document_rows"] == 0
    assert high_entropy["passed"] is True
    assert broad_match["authorized_candidates"] == 256
    assert broad_match["other_section_candidates"] == 1024
    assert broad_match["passed"] is True
    assert query_plan["uses_temporary_sort"] is False
    assert all(
        "USE TEMP B-TREE" not in detail.upper() for detail in cast(list[str], query_plan["details"])
    )
    opcodes = cast(list[str], query_plan["vm_opcodes"])
    assert query_plan["limit_guard_precedes_next_match"] is True
    assert opcodes.index("DecrJumpZero") < opcodes.index("VNext")
    assert guardrails["candidate_overflow_failed_closed"] is True
    assert late_intersection == {
        "pages_at_limit": 1024,
        "hit_ids_at_limit": [],
        "write_overflow_failed_closed": True,
        "durable_pages_after_failed_write": 1024,
        "query_overflow_failed_closed": True,
        "partial_hit_ids_on_overflow": [],
        "passed": True,
    }
    assert guardrails["query_codepoint_overflow_failed_closed"] is True
    assert guardrails["query_byte_overflow_failed_closed"] is True
    assert guardrails["query_token_overflow_failed_closed"] is True
    assert scoped_postings["passed"] is True
    assert (
        scoped_postings["authorized_compiled_query"]
        != scoped_postings["other_section_compiled_query"]
    )


def test_tokenized_candidate_does_not_claim_native_original_snippets(
    evaluation_report: dict[str, object],
) -> None:
    trigram = _candidate(evaluation_report, "trigram")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    trigram_hits = cast(list[dict[str, object]], _query(trigram, "han-3")["hits"])
    application_hits = cast(list[dict[str, object]], _query(application, "han-3")["hits"])
    assert trigram_hits[0]["native_snippet"]
    assert application["native_snippet_supported"] is False
    assert application_hits[0]["native_snippet"] is None


def test_markdown_report_states_evaluation_boundary() -> None:
    rendered = _run_evaluation("markdown")

    assert "| `unicode61`" in rendered
    assert "| `trigram`" in rendered
    assert "| `application_cjk_ngrams`" in rendered
    assert "Han range table: `Unicode 15.1`" in rendered
    assert "Section isolation" in rendered
    assert "## Resource measurements" in rendered
    assert "does not select a production tokenizer" in rendered
