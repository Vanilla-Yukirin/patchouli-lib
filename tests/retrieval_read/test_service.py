from __future__ import annotations

import dataclasses
import hashlib

import pytest
from sqlalchemy import Connection, Engine, delete, insert, select

from patchouli_lib.auth.library_policy import LegacySectionPolicy
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerRecord,
    CredentialRecord,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.content import page_current_etag
from patchouli_lib.content.models import Page
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import InvalidPageIdError, InvalidRevisionNumberError
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewBook
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import ReadWindow
from patchouli_lib.retrieval.service import (
    RetrievalAuthenticationError,
    RetrievalAuthorizationError,
    RetrievalNotFoundError,
    RetrievalPersistenceError,
    RetrievalService,
    RetrievalUnsupportedFormatError,
)

from .conftest import CALLER_ID, EXTRA_QUERY_BOOK_ID, RetrievalScope


def _service(engine: Engine, scope: RetrievalScope) -> tuple[Connection, RetrievalService]:
    # create_all() does not run Alembic's 0013 backfill for this synthetic fixture.
    with immediate_transaction(engine) as fixture_connection:
        fixture_connection.exec_driver_sql(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "SELECT r.library_id, r.page_uid, r.revision_id, r.revision_number, "
            "'legacy_markdown', 1, r.content_size_bytes, NULL FROM revisions AS r "
            "WHERE NOT EXISTS (SELECT 1 FROM revision_file_sets AS m WHERE "
            "m.library_id = r.library_id AND m.page_uid = r.page_uid "
            "AND m.revision_id = r.revision_id AND m.revision_number = r.revision_number)"
        )
    connection = engine.connect()
    return connection, RetrievalService(
        RetrievalRepository(connection),
        scope.authenticated,
        clock=lambda: 2_000_000,
    )


def test_sections_only_discover_query_grants_with_bounded_keyset(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        first = service.list_sections(ReadWindow(limit=1))
        assert [item.section_id for item in first.items] == [retrieval_scope.query_section_id]
        assert first.next_key == retrieval_scope.query_section_id
        second = service.list_sections(ReadWindow(limit=1, after_key=first.next_key))
        assert [item.section_id for item in second.items] == [
            retrieval_scope.second_query_section_id
        ]
        assert second.next_key is None
        assert retrieval_scope.read_section_id not in {
            item.section_id for item in (*first.items, *second.items)
        }
    finally:
        connection.close()


def test_opted_in_library_read_replaces_legacy_section_visibility(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    scope = retrieval_scope
    with immediate_transaction(retrieval_engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": scope.authenticated.credential.id,
                "caller_id": scope.authenticated.caller.id,
                "home_library_id": scope.library_id,
                "mode": "library_grants",
                "created_at": 1_000_000,
            },
        )
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": scope.authenticated.credential.id,
                "caller_id": scope.authenticated.caller.id,
                "home_library_id": scope.library_id,
                "target_library_id": scope.library_id,
                "action": "write",
                "created_at": 1_000_000,
            },
        )

    connection, service = _service(retrieval_engine, scope)
    try:
        with pytest.raises(RetrievalAuthorizationError):
            service.list_sections()
        with pytest.raises(RetrievalAuthorizationError):
            service.get_current_page(scope.query_section_id, scope.first_page_id)
    finally:
        connection.close()

    with immediate_transaction(retrieval_engine) as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": scope.authenticated.credential.id,
                "caller_id": scope.authenticated.caller.id,
                "home_library_id": scope.library_id,
                "target_library_id": scope.library_id,
                "action": "read",
                "created_at": 1_000_000,
            },
        )

    connection, service = _service(retrieval_engine, scope)
    try:
        first = service.list_sections(ReadWindow(limit=2))
        assert [item.section_id for item in first.items] == [
            scope.query_section_id,
            scope.second_query_section_id,
        ]
        assert first.next_key == scope.second_query_section_id
        second = service.list_sections(ReadWindow(limit=2, after_key=first.next_key))
        assert [item.section_id for item in second.items] == [
            scope.read_section_id,
            scope.hidden_section_id,
        ]
        assert second.next_key is None
        assert (
            service.get_current_page(
                scope.hidden_section_id, scope.hidden_page_id
            ).document.revision.content
            == "# Hidden\n"
        )
    finally:
        connection.close()


