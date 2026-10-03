"""Agent-prelabelled relevance regression over real, public repository documents.

These eleven intents and primary sources were selected by reading the eight
allowlisted documents before running search. Each distinct original is one
Page, without repeated padding, synthetic keyword injection or a score oracle.
This is not human-confirmed relevance, a representative corpus, HTTP/latency
acceptance or evidence that 5,000 real documents have been evaluated.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import seed_library_structure
from sqlalchemy import Engine
from test_service_v2 import CLOCK, _agent, _grant, _opt_in

from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_create_core import FileSetPageCreateCore
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveSourceInput
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.search.query_v2 import SearchQueryV2
from patchouli_lib.search.service_v2 import search_pages_v2

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "docs/search-index-maintenance.md",
    "docs/proposals/master-web-revision-restore.md",
    "docs/03-page-revision-and-history.md",
    "docs/06-identifiers-and-references.md",
    "docs/admin-web-console.md",
    "skills/patchouli-agent/references/http.md",
    "clients/python/README.md",
    "LICENSE",
)


@dataclass(frozen=True, slots=True)
class Intent:
    name: str
    keywords: tuple[str, ...]
    primary_source: str
    reason: str


# Source-based judgments, fixed before the first query. Top 5 is a retrieval
# usefulness gate, not a demand that the product reproduce a numerical score.
INTENTS = (
    Intent(
        "index_rebuild",
        ("patchouli-search-index", "rebuild"),
        SOURCES[0],
        "Explains the actual maintenance command and migration/rebuild boundary.",
    ),
    Intent(
        "restore_historical_group",
        ("恢复此版本", "完整文件组"),
        SOURCES[1],
        "Dedicated proposal for restoring a complete historical snapshot as a new Revision.",
    ),
    Intent(
        "correct_declared_time",
        ("声明时间校正",),
        SOURCES[2],
        "Dedicated occurrence correction section specifies stable identity and no new Revision.",
    ),
    Intent(
        "page_id_and_title",
        ("Page ID", "标题"),
        SOURCES[3],
        "Defines stable Page identity separately from its human-readable title.",
    ),
    Intent(
        "web_csrf",
        ("CSRF",),
        SOURCES[4],
        "Explains signed browser sessions, same-origin submissions and CSRF checks.",
    ),
    Intent(
        "raster_orientation",
        ("PNG", "EXIF"),
        SOURCES[4],
        "Safe preview section specifies PNG/JPEG derivation and preserved EXIF orientation.",
    ),
    Intent(
        "file_upload_precondition",
        ("multipart/form-data", "If-Match"),
        SOURCES[5],
        "Unified file-set instructions specify multipart parts and revision preconditions.",
    ),
    Intent(
        "invalid_search_cursor",
        ("invalid_cursor",),
        SOURCES[5],
        "HTTP reference explains the error and restarting from the first search page.",
    ),
    Intent(
        "local_master_recovery",
        ("patchouli-master-token", "recover --confirm-local-reset"),
        SOURCES[4],
        "Describes local recovery and invalidation of old master sessions.",
    ),
    Intent(
        "private_http_client",
        ("allow_private_http",),
        SOURCES[6],
        "Client configuration documents explicit private HTTP opt-in and endpoint restrictions.",
    ),
    Intent(
        "software_license",
        ("MIT License",),
        SOURCES[7],
        "The repository license itself supplies the permission and warranty terms.",
    ),
)


def _normalize(text: str) -> str:
    # Independent Unicode contract, not a product extraction/ranking helper.
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


@dataclass(frozen=True, slots=True)
class Document:
    source: str
    page_id: str
    filename: str
    original: bytes
    revision_id: str


@pytest.fixture(scope="module")
def public_corpus(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Engine, AuthenticatedRequestContext, tuple[Document, ...]]]:
    directory = tmp_path_factory.mktemp("public-document-relevance")
    database_url = f"sqlite:///{(directory / 'public-documents.sqlite').as_posix()}"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("PATCHOULI_DATABASE_URL", database_url)
        environment.setenv("PATCHOULI_ENVIRONMENT", "test")
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        context = _agent(engine, library_id, section_id)
        _opt_in(engine, library_id)
        _grant(engine, library_id, library_id, "read")
        documents = []
        seen: set[bytes] = set()
        with immediate_transaction(engine) as connection:
            book = ContentRepository(connection).get_book(library_id, book_id)
            assert book is not None
            for number, source in enumerate(SOURCES, start=1):
                original = (ROOT / source).read_bytes()
                digest = hashlib.sha256(original).digest()
                assert digest not in seen, "The allowlist must not count duplicate documents."
                seen.add(digest)
                text = original.decode("utf-8", errors="strict")
                title = text.splitlines()[0].removeprefix("# ").strip()
                filename = "LICENSE.txt" if source == "LICENSE" else Path(source).name
                page_uid = number.to_bytes(16, "big")
                revision_id = f"rev_{number:032x}"

                def source_id(number: int = number) -> str:
                    return f"{number:032x}"

                def identity(page_uid: bytes = page_uid) -> bytes:
                    return page_uid

                def revision_identity(revision_id: str = revision_id) -> str:
                    return revision_id

                page = FileSetPageCreateCore(
                    connection,
                    id_factory=source_id,
                    page_uid_factory=identity,
                    revision_id_factory=revision_identity,
                ).create_page(
                    book=book,
                    title=title,
                    occurred_at=1_700_000_000_000_000,
                    operation_at=2_000_000,
                    manifest=build_file_manifest(((filename, original),)),
                    source=ArchiveSourceInput(kind="public_repository_document"),
                )
                documents.append(Document(source, page.page_id, filename, original, revision_id))
        rebuild_search_index(engine, clock=lambda: CLOCK)
        yield engine, context, tuple(documents)
    finally:
        engine.dispose()


@pytest.mark.parametrize("intent", INTENTS, ids=lambda intent: intent.name)
def test_predeclared_primary_source_is_useful_and_excerpt_is_verifiable(
    public_corpus: tuple[Engine, AuthenticatedRequestContext, tuple[Document, ...]],
    intent: Intent,
) -> None:
    engine, context, documents = public_corpus
    by_page = {document.page_id: document for document in documents}
    primary = next(document for document in documents if document.source == intent.primary_source)
    needles = tuple(_normalize(keyword) for keyword in intent.keywords)
    # All declared terms actually occur in the chosen original, independently
    # of the search outcome; a stale document needs explicit re-annotation.
    assert all(needle in _normalize(primary.original.decode()) for needle in needles)
    result = search_pages_v2(
        engine,
        context,
        SearchQueryV2(intent.keywords, (), None, None, None, 5),
        clock=lambda: CLOCK,
    )
    identities = [item.page_id for item in result.items]
    assert len(identities) == len(set(identities))
    assert primary.page_id in identities, (
        f"Predeclared primary source missing from Top 5: {intent.name}: "
        f"{intent.primary_source}; returned {[by_page[page_id].source for page_id in identities]}"
    )
    for item in result.items:
        document = by_page[item.page_id]
        assert item.revision_id == document.revision_id
        assert item.revision_number == 1
        snippet = item.snippet
        assert snippet is not None
        assert snippet.file_name == document.filename
        assert 0 < len(snippet.text) <= 240
        assert snippet.text in _normalize(document.original.decode())
        assert snippet.matched == any(needle in snippet.text for needle in needles)
