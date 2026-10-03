"""Exact historical proof of a Library-explicit Caller lifecycle success."""

from __future__ import annotations

import sqlite3
from typing import Literal

from patchouli_lib.content.page_lifecycle_schemas import (
    LIBRARY_LIFECYCLE_REVISION,
    LIBRARY_LIFECYCLE_ROUTES,
    LibraryPageLifecycleBody,
    PageLifecycleCommand,
    lifecycle_fingerprint,
    lifecycle_route,
)
from patchouli_lib.content.page_membership_history import load_page_state_timeline
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.idempotency.schemas import StoredIdempotencyRecord
from patchouli_lib.identifiers import (
    canonical_utc_wire,
    page_id_registry_digest,
    parse_occurrence_time,
)


class PageLifecycleReplayCorruptError(RuntimeError):
    """Stored lifecycle success cannot prove its historical actor and transition."""


def validate_library_lifecycle_receipt(
    connection: sqlite3.Connection, record: StoredIdempotencyRecord, *, schema_revision: str
) -> LibraryPageLifecycleBody:
    try:
        if schema_revision != LIBRARY_LIFECYCLE_REVISION:
            raise ValueError
        body = LibraryPageLifecycleBody.model_validate_json(record.response_body)
        if body.model_dump_json().encode() != record.response_body:
            raise ValueError
        row = connection.execute(
            "SELECT page_uid FROM page_identifier_registry WHERE library_id=? "
            "AND identifier_digest=? AND identifier_text=?",
            (record.library_id, page_id_registry_digest(body.page_id), body.page_id),
        ).fetchone()
        if row is None:
            raise ValueError
        uid = row[0]
        timeline = load_page_state_timeline(
            connection, schema_revision=schema_revision, library_id=record.library_id, page_uid=uid
        )
        before = timeline.exact_state(
            parse_occurrence_time(body.original_updated_at).utc_microseconds
        )
        after = timeline.exact_state(parse_occurrence_time(body.updated_at).utc_microseconds)
        action: Literal["delete", "restore"] = "delete" if body.state == "trashed" else "restore"
        command = PageLifecycleCommand(
            library_id=body.library_id,
            page_id=body.page_id,
            action=action,
            expected_etag=body.request_etag,
            request_id=record.original_request_id,
        )
        if (
            (record.method, record.route_template) != lifecycle_route(action)
            or record.response_status != 200
            or record.response_media_type != "application/json"
            or record.response_location is not None
            or body.library_id != record.library_id
            or body.page_id != timeline.page_id
            or before.updated_at >= after.updated_at
            or before.section_id != after.section_id
            or after.section_id != body.section_id
            or before.book_id != after.book_id
            or after.book_id != body.book_id
            or before.title != after.title
            or before.revision_id != after.revision_id
            or after.revision_id != body.revision_id
            or before.revision_number != after.revision_number
            or after.revision_number != body.revision_number
            or before.occurred_at != after.occurred_at
            or canonical_utc_wire(after.occurred_at) != body.occurred_at
            or (before.deleted_at is None) != (action == "delete")
            or after.deleted_at != (after.updated_at if action == "delete" else None)
            or body.deleted_at != (body.updated_at if action == "delete" else None)
            or record.original_request_timestamp != body.updated_at
            or lifecycle_fingerprint(command) != record.request_fingerprint
            or body.request_etag
            != page_current_etag(
                uid,
                before.revision_id,
                before.revision_number,
                before.occurred_at,
                before.updated_at,
            )
            or record.response_etag
            != page_current_etag(
                uid, after.revision_id, after.revision_number, after.occurred_at, after.updated_at
            )
        ):
            raise ValueError
        events = connection.execute(
            "SELECT action, section_id, old_deleted_at, old_updated_at, at_revision_number, "
            "occurred_at_at_event, actor_caller_id, actor_home_library_id, request_id, "
            "master_audit_event_id "
            "FROM page_lifecycle_events WHERE library_id=? AND page_uid=? AND changed_at=?",
            (record.library_id, uid, after.updated_at),
        ).fetchall()
        if events != [
            (
                action,
                body.section_id,
                before.deleted_at,
                before.updated_at,
                body.revision_number,
                before.occurred_at,
                record.caller_id,
                record.actor_home_library_id,
                record.original_request_id,
                None,
            )
        ]:
            raise ValueError
        audits = connection.execute(
            "SELECT a.actor_home_library_id, a.actor_caller_id, c.caller_id, c.library_id, "
            "a.resource_type, a.outcome FROM auth_audit_events a "
            "JOIN auth_credentials c ON c.id=a.actor_credential_id "
            "WHERE a.library_id=? AND a.action=? AND a.resource_id=? "
            "AND a.request_id=? AND a.occurred_at=?",
            (
                record.library_id,
                f"content.page.{action}",
                body.page_id,
                record.original_request_id,
                after.updated_at,
            ),
        ).fetchall()
        if audits != [
            (
                record.actor_home_library_id,
                record.caller_id,
                record.caller_id,
                record.actor_home_library_id,
                "page",
                "succeeded",
            )
        ]:
            raise ValueError
        return body
    except (ValueError, sqlite3.Error, RuntimeError) as exc:
        raise PageLifecycleReplayCorruptError("Stored lifecycle success is invalid.") from exc


def require_library_lifecycle_graph(
    connection: sqlite3.Connection, *, schema_revision: str
) -> None:
    """Reject duplicate/orphan new successes and audits in either direction."""
    cursor = connection.execute("SELECT * FROM idempotency_records")
    names = tuple(column[0] for column in cursor.description)
    expected: list[tuple[str, str, str, str, str, str, int]] = []
    for values in cursor.fetchall():
        row = dict(zip(names, values, strict=True))
        if row["route_template"] not in LIBRARY_LIFECYCLE_ROUTES:
            continue
        record = StoredIdempotencyRecord.model_validate(row)
        body = validate_library_lifecycle_receipt(
            connection, record, schema_revision=schema_revision
        )
        expected.append(
            (
                record.library_id,
                record.actor_home_library_id,
                record.caller_id,
                f"content.page.{'delete' if body.state == 'trashed' else 'restore'}",
                body.page_id,
                record.original_request_id,
                parse_occurrence_time(body.updated_at).utc_microseconds,
            )
        )
    actual = connection.execute(
        "SELECT library_id, actor_home_library_id, actor_caller_id, action, "
        "resource_id, request_id, occurred_at "
        "FROM auth_audit_events WHERE action IN ('content.page.delete','content.page.restore')"
    ).fetchall()
    if sorted(expected) != sorted(actual) or len(set(expected)) != len(expected):
        raise PageLifecycleReplayCorruptError("Lifecycle audit and success graph is invalid.")