def test_query_grant_lists_books_and_current_page_metadata_without_bodies(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        books = service.list_books(
            retrieval_scope.query_section_id,
            ReadWindow(limit=1),
        )
        assert [(item.section_id, item.book_id, item.title) for item in books.items] == [
            (
                retrieval_scope.query_section_id,
                retrieval_scope.query_book_id,
                "Alpha Query Book",
            )
        ]
        assert books.next_key == retrieval_scope.query_book_id
        remaining_books = service.list_books(
            retrieval_scope.query_section_id,
            ReadWindow(limit=1, after_key=books.next_key),
        )
        assert [item.book_id for item in remaining_books.items] == [EXTRA_QUERY_BOOK_ID]
        assert remaining_books.next_key is None
        pages = service.list_pages(retrieval_scope.query_section_id)
        assert [item.page.page_id for item in pages.items] == [
            retrieval_scope.first_page_id,
            retrieval_scope.second_page_id,
        ]
        first = pages.items[0]
        assert first.page.current_revision_id == retrieval_scope.second_revision_id
        assert first.page.current_revision_number == 2
        assert first.citation.revision_id == retrieval_scope.second_revision_id
        assert "content" not in first.model_dump()
        assert not hasattr(pages, "total")
    finally:
        connection.close()


@pytest.mark.parametrize("actions", [(), ("write",), ("read",), ("read", "write")])
def test_explicit_library_discovery_requires_exact_target_read(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
    actions: tuple[str, ...],
) -> None:
    scope = retrieval_scope
    ids = iter(("01" * 16, "02" * 16, "03" * 16))
    with immediate_transaction(retrieval_engine) as connection:
        repository = LibraryRepository(connection)
        target = LibrarySeedService(
            repository, id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Discovery Library",
                section_name="Synthetic Discovery Section",
                book_name="Synthetic First Book",
            )
        )
        repository.add_book(
            NewBook(
                id="04" * 16,
                library_id=target.library.id,
                section_id=target.section.id,
                name="Synthetic Second Book",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": scope.authenticated.credential.id,
                "caller_id": scope.authenticated.caller.id,
                "home_library_id": scope.library_id,
                "mode": "library_grants",
                "created_at": 1_000_000,
            },
        )
        for action in actions:
            connection.execute(
                insert(CredentialLibraryGrant),
                {
                    "credential_id": scope.authenticated.credential.id,
                    "caller_id": scope.authenticated.caller.id,
                    "home_library_id": scope.library_id,
                    "target_library_id": target.library.id,
                    "action": action,
                    "created_at": 1_000_000,
                },
            )

    connection, service = _service(retrieval_engine, scope)
    try:
        # Existing home Section grants cannot rescue the opted-in credential.
        with pytest.raises(RetrievalAuthorizationError):
            service.list_sections()
        if "read" not in actions:
            with pytest.raises(RetrievalAuthorizationError):
                service.list_sections(library_id=target.library.id)
            with pytest.raises(RetrievalAuthorizationError):
                service.list_books(target.section.id, library_id=target.library.id)
            return
        sections = service.list_sections(library_id=target.library.id)
        assert [item.section_id for item in sections.items] == [target.section.id]
        first = service.list_books(
            target.section.id, ReadWindow(limit=1), library_id=target.library.id
        )
        assert [item.book_id for item in first.items] == [target.book.id]
        assert first.next_key == target.book.id
        second = service.list_books(
            target.section.id,
            ReadWindow(limit=1, after_key=first.next_key),
            library_id=target.library.id,
        )
        assert [item.book_id for item in second.items] == ["04" * 16]
        assert second.next_key is None
        with pytest.raises(RetrievalNotFoundError):
            service.list_books(scope.query_section_id, library_id=target.library.id)
    finally:
        connection.close()

    with immediate_transaction(retrieval_engine) as connection:
        connection.execute(
            delete(CredentialLibraryGrant).where(
                CredentialLibraryGrant.credential_id == scope.authenticated.credential.id,
                CredentialLibraryGrant.target_library_id == target.library.id,
                CredentialLibraryGrant.action == "read",
            )
        )
    connection, service = _service(retrieval_engine, scope)
    try:
        with pytest.raises(RetrievalAuthorizationError):
            service.list_books(
                target.section.id,
                ReadWindow(limit=1, after_key=first.next_key),
                library_id=target.library.id,
            )
    finally:
        connection.close()


