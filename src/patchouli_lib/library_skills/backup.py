"""Read-only validation of durable Library Skill mutation responses."""

from __future__ import annotations

import sqlite3

CREATE_SKILL_ROUTE = "/api/v1/libraries/{library_id}/skills"
REVISE_SKILL_ROUTE = CREATE_SKILL_ROUTE + "/{skill_id}/revisions"
SKILL_ROUTES = frozenset({CREATE_SKILL_ROUTE, REVISE_SKILL_ROUTE})


def validate_skill_replay(
    connection: sqlite3.Connection,
    *,
    library_id: str,
    caller_id: str,
    method: str,
    route: str,
    status: int,
    parsed: dict[str, object],
    location: str | None,
    etag: str,
    original_request_id: str,
) -> None:
    """Raise ValueError for an orphaned, altered or unaudited replay response."""
    if method != "POST" or route not in SKILL_ROUTES or type(parsed.get("changed")) is not bool:
        raise ValueError("Invalid Skill replay.")
    expected_fields = {
        "library_id",
        "id",
        "name",
        "description",
        "current_revision_id",
        "current_revision_number",
        "created_at",
        "updated_at",
        "revision_id",
        "revision_number",
        "file_count",
        "total_size_bytes",
        "snapshot_sha256",
        "etag",
        "changed",
    }
    if set(parsed) != expected_fields or parsed["library_id"] != library_id:
        raise ValueError("Invalid Skill replay fields.")
    row = connection.execute(
        "SELECT s.created_at, r.id, r.number, r.name, r.description, r.file_count, "
        "r.total_size_bytes, r.snapshot_sha256, r.created_at "
        "FROM library_skills s JOIN library_skill_revisions r "
        "ON r.library_id=s.library_id AND r.skill_id=s.id "
        "WHERE s.library_id=? AND s.id=? AND r.id=?",
        (library_id, parsed["id"], parsed["revision_id"]),
    ).fetchone()
    if row is None:
        raise ValueError("Orphaned Skill replay.")
    created_at, revision_id, number, name, description, count, size, digest, updated_at = row
    expected = {
        "created_at": created_at,
        "current_revision_id": revision_id,
        "revision_number": number,
        "current_revision_number": number,
        "name": name,
        "description": description,
        "file_count": count,
        "total_size_bytes": size,
        "snapshot_sha256": bytes(digest).hex(),
        "updated_at": updated_at,
    }
    if any(parsed[key] != value for key, value in expected.items()):
        raise ValueError("Changed Skill replay metadata.")
    expected_etag = f'"skill-v1-{parsed["id"]}-{revision_id}"'
    expected_location = (
        f"/api/v1/libraries/{library_id}/skills/{parsed['id']}/revisions/{revision_id}/manifest"
    )
    if etag != expected_etag or parsed["etag"] != etag or location != expected_location:
        raise ValueError("Changed Skill replay references.")
    if route == CREATE_SKILL_ROUTE:
        if status != (201 if parsed["changed"] else 200) or (status == 201 and number != 1):
            raise ValueError("Invalid Skill create response.")
        action = "library.skill.create"
    else:
        if status != 200:
            raise ValueError("Invalid Skill revision response.")
        action = "library.skill.revise"
    audit_count = connection.execute(
        "SELECT count(*) FROM auth_audit_events WHERE library_id=? AND actor_caller_id=? "
        "AND action=? AND resource_type='library_skill' AND resource_id=? "
        "AND request_id=? AND occurred_at=? AND outcome='succeeded'",
        (library_id, caller_id, action, parsed["id"], original_request_id, updated_at),
    ).fetchone()[0]
    if audit_count != int(bool(parsed["changed"])):
        raise ValueError("Missing or duplicate Skill mutation audit.")
