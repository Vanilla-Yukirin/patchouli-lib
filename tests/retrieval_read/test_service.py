from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import Connection, Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerRecord,
    CredentialRecord,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.content import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import InvalidPageIdError, InvalidRevisionNumberError
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import ReadWindow
from patchouli_lib.retrieval.service import (
    RetrievalAuthenticationError,
    RetrievalAuthorizationError,
    RetrievalNotFoundError,
    RetrievalService,
)

from .conftest import CALLER_ID, EXTRA_QUERY_BOOK_ID, RetrievalScope


def _service(engine: Engine, scope: RetrievalScope) -> tuple[Connection, RetrievalService]:
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
        assert current.etag == page_current_etag(
            retrieval_scope.first_page_uid,
            retrieval_scope.second_revision_id,
            2,
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
