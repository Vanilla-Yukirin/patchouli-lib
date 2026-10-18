"""Small lifecycle adapter over existing guarded Page mutation and Caller auth."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import Connection

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, NewAuditEvent
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.page_lifecycle_receipts import validate_library_lifecycle_receipt
from patchouli_lib.content.page_lifecycle_schemas import (
    LIBRARY_LIFECYCLE_REVISION,
    LibraryPageLifecycleBody,
    PageLifecycleCommand,
    lifecycle_fingerprint,
    lifecycle_key_digest,
    lifecycle_route,
)
from patchouli_lib.content.page_move_core import PageMoveNotFoundError
from patchouli_lib.content.page_move_service import authorize_page_move
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.idempotency.repository import IdempotencyRepository
from patchouli_lib.idempotency.schemas import (
    IdempotencyRequest,
    OriginalResponse,
    ReplayResponse,
    TransactionValidatedCaller,
)
from patchouli_lib.idempotency.service import IdempotencyService
from patchouli_lib.identifiers import canonical_utc_wire


class PageLifecycleStateConflictError(RuntimeError):
    """The requested transition does not change the present state."""


class CallerPageLifecycleService:
    def __init__(
        self, connection: Connection, *, clock: Callable[[], int] = utc_microseconds
    ) -> None:
        self._connection = connection
        self._clock = clock

    def transition(
        self, token: str, command: PageLifecycleCommand, idempotency: ArchiveIdempotencyKey
    ) -> OriginalResponse | ReplayResponse:
        raw = self._connection.connection.driver_connection
        if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
            raise RuntimeError("Caller lifecycle requires a concrete outer write transaction.")
        with self._connection.begin_nested():
            schema_revision = raw.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if schema_revision != LIBRARY_LIFECYCLE_REVISION:
                raise RuntimeError("Library lifecycle behavior version is unavailable.")
            now = self._clock()
            if type(now) is not int or now < 0:
                raise ValueError("Invalid lifecycle time.")
            canonical_utc_wire(now)
            # Identical explicit Library WRITE policy; no Section fallback or READ implication.
            authenticated = authorize_page_move(
                self._connection, token, command.library_id, now=now
            )
            caller = TransactionValidatedCaller(
                library_id=command.library_id,
                actor_home_library_id=authenticated.caller.library_id,
                caller_id=authenticated.caller.id,
            )
            method, route = lifecycle_route(command.action)
            request = IdempotencyRequest(
                method=method,
                route_template=route,
                key_digest=lifecycle_key_digest(
                    idempotency.key_digest, caller.actor_home_library_id
                ),
                request_fingerprint=lifecycle_fingerprint(command),
            )
            receipts = IdempotencyRepository(self._connection)
            successes = IdempotencyService(receipts)
            replay = successes.lookup(caller, request)
            if replay is not None:
                record = receipts.get(caller, request)
                assert record is not None
                validate_library_lifecycle_receipt(raw, record, schema_revision=schema_revision)
                return replay
            content = ContentRepository(self._connection)
            page = content.get_page(command.library_id, command.page_id)
            if page is None or page.page_type != "archive":
                raise PageMoveNotFoundError
            etag = page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            )
            if etag != command.expected_etag:
                raise FileSetPreconditionFailedError
            if (page.deleted_at is None) != (command.action == "delete"):
                raise PageLifecycleStateConflictError
            content.get_current_file_manifest(page)
            updated, event = content.transition_page_lifecycle(
                page,
                action=command.action,
                actor_caller_id=caller.caller_id,
                actor_home_library_id=caller.actor_home_library_id,
                request_id=command.request_id,
                changed_at=now,
            )
            AuthRepository(self._connection).add_audit_event(
                NewAuditEvent(
                    id=uuid4().hex,
                    library_id=command.library_id,
                    actor_home_library_id=caller.actor_home_library_id,
                    actor_caller_id=caller.caller_id,
                    actor_credential_id=authenticated.credential.id,
                    action=f"content.page.{command.action}",
                    resource_type="page",
                    resource_id=page.page_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=command.request_id,
                    occurred_at=event.changed_at,
                )
            )
            body = LibraryPageLifecycleBody(
                library_id=page.library_id,
                page_id=page.page_id,
                section_id=page.section_id,
                book_id=page.book_id,
                state="trashed" if updated.deleted_at is not None else "active",
                deleted_at=canonical_utc_wire(updated.deleted_at)
                if updated.deleted_at is not None
                else None,
                revision_id=page.current_revision_id,
                revision_number=page.current_revision_number,
                occurred_at=canonical_utc_wire(page.occurred_at),
                original_updated_at=canonical_utc_wire(page.updated_at),
                updated_at=canonical_utc_wire(updated.updated_at),
                request_etag=command.expected_etag,
            )
            response = OriginalResponse(
                response_status=200,
                response_body=body.model_dump_json().encode(),
                response_etag=page_current_etag(
                    updated.page_uid,
                    updated.current_revision_id,
                    updated.current_revision_number,
                    updated.occurred_at,
                    updated.updated_at,
                ),
                original_request_id=command.request_id,
                original_request_timestamp=body.updated_at,
            )
            record = successes.record_success(caller, request, response)
            validate_library_lifecycle_receipt(raw, record, schema_revision=schema_revision)
            return response
