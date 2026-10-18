"""Immutable master-owned success records, separate from Agent idempotency."""

from __future__ import annotations

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

from patchouli_lib.content.schemas import (
    ContentSchema,
    OpaqueId,
    PageId,
    RevisionId,
    StrongPageETag,
)
from patchouli_lib.models import Base


class MasterFileSetReceiptRow(Base):
    __tablename__ = "admin_master_file_set_receipts"
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
            ["book_id", "section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["library_id", "source_id"],
            ["page_sources.library_id", "page_sources.source_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("library_id", "source_id"),
        UniqueConstraint("master_audit_event_id"),
        CheckConstraint("length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'"),
        CheckConstraint("operation IN ('create', 'revise')"),
        CheckConstraint("typeof(key_digest) = 'blob' AND length(key_digest) = 32"),
        CheckConstraint(
            "typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32"
        ),
        CheckConstraint("typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32"),
        CheckConstraint("typeof(page_uid) = 'blob' AND length(page_uid) = 16"),
        CheckConstraint("typeof(changed) = 'integer' AND changed IN (0, 1)"),
        CheckConstraint("operation != 'create' OR (changed = 1 AND revision_number = 1)"),
        CheckConstraint(
            "(changed = 1 AND source_id IS NOT NULL AND master_audit_event_id IS NOT NULL) OR "
            "(changed = 0 AND source_id IS NULL AND master_audit_event_id IS NULL)"
        ),
        CheckConstraint(
            "typeof(operation_at) = 'integer' AND operation_at >= 0 AND "
            "typeof(original_page_updated_at) = 'integer' AND original_page_updated_at >= 0"
        ),
        CheckConstraint(
            "typeof(original_occurred_at) = 'integer' AND "
            "original_occurred_at BETWEEN -62135596800000000 AND 253402300799999999"
        ),
    )

    identity_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation: Mapped[str] = mapped_column(String(6), primary_key=True)
    key_digest: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    library_id: Mapped[str] = mapped_column(String(32), nullable=False)
    section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    book_id: Mapped[str] = mapped_column(String(32), nullable=False)
    page_id: Mapped[str] = mapped_column(String(80), nullable=False)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    changed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_page_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    response_etag: Mapped[str] = mapped_column(String(100), nullable=False)
    operation_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(32))
    master_audit_event_id: Mapped[str | None] = mapped_column(String(32))


class MasterFileSetReceipt(ContentSchema):
    identity_id: OpaqueId
    operation: Literal["create", "revise"]
    key_digest: bytes = Field(min_length=32, max_length=32, repr=False)
    request_fingerprint: bytes = Field(min_length=32, max_length=32, repr=False)
    library_id: OpaqueId
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    page_uid: bytes = Field(min_length=16, max_length=16)
    revision_id: RevisionId
    revision_number: int = Field(ge=1)
    snapshot_sha256: bytes = Field(min_length=32, max_length=32)
    changed: Literal[0, 1]
    original_occurred_at: int
    original_page_updated_at: int = Field(ge=0)
    response_etag: StrongPageETag
    operation_at: int = Field(ge=0)
    source_id: OpaqueId | None
    master_audit_event_id: OpaqueId | None


class MasterFileSetReceiptRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def find(
        self, identity_id: str, operation: str, key_digest: bytes
    ) -> MasterFileSetReceipt | None:
        row = (
            self._connection.execute(
                select(MasterFileSetReceiptRow.__table__).where(
                    MasterFileSetReceiptRow.identity_id == identity_id,
                    MasterFileSetReceiptRow.operation == operation,
                    MasterFileSetReceiptRow.key_digest == key_digest,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else MasterFileSetReceipt.model_validate(dict(row))

    def add(self, receipt: MasterFileSetReceipt) -> None:
        if not self._connection.in_transaction():
            raise RuntimeError("Master file-set receipt requires an active transaction.")
        self._connection.execute(insert(MasterFileSetReceiptRow), receipt.model_dump())
