"""Synthetic multi-Library matrix for the real current-Page search path.

This operator-run experiment creates only a disposable local SQLite database.
It is not a production latency or human-relevance acceptance test.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patchouli_lib.api.authentication import AuthenticatedRequestContext  # noqa: E402
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy  # noqa: E402
from patchouli_lib.auth.repository import AuthRepository  # noqa: E402
from patchouli_lib.auth.schemas import (  # noqa: E402
    AuthenticatedCaller,
    CallerKind,
    NewCaller,
    NewCredential,
    credential_metadata,
)
from patchouli_lib.auth.tokens import generate_token  # noqa: E402
from patchouli_lib.content.file_manifest import build_file_manifest  # noqa: E402
from patchouli_lib.content.file_set_create_core import FileSetPageCreateCore  # noqa: E402
from patchouli_lib.content.file_set_write_service import (  # noqa: E402
    FileSetAppendCommand,
    FileSetWriteService,
    FileSetWriteSuccess,
)
from patchouli_lib.content.repository import ContentRepository  # noqa: E402
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput  # noqa: E402
from patchouli_lib.content.service import page_current_etag  # noqa: E402
from patchouli_lib.database import build_engine, immediate_transaction  # noqa: E402
from patchouli_lib.idempotency.schemas import digest_idempotency_key  # noqa: E402
from patchouli_lib.library.repository import LibraryRepository  # noqa: E402
from patchouli_lib.library.schemas import LibraryStructureSeed  # noqa: E402
from patchouli_lib.library.service import LibrarySeedService  # noqa: E402
from patchouli_lib.retrieval.cursor import CursorCodec, InvalidCursorError  # noqa: E402
from patchouli_lib.search.index_v2 import rebuild_search_index  # noqa: E402
from patchouli_lib.search.query_v2 import SearchQueryV2, parse_query_v2_json  # noqa: E402
from patchouli_lib.search.service_v2 import SearchScopeError, search_pages_v2  # noqa: E402
from patchouli_lib.tags.repository import TagRepository  # noqa: E402

DEFAULT_PAGES = 48
DEFAULT_BYTES = 1_024
SCALE_PAGES = 5_000
SCALE_BYTES = 10_240
MAX_BODY_BUDGET = 64 * 1_024 * 1_024
BASE_TIME = 1_700_000_000_000_000
AUTH_CLOCK = 3_000_000
CALLER_ID = "a" * 32
CREDENTIAL_ID = "b" * 32
SHARED_TAG = "c" * 32
SECOND_TAG = "d" * 32
RARE = "罕见信号"
UPDATED = "更新信号"
COMMON = "共同主题"
NO_HIT = "永不存在的紫色引力井"
OR_WORDS = tuple(f"矩阵专用长词{number:02d}尾" for number in range(16))
MULTI_TEXT = "双文本重复命中哨兵"
TEXT_ONLY = "附加文本独占哨兵"
BINARY_BODY = "二进制正文隔离哨兵"
SPLIT_LEFT = "不可跨字段拼接前段"
SPLIT_RIGHT = "不可跨字段拼接后段"
CURSOR_SECRET = "synthetic-search-matrix-cursor-secret-20261002"
CURSOR_CODEC = CursorCodec(CURSOR_SECRET.encode("ascii"))
_SERVER_TIMEOUT_SECONDS = 30
_HTTP_TIMEOUT_SECONDS = 15
_MAX_HTTP_RESPONSE_BYTES = 1_048_576
_HTTP_MIGRATE = """
import sys
from alembic import command
from alembic.config import Config
command.upgrade(Config(sys.argv[1]), "head")
"""
_HTTP_SERVER = """
import os
import socket
import sys
import threading
import uvicorn
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings

settings = Settings(_env_file=None, database_url=os.environ["PATCHOULI_DATABASE_URL"],
                    environment="test",
                    retrieval_cursor_signing_secret=os.environ["PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET"])
application = create_app(settings)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
print(listener.getsockname()[1], flush=True)
server = uvicorn.Server(uvicorn.Config(application, log_level="critical", access_log=False,
                                      lifespan="on", timeout_graceful_shutdown=10))
worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
worker.start()
sys.stdin.read()
server.should_exit = True
worker.join(timeout=15)
"""


@dataclass(frozen=True, slots=True)
class SyntheticPage:
    number: int
    library_id: str
    section_id: str
    page_id: str
    title: str
    body: str
    occurred_at: int
    tags: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True, slots=True)
class SeededMatrix:
    libraries: tuple[str, str, str]
    pages: tuple[SyntheticPage, ...]
    context: AuthenticatedRequestContext
    token: str


def resolve_dimensions(
    *, scale: bool, pages: int | None, bytes_per_page: int | None
) -> tuple[int, int]:
    if scale and (pages is not None or bytes_per_page is not None):
        raise ValueError("--scale cannot be combined with explicit dimensions.")
    count = SCALE_PAGES if scale else DEFAULT_PAGES if pages is None else pages
    size = (
        SCALE_BYTES
        if scale or (bytes_per_page is None and count >= SCALE_PAGES)
        else DEFAULT_BYTES
        if bytes_per_page is None
        else bytes_per_page
    )
    if not 36 <= count <= 10_000 or not 512 <= size <= 32_768 or count * size > MAX_BODY_BUDGET:
        raise ValueError("Synthetic dimensions exceed the reviewed local budget.")
    return count, size


def _body(number: int, size: int) -> str:
    prefix = f"# {COMMON}\n合成资料 {number:05d} {OR_WORDS[number % len(OR_WORDS)]} {MULTI_TEXT} "
    if number in (1, 3, 8):
        prefix += f"{RARE} "
    words = ("知识库", "版本", "检索", "技术报告", "archive", "source", "安全", "研究")
    rotated = words[number % len(words) :] + words[: number % len(words)]
    pattern = (" ".join(rotated) + f" topic{number % 101:03d} ").encode("utf-8")
    start = prefix.encode("utf-8")
    suffix = f"\n{SPLIT_LEFT}".encode()
    if len(start) + len(suffix) > size:
        raise ValueError("The synthetic prefix exceeds its body budget.")
    repeats, remainder = divmod(size - len(start) - len(suffix), len(pattern))
    return (start + pattern * repeats + b"x" * remainder + suffix).decode("utf-8")


def _files(number: int, total_bytes: int) -> tuple[tuple[str, bytes], ...]:
    """Budget includes *all* bytes, not merely the main Markdown file."""

    notes = f"{SPLIT_RIGHT}\n{MULTI_TEXT}\n{TEXT_ONLY}".encode()
    opaque = b"\xff\x00" + BINARY_BODY.encode("utf-8")
    body = _body(number, total_bytes - len(notes) - len(opaque)).encode("utf-8")
    return (("content.md", body), ("notes.txt", notes), ("opaque-file.bin", opaque))


def _seed(
    engine: Engine, page_count: int, body_bytes: int, *, expires_at: int = 10_000_000
) -> SeededMatrix:
    issued = generate_token()
    recorded: list[SyntheticPage] = []
    with immediate_transaction(engine) as connection:
        structures = []
        for prefix, label in (("1", "Home"), ("4", "Readable"), ("7", "Unreachable")):
            identifiers = iter(
                f"{value:x}" * 32 for value in range(int(prefix, 16), int(prefix, 16) + 3)
            )

            def next_structure_id(identifiers: Iterator[str] = identifiers) -> str:
                return next(identifiers)

            structure = LibrarySeedService(
                LibraryRepository(connection),
                id_factory=next_structure_id,
                clock=lambda: 1_000_000,
            ).seed(
                LibraryStructureSeed(
                    library_name=f"{label} Synthetic Library",
                    section_name=f"{label} Synthetic Section",
                    book_name=f"{label} Synthetic Book",
                )
            )
            structures.append(structure)
        libraries = (
            structures[0].library.id,
            structures[1].library.id,
            structures[2].library.id,
        )
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id=CALLER_ID,
                library_id=libraries[0],
                kind=CallerKind.AGENT,
                name="Synthetic matrix caller",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        credential = auth.add_credential(
            NewCredential(
                id=CREDENTIAL_ID,
                library_id=libraries[0],
                caller_id=CALLER_ID,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=expires_at,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": CREDENTIAL_ID,
                "caller_id": CALLER_ID,
                "home_library_id": libraries[0],
                "mode": "library_grants",
                "created_at": AUTH_CLOCK,
            },
        )
        for library_id, action in (
            (libraries[0], "read"),
            (libraries[0], "write"),
            (libraries[1], "read"),
        ):
            connection.execute(
                insert(CredentialLibraryGrant),
                {
                    "credential_id": CREDENTIAL_ID,
                    "caller_id": CALLER_ID,
                    "home_library_id": libraries[0],
                    "target_library_id": library_id,
                    "action": action,
                    "created_at": AUTH_CLOCK,
                },
            )
        tags = TagRepository(connection)
        for library_id in libraries:
            tags.add_tag(
                library_id=library_id, tag_id=SHARED_TAG, name="Shared synthetic", created_at=1
            )
            tags.add_tag(
                library_id=library_id, tag_id=SECOND_TAG, name="Second synthetic", created_at=1
            )
        for number in range(page_count):
            book = structures[number % 3].book
            library_id, section_id = book.library_id, book.section_id
            files = _files(number, body_bytes)
            body = files[0][1].decode("utf-8")
            occurred_at = BASE_TIME + number * 1_000_000
            uid = (number + 1).to_bytes(16, "big")
            revision_id = f"rev_{number + 1:032x}"

            def next_source_id(number: int = number) -> str:
                return f"{number + 1:032x}"

            def next_page_uid(uid: bytes = uid) -> bytes:
                return uid

            def next_revision_id(revision_id: str = revision_id) -> str:
                return revision_id

            page = FileSetPageCreateCore(
                connection,
                id_factory=next_source_id,
                page_uid_factory=next_page_uid,
                revision_id_factory=next_revision_id,
            ).create_page(
                book=book,
                title=f"合成资料 {number:05d}",
                occurred_at=occurred_at,
                operation_at=2_000_000,
                manifest=build_file_manifest(files),
                source=ArchiveSourceInput(kind="synthetic"),
            )
            assigned = (
                (SHARED_TAG,) if number % 7 == 0 else (SECOND_TAG,) if number % 7 == 1 else ()
            )
            for tag_id in assigned:
                tags.attach_page(library_id=library_id, page_uid=uid, tag_id=tag_id, created_at=1)
            recorded.append(
                SyntheticPage(
                    number,
                    library_id,
                    section_id,
                    page.page_id,
                    page.title,
                    body,
                    occurred_at,
                    assigned,
                    files,
                )
            )
    context = AuthenticatedRequestContext(
        authenticated=AuthenticatedCaller(
            caller=caller, credential=credential_metadata(credential)
        ),
        grants=(),
    )
    return SeededMatrix(libraries, tuple(recorded), context, issued.value)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _oracle(
    pages: tuple[SyntheticPage, ...], readable: frozenset[str], query: SearchQueryV2
) -> tuple[tuple[str, str], ...]:
    """Literal full-corpus scan; fixture makes every hit equal-weight and uniquely dated.

    No index, service ranking helper, or SQL candidate implementation is used.
    Each Page contains exactly one of the sixteen OR terms, in the same text
    field. Other keyword scenarios contain one keyword and have identical hit
    fields across Pages, even when both text files match. No score formula is
    reproduced here; unequal-weight relevance is deliberately outside this
    fixture. At most one Tag is attached per Page. Order is therefore time/ID.
    """

    selected = readable if query.libraries is None else frozenset(query.libraries)
    wanted_tags = {(tag.library_id, tag.tag_id) for tag in query.tags_any}
    needles = tuple(_normalize(keyword) for keyword in query.keywords)
    matches: list[SyntheticPage] = []
    for page in pages:
        if page.library_id not in selected:
            continue
        if query.occurred_from_us is not None and page.occurred_at < query.occurred_from_us:
            continue
        if query.occurred_before_us is not None and page.occurred_at >= query.occurred_before_us:
            continue
        if wanted_tags and not any((page.library_id, tag) in wanted_tags for tag in page.tags):
            continue
        # The fixture explicitly declares md/txt as text and bin as opaque.
        # Do not call the product extractor, candidate query or ranker.
        fields = (
            page.title,
            *(name for name, _ in page.files),
            *(
                content.decode("utf-8")
                for name, content in page.files
                if name.endswith((".md", ".txt"))
            ),
        )
        matched = {
            needle for needle in needles if any(needle in _normalize(field) for field in fields)
        }
        if len(matched) > 1:
            raise ValueError("The independent oracle needs an equal-weight fixture.")
        if needles and not matched:
            continue
        matches.append(page)
    matches.sort(key=lambda page: (-page.occurred_at, page.library_id, page.page_id))
    return tuple((page.library_id, page.page_id) for page in matches)


def _queries(libraries: tuple[str, str, str]) -> tuple[tuple[str, dict[str, object]], ...]:
    home, readable, _hidden = libraries
    lower = BASE_TIME + 10 * 1_000_000
    upper = BASE_TIME + 30 * 1_000_000
    tags = [
        {"library_id": home, "tag_id": SHARED_TAG},
        {"library_id": readable, "tag_id": SECOND_TAG},
    ]
    return (
        ("common_default_scope", {"keywords": [COMMON]}),
        ("common_cross_library", {"keywords": [COMMON], "libraries": [home, readable]}),
        ("common_one_library", {"keywords": [COMMON], "libraries": [readable]}),
        ("rare", {"keywords": [RARE]}),
        ("no_hit", {"keywords": [NO_HIT]}),
        ("wide_keyword_or", {"keywords": list(OR_WORDS)}),
        ("multi_file_duplicate", {"keywords": [MULTI_TEXT]}),
        ("additional_text_file", {"keywords": [TEXT_ONLY]}),
        ("binary_file_name", {"keywords": ["opaque-file"]}),
        ("binary_body_not_indexed", {"keywords": [BINARY_BODY]}),
        ("no_cross_field_join", {"keywords": [SPLIT_LEFT + SPLIT_RIGHT]}),
        ("tag_or_only", {"tags_any": tags}),
        (
            "tag_identity_scoped",
            {"tags_any": [{"library_id": home, "tag_id": SHARED_TAG}]},
        ),
        ("time_half_open", {"occurred_from_us": lower, "occurred_before_us": upper}),
        (
            "keyword_tag_time_combined",
            {
                "keywords": [COMMON],
                "tags_any": tags,
                "occurred_from_us": lower,
                "occurred_before_us": upper,
            },
        ),
    )


def _parsed(payload: dict[str, object]) -> SearchQueryV2:
    return parse_query_v2_json(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _identities(
    engine: Engine, context: AuthenticatedRequestContext, query: SearchQueryV2
) -> tuple[tuple[str, str], ...]:
    result = search_pages_v2(engine, context, query, clock=lambda: AUTH_CLOCK)
    return tuple((item.library_id, item.page_id) for item in result.items)


def _check_result(
    actual: tuple[tuple[str, str], ...],
    expected: tuple[tuple[str, str], ...],
    name: str,
) -> None:
    if actual != expected[:20] or len(actual) != len(set(actual)):
        raise RuntimeError(f"Synthetic search disagrees with the independent oracle: {name}.")


def _check_pagination(
    fetch: Callable[[dict[str, object]], tuple[tuple[tuple[str, str], ...], str | None]],
    payload: dict[str, object],
    expected: tuple[tuple[str, str], ...],
    *,
    limit: int = 100,
) -> dict[str, object]:
    """Check one complete signed chain outside latency sampling, with a bounded loop."""

    cursor: str | None = None
    cursors: set[str] = set()
    collected: list[tuple[str, str]] = []
    page_count = 0
    while page_count <= len(expected) // limit:
        request = payload | {"limit": limit}
        if cursor is not None:
            request["cursor"] = cursor
        actual, next_cursor = fetch(request)
        offset = len(collected)
        if actual != expected[offset : offset + limit]:
            raise RuntimeError("Synthetic cursor page disagrees with the full independent oracle.")
        collected.extend(actual)
        page_count += 1
        if len(collected) == len(expected):
            if next_cursor is not None:
                raise RuntimeError("Synthetic cursor did not end at the complete oracle boundary.")
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
            raise RuntimeError("Synthetic cursor chain ended early or repeated a cursor.")
        cursors.add(next_cursor)
        cursor = next_cursor
    if tuple(collected) != expected or len(collected) != len(set(collected)):
        raise RuntimeError("Synthetic cursor chain omitted or duplicated a Page.")
    return {
        "limit": limit,
        "page_count": page_count,
        "returned": len(collected),
        "complete_order_matches_independent_oracle": True,
        "hidden_library_excluded": True,
        "terminal_next_cursor_is_null": True,
    }


def _pagination_queries() -> tuple[tuple[str, dict[str, object]], ...]:
    return (
        ("common", {"keywords": [COMMON]}),
        ("wide_keyword_or", {"keywords": list(OR_WORDS)}),
        ("multi_file_duplicate", {"keywords": [MULTI_TEXT]}),
    )


def _service_pagination(engine: Engine, seed: SeededMatrix) -> list[dict[str, object]]:
    def fetch(payload: dict[str, object]) -> tuple[tuple[tuple[str, str], ...], str | None]:
        result = search_pages_v2(
            engine,
            seed.context,
            _parsed(payload),
            clock=lambda: AUTH_CLOCK,
            cursor_codec=CURSOR_CODEC,
        )
        return tuple((item.library_id, item.page_id) for item in result.items), result.next_cursor

    return [
        {"name": name}
        | _check_pagination(
            fetch, payload, _oracle(seed.pages, frozenset(seed.libraries[:2]), _parsed(payload))
        )
        for name, payload in _pagination_queries()
    ]


def _measure_queries(
    engine: Engine, database_url: str, seed: SeededMatrix, repeats: int
) -> list[dict[str, object]]:
    readable = frozenset(seed.libraries[:2])
    measurements: list[dict[str, object]] = []
    for name, payload in _queries(seed.libraries):
        query = _parsed(payload | {"limit": 20})
        expected = _oracle(seed.pages, readable, query)
        fresh_engine = build_engine(database_url)
        try:
            began = time.perf_counter_ns()
            first = _identities(fresh_engine, seed.context, query)
            fresh_ms = (time.perf_counter_ns() - began) / 1_000_000
        finally:
            fresh_engine.dispose()
        _check_result(first, expected, name)
        warm_ms: list[float] = []
        for _ in range(repeats):
            began = time.perf_counter_ns()
            actual = _identities(engine, seed.context, query)
            warm_ms.append((time.perf_counter_ns() - began) / 1_000_000)
            _check_result(actual, expected, name)
        measurements.append(
            {
                "name": name,
                "oracle_match_count": len(expected),
                "returned": len(first),
                "top20_matches_independent_oracle": True,
                "warm_median_ms": round(statistics.median(warm_ms), 3),
                "warm_max_ms": round(max(warm_ms), 3),
                "new_connection_ms": round(fresh_ms, 3),
            }
        )
    hidden = seed.libraries[2]
    negative_payloads: tuple[dict[str, object], ...] = (
        {"keywords": [COMMON], "libraries": [hidden]},
        {"tags_any": [{"library_id": hidden, "tag_id": SHARED_TAG}]},
    )
    for payload in negative_payloads:
        try:
            _identities(engine, seed.context, _parsed(payload))
        except SearchScopeError:
            continue
        raise RuntimeError("An unreadable Library was accepted by search.")
    return measurements


def _update_one(engine: Engine, seed: SeededMatrix) -> tuple[float, tuple[SyntheticPage, ...]]:
    target = seed.pages[3]  # Home Library, and one of the known rare-term sentinels.
    changed = target.body.replace(RARE, UPDATED, 1)
    if changed == target.body:
        raise RuntimeError("The update sentinel is absent.")
    began = time.perf_counter_ns()
    with immediate_transaction(engine) as connection:
        page = ContentRepository(connection).get_page(target.library_id, target.page_id)
        if page is None:
            raise RuntimeError("The update target is missing.")
        etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        result = FileSetWriteService(connection, clock=lambda: AUTH_CLOCK).append_existing_page(
            seed.token,
            FileSetAppendCommand(
                library_id=target.library_id,
                section_id=target.section_id,
                page_id=target.page_id,
                expected_etag=etag,
                files=tuple(
                    (name, changed.encode("utf-8") if name == "content.md" else content)
                    for name, content in target.files
                ),
                source=ArchiveSourceInput(kind="synthetic"),
                request_id=f"req_{'e' * 32}",
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("synthetic-matrix-update")),
        )
        if not isinstance(result, FileSetWriteSuccess) or not result.changed:
            raise RuntimeError("The synthetic update did not create a new Revision.")
    elapsed_ms = (time.perf_counter_ns() - began) / 1_000_000
    pages = tuple(
        replace(
            page,
            body=changed,
            files=tuple(
                (name, changed.encode("utf-8") if name == "content.md" else content)
                for name, content in page.files
            ),
        )
        if page.number == target.number
        else page
        for page in seed.pages
    )
    with engine.connect() as connection:
        repository = ContentRepository(connection)
        current = repository.get_page(target.library_id, target.page_id)
        if current is None:
            raise RuntimeError("The updated Page is missing.")
        saved = repository.get_current_file_manifest(current)
        expected_files = dict(pages[target.number].files)
        if {entry.name: entry.content for entry in saved.files} != expected_files:
            raise RuntimeError("The Revision update did not preserve the complete file snapshot.")
    for name, keyword in (("old_rare_after_update", RARE), ("new_term_after_update", UPDATED)):
        query = _parsed({"keywords": [keyword], "limit": 20})
        expected = _oracle(pages, frozenset(seed.libraries[:2]), query)
        _check_result(_identities(engine, seed.context, query), expected, name)
    return elapsed_ms, pages


def _require_update_invalidates_cursor(
    engine: Engine, seed: SeededMatrix, cursor: str | None
) -> None:
    if cursor is None:
        raise RuntimeError("The synthetic invalidation check needs a nonterminal cursor.")
    try:
        search_pages_v2(
            engine,
            seed.context,
            _parsed({"keywords": [COMMON], "limit": 1, "cursor": cursor}),
            clock=lambda: AUTH_CLOCK,
            cursor_codec=CURSOR_CODEC,
        )
    except InvalidCursorError:
        return
    raise RuntimeError("A visible Revision update did not invalidate the old cursor.")


def validate_http_parameters(repeats: int, concurrency: int, fresh_process_runs: int) -> None:
    if not 1 <= repeats <= 100:
        raise ValueError("HTTP repeat count must be between 1 and 100.")
    if not 1 <= concurrency <= 4:
        raise ValueError("HTTP concurrency must be between 1 and 4.")
    if not 1 <= fresh_process_runs <= 5:
        raise ValueError("Fresh-process count must be between 1 and 5.")


def latency_summary(samples: list[float], wall_seconds: float) -> dict[str, int | float]:
    """Nearest-rank descriptive percentiles, including sample count and every sample."""

    if (
        not samples
        or any(not math.isfinite(value) or value < 0 for value in samples)
        or not math.isfinite(wall_seconds)
        or wall_seconds <= 0
    ):
        raise ValueError("Latency statistics require finite samples and positive elapsed time.")
    ordered = sorted(samples)
    return {
        "sample_count": len(ordered),
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "p99_ms": round(ordered[math.ceil(len(ordered) * 0.99) - 1], 3),
        "max_ms": round(ordered[-1], 3),
        "batch_wall_seconds": round(wall_seconds, 6),
        "throughput_rps": round(len(ordered) / wall_seconds, 3),
    }


def _server_environment(database_url: str) -> dict[str, str]:
    # No user Patchouli configuration, .env, credentials or service endpoints.
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("PATCHOULI_")
    }
    environment.update(
        PATCHOULI_DATABASE_URL=database_url,
        PATCHOULI_ENVIRONMENT="test",
        PYTHONPATH=str(ROOT / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET=CURSOR_SECRET,
    )
    return environment


@contextmanager
def _http_server(database_url: str, directory: Path) -> Iterator[int]:
    """Own one ephemeral-loopback server; never probe or terminate an existing listener."""

    process = subprocess.Popen(
        (sys.executable, "-c", _HTTP_SERVER),
        cwd=directory,
        env=_server_environment(database_url),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    announcements: queue.Queue[str] = queue.Queue()

    def read_port() -> None:
        assert process.stdout is not None
        announcements.put(process.stdout.readline())

    reader = threading.Thread(target=read_port, daemon=True)
    reader.start()
    try:
        try:
            announced = announcements.get(timeout=_SERVER_TIMEOUT_SECONDS).strip()
        except queue.Empty:
            raise RuntimeError("Synthetic HTTP server startup timed out.") from None
        if not announced.isascii() or not announced.isdecimal() or not 1 <= int(announced) <= 65535:
            raise RuntimeError("Synthetic HTTP server did not announce a loopback port.")
        port = int(announced)
        deadline = time.monotonic() + _SERVER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Synthetic HTTP server stopped before readiness.")
            connection = HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request("GET", "/health/ready")
                response = connection.getresponse()
                response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
                if response.status == 200:
                    break
            except OSError:
                pass
            finally:
                connection.close()
            time.sleep(0.05)
        else:
            raise RuntimeError("Synthetic HTTP server was not ready before the deadline.")
        yield port
    finally:
        if process.poll() is None:
            assert process.stdin is not None
            # EOF asks this owned server to stop gracefully, including on Windows.
            process.stdin.close()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        reader.join(timeout=1)
        process.stdout.close()


def _http_search(
    port: int,
    token: str,
    payload: dict[str, object],
    *,
    expected_status: int = 200,
    expected_code: str = "resource_not_found",
) -> tuple[float, tuple[tuple[str, str], ...], str | None]:
    # HTTPConnection goes directly to loopback, ignoring all HTTP proxy variables.
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = HTTPConnection("127.0.0.1", port, timeout=_HTTP_TIMEOUT_SECONDS)
    started = time.perf_counter_ns()
    try:
        connection.request(
            "POST",
            "/api/v1/search",
            body=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if response.status != expected_status or len(raw) > _MAX_HTTP_RESPONSE_BYTES:
            raise RuntimeError(
                f"Synthetic HTTP search status={response.status}, expected={expected_status}, "
                f"oversize={len(raw) > _MAX_HTTP_RESPONSE_BYTES}."
            )
        document = json.loads(raw)
        if expected_status != 200:
            if document.get("code") != expected_code:
                raise RuntimeError("Synthetic HTTP scope rejection was not explicit.")
            return elapsed_ms, (), None
        items = document["items"]
        identities = tuple((item["library_id"], item["page_id"]) for item in items)
        if not all(
            isinstance(library, str) and isinstance(page, str) for library, page in identities
        ):
            raise RuntimeError("Synthetic HTTP search returned invalid identities.")
        cursor = document["next_cursor"]
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise RuntimeError("Synthetic HTTP search returned an invalid cursor.")
        return elapsed_ms, identities, cursor
    finally:
        connection.close()


def _measure_http_queries(
    engine: Engine,
    database_url: str,
    directory: Path,
    seed: SeededMatrix,
    repeats: int,
    concurrency: int,
    fresh_process_runs: int,
) -> tuple[list[dict[str, object]], float, list[dict[str, object]]]:
    measurements: list[dict[str, object]] = []
    readable = frozenset(seed.libraries[:2])
    for name, base_payload in _queries(seed.libraries):
        payload = base_payload | {"limit": 20}
        expected = _oracle(seed.pages, readable, _parsed(payload))
        first_samples: list[float] = []
        began = time.perf_counter()
        for _ in range(fresh_process_runs):
            with _http_server(database_url, directory) as port:
                latency, actual, _cursor = _http_search(port, seed.token, payload)
                _check_result(actual, expected, name)
                first_samples.append(latency)
        measurements.append(
            {
                "name": name,
                "oracle_match_count": len(expected),
                "returned": min(20, len(expected)),
                "every_response_matches_independent_oracle": True,
                "fresh_process_first_request": latency_summary(
                    first_samples, time.perf_counter() - began
                ),
            }
        )
    with _http_server(database_url, directory) as port:
        for measurement, (name, base_payload) in zip(
            measurements, _queries(seed.libraries), strict=True
        ):
            payload = base_payload | {"limit": 20}
            expected = _oracle(seed.pages, readable, _parsed(payload))

            def sample(
                _number: int,
                payload: dict[str, object] = payload,
                expected: tuple[tuple[str, str], ...] = expected,
                name: str = name,
            ) -> float:
                latency, actual, _cursor = _http_search(port, seed.token, payload)
                _check_result(actual, expected, name)
                return latency

            sample(0)  # Explicit unmeasured warm-up; its result is still checked.
            began = time.perf_counter()
            warm = [sample(number) for number in range(repeats)]
            measurement["warm_serial"] = latency_summary(warm, time.perf_counter() - began)
            began = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as workers:
                concurrent = list(workers.map(sample, range(repeats * concurrency)))
            measurement["warm_concurrent"] = latency_summary(
                concurrent, time.perf_counter() - began
            )
        negative_payloads: tuple[dict[str, object], ...] = (
            {"keywords": [COMMON], "libraries": [seed.libraries[2]]},
            {"tags_any": [{"library_id": seed.libraries[2], "tag_id": SHARED_TAG}]},
        )
        for payload in negative_payloads:
            _http_search(port, seed.token, payload, expected_status=404)

        def fetch(payload: dict[str, object]) -> tuple[tuple[tuple[str, str], ...], str | None]:
            _latency, actual, cursor = _http_search(port, seed.token, payload)
            return actual, cursor

        pagination = [
            {"name": name}
            | _check_pagination(fetch, payload, _oracle(seed.pages, readable, _parsed(payload)))
            for name, payload in _pagination_queries()
        ]
        _latency, _actual, stale_cursor = _http_search(
            port, seed.token, {"keywords": [COMMON], "limit": 1}
        )
        if stale_cursor is None:
            raise RuntimeError("The HTTP invalidation check needs a nonterminal cursor.")
        update_ms, updated = _update_one(engine, seed)
        _http_search(
            port,
            seed.token,
            {"keywords": [COMMON], "limit": 1, "cursor": stale_cursor},
            expected_status=400,
            expected_code="invalid_cursor",
        )
        for name, keyword in (("old_rare_after_update", RARE), ("new_term_after_update", UPDATED)):
            payload = {"keywords": [keyword], "limit": 20}
            expected = _oracle(updated, readable, _parsed(payload))
            _, actual, _cursor = _http_search(port, seed.token, payload)
            _check_result(actual, expected, name)
    return measurements, update_ms, pagination


def run_http_matrix(
    pages: int,
    bytes_per_page: int,
    repeats: int,
    concurrency: int,
    fresh_process_runs: int,
) -> dict[str, Any]:
    resolve_dimensions(scale=False, pages=pages, bytes_per_page=bytes_per_page)
    validate_http_parameters(repeats, concurrency, fresh_process_runs)
    with TemporaryDirectory(prefix="patchouli-search-http-matrix-") as directory:
        temporary = Path(directory)
        database = temporary / "synthetic.sqlite"
        database_url = f"sqlite:///{database.as_posix()}"
        subprocess.run(
            (sys.executable, "-c", _HTTP_MIGRATE, str(ROOT / "alembic.ini")),
            cwd=temporary,
            env=_server_environment(database_url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=_SERVER_TIMEOUT_SECONDS,
        )
        engine = build_engine(database_url)
        try:
            began = time.perf_counter()
            seed = _seed(
                engine, pages, bytes_per_page, expires_at=time.time_ns() // 1_000 + 3_600_000_000
            )
            seed_ms = (time.perf_counter() - began) * 1_000
            began = time.perf_counter()
            rebuild_search_index(engine, clock=lambda: AUTH_CLOCK)
            rebuild_ms = (time.perf_counter() - began) * 1_000
            queries, update_ms, pagination = _measure_http_queries(
                engine, database_url, temporary, seed, repeats, concurrency, fresh_process_runs
            )
            database_mib = database.stat().st_size / 1_048_576
        finally:
            engine.dispose()
    return {
        "mode": "synthetic_real_http_search_v2_matrix",
        "status": "Experimental loopback HTTP measurement; not production acceptance",
        "provenance": "Deterministic synthetic content only; no private source or server",
        "pages": pages,
        "bytes_per_page": bytes_per_page,
        "library_count": 3,
        "readable_library_count": 2,
        "workers": 1,
        "concurrency": concurrency,
        "serial_requests_per_query": repeats,
        "concurrent_requests_per_query": repeats * concurrency,
        "fresh_process_runs": fresh_process_runs,
        "queries": queries,
        "pagination": pagination,
        "visible_update_invalidates_cursor": True,
        "oracle_note": "Equal-weight literal scan: each Page has exactly one of 16 OR terms "
        "in content.md. Field-specific scenarios are separate, not an unequal-score oracle.",
        "seed_ms": round(seed_ms, 3),
        "rebuild_ms": round(rebuild_ms, 3),
        "update_ms": round(update_ms, 3),
        "database_mib": round(database_mib, 3),
        "scope_rejections_checked": 2,
        "updated_terms_checked_over_http": 2,
        "latency_note": "Client time from connect/request through complete response body; "
        "includes real authentication, middleware and JSON response, not oracle verification. "
        "Post-response log persistence may finish after the client receives the body. "
        "Each request uses a fresh TCP connection; server and OS caches may remain warm.",
        "throughput_note": "Warm batch wall time includes client scheduling and result checks. "
        "Fresh-process batch wall time additionally includes server startup/readiness/shutdown; "
        "its throughput is whole process cycles, not steady-state HTTP throughput.",
        "cold_cache_note": "Fresh-process first search after readiness is NOT OS-cold cache. "
        "Seeding/rebuilding and health checks may warm caches; no system cache is evicted.",
        "percentile_note": "Nearest-rank descriptive samples; "
        "small-n p95/p99 are not robust tail estimates.",
        "limitations": "Equal-weight synthetic md/txt/bin fixture and 16-term OR; no human "
        "relevance, OS-cold measurement, TLS/proxy latency, resource peaks, concurrent content "
        "writers, production hardware or full acceptance. Initial writes/update use application "
        "cores; this is HTTP search, not HTTP upload performance. Request logging remains enabled.",
    }


def run_matrix(pages: int, bytes_per_page: int, repeats: int) -> dict[str, Any]:
    resolve_dimensions(scale=False, pages=pages, bytes_per_page=bytes_per_page)
    if not 1 <= repeats <= 20:
        raise ValueError("Repeat count must be between 1 and 20.")
    original_url = os.environ.get("PATCHOULI_DATABASE_URL")
    original_environment = os.environ.get("PATCHOULI_ENVIRONMENT")
    try:
        with TemporaryDirectory(prefix="patchouli-search-v2-matrix-") as directory:
            database = Path(directory) / "synthetic.sqlite"
            database_url = f"sqlite:///{database.as_posix()}"
            os.environ["PATCHOULI_DATABASE_URL"] = database_url
            os.environ["PATCHOULI_ENVIRONMENT"] = "test"
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
            engine = build_engine(database_url)
            try:
                started = time.perf_counter_ns()
                seed = _seed(engine, pages, bytes_per_page)
                seed_ms = (time.perf_counter_ns() - started) / 1_000_000
                started = time.perf_counter_ns()
                rebuild_search_index(engine, clock=lambda: AUTH_CLOCK)
                rebuild_ms = (time.perf_counter_ns() - started) / 1_000_000
                queries = _measure_queries(engine, database_url, seed, repeats)
                pagination = _service_pagination(engine, seed)
                stale_cursor = search_pages_v2(
                    engine,
                    seed.context,
                    _parsed({"keywords": [COMMON], "limit": 1}),
                    clock=lambda: AUTH_CLOCK,
                    cursor_codec=CURSOR_CODEC,
                ).next_cursor
                update_ms, _updated_pages = _update_one(engine, seed)
                _require_update_invalidates_cursor(engine, seed, stale_cursor)
                database_mib = database.stat().st_size / 1_048_576
            finally:
                engine.dispose()
    finally:
        for key, original in (
            ("PATCHOULI_DATABASE_URL", original_url),
            ("PATCHOULI_ENVIRONMENT", original_environment),
        ):
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
    return {
        "mode": "synthetic_real_search_v2_matrix",
        "status": "Experimental local measurement; not production acceptance",
        "provenance": "Deterministic synthetic content only; no private source or server",
        "pages": pages,
        "bytes_per_page": bytes_per_page,
        "library_count": 3,
        "readable_library_count": 2,
        "queries": queries,
        "pagination": pagination,
        "visible_update_invalidates_cursor": True,
        "oracle_note": "Equal-weight literal scan: each Page has exactly one of 16 OR terms "
        "in content.md. Field-specific scenarios are separate, not an unequal-score oracle.",
        "seed_ms": round(seed_ms, 3),
        "rebuild_ms": round(rebuild_ms, 3),
        "update_ms": round(update_ms, 3),
        "database_mib": round(database_mib, 3),
        "new_connection_note": "Fresh DBAPI connection; operating-system file cache may be warm.",
        "limitations": (
            "Only deterministic synthetic Pages holding md/txt/bin file sets and 16-term OR, "
            "one real file-set Revision update, and one Agent credential. "
            "The oracle checks Top 20 plus three complete signed cursor chains, not human "
            "relevance, HTTP overhead, concurrency, OS-cold latency, "
            "real diverse content, backups, or production performance."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", action="store_true", help="Explicitly run 5000 Pages x 10 KiB.")
    parser.add_argument("--pages", type=int, default=None)
    parser.add_argument("--bytes-per-page", type=int, default=None)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--http", action="store_true", help="Measure actual ephemeral-loopback HTTP search."
    )
    parser.add_argument(
        "--concurrency", type=int, default=None, help="HTTP clients: 1 to 4 (default 4)."
    )
    parser.add_argument(
        "--fresh-process-runs",
        type=int,
        default=None,
        help="First requests per query: 1 to 5 (default 3).",
    )
    args = parser.parse_args()
    try:
        count, size = resolve_dimensions(
            scale=args.scale, pages=args.pages, bytes_per_page=args.bytes_per_page
        )
        if args.http:
            report = run_http_matrix(
                count,
                size,
                args.repeats,
                4 if args.concurrency is None else args.concurrency,
                3 if args.fresh_process_runs is None else args.fresh_process_runs,
            )
        else:
            if args.concurrency is not None or args.fresh_process_runs is not None:
                raise ValueError("HTTP-specific options require --http.")
            report = run_matrix(count, size, args.repeats)
    except ValueError as error:
        parser.error(str(error))
    except Exception as error:
        # Do not publish child logs, response bodies or exceptions with SQL-bound values.
        print(f"Synthetic matrix failed safely ({type(error).__name__}).", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
