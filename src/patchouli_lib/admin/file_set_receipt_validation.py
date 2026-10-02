"""Verify frozen master write results against exact content and history.

Shared by transaction-local replay and closed SQLite backup verification.
This proves stored consistency, not the original fingerprint's preimage or
the occurrence of an HTTP request. It makes no authorization decision.
"""

from __future__ import annotations

import sqlite3

from patchouli_lib.admin.file_set_receipts import MasterFileSetReceipt
from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.page_membership_history import (
    PageMembershipHistoryError,
    load_page_state_timeline,
)
from patchouli_lib.content.service import page_current_etag


class MasterFileSetReceiptCorruptError(RuntimeError):
    """The stored result cannot be proven from its precise immutable history."""


def _require(condition: bool) -> None:
    if not condition:
        raise MasterFileSetReceiptCorruptError("Stored file-set success is invalid.")


def _active_states(
    connection: sqlite3.Connection,
    receipt: MasterFileSetReceipt,
    current_occurrence: int,
    revision_at: int,
) -> set[tuple[int, int]]:
    key = (receipt.library_id, receipt.page_uid)
    corrections = connection.execute(
        "SELECT old_occurred_at, new_occurred_at, corrected_at FROM page_occurrence_corrections "
        "WHERE library_id = ? AND page_uid = ? ORDER BY sequence",
        key,
    ).fetchall()
    occurrence = corrections[0][0] if corrections else current_occurrence
    for _old, new, changed_at in corrections:
        if changed_at >= revision_at:
            break
        occurrence = new
    states = {(occurrence, revision_at)}
    events: list[tuple[int, str, int | str]] = []
    for new, changed_at in connection.execute(
        "SELECT new_occurred_at, corrected_at FROM page_occurrence_corrections "
        "WHERE library_id = ? AND page_uid = ? AND at_revision_number = ?",
        (*key, receipt.revision_number),
    ):
        events.append((changed_at, "occurrence", new))
    for action, changed_at in connection.execute(
        "SELECT action, changed_at FROM page_lifecycle_events "
        "WHERE library_id = ? AND page_uid = ? AND at_revision_number = ?",
        (*key, receipt.revision_number),
    ):
        events.append((changed_at, "lifecycle", action))
    for (changed_at,) in connection.execute(
        "SELECT changed_at FROM page_title_events "
        "WHERE library_id = ? AND page_uid = ? AND at_revision_number = ?",
        (*key, receipt.revision_number),
    ):
        events.append((changed_at, "title", 0))
    next_revision = connection.execute(
        "SELECT created_at FROM revisions WHERE library_id = ? AND page_uid = ? "
        "AND revision_number = ?",
        (*key, receipt.revision_number + 1),
    ).fetchone()
    active = True
    previous = revision_at
    for changed_at, kind, value in sorted(events):
        _require(type(changed_at) is int and changed_at > previous)
        _require(next_revision is None or changed_at < next_revision[0])
        previous = changed_at
        if kind == "occurrence":
            _require(type(value) is int)
            occurrence = value
        elif kind == "lifecycle":
            _require(value in {"delete", "restore"})
            active = value == "restore"
        if active:
            states.add((occurrence, changed_at))
    return states