def test_legacy_discovery_allows_explicit_home_but_never_other_libraries(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    scope = retrieval_scope
    connection, service = _service(retrieval_engine, scope)
    try:
        assert service.list_sections() == service.list_sections(library_id=scope.library_id)
        assert service.list_books(scope.query_section_id) == service.list_books(
            scope.query_section_id, library_id=scope.library_id
        )
        for target in ("01" * 16, "ff" * 16):
            with pytest.raises(RetrievalAuthorizationError):
                service.list_sections(library_id=target)
            with pytest.raises(RetrievalAuthorizationError):
                service.list_books(scope.query_section_id, library_id=target)
    finally:
        connection.close()


def test_current_and_explicit_revision_reads_are_exact_and_unrendered(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        current = service.get_current_page(
            retrieval_scope.query_section_id,
            retrieval_scope.first_page_id,
        )
        assert current.document.revision.content == retrieval_scope.current_content
        assert current.document.revision.revision_id == retrieval_scope.second_revision_id
        assert current.document.citation.href.endswith("/revisions/2")
        stored_page = connection.execute(
            select(Page.occurred_at, Page.updated_at).where(
                Page.page_uid == retrieval_scope.first_page_uid
            )
        ).one()
        assert current.etag == page_current_etag(
            retrieval_scope.first_page_uid,
            retrieval_scope.second_revision_id,
            2,
            stored_page[0],
            stored_page[1],
        )

        historical = service.get_revision(
            retrieval_scope.query_section_id,
            retrieval_scope.first_page_id,
            1,
        )
        assert historical.revision.content == retrieval_scope.historical_content
        assert "<script>" in historical.revision.content
        assert historical.revision.revision_id == retrieval_scope.first_revision_id
        assert historical.citation.href.endswith("/revisions/1")
        assert historical.page.current_revision_number == 2

        alias = service.get_current_page(
            retrieval_scope.query_section_id,
            retrieval_scope.first_page_alias,
        )
        assert alias.document.page.page_id == retrieval_scope.first_page_id
        assert alias.document.citation.page_id == retrieval_scope.first_page_id
    finally:
        connection.close()


def test_revision_file_reads_bind_exact_history_and_current_revision(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        historical = service.list_revision_files(
            retrieval_scope.query_section_id,
            retrieval_scope.first_page_id,
            1,
        )
        assert historical.page_id == retrieval_scope.first_page_id
        assert historical.revision_id == retrieval_scope.first_revision_id
        assert historical.revision_number == 1
        assert [
            (item.filename, item.size_bytes, item.content_sha256) for item in historical.files
        ] == [
            (
                "content.md",
                len(retrieval_scope.historical_content.encode()),
                hashlib.sha256(retrieval_scope.historical_content.encode()).hexdigest(),
            )
        ]
        assert (
            service.get_revision_file(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                1,
                "content.md",
            ).content
            == retrieval_scope.historical_content.encode()
        )
        current = service.get_revision_file(
            retrieval_scope.query_section_id,
            retrieval_scope.first_page_alias,
            2,
            "content.md",
        )
        assert current.content == retrieval_scope.current_content.encode()
        assert "Historical" not in current.content.decode()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("revision_number", "file_kind"),
    [(1, "mixed"), (2, "binary")],
)
def test_legacy_reads_reject_file_set_before_markdown_validation(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
    revision_number: int,
    file_kind: str,
) -> None:
    revision_id = (
        retrieval_scope.first_revision_id
        if revision_number == 1
        else retrieval_scope.second_revision_id
    )
    with immediate_transaction(retrieval_engine) as connection:
        connection.exec_driver_sql(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "VALUES (?, ?, ?, ?, 'file_set_v1', ?, ?, ?)",
            (
                retrieval_scope.library_id,
                retrieval_scope.first_page_uid,
                revision_id,
                revision_number,
                2 if file_kind == "mixed" else 1,
                (
                    2
                    if file_kind == "binary"
                    else len(retrieval_scope.historical_content.encode()) + 2
                ),
                b"s" * 32,
            ),
        )
        if file_kind == "binary":
            connection.exec_driver_sql(
                "UPDATE revisions SET content_md = NULL, content_size_bytes = NULL, "
                "content_sha256 = NULL WHERE revision_id = ?",
                (revision_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM revision_files WHERE revision_id = ?",
                (revision_id,),
            )
        binary = b"\x00\xff"
        connection.exec_driver_sql(
            "INSERT INTO revision_files "
            "(library_id, page_uid, revision_id, revision_number, filename, "
            "content_bytes, size_bytes, content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                retrieval_scope.library_id,
                retrieval_scope.first_page_uid,
                revision_id,
                revision_number,
                "artifact.bin",
                binary,
                len(binary),
                hashlib.sha256(binary).digest(),
            ),
        )

    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        if revision_number == 2:
            with pytest.raises(RetrievalUnsupportedFormatError):
                service.get_current_page(
                    retrieval_scope.query_section_id, retrieval_scope.first_page_id
                )
            assert (
                service.get_revision(
                    retrieval_scope.query_section_id, retrieval_scope.first_page_id, 1
                ).revision.content
                == retrieval_scope.historical_content
            )
        else:
            assert (
                service.get_current_page(
                    retrieval_scope.query_section_id, retrieval_scope.first_page_id
                ).document.revision.content
                == retrieval_scope.current_content
            )
        with pytest.raises(RetrievalUnsupportedFormatError):
            service.get_revision(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                revision_number,
            )
        with pytest.raises(RetrievalUnsupportedFormatError):
            service.list_revision_files(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                revision_number,
            )
        with pytest.raises(RetrievalUnsupportedFormatError):
            service.get_revision_file(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                revision_number,
                "artifact.bin",
            )
        with pytest.raises(RetrievalAuthorizationError):
            service.get_revision(
                retrieval_scope.second_query_section_id,
                retrieval_scope.first_page_id,
                revision_number,
            )
        with pytest.raises(RetrievalNotFoundError):
            service.get_revision(
                retrieval_scope.hidden_section_id,
                retrieval_scope.first_page_id,
                revision_number,
            )
    finally:
        connection.close()


def test_missing_revision_manifest_fails_closed(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        with immediate_transaction(retrieval_engine) as mutation:
            mutation.exec_driver_sql(
                "DELETE FROM revision_file_sets WHERE revision_id = ?",
                (retrieval_scope.second_revision_id,),
            )
        with pytest.raises(RetrievalPersistenceError):
            service.get_current_page(
                retrieval_scope.query_section_id, retrieval_scope.first_page_id
            )
        with pytest.raises(RetrievalPersistenceError):
            service.get_revision(retrieval_scope.query_section_id, retrieval_scope.first_page_id, 2)
        with pytest.raises(RetrievalPersistenceError):
            service.list_revision_files(
                retrieval_scope.query_section_id, retrieval_scope.first_page_id, 2
            )
        with pytest.raises(RetrievalPersistenceError):
            service.get_revision_file(
                retrieval_scope.query_section_id, retrieval_scope.first_page_id, 2, "content.md"
            )
    finally:
        connection.close()


def test_revision_file_reads_hide_absent_tombstoned_and_unauthorized_pages(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        for section_id, page_id in (
            (retrieval_scope.query_section_id, retrieval_scope.deleted_page_id),
            (retrieval_scope.hidden_section_id, retrieval_scope.hidden_page_id),
            (retrieval_scope.query_section_id, retrieval_scope.read_page_id),
        ):
            with pytest.raises(RetrievalNotFoundError):
                service.list_revision_files(section_id, page_id, 1)
        with pytest.raises(RetrievalNotFoundError):
            service.get_revision_file(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                1,
                "missing.md",
            )
        with pytest.raises(RetrievalNotFoundError):
            service.list_revision_files(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                3,
            )
        with pytest.raises(RetrievalAuthorizationError):
            service.list_revision_files(
                retrieval_scope.second_query_section_id,
                retrieval_scope.first_page_id,
                1,
            )
    finally:
        connection.close()


@pytest.mark.parametrize("damage", ["digest", "bytes", "seal"])
def test_revision_file_reads_refuse_inconsistent_stored_snapshot(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
    damage: str,
) -> None:
    with immediate_transaction(retrieval_engine) as connection:
        if damage == "digest":
            connection.exec_driver_sql(
                "UPDATE revision_files SET content_sha256 = zeroblob(32) WHERE revision_id = ?",
                (retrieval_scope.first_revision_id,),
            )
        elif damage == "bytes":
            connection.exec_driver_sql(
                "UPDATE revision_files SET content_bytes = x'42', size_bytes = 1, "
                "content_sha256 = ? WHERE revision_id = ?",
                (hashlib.sha256(b"B").digest(), retrieval_scope.first_revision_id),
            )
        else:
            connection.exec_driver_sql(
                "DELETE FROM revision_file_seal_guards WHERE revision_id = ?",
                (retrieval_scope.first_revision_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM revision_file_seals WHERE revision_id = ?",
                (retrieval_scope.first_revision_id,),
            )

    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        with pytest.raises(RetrievalPersistenceError):
            service.list_revision_files(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                1,
            )
    finally:
        connection.close()


@pytest.mark.parametrize("revision_number", [0, -1, (1 << 63), True])
def test_revision_read_rejects_invalid_numbers_before_query(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
    revision_number: int,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        with pytest.raises(InvalidRevisionNumberError):
            service.get_revision(
                retrieval_scope.query_section_id,
                retrieval_scope.first_page_id,
                revision_number,
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "page_id",
    [
        "",
        "页面",
        "x" * 81,
        "control\x00value",
        "slash/value",
        r"backslash\value",
        "literal%2fescape",
        "unsupported-page-id",
    ],
)
def test_visible_page_reads_reject_malformed_page_ids(
    retrieval_scope: RetrievalScope,
    page_id: str,
) -> None:
    class RejectingRepository:
        def get_caller(self, library_id: str, caller_id: str) -> CallerRecord:
            return retrieval_scope.authenticated.caller

        def section_actions(
            self,
            library_id: str,
            caller_id: str,
            section_id: str,
        ) -> tuple[SectionAction, ...]:
            return (SectionAction.PAGE_READ,)

        def get_credential(
            self,
            library_id: str,
            caller_id: str,
            credential_id: str,
        ) -> CredentialRecord:
            return retrieval_scope.authenticated.credential

        def get_library_policy(self, **kwargs: object) -> LegacySectionPolicy:
            return LegacySectionPolicy()

        def get_current_document(self, *args: object) -> None:
            raise AssertionError("Malformed Page IDs must not reach persistence.")

    service = RetrievalService(
        RejectingRepository(),  # type: ignore[arg-type]
        retrieval_scope.authenticated,
        clock=lambda: 2_000_000,
    )
    with pytest.raises(InvalidPageIdError):
        service.get_current_page(retrieval_scope.query_section_id, page_id)


@pytest.mark.parametrize("missing_page", [False, True])
def test_hidden_and_absent_pages_share_not_found_behavior(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
    missing_page: bool,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        page_id = "missing-page" if missing_page else retrieval_scope.hidden_page_id
        with pytest.raises(RetrievalNotFoundError):
            service.get_current_page(retrieval_scope.hidden_section_id, page_id)
    finally:
        connection.close()


def test_visible_section_without_required_action_is_insufficient_scope(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    with immediate_transaction(retrieval_engine) as connection:
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=retrieval_scope.library_id,
                caller_id=CALLER_ID,
                section_id=retrieval_scope.hidden_section_id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=1_000_000,
            )
        )

    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        with pytest.raises(RetrievalAuthorizationError):
            service.list_pages(retrieval_scope.hidden_section_id)
        with pytest.raises(RetrievalAuthorizationError):
            service.get_current_page(
                retrieval_scope.hidden_section_id,
                "missing-page",
            )
    finally:
        connection.close()


def test_page_read_grant_fetches_body_but_does_not_discover_or_list_section(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        sections = service.list_sections()
        assert retrieval_scope.read_section_id not in {item.section_id for item in sections.items}
        with pytest.raises(RetrievalAuthorizationError):
            service.list_books(retrieval_scope.read_section_id)
        document = service.get_current_page(
            retrieval_scope.read_section_id,
            retrieval_scope.read_page_id,
        )
        assert document.document.revision.content == "# Read only\n"
    finally:
        connection.close()


def test_disabled_caller_is_denied_even_with_previously_authenticated_context(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    with immediate_transaction(retrieval_engine) as connection:
        AuthRepository(connection).disable_caller(
            retrieval_scope.library_id,
            CALLER_ID,
            disabled_at=4_000_000,
        )

    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        with pytest.raises(RetrievalAuthenticationError):
            service.list_sections()
    finally:
        connection.close()


def test_revoked_credential_is_denied_even_with_previously_authenticated_context(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    with immediate_transaction(retrieval_engine) as connection:
        repository = AuthRepository(connection)
        credential = repository.get_credential(
            retrieval_scope.library_id,
            CALLER_ID,
            retrieval_scope.authenticated.credential.id,
        )
        assert credential is not None
        repository.revoke_credential(credential, revoked_at=3_000_000)

    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        with pytest.raises(RetrievalAuthenticationError):
            service.list_sections()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("state", "now"),
    [("rotated", 2_000_000), ("expired", 10_000_000)],
)
def test_inactive_credential_states_are_authentication_failures(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
    state: str,
    now: int,
) -> None:
    if state == "rotated":
        with immediate_transaction(retrieval_engine) as connection:
            repository = AuthRepository(connection)
            credential = repository.get_credential(
                retrieval_scope.library_id,
                CALLER_ID,
                retrieval_scope.authenticated.credential.id,
            )
            assert credential is not None
            replacement = repository.add_credential(
                NewCredential(
                    id="d" * 32,
                    library_id=retrieval_scope.library_id,
                    caller_id=CALLER_ID,
                    selector="A" * 22,
                    token_version=1,
                    verifier=b"z" * 32,
                    expires_at=20_000_000,
                    created_at=now,
                    updated_at=now,
                )
            )
            assert (
                repository.mark_credential_rotated(
                    credential,
                    replacement.id,
                    rotated_at=now,
                )
                is not None
            )

    connection = retrieval_engine.connect()
    service = RetrievalService(
        RetrievalRepository(connection),
        retrieval_scope.authenticated,
        clock=lambda: now,
    )
    try:
        with pytest.raises(RetrievalAuthenticationError):
            service.list_sections()
    finally:
        connection.close()


def test_not_yet_valid_credential_is_authentication_failure(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    with immediate_transaction(retrieval_engine) as connection:
        future_credential = AuthRepository(connection).add_credential(
            NewCredential(
                id="e" * 32,
                library_id=retrieval_scope.library_id,
                caller_id=CALLER_ID,
                selector="B" * 22,
                token_version=1,
                verifier=b"y" * 32,
                expires_at=20_000_000,
                created_at=5_000_000,
                updated_at=5_000_000,
            )
        )
    stale_context = retrieval_scope.authenticated.model_copy(
        update={"credential": future_credential}
    )
    connection = retrieval_engine.connect()
    service = RetrievalService(
        RetrievalRepository(connection),
        stale_context,
        clock=lambda: 4_000_000,
    )
    try:
        with pytest.raises(RetrievalAuthenticationError):
            service.list_sections()
    finally:
        connection.close()


def test_result_models_do_not_expose_internal_page_uid(
    retrieval_engine: Engine,
    retrieval_scope: RetrievalScope,
) -> None:
    connection, service = _service(retrieval_engine, retrieval_scope)
    try:
        document = service.get_revision(
            retrieval_scope.query_section_id,
            retrieval_scope.first_page_id,
            1,
        )
        serialized = document.model_dump()
        assert "page_uid" not in repr(serialized)
        assert not dataclasses.is_dataclass(document)
    finally:
        connection.close()
