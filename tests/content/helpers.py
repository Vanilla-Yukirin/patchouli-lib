from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Connection, Engine, insert

from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageSource,
    Revision,
)
from patchouli_lib.content.schemas import (
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import OccurrenceTime, parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService


def seed_library_structure(
    engine: Engine,
    *,
    prefix: str = "1",
    label: str = "First",
) -> tuple[str, str, str]:
    identifiers: Iterator[str] = iter(
        (
            prefix * 32,
            format(int(prefix, 16) + 1, "x") * 32,
            format(int(prefix, 16) + 2, "x") * 32,
        )
    )
    with immediate_transaction(engine) as connection:
        result = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name=f"{label} Synthetic Library",
                section_name=f"{label} Synthetic Section",
                book_name=f"{label} Synthetic Book",
            )
        )
    return result.library.id, result.section.id, result.book.id


def page_graph_values(
    *,
    library_id: str,
    section_id: str,
    book_id: str,
    page_byte: int = 0x11,
    revision_hex: str = "22",
    source_hex: str = "3",
    title: str = "Synthetic Archive",
    occurrence_wire: str = "2026-08-13T10:00:00.123456Z",
    collision_ordinal: int = 1,
    content_md: bytes = b"# Synthetic archive\r\n\r\nExact bytes.\n",
) -> tuple[NewPage, NewRevision, NewPageIdentifier, NewPageIdCollisionCounter, NewPageSource]:
    occurrence: OccurrenceTime = parse_occurrence_time(occurrence_wire)
    generated = generate_page_id(
        occurrence,
        title,
        collision_ordinal=collision_ordinal,
    )
    page_uid = bytes([page_byte]) * 16
    revision_id = f"rev_{revision_hex * 16}"
    stored_content = MarkdownContent.from_bytes(content_md)
    page = NewPage(
        library_id=library_id,
        page_uid=page_uid,
        section_id=section_id,
        book_id=book_id,
        page_id=generated.value,
        id_scheme=PAGE_ID_SCHEME,
        id_timestamp_micros=(occurrence.utc_microseconds // 1_000) * 1_000,
        base_slug=generated.base_slug,
        collision_ordinal=collision_ordinal,
        title=title,
        page_type="archive",
        occurred_at=occurrence.utc_microseconds,
        current_revision_id=revision_id,
        current_revision_number=1,
        created_at=2_000_000,
        updated_at=2_000_000,
    )
    revision = NewRevision(
        library_id=library_id,
        revision_id=revision_id,
        page_uid=page_uid,
        revision_number=1,
        created_at=2_000_000,
        **stored_content.model_dump(),
    )
    identifier = NewPageIdentifier(
        library_id=library_id,
        identifier_digest=page_id_registry_digest(generated.value),
        identifier_text=generated.value,
        id_scheme=PAGE_ID_SCHEME,
        identifier_kind="canonical",
        page_uid=page_uid,
        created_at=2_000_000,
    )
    counter = NewPageIdCollisionCounter(
        library_id=library_id,
        id_scheme=PAGE_ID_SCHEME,
        id_timestamp_micros=(occurrence.utc_microseconds // 1_000) * 1_000,
        base_slug=generated.base_slug,
        next_ordinal=max(2, collision_ordinal + 1),
    )
    source = NewPageSource(
        library_id=library_id,
        source_id=source_hex * 32,
        page_uid=page_uid,
        revision_id=revision_id,
        revision_number=1,
        kind="synthetic",
        locator="urn:synthetic:archive",
        captured_at=occurrence.utc_microseconds,
        created_at=2_000_000,
    )
    return page, revision, identifier, counter, source


def insert_page_graph(
    connection: Connection,
    values: tuple[
        NewPage,
        NewRevision,
        NewPageIdentifier,
        NewPageIdCollisionCounter,
        NewPageSource,
    ],
    *,
    include_counter: bool = True,
    include_source: bool = True,
) -> None:
    page, revision, identifier, counter, source = values
    connection.execute(insert(Page), page.model_dump())
    connection.execute(insert(Revision), revision.model_dump())
    connection.execute(insert(PageIdentifier), identifier.model_dump())
    if include_counter:
        connection.execute(insert(PageIdCollisionCounter), counter.model_dump())
    if include_source:
        connection.execute(insert(PageSource), source.model_dump())
