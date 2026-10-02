"""Role-neutral membership mutation inside the outer authorized transaction.

The wrapper must persist the matching real audit before applying, and a frozen
success afterwards. This core never authenticates, commits, or creates content.
"""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass

from sqlalchemy import Connection, func, insert, select, update

from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.models import Page
from patchouli_lib.content.page_move_models import PageMoveEvent, PageMoveGuard
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.identifiers import canonical_utc_wire
from patchouli_lib.library.repository import LibraryRepository


class PageMoveNotFoundError(RuntimeError):
    """The active source or the same-Library target is unavailable."""


@dataclass(frozen=True, slots=True)
class PageMovePlan:
    page: PageRecord
    target_section_id: str
    target_book_id: str
    updated_at: int
    sequence: int | None

    @property
    def changed(self) -> bool:
        return self.sequence is not None


def prepare_page_move(
    connection: Connection,
    *,
    library_id: str,
    page_id: str,
    source_section_id: str,
    source_book_id: str,
    target_section_id: str,
    target_book_id: str,
    expected_etag: str,
    operation_at: int,
) -> PageMovePlan:
    raw = connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
        raise RuntimeError("Page move requires a concrete caller-owned transaction.")
    page = ContentRepository(connection).get_page(library_id, page_id)
    if (
        page is None
        or page.page_id != page_id
        or page.deleted_at is not None
        or (page.section_id, page.book_id) != (source_section_id, source_book_id)
    ):
        raise PageMoveNotFoundError
    expected = page_current_etag(
        page.page_uid,
        page.current_revision_id,
        page.current_revision_number,
        page.occurred_at,
        page.updated_at,
    )
    if not hmac.compare_digest(expected, expected_etag):
        raise FileSetPreconditionFailedError
    if (
        LibraryRepository(connection).get_book(library_id, target_section_id, target_book_id)
        is None
    ):
        raise PageMoveNotFoundError
    if type(operation_at) is not int or operation_at < 0:
        raise ValueError("Invalid movement time.")
    canonical_utc_wire(operation_at)
    changed = (source_section_id, source_book_id) != (target_section_id, target_book_id)
    updated_at = max(operation_at, page.updated_at + 1) if changed else page.updated_at
    canonical_utc_wire(updated_at)
    sequence = (
        1
        + (
            connection.scalar(
                select(func.max(PageMoveEvent.sequence)).where(
                    PageMoveEvent.library_id == library_id,
                    PageMoveEvent.page_uid == page.page_uid,
                )
            )
            or 0
        )
        if changed
        else None
    )
    return PageMovePlan(page, target_section_id, target_book_id, updated_at, sequence)


def apply_page_move(
    connection: Connection,
    plan: PageMovePlan,
    *,
    master_audit_event_id: str | None = None,
    caller_audit_event_id: str | None = None,
) -> None:
    if not plan.changed:
        if master_audit_event_id is not None or caller_audit_event_id is not None:
            raise ValueError("A no-op cannot have a movement audit.")
        return
    if (master_audit_event_id is None) == (caller_audit_event_id is None):
        raise ValueError("Exactly one movement audit actor is required.")
    page = plan.page
    connection.execute(
        insert(PageMoveGuard),
        {
            "library_id": page.library_id,
            "page_uid": page.page_uid,
            "sequence": plan.sequence,
            "old_section_id": page.section_id,
            "old_book_id": page.book_id,
            "new_section_id": plan.target_section_id,
            "new_book_id": plan.target_book_id,
            "old_updated_at": page.updated_at,
            "changed_at": plan.updated_at,
            "at_revision_id": page.current_revision_id,
            "at_revision_number": page.current_revision_number,
            "occurred_at_at_event": page.occurred_at,
            "master_audit_event_id": master_audit_event_id,
            "caller_audit_event_id": caller_audit_event_id,
        },
    )
    affected = connection.execute(
        update(Page)
        .where(
            Page.library_id == page.library_id,
            Page.page_uid == page.page_uid,
            Page.section_id == page.section_id,
            Page.book_id == page.book_id,
            Page.updated_at == page.updated_at,
            Page.deleted_at.is_(None),
        )
        .values(
            section_id=plan.target_section_id,
            book_id=plan.target_book_id,
            updated_at=plan.updated_at,
        )
    )
    if affected.rowcount != 1:
        raise FileSetPreconditionFailedError
    if connection.scalar(
        select(func.count())
        .select_from(PageMoveGuard)
        .where(
            PageMoveGuard.library_id == page.library_id,
            PageMoveGuard.page_uid == page.page_uid,
        )
    ):
        raise RuntimeError("Page move left an incomplete guard.")
