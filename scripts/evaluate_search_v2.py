"""Offline search-v2 experiment; never opens or migrates an application database.

This evaluates one disposable FTS5 candidate, not an accepted search contract.
The independent literal-scan oracle checks complete Page membership; a few
hand-labelled sentinels check ordering without sharing the candidate ranker.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from patchouli_lib.search.ngram import (
    HAN_RANGE_VERSION,
    NORMALIZATION_VERSION,
    extract_term_frequencies,
)

DEFAULT_PAGES = 96
DEFAULT_BODY_BYTES = 1_024
SCALE_PAGES = 5_000
SCALE_BODY_BYTES = 10 * 1_024
BASE_TIME = 1_700_000_000_000_000
TOP_K = 20
MAX_CORPUS_BODY_BYTES = 64 * 1_024 * 1_024
SHORT_LITERAL_TERMS_VERSION = "non-Han-codepoint-1-2-3-NFC-casefold-NFC-v1"
_HAN_ALPHABET = "知识库文档版本检索时间来源技术新闻视频摘要索引安全权限项目系统研究实验数据分析报告"
_HAN_RANGES = (
    (0x3007, 0x3007),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0x2CEB0, 0x2EBEF),
    (0x2EBF0, 0x2EE5F),
    (0x2F800, 0x2FA1F),
    (0x30000, 0x3134F),
    (0x31350, 0x323AF),
)


@dataclass(frozen=True, slots=True)
class Document:
    number: int
    page_id: str
    library_id: str
    section_id: str
    book_id: str
    revision_id: str
    title: str
    body: str
    tags: tuple[str, ...]
    occurred_at: int
    old_body: str | None
    deleted: bool


@dataclass(frozen=True, slots=True)
class Query:
    name: str
    libraries: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    tags_any: tuple[str, ...] = ()
    occurred_from: int | None = None
    occurred_before: int | None = None
    expected_first: tuple[str, ...] = ()


class DatabaseSizes(TypedDict):
    database_bytes: int
    fts_pages_bytes: int | None


def _han_word(number: int) -> str:
    radix = len(_HAN_ALPHABET)
    return "".join(_HAN_ALPHABET[(number // radix**position) % radix] for position in (2, 1, 0))


def generate_corpus(page_count: int, body_bytes: int) -> tuple[Document, ...]:
    """Generate exact-size, varied, original synthetic text with known sentinels."""

    if (
        not 36 <= page_count <= 10_000
        or not 512 <= body_bytes <= 32 * 1_024
        or page_count * body_bytes > MAX_CORPUS_BODY_BYTES
    ):
        raise ValueError("Benchmark dimensions exceed the isolated evaluator's resource budget.")
    documents: list[Document] = []
    for number in range(page_count):
        rng = random.Random(20_260_929 + number)
        prefix = "共同主题 "
        if number == 1:
            prefix += "独家信号 "
        elif number == 2:
            prefix += "独家信号 " * 24
        elif number == 0:
            prefix += "stararchive "
        elif number == 3:
            prefix += "literal🙂!? 技术-lit🙂? "
        if number >= 3 and number % 19 == 0:
            prefix += "回收站独有标记 "
        parts = [prefix]
        used = len(prefix.encode("utf-8"))
        while used < body_bytes:
            token = f"{_han_word(rng.randrange(12_000))} topic{rng.randrange(12_000):05d} "
            token_bytes = len(token.encode("utf-8"))
            if used + token_bytes > body_bytes:
                break
            parts.append(token)
            used += token_bytes
        parts.append(" " * (body_bytes - used))
        body = "".join(parts)
        assert len(body.encode("utf-8")) == body_bytes
        title = "独家信号资料" if number == 0 else f"合成资料 {number:05d}"
        if number == 2:
            title = "独家信号 独家信号"
        documents.append(
            Document(
                number=number + 1,
                page_id=f"page-{number:05d}",
                library_id=f"library-{number % 3}",
                section_id=f"section-{number % 3}-{(number // 3) % 4}",
                book_id=f"book-{number % 3}-{(number // 3) % 4}-{(number // 12) % 2}",
                revision_id=f"revision-{number:05d}-current",
                title=title,
                body=body,
                tags=(f"tag-{number % 7}",) + (("shared",) if number % 5 == 0 else ()),
                occurred_at=BASE_TIME + number * 1_000_000,
                old_body=f"旧版独有标记 page-{number:05d}" if number % 11 == 0 else None,
                deleted=number >= 3 and number % 19 == 0,
            )
        )
    return tuple(documents)


def evaluation_queries(page_count: int) -> tuple[Query, ...]:
    authorized = ("library-0", "library-1")
    sentinel = ("page-00000", "page-00001")
    return (
        Query("han_1", authorized, ("独",), expected_first=sentinel),
        Query("han_2", authorized, ("独家",), expected_first=sentinel),
        Query("han_3", authorized, ("独家信",), expected_first=sentinel),
        Query("sentinel", authorized, ("独家信号",), expected_first=sentinel),
        Query("english", authorized, ("stararchive",), expected_first=("page-00000",)),
        Query("english_substring", authorized, ("lit",), expected_first=("page-00003",)),
        Query("emoji", authorized, ("🙂",), expected_first=("page-00003",)),
        Query("punctuation", authorized, ("!?",), expected_first=("page-00003",)),
        Query("symbol_boundary", authorized, ("ral🙂!",), expected_first=("page-00003",)),
        Query("mixed_han_symbols", authorized, ("技术-lit🙂?",), expected_first=("page-00003",)),
        Query("keywords_or", authorized, ("独家信号", "共同主题"), expected_first=sentinel),
        Query("broad_all", ("library-0", "library-1", "library-2"), ("共同主题",)),
        Query(
            "tags_and_time_only",
            authorized,
            tags_any=("tag-1", "tag-2"),
            occurred_from=BASE_TIME + 60 * 1_000_000,
            occurred_before=BASE_TIME + min(page_count, 240) * 1_000_000,
        ),
        Query("time_only", ("library-0",), occurred_from=BASE_TIME + 10 * 1_000_000),
        Query("old_revision_only", authorized, ("旧版独有标记",)),
        Query("deleted_only", authorized, ("回收站独有标记",)),
        Query("no_match", authorized, ("绝不会出现的镜面词",)),
    )


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _is_han(character: str) -> bool:
    point = ord(character)
    return any(start <= point <= end for start, end in _HAN_RANGES)


def _non_han_short_terms(value: str) -> set[str]:
    """Versioned codepoint grams include symbols and substrings within words.

    A literal without Han remains inside one non-Han run when it occurs. Han
    literals use the separately versioned Han grams from search.ngram.
    """

    normalized = _normalized(value)
    terms: set[str] = set()
    start = 0
    while start < len(normalized):
        if _is_han(normalized[start]):
            start += 1
            continue
        end = start + 1
        while end < len(normalized) and not _is_han(normalized[end]):
            end += 1
        run = normalized[start:end]
        for width in range(1, min(3, len(run)) + 1):
            for offset in range(len(run) - width + 1):
                terms.add(f"c{width}{run[offset : offset + width].encode('utf-8').hex()}")
        start = end
    return terms


def literal_oracle(documents: tuple[Document, ...], query: Query) -> frozenset[str]:
    """Independent Page-relevance judgment by literal scan, never FTS or rank code."""

    relevant: set[str] = set()
    for document in documents:
        if document.deleted or document.library_id not in query.libraries:
            continue
        if query.occurred_from is not None and document.occurred_at < query.occurred_from:
            continue
        if query.occurred_before is not None and document.occurred_at >= query.occurred_before:
            continue
        if query.tags_any and not set(document.tags).intersection(query.tags_any):
            continue
        if query.keywords:
            if any(keyword == "" for keyword in query.keywords):
                raise ValueError("Empty keywords have no accepted experimental semantics.")
            title_text = _normalized(document.title)
            body_text = _normalized(document.body)
            if not any(
                _normalized(keyword) in title_text or _normalized(keyword) in body_text
                for keyword in query.keywords
            ):
                continue
        relevant.add(document.page_id)
    return frozenset(relevant)


def _encoded_terms(text: str) -> str:
    terms = {
        f"{term.kind}{term.text.encode('utf-8').hex()}"
        for term in extract_term_frequencies(text)
        if term.kind != "word"
    }
    terms.update(_non_han_short_terms(text))
    return " ".join(sorted(terms))


def _keyword_match(keywords: tuple[str, ...]) -> str:
    tokens: set[str] = set()
    for keyword in keywords:
        if keyword == "":
            raise ValueError("Empty keywords have no accepted experimental semantics.")
        han_terms = [
            term
            for term in extract_term_frequencies(keyword)
            if term.kind in {"han1", "han2", "han3"}
        ]
        if han_terms:
            chosen = max(han_terms, key=lambda term: (int(term.kind[-1]), term.text))
            tokens.add(f"{chosen.kind}{chosen.text.encode('utf-8').hex()}")
        else:
            short_terms = _non_han_short_terms(keyword)
            if not short_terms:
                raise ValueError("Keyword has no indexable literal terms.")
            tokens.add(max(short_terms, key=lambda token: (int(token[1]), token)))
    # Hex-encoded terms contain only [a-z0-9]; caller text never becomes FTS syntax.
    return " OR ".join(f'"{token}"' for token in sorted(tokens))


def _create_authority(connection: sqlite3.Connection, documents: tuple[Document, ...]) -> None:
    connection.executescript(
        "CREATE TABLE documents (number INTEGER PRIMARY KEY, page_id TEXT UNIQUE NOT NULL, "
        "library_id TEXT NOT NULL, section_id TEXT NOT NULL, book_id TEXT NOT NULL, "
        "revision_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, "
        "occurred_at INTEGER NOT NULL, deleted INTEGER NOT NULL);"
        "CREATE INDEX documents_scope ON documents(library_id, deleted, occurred_at);"
        "CREATE TABLE tags (number INTEGER NOT NULL, tag TEXT NOT NULL, "
        "PRIMARY KEY (number, tag));"
        "CREATE INDEX tags_lookup ON tags(tag, number);"
        "CREATE TABLE old_revisions (number INTEGER PRIMARY KEY, body TEXT NOT NULL);"
        "CREATE VIRTUAL TABLE search_index USING fts5(title_terms, body_terms, "
        "tokenize='unicode61');"
    )
    with connection:
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    doc.number,
                    doc.page_id,
                    doc.library_id,
                    doc.section_id,
                    doc.book_id,
                    doc.revision_id,
                    doc.title,
                    doc.body,
                    doc.occurred_at,
                    int(doc.deleted),
                )
                for doc in documents
            ),
        )
        connection.executemany(
            "INSERT INTO tags VALUES (?, ?)",
            ((doc.number, tag) for doc in documents for tag in doc.tags),
        )
        connection.executemany(
            "INSERT INTO old_revisions VALUES (?, ?)",
            ((doc.number, doc.old_body) for doc in documents if doc.old_body is not None),
        )


def _index_documents(connection: sqlite3.Connection) -> float:
    started = time.perf_counter_ns()
    rows = connection.execute("SELECT number, title, body FROM documents").fetchall()
    with connection:
        connection.executemany(
            "INSERT INTO search_index(rowid, title_terms, body_terms) VALUES (?, ?, ?)",
            ((number, _encoded_terms(title), _encoded_terms(body)) for number, title, body in rows),
        )
    return (time.perf_counter_ns() - started) / 1_000_000


def _statement(query: Query) -> tuple[str, tuple[object, ...]]:
    if not query.libraries:
        raise ValueError("The evaluator requires an explicit authorized Library scope.")
    parameters: list[object] = []
    from_clause = "documents AS d"
    conditions = ["d.deleted = 0"]
    if query.keywords:
        from_clause += " JOIN search_index ON search_index.rowid = d.number"
        match = _keyword_match(query.keywords)
        if not match:
            return "SELECT d.number FROM documents AS d WHERE 0", ()
        conditions.append("search_index MATCH ?")
        parameters.append(match)
    slots = ", ".join("?" for _ in query.libraries)
    conditions.append(f"d.library_id IN ({slots})")
    parameters.extend(query.libraries)
    if query.occurred_from is not None:
        conditions.append("d.occurred_at >= ?")
        parameters.append(query.occurred_from)
    if query.occurred_before is not None:
        conditions.append("d.occurred_at < ?")
        parameters.append(query.occurred_before)
    if query.tags_any:
        slots = ", ".join("?" for _ in query.tags_any)
        conditions.append(
            f"EXISTS (SELECT 1 FROM tags AS t WHERE t.number = d.number AND t.tag IN ({slots}))"
        )
        parameters.extend(query.tags_any)
    sql = (
        "SELECT d.page_id, d.title, d.body, d.number FROM "
        + from_clause
        + " WHERE "
        + " AND ".join(conditions)
    )
    return sql, tuple(parameters)


def _candidate_rank(
    page_id: str, title: str, body: str, tags: set[str], query: Query
) -> tuple[int, str] | None:
    title_text = _normalized(title)
    body_text = _normalized(body)
    score = 0
    for keyword in query.keywords:
        literal = _normalized(keyword)
        if literal in title_text:
            score += 100
        if literal in body_text:
            score += 10
    if query.keywords and score == 0:
        return None
    score += 2 * len(tags.intersection(query.tags_any))
    return -score, page_id


def _candidate_search(connection: sqlite3.Connection, query: Query) -> tuple[tuple[str, ...], int]:
    statement, parameters = _statement(query)
    rows = connection.execute(statement, parameters).fetchall()
    tag_matches: dict[int, set[str]] = {}
    if query.tags_any:
        slots = ", ".join("?" for _ in query.tags_any)
        for number, tag in connection.execute(
            f"SELECT number, tag FROM tags WHERE tag IN ({slots})", query.tags_any
        ):
            tag_matches.setdefault(number, set()).add(tag)
    ranked: list[tuple[int, str]] = []
    for page_id, title, body, number in rows:
        score = _candidate_rank(page_id, title, body, tag_matches.get(number, set()), query)
        if score is not None:
            ranked.append(score)
    ranked.sort()
    return tuple(page_id for _score, page_id in ranked), len(rows)


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("No latency samples were collected.")

    def nearest(percent: float) -> float:
        return ordered[math.ceil(len(ordered) * percent) - 1]

    return {
        "p50_ms": round(nearest(0.50), 3),
        "p95_ms": round(nearest(0.95), 3),
        "p99_ms": round(nearest(0.99), 3),
        "worst_ms": round(ordered[-1], 3),
        "sample_count": len(ordered),
    }


def _database_sizes(connection: sqlite3.Connection, path: Path) -> DatabaseSizes:
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    try:
        indexed_bytes = connection.execute(
            "SELECT sum(pgsize) FROM dbstat WHERE name GLOB 'search_index*'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        indexed_bytes = None
    return {"database_bytes": path.stat().st_size, "fts_pages_bytes": indexed_bytes}


def _update_one_current_page(connection: sqlite3.Connection) -> float:
    row = connection.execute("SELECT title, body FROM documents WHERE number = 2").fetchone()
    assert row is not None
    title, body = row
    changed = body.replace("独家信号", "更新后信号", 1)
    started = time.perf_counter_ns()
    with connection:
        connection.execute(
            "UPDATE documents SET body = ?, revision_id = ? WHERE number = 2",
            (changed, "revision-00001-updated"),
        )
        connection.execute("DELETE FROM search_index WHERE rowid = 2")
        connection.execute(
            "INSERT INTO search_index(rowid, title_terms, body_terms) VALUES (?, ?, ?)",
            (2, _encoded_terms(title), _encoded_terms(changed)),
        )
    return (time.perf_counter_ns() - started) / 1_000_000


def _probe_unauthorized_noise(connection: sqlite3.Connection, number: int) -> dict[str, Any]:
    query = Query("auth_probe", ("library-0", "library-1"), ("共同主题",))
    before_hits, before_candidates = _candidate_search(connection, query)
    noise_count = TOP_K + 5
    noise_title = "共同主题 " * 30
    noise_body = "共同主题 " * 30
    connection.execute("SAVEPOINT unauthorized_probe")
    try:
        for offset in range(noise_count):
            noise_number = number + offset
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    noise_number,
                    f"page-unauthorized-noise-{offset:02d}",
                    "library-2",
                    "section-noise",
                    "book-noise",
                    f"revision-noise-{offset:02d}",
                    noise_title,
                    noise_body,
                    BASE_TIME,
                    0,
                ),
            )
            connection.execute(
                "INSERT INTO search_index(rowid, title_terms, body_terms) VALUES (?, ?, ?)",
                (noise_number, _encoded_terms(noise_title), _encoded_terms(noise_body)),
            )
        after_hits, after_candidates = _candidate_search(connection, query)
        global_query = Query(
            "unfiltered_probe", ("library-0", "library-1", "library-2"), ("共同主题",)
        )
        global_hits, _ = _candidate_search(connection, global_query)
    finally:
        connection.execute("ROLLBACK TO unauthorized_probe")
        connection.execute("RELEASE unauthorized_probe")
    full_set_stable = frozenset(before_hits) == frozenset(after_hits)
    top_k_stable = before_hits[:TOP_K] == after_hits[:TOP_K]
    candidate_count_stable = before_candidates == after_candidates
    unfiltered_top_k_all_noise = all(
        page_id.startswith("page-unauthorized-noise-") for page_id in global_hits[:TOP_K]
    )
    return {
        "authorized_count": len(before_hits),
        "top_k_size": TOP_K,
        "higher_ranked_unauthorized_count": noise_count,
        "unfiltered_top_k_all_unauthorized": unfiltered_top_k_all_noise,
        "complete_set_stable": full_set_stable,
        "top_k_stable": top_k_stable,
        "candidate_count_stable": candidate_count_stable,
        "passed": (
            len(before_hits) > TOP_K
            and noise_count > TOP_K
            and unfiltered_top_k_all_noise
            and full_set_stable
            and top_k_stable
            and candidate_count_stable
        ),
    }


def evaluate(
    page_count: int = DEFAULT_PAGES, body_bytes: int = DEFAULT_BODY_BYTES
) -> dict[str, Any]:
    """Run only against a fresh temporary SQLite database built from synthetic text."""

    documents = generate_corpus(page_count, body_bytes)
    queries = evaluation_queries(page_count)
    with tempfile.TemporaryDirectory(prefix="patchouli-search-v2-") as directory:
        path = Path(directory) / "synthetic.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            _create_authority(connection, documents)
            authority_sizes = _database_sizes(connection, path)
            build_ms = _index_documents(connection)
            sizes = _database_sizes(connection, path)
            observations: list[dict[str, Any]] = []
            all_samples: list[float] = []
            for query in queries:
                expected = literal_oracle(documents, query)
                sql, parameters = _statement(query)
                plan = [
                    detail
                    for _id, _parent, _unused, detail in connection.execute(
                        "EXPLAIN QUERY PLAN " + sql, parameters
                    )
                ]
                samples: list[float] = []
                actual: tuple[str, ...] = ()
                candidate_count = 0
                for _ in range(5):
                    started = time.perf_counter_ns()
                    actual, candidate_count = _candidate_search(connection, query)
                    samples.append((time.perf_counter_ns() - started) / 1_000_000)
                all_samples.extend(samples)
                observations.append(
                    {
                        "name": query.name,
                        "authorized_libraries": list(query.libraries),
                        "candidate_count": candidate_count,
                        "matched_count": len(actual),
                        "oracle_count": len(expected),
                        "complete_membership": frozenset(actual) == expected,
                        "hand_labelled_top_k": (
                            list(actual[: len(query.expected_first)]) == list(query.expected_first)
                            if query.expected_first
                            else None
                        ),
                        "top_k_page_ids": list(actual[:TOP_K]),
                        "latency": _percentiles(samples),
                        "query_plan": plan,
                    }
                )
            auth_isolation = _probe_unauthorized_noise(connection, page_count + 1)
            update_ms = _update_one_current_page(connection)
            sentinel = Query("post_update", ("library-0", "library-1"), ("独家信号",))
            after_update, _ = _candidate_search(connection, sentinel)
            updated = after_update == ("page-00000",)
            with connection:
                connection.execute("DELETE FROM search_index")
            rebuild_ms = _index_documents(connection)
            after_rebuild, _ = _candidate_search(connection, sentinel)
            rebuilt = after_rebuild == after_update
            rebuilt_sizes = _database_sizes(connection, path)
    evaluation_passed = (
        all(
            observation["complete_membership"] and observation["hand_labelled_top_k"] is not False
            for observation in observations
        )
        and auth_isolation["passed"]
        and updated
        and rebuilt
    )
    return {
        "mode": "offline_search_v2_candidate",
        "status": "Proposed; evaluation only; no production API, migration, or deployment",
        "evaluation_passed": evaluation_passed,
        "provenance": "CC0-1.0 original deterministic synthetic text; no private data",
        "page_count": page_count,
        "body_bytes_per_page": body_bytes,
        "library_count": 3,
        "old_revision_count": sum(doc.old_body is not None for doc in documents),
        "deleted_count": sum(doc.deleted for doc in documents),
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "unicode_version": unicodedata.unidata_version,
        "normalization_version": NORMALIZATION_VERSION,
        "han_range_version": HAN_RANGE_VERSION,
        "short_literal_terms_version": SHORT_LITERAL_TERMS_VERSION,
        "corpus_body_budget_bytes": MAX_CORPUS_BODY_BYTES,
        "build_ms": round(build_ms, 3),
        "update_ms": round(update_ms, 3),
        "rebuild_ms": round(rebuild_ms, 3),
        "sizes_after_build": sizes,
        "estimated_fts_growth_bytes": (sizes["database_bytes"] - authority_sizes["database_bytes"]),
        "index_size_method": (
            "fts_pages_bytes uses SQLite dbstat when available; estimated_fts_growth_bytes "
            "is the whole-database growth after indexing, not an exact index-only size."
        ),
        "sizes_after_rebuild": rebuilt_sizes,
        "overall_latency": _percentiles(all_samples),
        "queries": observations,
        "unauthorized_high_relevance_probe": auth_isolation,
        "current_revision_update_replaces_old_match": updated,
        "rebuild_matches_updated_projection": rebuilt,
        "limitations": (
            "This is one local FTS5 candidate and an isolated SQLite schema, not the production "
            "authorization, API, migration, backup, concurrency, or restore path. Text is "
            "synthetic, not human relevance judgments. Hand-labelled sentinels cover only a few "
            "rank cases. Five repetitions per query give indicative p99, "
            "not a stable tail estimate. "
            "Measurements use a warm OS cache; a new connection is not an OS-cold measurement. "
            "The 1-second target and design acceptance are not established."
            " Empty keywords are explicitly unsupported pending a public semantics decision."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 搜索 v2 离线候选评估（Proposed，未启用生产搜索）",
        "",
        f"- 独立判断与安全探针全部通过：{report['evaluation_passed']}。",
        f"- 合成 Page：{report['page_count']} × {report['body_bytes_per_page']} UTF-8 字节。",
        f"- FTS 建立／更新／重建：{report['build_ms']}／{report['update_ms']}／"
        f"{report['rebuild_ms']} 毫秒。",
        f"- 建立后数据库／FTS 页字节：{report['sizes_after_build']}。",
        f"- 全部查询延迟：{report['overall_latency']}。",
        "",
        "| 查询 | 候选 | 命中 | 独立相关性数 | 完整召回 | 手工 TopK | p95 ms | 最差 ms |",
        "| --- | ---: | ---: | ---: | --- | --- | ---: | ---: |",
    ]
    for query in report["queries"]:
        latency = query["latency"]
        lines.append(
            f"| `{query['name']}` | {query['candidate_count']} | {query['matched_count']} | "
            f"{query['oracle_count']} | {query['complete_membership']} | "
            f"{query['hand_labelled_top_k']} | {latency['p95_ms']} | {latency['worst_ms']} |"
        )
    lines.extend(("", f"局限：{report['limitations']}", ""))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an isolated synthetic search-v2 evaluation.")
    parser.add_argument("--scale", action="store_true", help="Use the manual 5,000 x 10 KiB scale.")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument("--body-bytes", type=int, default=DEFAULT_BODY_BYTES)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    page_count = SCALE_PAGES if args.scale else args.pages
    body_bytes = SCALE_BODY_BYTES if args.scale else args.body_bytes
    report = evaluate(page_count, body_bytes)
    if args.format == "markdown":
        print(render_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report["evaluation_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
