import hashlib

import pytest
from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.content.models import (
    EXHAUSTED_COLLISION_ORDINAL,
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageSource,
    Revision,
)
from patchouli_lib.content.schemas import (
    MarkdownContent,
    NewPageIdentifier,
    NewRevision,
    PageRecord,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time

from .helpers import insert_page_graph, page_graph_values, seed_library_structure


def test_page_and_current_revision_must_commit_as_one_valid_shape(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    page, revision, identifier, _, _ = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Page), page.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Page)) == 0

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Revision), revision.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 0

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Page), page.model_dump())
        connection.execute(insert(Revision), revision.model_dump())

    with immediate_transaction(content_engine) as connection:
        connection.execute(insert(Page), page.model_dump())
        connection.execute(insert(Revision), revision.model_dump())
        connection.execute(insert(PageIdentifier), identifier.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(Page.current_revision_id)) == revision.revision_id
        assert connection.scalar(select(Page.current_revision_number)) == 1
        assert connection.scalar(select(Revision.revision_number)) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_canonical_registry_kind_must_match_the_page(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    page, revision, identifier, _, _ = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    alias_only = identifier.model_copy(update={"identifier_kind": "alias"})

    with (
        pytest.raises(IntegrityError, match="canonical|kind"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(insert(Page), page.model_dump())
        connection.execute(insert(Revision), revision.model_dump())
        connection.execute(insert(PageIdentifier), alias_only.model_dump())


def test_initial_page_current_revision_must_be_number_one(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    page, revision, identifier, _, _ = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    invalid_page = page.model_copy(update={"current_revision_number": 2})
    invalid_revision = revision.model_copy(update={"revision_number": 2})

    with (
        pytest.raises(IntegrityError, match="must be 1"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(insert(Page), invalid_page.model_dump())
        connection.execute(insert(Revision), invalid_revision.model_dump())
        connection.execute(insert(PageIdentifier), identifier.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Page)) == 0
        assert connection.scalar(select(func.count()).select_from(Revision)) == 0


def test_current_revision_must_belong_to_the_same_page(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    first = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    second = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        page_byte=0x44,
        revision_hex="55",
        source_hex="6",
        title="Second Synthetic Archive",
        occurrence_wire="2026-08-13T10:00:01Z",
    )
    first_page, first_revision, first_identifier = first[0], first[1], first[2]
    second_page, second_revision, second_identifier = second[0], second[1], second[2]

    mismatched = first_page.model_copy(update={"current_revision_id": second_revision.revision_id})
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Page), mismatched.model_dump())
        connection.execute(insert(Revision), first_revision.model_dump())
        connection.execute(insert(PageIdentifier), first_identifier.model_dump())
        connection.execute(insert(Page), second_page.model_dump())
        connection.execute(insert(Revision), second_revision.model_dump())
        connection.execute(insert(PageIdentifier), second_identifier.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Page)) == 0
        assert connection.scalar(select(func.count()).select_from(Revision)) == 0


def test_page_cannot_cross_library_section_or_book_scope(content_engine: Engine) -> None:
    first_library, first_section, first_book = seed_library_structure(content_engine)
    second_library, _, _ = seed_library_structure(
        content_engine,
        prefix="4",
        label="Second",
    )
    page, revision, identifier, _, _ = page_graph_values(
        library_id=second_library,
        section_id=first_section,
        book_id=first_book,
    )

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Page), page.model_dump())
        connection.execute(insert(Revision), revision.model_dump())
        connection.execute(insert(PageIdentifier), identifier.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Page)) == 0
        assert connection.scalar(select(func.count()).select_from(Revision)) == 0
        assert connection.scalar(select(func.count()).where(Page.library_id == first_library)) == 0


