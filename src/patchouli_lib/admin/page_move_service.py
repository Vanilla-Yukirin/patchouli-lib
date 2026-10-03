"""Move one active Page without copying its identity or content history."""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import Engine, func, select

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.move_receipts import (
    MasterMoveReceipt,
    MasterMoveReceiptRepository,
    validate_master_move_receipt,
)
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.service import AuthenticationError, utc_microseconds
from patchouli_lib.content.page_move_core import (
    PageMoveNotFoundError,
    apply_page_move,
    prepare_page_move,
)
from patchouli_lib.content.page_move_models import PageMoveGuard
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ContentSchema,
    OpaqueId,
    PageId,
    StrongPageETag,
)
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_request_fingerprint
from patchouli_lib.identifiers import validate_page_id

MasterPageMoveNotFoundError = PageMoveNotFoundError


class MasterPageMoveConflictError(RuntimeError):
    """The operation key already belongs to a different request."""


class MasterPageMoveCommand(ContentSchema):
    library_id: OpaqueId
    page_id: PageId
    source_section_id: OpaqueId
    source_book_id: OpaqueId
    target_section_id: OpaqueId
    target_book_id: OpaqueId
    expected_etag: StrongPageETag = Field(repr=False)

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)


@dataclass(frozen=True, slots=True)
class MasterPageMoveResult:
    receipt: MasterMoveReceipt
    replayed: bool


class MasterPageMoveService:
    """Authenticate and persist the complete movement in one write transaction."""

    def __init__(self, engine: Engine, *, clock: Callable[[], int] = utc_microseconds) -> None:
        self._engine = engine
        self._clock = clock

    def move_page(
        self,
        command: MasterPageMoveCommand,
        idempotency: ArchiveIdempotencyKey,
        *,
        master_session: MasterAdminSession,
    ) -> MasterPageMoveResult:
        fingerprint = digest_request_fingerprint(
            b"master-page-move-v1",
            json.dumps(command.model_dump(), sort_keys=True, separators=(",", ":")).encode(),
        )
        with immediate_transaction(self._engine) as connection:
            if (
                type(master_session) is not MasterAdminSession
                or master_session.expires_at <= int(time())
                or not MasterTokenRepository(connection).is_session_generation_current(
                    master_session.identity_id, master_session.session_generation
                )
            ):
                raise AuthenticationError
            raw = connection.connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise RuntimeError("Page movement requires SQLite.")
            receipts = MasterMoveReceiptRepository(connection)
            previous = receipts.find(master_session.identity_id, "move", idempotency.key_digest)
            if previous is not None:
                if not hmac.compare_digest(previous.request_fingerprint, fingerprint):
                    raise MasterPageMoveConflictError
                validate_master_move_receipt(raw, previous)
                result = MasterPageMoveResult(previous, True)
            else:
                now = self._clock()
                plan = prepare_page_move(
                    connection,
                    library_id=command.library_id,
                    page_id=command.page_id,
                    source_section_id=command.source_section_id,
                    source_book_id=command.source_book_id,
                    target_section_id=command.target_section_id,
                    target_book_id=command.target_book_id,
                    expected_etag=command.expected_etag,
                    operation_at=now,
                )
                page = plan.page
                expected = command.expected_etag
                changed = plan.changed
                updated_at = plan.updated_at
                sequence = plan.sequence
                audit_id = None
                if changed:
                    audit_id = uuid4().hex
                    MasterAuditRepository(connection).add_success(
                        identity_id=master_session.identity_id,
                        session_generation=master_session.session_generation,
                        session_fingerprint=master_session.audit_fingerprint(),
                        action="content.page.move",
                        target_type="page",
                        target_id=f"{page.library_id}:{page.page_uid.hex()}",
                        occurred_at=updated_at,
                        event_id=audit_id,
                    )
                apply_page_move(connection, plan, master_audit_event_id=audit_id)
                receipt = MasterMoveReceipt(
                    identity_id=master_session.identity_id,
                    operation="move",
                    key_digest=idempotency.key_digest,
                    request_fingerprint=fingerprint,
                    library_id=page.library_id,
                    page_uid=page.page_uid,
                    page_id=page.page_id,
                    source_section_id=page.section_id,
                    source_book_id=page.book_id,
                    target_section_id=plan.target_section_id,
                    target_book_id=plan.target_book_id,
                    revision_id=page.current_revision_id,
                    revision_number=page.current_revision_number,
                    original_occurred_at=page.occurred_at,
                    original_page_updated_at=page.updated_at,
                    result_updated_at=updated_at,
                    request_etag=expected,
                    response_etag=page_current_etag(
                        page.page_uid,
                        page.current_revision_id,
                        page.current_revision_number,
                        page.occurred_at,
                        updated_at,
                    ),
                    operation_at=now,
                    changed=1 if changed else 0,
                    move_sequence=sequence,
                    master_audit_event_id=audit_id,
                )
                receipts.add(receipt)
                validate_master_move_receipt(raw, receipt)
                if connection.scalar(
                    select(func.count())
                    .select_from(PageMoveGuard)
                    .where(
                        PageMoveGuard.library_id == page.library_id,
                        PageMoveGuard.page_uid == page.page_uid,
                    )
                ):
                    raise RuntimeError("Page movement left an incomplete transition.")
                result = MasterPageMoveResult(receipt, False)
        return result
