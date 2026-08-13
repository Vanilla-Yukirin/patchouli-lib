from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuthenticatedCaller,
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
    credential_metadata,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewRevision,
)
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.identifiers import (
    PAGE_ID_SCHEME,
    generate_page_id,
    page_id_registry_digest,
    parse_occurrence_time,
)
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewBook, NewSection
from patchouli_lib.library.service import LibrarySeedService

LIBRARY_ID = "1" * 32
QUERY_SECTION_ID = "2" * 32
QUERY_BOOK_ID = "3" * 32
SECOND_QUERY_SECTION_ID = "4" * 32
SECOND_QUERY_BOOK_ID = "5" * 32
READ_SECTION_ID = "6" * 32
READ_BOOK_ID = "7" * 32
HIDDEN_SECTION_ID = "8" * 32
HIDDEN_BOOK_ID = "9" * 32
CALLER_ID = "a" * 32
CREDENTIAL_ID = "b" * 32
EXTRA_QUERY_BOOK_ID = "c" * 32
STORED_AT = 2_000_000


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    library_id: str
    query_section_id: str
    query_book_id: str
    second_query_section_id: str
    read_section_id: str
    hidden_section_id: str
    authenticated: AuthenticatedCaller
    first_page_id: str
    first_page_alias: str
    first_page_uid: bytes
    second_page_id: str
    read_page_id: str
    hidden_page_id: str
    deleted_page_id: str
    first_revision_id: str
    second_revision_id: str
    current_content: str
    historical_content: str