def test_revision_rows_are_immutable_and_numbers_are_page_local(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page, revision = values[0], values[1]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)

    with (
        pytest.raises(IntegrityError, match="immutable"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            update(Revision)
            .where(
                Revision.library_id == library_id,
                Revision.revision_id == revision.revision_id,
            )
            .values(content_md=b"changed", content_size_bytes=7)
        )

    with (
        pytest.raises(IntegrityError, match="immutable"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            delete(Revision).where(
                Revision.library_id == library_id,
                Revision.revision_id == revision.revision_id,
            )
        )

    duplicate_number = NewRevision(
        **{
            **revision.model_dump(),
            "revision_id": "rev_" + "77" * 16,
            "content_sha256": hashlib.sha256(revision.content_md).digest(),
        }
    )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Revision), duplicate_number.model_dump())

    with content_engine.connect() as connection:
        stored = connection.execute(
            select(Revision.content_md, Revision.revision_number).where(
                Revision.library_id == library_id,
                Revision.revision_id == revision.revision_id,
            )
        ).one()
        assert stored == (revision.content_md, 1)
        assert connection.scalar(select(Page.current_revision_id)) == page.current_revision_id


def test_skipped_and_out_of_order_revision_inserts_fail(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page = values[0]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)

    content = MarkdownContent.from_bytes(b"# Sequential synthetic content\n")
    skipped = NewRevision(
        library_id=library_id,
        revision_id=f"rev_{'33' * 16}",
        page_uid=page.page_uid,
        revision_number=3,
        created_at=3_000_000,
        **content.model_dump(),
    )
    with (
        pytest.raises(IntegrityError, match="sequentially"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(insert(Revision), skipped.model_dump())

    second = skipped.model_copy(update={"revision_id": f"rev_{'44' * 16}", "revision_number": 2})
    with immediate_transaction(content_engine) as connection:
        connection.execute(insert(Revision), second.model_dump())
        assert (
            connection.exec_driver_sql(
                "SELECT revision_number FROM page_revision_append_guards"
            ).scalar_one()
            == 2
        )
        with pytest.raises(IntegrityError, match="cannot be discarded"):
            connection.exec_driver_sql("DELETE FROM page_revision_append_guards")
        with pytest.raises(IntegrityError, match="immutable"):
            connection.exec_driver_sql("UPDATE page_revision_append_guards SET revision_number = 3")
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_uid == page.page_uid)
            .values(
                current_revision_id=second.revision_id,
                current_revision_number=second.revision_number,
                updated_at=3_000_000,
            )
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM page_revision_append_guards"
            ).scalar_one()
            == 0
        )

    for number, revision_hex in ((1, "55"), (2, "66")):
        out_of_order = skipped.model_copy(
            update={"revision_id": f"rev_{revision_hex * 16}", "revision_number": number}
        )
        with (
            pytest.raises(IntegrityError, match="sequentially"),
            immediate_transaction(content_engine) as connection,
        ):
            connection.execute(insert(Revision), out_of_order.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 2
        assert connection.scalar(select(Page.current_revision_number)) == 2
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM page_revision_append_guards"
            ).scalar_one()
            == 0
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_revision_append_without_pointer_advance_rolls_back_at_commit(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page = values[0]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)

    content = MarkdownContent.from_bytes(b"# Pending synthetic content\n")
    second = NewRevision(
        library_id=library_id,
        revision_id=f"rev_{'33' * 16}",
        page_uid=page.page_uid,
        revision_number=2,
        created_at=3_000_000,
        **content.model_dump(),
    )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Revision), second.model_dump())

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.scalar(select(Page.current_revision_number)) == 1
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM page_revision_append_guards"
            ).scalar_one()
            == 0
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize(
    "replacement",
    [
        {"revision_id": "rev_" + "A" * 32},
        {"revision_number": 0},
        {"content_md": b"", "content_size_bytes": 0},
        {"content_md": b"synthetic\x00body", "content_size_bytes": 14},
        {"content_size_bytes": 1},
        {"content_sha256": b"short"},
    ],
)
def test_revision_database_constraints_reject_malformed_storage(
    content_engine: Engine,
    replacement: dict[str, object],
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    page, revision, _, _, _ = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    invalid_revision = {**revision.model_dump(), **replacement}

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(Page), page.model_dump())
        connection.execute(insert(Revision), invalid_revision)

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Page)) == 0
        assert connection.scalar(select(func.count()).select_from(Revision)) == 0