def validate_master_file_set_receipt(
    connection: sqlite3.Connection, receipt: MasterFileSetReceipt
) -> FileManifest:
    """Verify complete snapshot, original active clock, Source and audit links."""
    key = (receipt.library_id, receipt.page_uid, receipt.revision_id, receipt.revision_number)
    row = connection.execute(
        "SELECT p.page_id, p.section_id, p.book_id, p.occurred_at, p.created_at, "
        "r.created_at, r.content_md, "
        "r.content_size_bytes, r.content_sha256, m.storage_format, m.file_count, "
        "m.total_size_bytes, m.snapshot_sha256 FROM pages p JOIN revisions r "
        "ON r.library_id = p.library_id AND r.page_uid = p.page_uid "
        "JOIN revision_file_sets m ON m.library_id = r.library_id AND m.page_uid = r.page_uid "
        "AND m.revision_id = r.revision_id AND m.revision_number = r.revision_number "
        "WHERE r.library_id = ? AND r.page_uid = ? AND r.revision_id = ? AND r.revision_number = ?",
        key,
    ).fetchone()
    _require(row is not None)
    assert row is not None
    (
        page_id,
        section_id,
        book_id,
        occurrence,
        page_created,
        revision_at,
        md,
        md_size,
        md_hash,
        format_,
        count,
        total,
        digest,
    ) = row
    _require(page_id == receipt.page_id)
    revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    from patchouli_lib.backup.manifest import (
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
    )

    timeline = None
    if revision in {(PAGE_MOVE_SCHEMA_REVISION,), (CALLER_PAGE_MOVE_SCHEMA_REVISION,)}:
        try:
            timeline = load_page_state_timeline(
                connection,
                schema_revision=revision[0],
                library_id=receipt.library_id,
                page_uid=receipt.page_uid,
            )
            state = timeline.exact_state(receipt.original_page_updated_at)
            _require(
                state.deleted_at is None
                and state.revision_id == receipt.revision_id
                and state.revision_number == receipt.revision_number
                and state.occurred_at == receipt.original_occurred_at
                and (state.section_id, state.book_id) == (receipt.section_id, receipt.book_id)
            )
        except PageMembershipHistoryError:
            raise MasterFileSetReceiptCorruptError("Stored file-set success is invalid.") from None
    else:
        _require((section_id, book_id) == (receipt.section_id, receipt.book_id))
    _require(
        connection.execute(
            "SELECT count(*) FROM revision_file_seals WHERE library_id = ? AND page_uid = ? "
            "AND revision_id = ? AND revision_number = ?",
            key,
        ).fetchone()
        == (1,)
    )
    _require(
        connection.execute(
            "SELECT count(*) FROM revision_file_seal_guards WHERE library_id = ? AND page_uid = ? "
            "AND revision_id = ? AND revision_number = ?",
            key,
        ).fetchone()
        == (1,)
    )
    files = connection.execute(
        "SELECT filename, content_bytes, size_bytes, content_sha256 FROM revision_files "
        "WHERE library_id = ? AND page_uid = ? AND revision_id = ? AND revision_number = ? "
        "ORDER BY filename",
        key,
    ).fetchall()
    try:
        manifest = build_file_manifest((name, content) for name, content, _size, _digest in files)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise MasterFileSetReceiptCorruptError("Stored file-set success is invalid.") from None
    _require(len(files) == len(manifest.files) == count and manifest.total_size_bytes == total)
    _require(manifest.snapshot_sha256 == receipt.snapshot_sha256)
    _require(
        [(entry.name, entry.content_size_bytes, entry.content_sha256) for entry in manifest.files]
        == [(name, size, hash_) for name, _content, size, hash_ in files]
    )
    if format_ == "legacy_markdown":
        _require(
            len(manifest.files) == 1
            and manifest.files[0].name == "content.md"
            and manifest.files[0].content == md
            and manifest.files[0].content_size_bytes == md_size
            and manifest.files[0].content_sha256 == md_hash
            and digest is None
        )
    else:
        _require(
            format_ == "file_set_v1"
            and (md, md_size, md_hash) == (None, None, None)
            and digest == manifest.snapshot_sha256
        )
    if timeline is None:
        _require(
            (receipt.original_occurred_at, receipt.original_page_updated_at)
            in _active_states(connection, receipt, occurrence, revision_at)
        )
    _require(
        receipt.response_etag
        == page_current_etag(
            receipt.page_uid,
            receipt.revision_id,
            receipt.revision_number,
            receipt.original_occurred_at,
            receipt.original_page_updated_at,
        )
    )
    if receipt.operation == "create":
        _require(
            receipt.changed == 1
            and receipt.revision_number == 1
            and page_created == revision_at == receipt.operation_at
        )
    if receipt.changed:
        _require(
            format_ == "file_set_v1"
            and receipt.original_page_updated_at == revision_at
            and revision_at >= receipt.operation_at
            and receipt.source_id is not None
            and receipt.master_audit_event_id is not None
        )
        _require(
            connection.execute(
                "SELECT page_uid, revision_id, revision_number, created_at FROM page_sources "
                "WHERE library_id = ? AND source_id = ?",
                (receipt.library_id, receipt.source_id),
            ).fetchone()
            == (
                receipt.page_uid,
                receipt.revision_id,
                receipt.revision_number,
                receipt.operation_at,
            )
        )
        _require(
            connection.execute(
                "SELECT identity_id, action, target_type, target_id, occurred_at "
                "FROM admin_master_audit_events WHERE id = ?",
                (receipt.master_audit_event_id,),
            ).fetchone()
            == (
                receipt.identity_id,
                f"content.page.file_set.{receipt.operation}",
                "page",
                f"{receipt.library_id}:{receipt.page_uid.hex()}",
                receipt.operation_at,
            )
        )
    else:
        _require(
            receipt.operation == "revise"
            and receipt.source_id is None
            and receipt.master_audit_event_id is None
        )
    return manifest
