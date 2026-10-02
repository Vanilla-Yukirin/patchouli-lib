"""Exactly one real audit actor per immutable movement and consumed guard."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Constraint

from patchouli_lib.models import Base


def _constraints(*, guard: bool) -> tuple[Constraint, ...]:
    constraints: list[Constraint] = [
        PrimaryKeyConstraint("library_id", "page_uid", *(("sequence",) if not guard else ())),
        ForeignKeyConstraint(
            ["library_id", "page_uid"], ["pages.library_id", "pages.page_uid"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["library_id", "page_uid", "at_revision_id", "at_revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("master_audit_event_id"),
        ForeignKeyConstraint(
            ["caller_audit_event_id"], ["auth_audit_events.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("caller_audit_event_id"),
        CheckConstraint(
            "(master_audit_event_id IS NOT NULL AND caller_audit_event_id IS NULL) OR "
            "(master_audit_event_id IS NULL AND caller_audit_event_id IS NOT NULL)"
        ),
        CheckConstraint("typeof(page_uid) = 'blob' AND length(page_uid) = 16"),
        CheckConstraint(
            "typeof(sequence) = 'integer' AND sequence BETWEEN 1 AND 9223372036854775807"
        ),
        CheckConstraint("old_section_id != new_section_id OR old_book_id != new_book_id"),
        CheckConstraint(
            "typeof(old_updated_at) = 'integer' AND old_updated_at >= 0 "
            "AND typeof(changed_at) = 'integer' AND changed_at > old_updated_at "
            "AND changed_at <= 253402300799999999"
        ),
        CheckConstraint(
            "typeof(at_revision_number) = 'integer' "
            "AND at_revision_number BETWEEN 1 AND 9223372036854775807 "
            "AND typeof(occurred_at_at_event) = 'integer' "
            "AND occurred_at_at_event BETWEEN -62135596800000000 AND 253402300799999999"
        ),
    ]
    for prefix in ("old", "new"):
        constraints.append(
            ForeignKeyConstraint(
                [f"{prefix}_book_id", f"{prefix}_section_id", "library_id"],
                ["books.id", "books.section_id", "books.library_id"],
                ondelete="RESTRICT",
            )
        )
    if guard:
        constraints.append(
            ForeignKeyConstraint(
                ["library_id", "page_uid", "sequence"],
                [
                    "page_move_events.library_id",
                    "page_move_events.page_uid",
                    "page_move_events.sequence",
                ],
                deferrable=True,
                initially="DEFERRED",
            )
        )
    return tuple(constraints)


class _MoveColumns:
    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(16), primary_key=True)
    old_section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    old_book_id: Mapped[str] = mapped_column(String(32), nullable=False)
    new_section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    new_book_id: Mapped[str] = mapped_column(String(32), nullable=False)
    old_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    at_revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at_at_event: Mapped[int] = mapped_column(BigInteger, nullable=False)
    master_audit_event_id: Mapped[str | None] = mapped_column(String(32))
    caller_audit_event_id: Mapped[str | None] = mapped_column(String(32))


class PageMoveEvent(_MoveColumns, Base):
    __tablename__ = "page_move_events"
    __table_args__ = _constraints(guard=False)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class PageMoveGuard(_MoveColumns, Base):
    __tablename__ = "page_move_guards"
    __table_args__ = _constraints(guard=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["PageMoveEvent", "PageMoveGuard"]
