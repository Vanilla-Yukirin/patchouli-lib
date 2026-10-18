"""Validate a frozen Caller success against exact historical states and audit."""

from __future__ import annotations

import sqlite3

from patchouli_lib.content.page_membership_history import load_page_state_timeline
from patchouli_lib.content.page_move_schemas import (
    PAGE_MOVE_ROUTE_TEMPLATE,
    PageMoveBody,
    PageMoveCommand,
    move_fingerprint,
)
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.idempotency.schemas import StoredIdempotencyRecord
from patchouli_lib.identifiers import page_id_registry_digest, parse_occurrence_time


class PageMoveReplayCorruptError(RuntimeError):
    """A frozen move cannot prove its exact original state and actor."""


def validate_caller_move_receipt(
    connection: sqlite3.Connection,
    record: StoredIdempotencyRecord,
    *,
    schema_revision: str,
) -> PageMoveBody:
    try:
        body = PageMoveBody.model_validate_json(record.response_body)
        # Canonical exact bytes also reject duplicate keys/unknown wire shapes.
        if body.model_dump_json().encode() != record.response_body:
            raise ValueError
        row = connection.execute(
            "SELECT page_uid FROM page_identifier_registry WHERE library_id = ? "
            "AND identifier_digest = ? AND identifier_text = ?",
            (record.library_id, page_id_registry_digest(body.page_id), body.page_id),
        ).fetchone()
        if row is None:
            raise ValueError
        page_uid = row[0]
        timeline = load_page_state_timeline(
            connection,
            schema_revision=schema_revision,
            library_id=record.library_id,
            page_uid=page_uid,
        )
        source = timeline.exact_state(
            parse_occurrence_time(body.original_updated_at).utc_microseconds
        )
        result = timeline.exact_state(parse_occurrence_time(body.updated_at).utc_microseconds)
        command = PageMoveCommand(
            **body.model_dump(
                include={
                    "library_id",
                    "page_id",
                    "source_section_id",
                    "source_book_id",
                    "target_section_id",
                    "target_book_id",
                }
            ),
            expected_etag=body.request_etag,
            request_id=record.original_request_id,
        )
        if (
            record.method != "POST"
            or record.route_template != PAGE_MOVE_ROUTE_TEMPLATE
            or record.response_status != 200
            or record.response_location is not None
            or body.library_id != record.library_id
            or timeline.page_id != body.page_id
            or source.deleted_at is not None
            or result.deleted_at is not None
            or (source.section_id, source.book_id) != (body.source_section_id, body.source_book_id)
            or (result.section_id, result.book_id) != (body.target_section_id, body.target_book_id)
            or source.revision_id != result.revision_id
            or result.revision_id != body.revision_id
            or source.revision_number != result.revision_number
            or result.revision_number != body.revision_number
            or source.occurred_at != result.occurred_at
            or source.occurred_at != parse_occurrence_time(body.occurred_at).utc_microseconds
            or source.title != result.title
            or move_fingerprint(command) != record.request_fingerprint
            or body.request_etag
            != page_current_etag(
                page_uid,
                source.revision_id,
                source.revision_number,
                source.occurred_at,
                source.updated_at,
            )
            or record.response_etag
            != page_current_etag(
                page_uid,
                result.revision_id,
                result.revision_number,
                result.occurred_at,
                result.updated_at,
            )
        ):
            raise ValueError
        if body.changed:
            events = connection.execute(
                "SELECT e.old_section_id, e.old_book_id, e.new_section_id, e.new_book_id, "
                "e.old_updated_at, e.at_revision_id, e.at_revision_number, e.occurred_at_at_event, "
                "a.library_id, a.actor_home_library_id, a.actor_caller_id, a.action, "
                "a.resource_type, a.resource_id, a.outcome, a.request_id, a.occurred_at "
                "FROM page_move_events e JOIN auth_audit_events a ON a.id=e.caller_audit_event_id "
                "WHERE e.library_id=? AND e.page_uid=? AND e.changed_at=? "
                "AND e.master_audit_event_id IS NULL",
                (record.library_id, page_uid, result.updated_at),
            ).fetchall()
            if (
                events
                != [
                    (
                        body.source_section_id,
                        body.source_book_id,
                        body.target_section_id,
                        body.target_book_id,
                        source.updated_at,
                        body.revision_id,
                        body.revision_number,
                        source.occurred_at,
                        record.library_id,
                        record.actor_home_library_id,
                        record.caller_id,
                        "content.page.move",
                        "page",
                        body.page_id,
                        "succeeded",
                        record.original_request_id,
                        result.updated_at,
                    )
                ]
                or source.updated_at >= result.updated_at
            ):
                raise ValueError
        elif source != result:
            raise ValueError
        return body
    except (ValueError, sqlite3.Error, RuntimeError) as exc:
        raise PageMoveReplayCorruptError("Stored movement success is invalid.") from exc
