"""Exact Page states reconstructed in the caller's existing SQLite snapshot.

This module proves persistence, not authorization or an HTTP request. Raw wall
clock request timestamps cannot select a logical Page state. No file contents
are loaded, and no historical paths, receipts or events are rewritten.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from patchouli_lib.content.service import page_current_etag

_PAGE_MOVE_REVISION = "20261001_0028"
_CALLER_MOVE_REVISION = "20261002_0029"
_LIBRARY_LIFECYCLE_REVISION = "20261003_0030"
_CALLER_MOVE_REVISIONS = frozenset({_CALLER_MOVE_REVISION, _LIBRARY_LIFECYCLE_REVISION})
_MOVE_REVISIONS = frozenset({_PAGE_MOVE_REVISION, *_CALLER_MOVE_REVISIONS})
_TITLE_REVISIONS = frozenset(
    {
        "20260930_0022",
        "20260930_0023",
        "20260930_0024",
        "20261001_0025",
        "20261001_0026",
        "20261001_0027",
        _PAGE_MOVE_REVISION,
        _CALLER_MOVE_REVISION,
        _LIBRARY_LIFECYCLE_REVISION,
    }
)


class PageMembershipHistoryError(RuntimeError):
    """The full immutable chain cannot prove an exact requested state."""


def _require(condition: bool) -> None:
    if not condition:
        raise PageMembershipHistoryError("Page state history is inconsistent.")


@dataclass(frozen=True, slots=True)
class PageState:
    updated_at: int
    revision_id: str
    revision_number: int
    occurred_at: int
    title: str
    deleted_at: int | None
    section_id: str
    book_id: str


@dataclass(frozen=True, slots=True)
class PageStateTimeline:
    library_id: str
    page_uid: bytes
    page_id: str
    states: tuple[PageState, ...]

    @property
    def current(self) -> PageState:
        return self.states[-1]

    def exact_state(self, updated_at: int) -> PageState:
        matches = [state for state in self.states if state.updated_at == updated_at]
        _require(len(matches) == 1)
        return matches[0]

    def match_active_etag(
        self,
        *,
        revision_id: str,
        revision_number: int,
        etag: str,
        section_id: str,
        book_id: str | None = None,
    ) -> PageState:
        matches = [
            state
            for state in self.states
            if state.deleted_at is None
            and state.revision_id == revision_id
            and state.revision_number == revision_number
            and state.section_id == section_id
            and (book_id is None or state.book_id == book_id)
            and page_current_etag(
                self.page_uid,
                state.revision_id,
                state.revision_number,
                state.occurred_at,
                state.updated_at,
            )
            == etag
        ]
        _require(len(matches) == 1)
        return matches[0]


def _rows(
    connection: sqlite3.Connection, query: str, key: tuple[str, bytes]
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, key)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def load_page_state_timeline(
    connection: sqlite3.Connection, *, schema_revision: str, library_id: str, page_uid: bytes
) -> PageStateTimeline:
    """Validate every mutation and final state, never mere path membership.

    Historical formats do not query tables introduced by a later revision.
    The caller owns snapshot isolation, authentication, and format acceptance.
    """
    key = (library_id, page_uid)
    pages = _rows(
        connection,
        "SELECT page_id, section_id, book_id, title, occurred_at, deleted_at, created_at, "
        "updated_at, current_revision_id, current_revision_number FROM pages "
        "WHERE library_id = ? AND page_uid = ?",
        key,
    )
    _require(len(pages) == 1)
    page = pages[0]
    for field in ("occurred_at", "created_at", "updated_at", "current_revision_number"):
        _require(type(page[field]) is int)
    for field in ("page_id", "section_id", "book_id", "title", "current_revision_id"):
        _require(type(page[field]) is str)
    _require(page["deleted_at"] is None or type(page["deleted_at"]) is int)
    revisions = _rows(
        connection,
        "SELECT revision_id, revision_number, created_at FROM revisions "
        "WHERE library_id = ? AND page_uid = ? ORDER BY revision_number",
        key,
    )
    _require(
        bool(revisions)
        and revisions[0]["revision_number"] == 1
        and revisions[0]["created_at"] == page["created_at"]
    )
    corrections = _rows(
        connection,
        "SELECT sequence, old_occurred_at, new_occurred_at, corrected_at, at_revision_number "
        "FROM page_occurrence_corrections WHERE library_id = ? AND page_uid = ? ORDER BY sequence",
        key,
    )
    lifecycle = _rows(
        connection,
        "SELECT sequence, action, section_id, old_deleted_at, old_updated_at, changed_at, "
        "at_revision_number, occurred_at_at_event FROM page_lifecycle_events "
        "WHERE library_id = ? AND page_uid = ? ORDER BY sequence",
        key,
    )
    titles = (
        _rows(
            connection,
            "SELECT sequence, old_title, new_title, old_updated_at, changed_at, at_revision_number "
            "FROM page_title_events WHERE library_id = ? AND page_uid = ? ORDER BY sequence",
            key,
        )
        if schema_revision in _TITLE_REVISIONS
        else []
    )
    moves = (
        _rows(
            connection,
            "SELECT sequence, old_section_id, old_book_id, new_section_id, new_book_id, "
            "old_updated_at, changed_at, at_revision_id, at_revision_number, "
            "occurred_at_at_event, master_audit_event_id, "
            + ("caller_audit_event_id" if schema_revision in _CALLER_MOVE_REVISIONS else "NULL")
            + " AS caller_audit_event_id FROM page_move_events "
            "WHERE library_id = ? AND page_uid = ? ORDER BY sequence",
            key,
        )
        if schema_revision in _MOVE_REVISIONS
        else []
    )
    for table in (
        "page_occurrence_correction_guards",
        "page_lifecycle_guards",
        *(("page_move_guards",) if schema_revision in _MOVE_REVISIONS else ()),
    ):
        _require(
            connection.execute(
                f"SELECT count(*) FROM {table} WHERE library_id = ? AND page_uid = ?", key
            ).fetchone()
            == (0,)
        )
    state = PageState(
        page["created_at"],
        revisions[0]["revision_id"],
        1,
        corrections[0]["old_occurred_at"] if corrections else page["occurred_at"],
        titles[0]["old_title"] if titles else page["title"],
        None,
        moves[0]["old_section_id"] if moves else page["section_id"],
        moves[0]["old_book_id"] if moves else page["book_id"],
    )
    operations: list[tuple[int, str, dict[str, Any]]] = []
    for kind, rows, clock in (
        ("revision", revisions[1:], "created_at"),
        ("correction", corrections, "corrected_at"),
        ("lifecycle", lifecycle, "changed_at"),
        ("title", titles, "changed_at"),
        ("move", moves, "changed_at"),
    ):
        for row in rows:
            _require(type(row[clock]) is int)
            operations.append((row[clock], kind, row))
    states = [state]
    sequences = {kind: 0 for kind in ("correction", "lifecycle", "title", "move")}
    for at, kind, row in sorted(operations, key=lambda item: item[0]):
        _require(at > state.updated_at)
        if kind == "revision":
            _require(
                state.deleted_at is None
                and type(row["revision_number"]) is int
                and row["revision_number"] == state.revision_number + 1
                and type(row["revision_id"]) is str
            )
            state = replace(
                state, revision_id=row["revision_id"], revision_number=row["revision_number"]
            )
        else:
            _require(
                type(row["sequence"]) is int
                and row["sequence"] == sequences[kind] + 1
                and row["at_revision_number"] == state.revision_number
            )
            sequences[kind] = row["sequence"]
            if kind != "correction":
                _require(row["old_updated_at"] == state.updated_at)
            if kind == "correction":
                _require(
                    state.deleted_at is None
                    and type(row["old_occurred_at"]) is int
                    and type(row["new_occurred_at"]) is int
                    and row["old_occurred_at"] == state.occurred_at
                    and row["new_occurred_at"] != state.occurred_at
                )
                state = replace(state, occurred_at=row["new_occurred_at"])
            elif kind == "lifecycle":
                _require(
                    row["section_id"] == state.section_id
                    and row["old_deleted_at"] == state.deleted_at
                    and row["occurred_at_at_event"] == state.occurred_at
                    and row["action"] in {"delete", "restore"}
                    and (row["action"] == "delete") == (state.deleted_at is None)
                )
                state = replace(state, deleted_at=at if row["action"] == "delete" else None)
            elif kind == "title":
                _require(
                    state.deleted_at is None
                    and row["old_title"] == state.title
                    and type(row["new_title"]) is str
                    and row["new_title"] != state.title
                )
                state = replace(state, title=row["new_title"])
            else:
                _require(
                    state.deleted_at is None
                    and (row["old_section_id"], row["old_book_id"])
                    == (state.section_id, state.book_id)
                    and (row["new_section_id"], row["new_book_id"])
                    != (state.section_id, state.book_id)
                    and row["at_revision_id"] == state.revision_id
                    and row["occurred_at_at_event"] == state.occurred_at
                )
                if row["master_audit_event_id"] is None:
                    audit = connection.execute(
                        "SELECT library_id, action, resource_type, resource_id, outcome, "
                        "occurred_at "
                        "FROM auth_audit_events WHERE id = ?",
                        (row["caller_audit_event_id"],),
                    ).fetchone()
                    _require(
                        audit
                        == (
                            library_id,
                            "content.page.move",
                            "page",
                            page["page_id"],
                            "succeeded",
                            at,
                        )
                    )
                else:
                    _require(row["caller_audit_event_id"] is None)
                    audit = connection.execute(
                        "SELECT action, target_type, target_id, occurred_at "
                        "FROM admin_master_audit_events WHERE id = ?",
                        (row["master_audit_event_id"],),
                    ).fetchone()
                    _require(
                        audit == ("content.page.move", "page", f"{library_id}:{page_uid.hex()}", at)
                    )
                state = replace(state, section_id=row["new_section_id"], book_id=row["new_book_id"])
        state = replace(state, updated_at=at)
        states.append(state)
    _require(
        state
        == PageState(
            page["updated_at"],
            page["current_revision_id"],
            page["current_revision_number"],
            page["occurred_at"],
            page["title"],
            page["deleted_at"],
            page["section_id"],
            page["book_id"],
        )
    )
    for section, book in {(item.section_id, item.book_id) for item in states}:
        _require(
            connection.execute(
                "SELECT count(*) FROM books WHERE library_id = ? AND section_id = ? AND id = ?",
                (library_id, section, book),
            ).fetchone()
            == (1,)
        )
    return PageStateTimeline(library_id, page_uid, page["page_id"], tuple(states))


__all__ = [
    "PageMembershipHistoryError",
    "PageState",
    "PageStateTimeline",
    "load_page_state_timeline",
]