@pytest.fixture
def retrieval_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'retrieval.db').as_posix()}")
    from patchouli_lib.models import Base

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _add_page(
    repository: ContentRepository,
    *,
    section_id: str,
    book_id: str,
    page_byte: int,
    title: str,
    occurrence: str,
    revision_hex: str,
    content: bytes,
    deleted_at: int | None = None,
) -> tuple[str, bytes, str]:
    parsed = parse_occurrence_time(occurrence)
    generated = generate_page_id(parsed, title)
    page_uid = bytes([page_byte]) * 16
    revision_id = f"rev_{revision_hex * 32}"
    markdown = MarkdownContent.from_bytes(content)
    page = repository.add_page(
        NewPage(
            library_id=LIBRARY_ID,
            page_uid=page_uid,
            section_id=section_id,
            book_id=book_id,
            page_id=generated.value,
            id_scheme=PAGE_ID_SCHEME,
            id_timestamp_micros=(parsed.utc_microseconds // 1_000) * 1_000,
            base_slug=generated.base_slug,
            collision_ordinal=1,
            title=title,
            page_type="archive",
            occurred_at=parsed.utc_microseconds,
            current_revision_id=revision_id,
            current_revision_number=1,
            created_at=STORED_AT,
            updated_at=STORED_AT if deleted_at is None else deleted_at,
            deleted_at=deleted_at,
        )
    )
    repository.add_revision(
        NewRevision(
            library_id=LIBRARY_ID,
            revision_id=revision_id,
            page_uid=page_uid,
            revision_number=1,
            created_at=STORED_AT,
            **markdown.model_dump(),
        )
    )
    repository.add_identifier(
        NewPageIdentifier(
            library_id=LIBRARY_ID,
            identifier_digest=page_id_registry_digest(page.page_id),
            identifier_text=page.page_id,
            id_scheme=PAGE_ID_SCHEME,
            identifier_kind="canonical",
            page_uid=page_uid,
            created_at=STORED_AT,
        )
    )
    repository.add_collision_counter(
        NewPageIdCollisionCounter(
            library_id=LIBRARY_ID,
            id_scheme=PAGE_ID_SCHEME,
            id_timestamp_micros=(parsed.utc_microseconds // 1_000) * 1_000,
            base_slug=generated.base_slug,
            next_ordinal=2,
        )
    )
    return page.page_id, page_uid, revision_id


@pytest.fixture
def retrieval_scope(retrieval_engine: Engine) -> RetrievalScope:
    with immediate_transaction(retrieval_engine) as connection:
        identifiers = iter((LIBRARY_ID, QUERY_SECTION_ID, QUERY_BOOK_ID))
        seeded = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Retrieval Library",
                section_name="Alpha Query Section",
                book_name="Alpha Query Book",
            )
        )
        assert seeded.library.id == LIBRARY_ID
        library = LibraryRepository(connection)
        for section_id, name, book_id in (
            (SECOND_QUERY_SECTION_ID, "Beta Query Section", SECOND_QUERY_BOOK_ID),
            (READ_SECTION_ID, "Read Only Section", READ_BOOK_ID),
            (HIDDEN_SECTION_ID, "Hidden Section", HIDDEN_BOOK_ID),
        ):
            library.add_section(
                NewSection(
                    id=section_id,
                    library_id=LIBRARY_ID,
                    name=name,
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
            library.add_book(
                NewBook(
                    id=book_id,
                    library_id=LIBRARY_ID,
                    section_id=section_id,
                    name=f"{name} Book",
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
        library.add_book(
            NewBook(
                id=EXTRA_QUERY_BOOK_ID,
                library_id=LIBRARY_ID,
                section_id=QUERY_SECTION_ID,
                name="Beta Query Book",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )

        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id=CALLER_ID,
                library_id=LIBRARY_ID,
                kind=CallerKind.AGENT,
                name="Synthetic Retrieval Agent",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        issued = generate_token()
        credential = auth.add_credential(
            NewCredential(
                id=CREDENTIAL_ID,
                library_id=LIBRARY_ID,
                caller_id=CALLER_ID,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        for section_id, action in (
            (QUERY_SECTION_ID, SectionAction.QUERY),
            (QUERY_SECTION_ID, SectionAction.PAGE_READ),
            (SECOND_QUERY_SECTION_ID, SectionAction.QUERY),
            (READ_SECTION_ID, SectionAction.PAGE_READ),
        ):
            auth.add_grant(
                NewSectionGrant(
                    library_id=LIBRARY_ID,
                    caller_id=CALLER_ID,
                    section_id=section_id,
                    action=action,
                    created_at=1_000_000,
                )
            )

        content = ContentRepository(connection)
        historical = "# Historical\n\n<script>alert('stored')</script>\n"
        first_page_id, first_page_uid, first_revision_id = _add_page(
            content,
            section_id=QUERY_SECTION_ID,
            book_id=QUERY_BOOK_ID,
            page_byte=0x11,
            title="Alpha Archive",
            occurrence="2026-08-13T10:00:00.123456Z",
            revision_hex="1",
            content=historical.encode(),
        )
        second_markdown = MarkdownContent.from_bytes(b"# Current\n\nExact **Markdown**.\n")
        second_revision_id = f"rev_{'2' * 32}"
        second_revision = content.add_revision(
            NewRevision(
                library_id=LIBRARY_ID,
                revision_id=second_revision_id,
                page_uid=first_page_uid,
                revision_number=2,
                created_at=3_000_000,
                **second_markdown.model_dump(),
            )
        )
        first_page = content.get_page(LIBRARY_ID, first_page_id)
        assert first_page is not None
        first_page_alias = generate_page_id(
            parse_occurrence_time("2026-08-13T09:59:59.000000Z"),
            "Synthetic Alpha Alias",
        ).value
        content.add_identifier(
            NewPageIdentifier(
                library_id=LIBRARY_ID,
                identifier_digest=page_id_registry_digest(first_page_alias),
                identifier_text=first_page_alias,
                id_scheme=PAGE_ID_SCHEME,
                identifier_kind="alias",
                page_uid=first_page_uid,
                created_at=STORED_AT,
            )
        )
        assert (
            content.advance_current_revision(
                first_page,
                second_revision,
                updated_at=3_000_000,
            )
            is not None
        )
        second_page_id, _, _ = _add_page(
            content,
            section_id=QUERY_SECTION_ID,
            book_id=QUERY_BOOK_ID,
            page_byte=0x22,
            title="Beta Archive",
            occurrence="2026-08-13T10:00:01.123456Z",
            revision_hex="3",
            content=b"# Beta\n",
        )
        deleted_page_id, _, _ = _add_page(
            content,
            section_id=QUERY_SECTION_ID,
            book_id=QUERY_BOOK_ID,
            page_byte=0x33,
            title="Deleted Archive",
            occurrence="2026-08-13T10:00:02.123456Z",
            revision_hex="4",
            content=b"# Deleted\n",
            deleted_at=4_000_000,
        )
        read_page_id, _, _ = _add_page(
            content,
            section_id=READ_SECTION_ID,
            book_id=READ_BOOK_ID,
            page_byte=0x44,
            title="Read Only Archive",
            occurrence="2026-08-13T10:00:03.123456Z",
            revision_hex="5",
            content=b"# Read only\n",
        )
        hidden_page_id, _, _ = _add_page(
            content,
            section_id=HIDDEN_SECTION_ID,
            book_id=HIDDEN_BOOK_ID,
            page_byte=0x55,
            title="Hidden Archive",
            occurrence="2026-08-13T10:00:04.123456Z",
            revision_hex="6",
            content=b"# Hidden\n",
        )

    return RetrievalScope(
        library_id=LIBRARY_ID,
        query_section_id=QUERY_SECTION_ID,
        query_book_id=QUERY_BOOK_ID,
        second_query_section_id=SECOND_QUERY_SECTION_ID,
        read_section_id=READ_SECTION_ID,
        hidden_section_id=HIDDEN_SECTION_ID,
        authenticated=AuthenticatedCaller(
            caller=caller,
            credential=credential_metadata(credential),
        ),
        first_page_id=first_page_id,
        first_page_alias=first_page_alias,
        first_page_uid=first_page_uid,
        second_page_id=second_page_id,
        read_page_id=read_page_id,
        hidden_page_id=hidden_page_id,
        deleted_page_id=deleted_page_id,
        first_revision_id=first_revision_id,
        second_revision_id=second_revision_id,
        current_content=second_markdown.content_md.decode(),
        historical_content=historical,
    )