def test_registry_shares_canonical_alias_namespace_and_fails_closed_on_digest_collision(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page, _, canonical = values[0], values[1], values[2]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)

    alias_occurrence = parse_occurrence_time("2026-08-13T11:00:00Z")
    alias_text = generate_page_id(alias_occurrence, "Synthetic Alias").value
    alias = NewPageIdentifier(
        library_id=library_id,
        identifier_digest=page_id_registry_digest(alias_text),
        identifier_text=alias_text,
        id_scheme=PAGE_ID_SCHEME,
        identifier_kind="alias",
        page_uid=page.page_uid,
        created_at=3_000_000,
    )
    with immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageIdentifier), alias.model_dump())

    second_canonical_text = generate_page_id(
        parse_occurrence_time("2026-08-13T12:00:00Z"),
        "Second Canonical",
    ).value
    second_canonical = NewPageIdentifier(
        library_id=library_id,
        identifier_digest=page_id_registry_digest(second_canonical_text),
        identifier_text=second_canonical_text,
        id_scheme=PAGE_ID_SCHEME,
        identifier_kind="canonical",
        page_uid=page.page_uid,
        created_at=4_000_000,
    )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageIdentifier), second_canonical.model_dump())

    collision_text = generate_page_id(
        parse_occurrence_time("2026-08-13T13:00:00Z"),
        "Digest Collision",
    ).value
    collision = {
        **canonical.model_dump(),
        "identifier_text": collision_text,
        "identifier_kind": "alias",
    }
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageIdentifier), collision)

    with content_engine.connect() as connection:
        rows = [
            (row.identifier_kind, row.page_uid)
            for row in connection.execute(
                select(PageIdentifier.identifier_kind, PageIdentifier.page_uid).where(
                    PageIdentifier.library_id == library_id
                )
            )
        ]
        assert set(rows) == {("alias", page.page_uid), ("canonical", page.page_uid)}


def test_identifier_and_revision_values_are_library_scoped(content_engine: Engine) -> None:
    first = seed_library_structure(content_engine)
    second = seed_library_structure(content_engine, prefix="4", label="Second")
    first_values = page_graph_values(
        library_id=first[0],
        section_id=first[1],
        book_id=first[2],
    )
    second_values = page_graph_values(
        library_id=second[0],
        section_id=second[1],
        book_id=second[2],
    )

    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, first_values)
        insert_page_graph(connection, second_values)

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Page)) == 2
        assert connection.scalar(select(func.count()).select_from(Revision)) == 2
        assert connection.scalar(select(func.count()).select_from(PageIdentifier)) == 2


