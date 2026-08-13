from sqlalchemy import Engine, select

from patchouli_lib.content.models import Page, Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import page_id_registry_digest

from .helpers import insert_page_graph, page_graph_values, seed_library_structure


def test_repository_round_trips_page_graph_and_alias_lookup(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page, revision, identifier, counter, source = values

    with immediate_transaction(content_engine) as connection:
        repository = ContentRepository(connection)
        repository.add_collision_counter(counter)
        repository.add_page(page)
        repository.add_revision(revision)
        repository.add_identifier(identifier)
        repository.add_source(source)

        assert repository.get_book(library_id, book_id) is not None
        stored_page = repository.get_page(library_id, page.page_id)
        stored_revision = repository.get_revision(library_id, page.page_uid, 1)
        assert stored_page is not None and stored_page.model_dump() == page.model_dump()
        assert stored_revision is not None and stored_revision.model_dump() == revision.model_dump()
        assert repository.identifier_exists(library_id, page.page_id)
        assert repository.page_uid_exists(library_id, page.page_uid)
        assert repository.revision_id_exists(library_id, revision.revision_id)
        stored_counter = repository.get_collision_counter(
            library_id,
            counter.id_scheme,
            counter.id_timestamp_micros,
            counter.base_slug,
        )
        assert stored_counter is not None
        assert stored_counter.model_dump() == counter.model_dump()


def test_repository_appends_then_advances_without_changing_old_revision(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    insert_page_graph_values = values
    page, first, _, _, _ = values
    second_content = MarkdownContent.from_bytes(b"# Synthetic revision two\n")
    second = NewRevision(
        library_id=library_id,
        revision_id=f"rev_{'4' * 32}",
        page_uid=page.page_uid,
        revision_number=2,
        created_at=3_000_000,
        **second_content.model_dump(),
    )

    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, insert_page_graph_values)
        repository = ContentRepository(connection)
        stored_page = repository.get_page(library_id, page.page_id)
        assert stored_page is not None
        stored_second = repository.add_revision(second)
        advanced = repository.advance_current_revision(
            stored_page,
            stored_second,
            updated_at=3_000_000,
        )
        assert advanced is not None
        assert advanced.current_revision_number == 2
        stored_first = repository.get_revision(library_id, page.page_uid, 1)
        assert stored_first is not None and stored_first.model_dump() == first.model_dump()

    with content_engine.connect() as connection:
        revisions = connection.execute(
            select(Revision.revision_number, Revision.content_md)
            .where(Revision.library_id == library_id, Revision.page_uid == page.page_uid)
            .order_by(Revision.revision_number)
        ).all()
        current = connection.execute(
            select(Page.current_revision_number).where(
                Page.library_id == library_id,
                Page.page_uid == page.page_uid,
            )
        ).scalar_one()
    assert [(row[0], row[1]) for row in revisions] == [
        (1, first.content_md),
        (2, second.content_md),
    ]
    assert current == 2


def test_repository_never_commits_caller_transaction(content_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    with content_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        page, revision, identifier, counter, source = values
        repository = ContentRepository(connection)
        repository.add_collision_counter(counter)
        repository.add_page(page)
        repository.add_revision(revision)
        repository.add_identifier(identifier)
        repository.add_source(source)
        assert connection.in_transaction()
        connection.rollback()

    with content_engine.connect() as connection:
        assert connection.execute(select(Page)).all() == []


def test_page_lookup_fails_closed_on_corrupt_digest_or_digest_collision(
    content_engine: Engine,
) -> None:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page, revision, identifier, counter, source = values
    collision_probe = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        title="Synthetic Digest Collision Probe",
    )[0].page_id
    corrupt_identifier = identifier.model_copy(
        update={"identifier_digest": page_id_registry_digest(collision_probe)}
    )

    with immediate_transaction(content_engine) as connection:
        insert_page_graph(
            connection,
            (page, revision, corrupt_identifier, counter, source),
        )
        repository = ContentRepository(connection)
        assert repository.get_page(library_id, page.page_id) is None
        assert repository.get_page(library_id, collision_probe) is None
