"""Atomic master-session Tag management without modifying Page content."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Connection, delete, select, update

from patchouli_lib.admin.contracts import MasterTagBatchInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.tags.models import PageTag, Tag
from patchouli_lib.tags.repository import normalize_tag_name
from patchouli_lib.tags.service import TagNotFoundError, TagValidationError


class TagBatchConflictError(ValueError):
    """A selected Tag changed, or a replacement name is already in use."""


def apply_master_tag_batch(
    connection: Connection,
    request: MasterTagBatchInput,
    *,
    master_session: MasterAdminSession,
    now: int,
) -> int:
    """Caller holds BEGIN IMMEDIATE and has revalidated the master session."""
    if not connection.in_transaction():
        raise RuntimeError("Tag management requires an authorized write transaction.")
    replacements: dict[tuple[str, str], tuple[str, str]] = {}
    replacement_keys: set[tuple[str, str]] = set()
    for item in request.changes:
        previous = connection.scalar(
            select(Tag.display_name).where(Tag.library_id == item.library_id, Tag.id == item.tag_id)
        )
        if previous is None:
            raise TagNotFoundError
        if previous != item.expected_name:
            raise TagBatchConflictError
        if request.operation == "rename":
            if item.new_name is None:
                raise TagValidationError
            try:
                display_name, match_key = normalize_tag_name(item.new_name)
            except ValueError:
                raise TagValidationError from None
            key = (item.library_id, match_key)
            if key in replacement_keys:
                raise TagBatchConflictError
            replacement_keys.add(key)
            owner = connection.scalar(
                select(Tag.id).where(Tag.library_id == item.library_id, Tag.match_key == match_key)
            )
            if owner is not None and owner != item.tag_id:
                # Explicitly reject swaps and merges rather than silently changing ownership.
                raise TagBatchConflictError
            if display_name != previous:
                replacements[(item.library_id, item.tag_id)] = (display_name, match_key)

    changed = 0
    for item in request.changes:
        if request.operation == "rename":
            replacement = replacements.get((item.library_id, item.tag_id))
            if replacement is None:
                continue
            connection.execute(
                update(Tag)
                .where(Tag.library_id == item.library_id, Tag.id == item.tag_id)
                .values(display_name=replacement[0], match_key=replacement[1])
            )
        else:
            # Includes associations on recycled Pages; Page bodies/history stay untouched.
            # Existing search triggers enqueue affected Pages automatically.
            connection.execute(
                delete(PageTag).where(
                    PageTag.library_id == item.library_id, PageTag.tag_id == item.tag_id
                )
            )
            connection.execute(
                delete(Tag).where(Tag.library_id == item.library_id, Tag.id == item.tag_id)
            )
        MasterAuditRepository(connection).add_success(
            identity_id=master_session.identity_id,
            session_generation=master_session.session_generation,
            session_fingerprint=master_session.audit_fingerprint(),
            action="tag.rename" if request.operation == "rename" else "tag.delete",
            target_type="tag",
            target_id=f"{item.library_id}:{item.tag_id}",
            occurred_at=now,
            event_id=uuid4().hex,
        )
        changed += 1
    return changed