def test_collision_counter_is_scoped_monotonic_and_has_explicit_exhaustion(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    counter = values[3]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)

    with immediate_transaction(content_engine) as connection:
        connection.execute(
            update(PageIdCollisionCounter)
            .where(
                PageIdCollisionCounter.library_id == library_id,
                PageIdCollisionCounter.id_timestamp_micros == counter.id_timestamp_micros,
                PageIdCollisionCounter.base_slug == counter.base_slug,
            )
            .values(next_ordinal=EXHAUSTED_COLLISION_ORDINAL)
        )

    with (
        pytest.raises(IntegrityError, match="monotonic"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            update(PageIdCollisionCounter)
            .where(PageIdCollisionCounter.library_id == library_id)
            .values(next_ordinal=2)
        )

    with (
        pytest.raises(IntegrityError, match="monotonic"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            update(PageIdCollisionCounter)
            .where(PageIdCollisionCounter.library_id == library_id)
            .values(base_slug="moved-scope")
        )

    with (
        pytest.raises(IntegrityError, match="permanent"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            delete(PageIdCollisionCounter).where(PageIdCollisionCounter.library_id == library_id)
        )

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(
            insert(PageIdCollisionCounter),
            {
                **counter.model_dump(),
                "base_slug": "other",
                "next_ordinal": EXHAUSTED_COLLISION_ORDINAL + 1,
            },
        )

    with content_engine.connect() as connection:
        assert connection.scalar(select(PageIdCollisionCounter.next_ordinal)) == (
            EXHAUSTED_COLLISION_ORDINAL
        )


def test_source_locator_is_not_a_deduplication_key(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    source = values[4]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
        connection.execute(
            insert(PageSource),
            {
                **source.model_dump(),
                "source_id": "4" * 32,
            },
        )

    with content_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(PageSource)
                .where(PageSource.locator == source.locator)
            )
            == 2
        )


def test_failed_graph_write_rolls_back_every_content_row(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
        connection.execute(insert(PageIdentifier), values[2].model_dump())

    with content_engine.connect() as connection:
        for model in (Page, Revision, PageIdentifier, PageIdCollisionCounter, PageSource):
            assert connection.scalar(select(func.count()).select_from(model)) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_soft_deleted_page_keeps_canonical_namespace_reservation(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
    with immediate_transaction(content_engine) as connection:
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_uid == values[0].page_uid)
            .values(deleted_at=3_000_000, updated_at=3_000_000)
        )

    with content_engine.connect() as connection:
        assert connection.scalar(select(Page.deleted_at)) == 3_000_000
        assert connection.scalar(select(func.count()).select_from(PageIdentifier)) == 1

    with (
        pytest.raises(IntegrityError, match="permanent"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(delete(PageIdentifier).where(PageIdentifier.library_id == library_id))

    with (
        pytest.raises(IntegrityError, match="stable"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            update(PageIdentifier)
            .where(PageIdentifier.library_id == library_id)
            .values(identifier_text="20260813t100000123z-reassigned")
        )


def test_page_identity_is_stable_while_revision_advances_sequentially(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page, first_revision = values[0], values[1]
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)

    with (
        pytest.raises(IntegrityError, match="stable"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_uid == page.page_uid)
            .values(page_id="20260813t100000123z-changed")
        )

    with immediate_transaction(content_engine) as connection:
        second_content = MarkdownContent.from_bytes(b"# Updated synthetic archive\n")
        second_revision = NewRevision(
            library_id=library_id,
            revision_id=f"rev_{'77' * 16}",
            page_uid=page.page_uid,
            revision_number=2,
            created_at=3_000_000,
            **second_content.model_dump(),
        )
        connection.execute(insert(Revision), second_revision.model_dump())
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_uid == page.page_uid)
            .values(
                title="Updated Synthetic Title",
                current_revision_id=second_revision.revision_id,
                current_revision_number=second_revision.revision_number,
                updated_at=3_000_000,
            )
        )

    with content_engine.connect() as connection:
        row = (
            connection.execute(
                select(Page.__table__).where(
                    Page.library_id == library_id,
                    Page.page_uid == page.page_uid,
                )
            )
            .mappings()
            .one()
        )
        record = PageRecord.model_validate(dict(row))
        assert record.title == "Updated Synthetic Title"
        assert record.page_id == page.page_id
        assert record.current_revision_id == second_revision.revision_id
        assert record.current_revision_number == 2
        assert (
            connection.scalar(
                select(Revision.revision_number).where(
                    Revision.library_id == library_id,
                    Revision.revision_id == first_revision.revision_id,
                )
            )
            == 1
        )

    for revision_id, revision_number in (
        (first_revision.revision_id, 1),
        (f"rev_{'88' * 16}", 4),
    ):
        with (
            pytest.raises(IntegrityError, match="advance sequentially"),
            immediate_transaction(content_engine) as connection,
        ):
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == page.page_uid)
                .values(
                    current_revision_id=revision_id,
                    current_revision_number=revision_number,
                    updated_at=4_000_000,
                )
            )

    with content_engine.connect() as connection:
        assert connection.scalar(select(Page.current_revision_id)) == second_revision.revision_id
        assert connection.scalar(select(Page.current_revision_number)) == 2
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
