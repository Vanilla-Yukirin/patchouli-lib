from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.content.models import (
    Page,
    PageLifecycleEvent,
    PageLifecycleGuard,
    PageSource,
    Revision,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveCitation,
    PageLifecycleCommand,
    PageLifecycleResponseBody,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import canonical_utc_wire

from .conftest import OPERATION_TIME, ArchiveScope
from .helpers import insert_page_graph, page_graph_values


def _seed_page(engine: Engine, scope: ArchiveScope) -> str:
    values = page_graph_values(
        library_id=scope.library_id,
        section_id=scope.section_id,
        book_id=scope.book_id,
    )
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    return values[0].page_id


def test_delete_restore_are_guarded_and_preserve_revision_source_graph(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id = _seed_page(content_engine, archive_scope)
    with immediate_transaction(content_engine) as connection:
        repository = ContentRepository(connection)
        before = repository.get_page(archive_scope.library_id, page_id)
        assert before is not None
        deleted, first = repository.transition_page_lifecycle(
            before,
            action="delete",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "a" * 32,
            changed_at=OPERATION_TIME,
        )
        assert deleted.deleted_at == deleted.updated_at == OPERATION_TIME
        assert first.action == "delete"
        assert first.old_deleted_at is None
        assert first.old_updated_at == before.updated_at
        assert first.at_revision_number == 1
        assert first.occurred_at_at_event == before.occurred_at
        assert repository.list_deleted_pages(
            archive_scope.library_id, archive_scope.section_id, limit=10
        ) == (deleted,)
        assert (
            repository.list_deleted_pages(
                archive_scope.library_id,
                archive_scope.section_id,
                limit=10,
                before=(deleted.deleted_at, page_id),
            )
            == ()
        )
        restored, second = repository.transition_page_lifecycle(
            deleted,
            action="restore",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "b" * 32,
            changed_at=OPERATION_TIME,  # same wall tick; logical clock must advance
        )
        assert restored.deleted_at is None
        assert restored.updated_at == OPERATION_TIME + 1
        assert second.action == "restore"
        assert second.old_deleted_at == OPERATION_TIME
        assert second.old_updated_at == OPERATION_TIME
        assert second.sequence == 2
        assert (
            repository.list_deleted_pages(
                archive_scope.library_id, archive_scope.section_id, limit=10
            )
            == ()
        )
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.scalar(select(func.count()).select_from(PageSource)) == 1
        assert connection.scalar(select(func.count()).select_from(PageLifecycleGuard)) == 0
        assert connection.scalar(select(func.count()).select_from(PageLifecycleEvent)) == 2
        persisted = repository.get_page(archive_scope.library_id, page_id)
        assert persisted == restored
        assert persisted.current_revision_id == before.current_revision_id
        assert persisted.current_revision_number == before.current_revision_number


def test_lifecycle_guards_reject_direct_state_changes_and_event_mutation(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id = _seed_page(content_engine, archive_scope)
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(
            update(Page)
            .where(Page.library_id == archive_scope.library_id, Page.page_id == page_id)
            .values(deleted_at=OPERATION_TIME, updated_at=OPERATION_TIME)
        )

    with immediate_transaction(content_engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(archive_scope.library_id, page_id)
        assert page is not None
        deleted, _ = repository.transition_page_lifecycle(
            page,
            action="delete",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "c" * 32,
            changed_at=OPERATION_TIME,
        )
        with pytest.raises(ValueError, match="does not change"):
            repository.transition_page_lifecycle(
                deleted,
                action="delete",
                actor_caller_id=archive_scope.caller_id,
                request_id="req_" + "d" * 32,
                changed_at=OPERATION_TIME + 1,
            )

    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(
            update(PageLifecycleEvent)
            .where(PageLifecycleEvent.library_id == archive_scope.library_id)
            .values(request_id="req_" + "e" * 32)
        )
    with immediate_transaction(content_engine) as connection:
        assert connection.scalar(select(func.count()).select_from(PageLifecycleEvent)) == 1
        assert connection.scalar(select(func.count()).select_from(PageLifecycleGuard)) == 0


def test_stale_page_snapshot_cannot_transition(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id = _seed_page(content_engine, archive_scope)
    with immediate_transaction(content_engine) as connection:
        repository = ContentRepository(connection)
        stale = repository.get_page(archive_scope.library_id, page_id)
        assert stale is not None
        deleted, _ = repository.transition_page_lifecycle(
            stale,
            action="delete",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "f" * 32,
            changed_at=OPERATION_TIME,
        )
        repository.transition_page_lifecycle(
            deleted,
            action="restore",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "1" * 32,
            changed_at=OPERATION_TIME + 1,
        )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        ContentRepository(connection).transition_page_lifecycle(
            stale,
            action="delete",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "2" * 32,
            changed_at=OPERATION_TIME + 2,
        )
    with immediate_transaction(content_engine) as connection:
        stored = ContentRepository(connection).get_page(archive_scope.library_id, page_id)
        assert stored is not None and stored.deleted_at is None
        assert connection.scalar(select(func.count()).select_from(PageLifecycleEvent)) == 2


def test_lifecycle_command_and_response_reject_invalid_state(archive_scope: ArchiveScope) -> None:
    command = PageLifecycleCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id="20260813t100000123z-synthetic-archive",
        expected_etag='"page-v2-' + "a" * 64 + '"',
        request_id="req_" + "a" * 32,
    )
    assert command.expected_etag.startswith('"page-v2-')
    citation = ArchiveCitation(
        section_id=archive_scope.section_id,
        page_id=command.page_id,
        revision_id="rev_" + "b" * 32,
        revision_number=1,
        href=f"/api/v1/sections/{archive_scope.section_id}/pages/{command.page_id}/revisions/1",
    )
    response = PageLifecycleResponseBody(
        section_id=archive_scope.section_id,
        page_id=command.page_id,
        state="trashed",
        deleted_at=canonical_utc_wire(OPERATION_TIME),
        updated_at=canonical_utc_wire(OPERATION_TIME),
        current_revision_id=citation.revision_id,
        current_revision_number=1,
        citation=citation,
    )
    assert response.state == "trashed"
    with pytest.raises(ValidationError):
        PageLifecycleResponseBody.model_validate(response.model_dump() | {"deleted_at": None})
