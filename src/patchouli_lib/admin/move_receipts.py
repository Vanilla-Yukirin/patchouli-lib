"""Frozen master movement successes, including durable unchanged results."""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import Field
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    insert,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.content.page_membership_history import (
    PageMembershipHistoryError,
    PageState,
    load_page_state_timeline,
)
from patchouli_lib.content.schemas import (
    ContentSchema,
    OpaqueId,
    PageId,
    RevisionId,
    StrongPageETag,
)
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.models import Base


class MasterMoveReceiptRow(Base):
    __tablename__ = "admin_master_move_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_book_id", "source_section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_book_id", "target_section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["library_id", "page_uid", "move_sequence"],
            [
                "page_move_events.library_id",
                "page_move_events.page_uid",
                "page_move_events.sequence",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("library_id", "page_uid", "move_sequence"),
        UniqueConstraint("master_audit_event_id"),
        CheckConstraint("length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'"),
        CheckConstraint("operation = 'move'"),
        CheckConstraint(
            "typeof(key_digest) = 'blob' AND length(key_digest) = 32 "
            "AND typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32"
        ),
        CheckConstraint("typeof(page_uid) = 'blob' AND length(page_uid) = 16"),
        CheckConstraint("typeof(changed) = 'integer' AND changed IN (0, 1)"),
        CheckConstraint(
            "typeof(operation_at) = 'integer' "
            "AND operation_at BETWEEN 0 AND 253402300799999999 "
            "AND typeof(original_page_updated_at) = 'integer' AND original_page_updated_at >= 0 "
            "AND typeof(result_updated_at) = 'integer' "
            "AND result_updated_at BETWEEN original_page_updated_at AND 253402300799999999"
        ),
        CheckConstraint(
            "typeof(original_occurred_at) = 'integer' "
            "AND original_occurred_at BETWEEN -62135596800000000 AND 253402300799999999"
        ),
        CheckConstraint(
            "(changed = 1 AND move_sequence IS NOT NULL AND master_audit_event_id IS NOT NULL "
            "AND result_updated_at > original_page_updated_at "
            "AND (source_section_id != target_section_id OR source_book_id != target_book_id)) "
            "OR (changed = 0 AND move_sequence IS NULL AND master_audit_event_id IS NULL "
            "AND result_updated_at = original_page_updated_at "
            "AND source_section_id = target_section_id AND source_book_id = target_book_id "
            "AND request_etag = response_etag)"
        ),
    )
    identity_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation: Mapped[str] = mapped_column(String(4), primary_key=True)
    key_digest: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    library_id: Mapped[str] = mapped_column(String(32), nullable=False)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    page_id: Mapped[str] = mapped_column(String(80), nullable=False)
    source_section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_book_id: Mapped[str] = mapped_column(String(32), nullable=False)
    target_section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    target_book_id: Mapped[str] = mapped_column(String(32), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_page_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_etag: Mapped[str] = mapped_column(String(100), nullable=False)
    response_etag: Mapped[str] = mapped_column(String(100), nullable=False)
    operation_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    move_sequence: Mapped[int | None] = mapped_column(BigInteger)
    master_audit_event_id: Mapped[str | None] = mapped_column(String(32))


class MasterMoveReceipt(ContentSchema):
    identity_id: OpaqueId
    operation: Literal["move"]
    key_digest: bytes = Field(min_length=32, max_length=32, repr=False)
    request_fingerprint: bytes = Field(min_length=32, max_length=32, repr=False)
    library_id: OpaqueId
    page_uid: bytes = Field(min_length=16, max_length=16)
    page_id: PageId
    source_section_id: OpaqueId
    source_book_id: OpaqueId
    target_section_id: OpaqueId
    target_book_id: OpaqueId
    revision_id: RevisionId
    revision_number: int = Field(ge=1)
    original_occurred_at: int
    original_page_updated_at: int = Field(ge=0)
    result_updated_at: int = Field(ge=0)
    request_etag: StrongPageETag
    response_etag: StrongPageETag
    operation_at: int = Field(ge=0)
    changed: Literal[0, 1]
    move_sequence: int | None
    master_audit_event_id: OpaqueId | None


class MasterMoveReceiptRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def find(self, identity_id: str, operation: str, key_digest: bytes) -> MasterMoveReceipt | None:
        row = (
            self._connection.execute(
                select(MasterMoveReceiptRow.__table__).where(
                    MasterMoveReceiptRow.identity_id == identity_id,
                    MasterMoveReceiptRow.operation == operation,
                    MasterMoveReceiptRow.key_digest == key_digest,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else MasterMoveReceipt.model_validate(dict(row))

    def add(self, receipt: MasterMoveReceipt) -> None:
        if not self._connection.in_transaction():
            raise RuntimeError("Master move receipt requires an active transaction.")
        self._connection.execute(insert(MasterMoveReceiptRow), receipt.model_dump())


class MasterMoveReceiptCorruptError(RuntimeError):
    """The frozen success does not match its exact immutable state history."""


def validate_master_move_receipt(
    connection: sqlite3.Connection,
    receipt: MasterMoveReceipt,
    *,
    schema_revision: str | None = None,
) -> PageState:

    if schema_revision is None:
        schema_revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[
            0
        ]
    try:
        timeline = load_page_state_timeline(
            connection,
            schema_revision=schema_revision,
            library_id=receipt.library_id,
            page_uid=receipt.page_uid,
        )
        source = timeline.exact_state(receipt.original_page_updated_at)
        result = timeline.exact_state(receipt.result_updated_at)
        valid = (
            timeline.page_id == receipt.page_id
            and source.deleted_at is None
            and result.deleted_at is None
            and (source.section_id, source.book_id)
            == (receipt.source_section_id, receipt.source_book_id)
            and (result.section_id, result.book_id)
            == (receipt.target_section_id, receipt.target_book_id)
            and source.revision_id == result.revision_id == receipt.revision_id
            and source.revision_number == result.revision_number == receipt.revision_number
            and source.occurred_at == result.occurred_at == receipt.original_occurred_at
            and source.title == result.title
            and receipt.request_etag
            == page_current_etag(
                receipt.page_uid,
                source.revision_id,
                source.revision_number,
                source.occurred_at,
                source.updated_at,
            )
            and receipt.response_etag
            == page_current_etag(
                receipt.page_uid,
                result.revision_id,
                result.revision_number,
                result.occurred_at,
                result.updated_at,
            )
        )
        if receipt.changed:
            event = connection.execute(
                "SELECT old_section_id, old_book_id, new_section_id, new_book_id, old_updated_at, "
                "changed_at, at_revision_id, at_revision_number, occurred_at_at_event, "
                "master_audit_event_id FROM page_move_events "
                "WHERE library_id = ? AND page_uid = ? AND sequence = ?",
                (receipt.library_id, receipt.page_uid, receipt.move_sequence),
            ).fetchone()
            audit = connection.execute(
                "SELECT identity_id, action, target_type, target_id, occurred_at "
                "FROM admin_master_audit_events WHERE id = ?",
                (receipt.master_audit_event_id,),
            ).fetchone()
            valid = (
                valid
                and event
                == (
                    receipt.source_section_id,
                    receipt.source_book_id,
                    receipt.target_section_id,
                    receipt.target_book_id,
                    source.updated_at,
                    result.updated_at,
                    receipt.revision_id,
                    receipt.revision_number,
                    receipt.original_occurred_at,
                    receipt.master_audit_event_id,
                )
                and audit
                == (
                    receipt.identity_id,
                    "content.page.move",
                    "page",
                    f"{receipt.library_id}:{receipt.page_uid.hex()}",
                    result.updated_at,
                )
            )
        else:
            valid = (
                valid
                and source == result
                and receipt.move_sequence is None
                and receipt.master_audit_event_id is None
            )
        if not valid:
            raise PageMembershipHistoryError
    except (ValueError, PageMembershipHistoryError) as exc:
        raise MasterMoveReceiptCorruptError("Stored move success is invalid.") from exc
    return result


__all__ = [
    "MasterMoveReceipt",
    "MasterMoveReceiptRow",
    "MasterMoveReceiptRepository",
    "MasterMoveReceiptCorruptError",
    "validate_master_move_receipt",
]
