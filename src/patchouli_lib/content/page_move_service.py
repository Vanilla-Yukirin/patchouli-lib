"""Authorized Caller movement, real audit and durable success in one write lock."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import Connection

from patchouli_lib.auth.library_policy import LegacySectionPolicy, LibraryAction
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, AuthenticatedCaller, CallerKind, NewAuditEvent
from patchouli_lib.auth.service import (
    LAST_USED_COALESCE_MICROSECONDS,
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
    utc_microseconds,
)
from patchouli_lib.content.page_move_core import apply_page_move, prepare_page_move
from patchouli_lib.content.page_move_receipts import validate_caller_move_receipt
from patchouli_lib.content.page_move_schemas import (
    PAGE_MOVE_ROUTE_TEMPLATE,
    PageMoveBody,
    PageMoveCommand,
    caller_move_key_digest,
    move_fingerprint,
)
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


def authorize_page_move(
    connection: Connection,
    token_value: str,
    library_id: str,
    *,
    now: int,
    touch_last_used: bool = True,
) -> AuthenticatedCaller:
    repository = AuthRepository(connection)
    authenticated = AuthenticationService(
        repository,
        clock=lambda: now,
        last_used_coalesce_microseconds=LAST_USED_COALESCE_MICROSECONDS if touch_last_used else -1,
    ).authenticate(token_value)
    if authenticated.caller.kind is not CallerKind.AGENT:
        raise AuthorizationError
    policy = repository.get_library_policy(
        credential_id=authenticated.credential.id,
        caller_id=authenticated.caller.id,
        home_library_id=authenticated.caller.library_id,
        target_library_id=library_id,
        active_at=now,
    )
    if policy is None:
        raise AuthenticationError
    if isinstance(policy, LegacySectionPolicy) or not policy.allows(LibraryAction.WRITE):
        raise AuthorizationError
    return authenticated


class CallerPageMoveService:
    def __init__(
        self, connection: Connection, *, clock: Callable[[], int] = utc_microseconds
    ) -> None:
        self._connection = connection
        self._clock = clock

    def move_page(
        self,
        token_value: str,
        command: PageMoveCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> OriginalResponse | ReplayResponse:
        raw = self._connection.connection.driver_connection
        if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
            raise RuntimeError("Caller move requires a concrete outer write transaction.")
        with self._connection.begin_nested():
            now = self._clock()
            if type(now) is not int or now < 0:
                raise ValueError("Invalid movement time.")
            canonical_utc_wire(now)
            repository = AuthRepository(self._connection)
            authenticated = authorize_page_move(
                self._connection, token_value, command.library_id, now=now
            )
            caller = TransactionValidatedCaller(
                library_id=command.library_id,
                actor_home_library_id=authenticated.caller.library_id,
                caller_id=authenticated.caller.id,
            )
            request = IdempotencyRequest(
                method="POST",
                route_template=PAGE_MOVE_ROUTE_TEMPLATE,
                key_digest=caller_move_key_digest(
                    idempotency.key_digest, caller.actor_home_library_id
                ),
                request_fingerprint=move_fingerprint(command),
            )
            receipts = IdempotencyRepository(self._connection)
            successes = IdempotencyService(receipts)
            replay = successes.lookup(caller, request)
            schema_revision = raw.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if replay is not None:
                record = receipts.get(caller, request)
                assert record is not None
                validate_caller_move_receipt(raw, record, schema_revision=schema_revision)
                return replay
            plan = prepare_page_move(
                self._connection, **command.model_dump(exclude={"request_id"}), operation_at=now
            )
            audit_id = None
            if plan.changed:
                audit_id = uuid4().hex
                repository.add_audit_event(
                    NewAuditEvent(
                        id=audit_id,
                        library_id=command.library_id,
                        actor_home_library_id=authenticated.caller.library_id,
                        actor_caller_id=authenticated.caller.id,
                        actor_credential_id=authenticated.credential.id,
                        action="content.page.move",
                        resource_type="page",
                        resource_id=command.page_id,
                        outcome=AuditOutcome.SUCCEEDED,
                        request_id=command.request_id,
                        occurred_at=plan.updated_at,
                    )
                )
            apply_page_move(self._connection, plan, caller_audit_event_id=audit_id)
            page = plan.page
            body = PageMoveBody(
                **command.model_dump(
                    include={
                        "library_id",
                        "page_id",
                        "source_section_id",
                        "source_book_id",
                        "target_section_id",
                        "target_book_id",
                    }
                ),
                changed=plan.changed,
                revision_id=page.current_revision_id,
                revision_number=page.current_revision_number,
                occurred_at=canonical_utc_wire(page.occurred_at),
                original_updated_at=canonical_utc_wire(page.updated_at),
                updated_at=canonical_utc_wire(plan.updated_at),
                request_etag=command.expected_etag,
            )
            response = OriginalResponse(
                response_status=200,
                response_body=body.model_dump_json().encode(),
                response_etag=page_current_etag(
                    page.page_uid,
                    page.current_revision_id,
                    page.current_revision_number,
                    page.occurred_at,
                    plan.updated_at,
                ),
                original_request_id=command.request_id,
                original_request_timestamp=canonical_utc_wire(now),
            )
            record = successes.record_success(caller, request, response)
            validate_caller_move_receipt(raw, record, schema_revision=schema_revision)
            return response
