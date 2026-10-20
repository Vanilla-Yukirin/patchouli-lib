"""Read-only validation of the additive workspace graph in exact backup formats."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.content.page_membership_history import (
    PageMembershipHistoryError,
    PageStateTimeline,
    load_page_state_timeline,
)
from patchouli_lib.content.presentation import extract_title, preview_text
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.library_skills.service import build_skill_snapshot


def _page_response(
    connection: sqlite3.Connection,
    body: dict[str, Any],
    created_at: int,
    schema_revision: str,
    timelines: dict[tuple[str, bytes], PageStateTimeline],
) -> None:
    common = {
        "library_id",
        "page_id",
        "revision_id",
        "revision_number",
        "etag",
        "changed",
        "replayed",
    }
    save_fields = {"section_id", "book_id", "overwritten_newer_revision"}
    if (
        set(body) not in (common, common | save_fields)
        or type(body["changed"]) is not bool
        or body["replayed"] is not False
        or type(body["revision_number"]) is not int
        or any(
            type(body[key]) is not str for key in ("library_id", "page_id", "revision_id", "etag")
        )
    ):
        raise BackupDatabaseError
    if save_fields <= body.keys() and (
        type(body["overwritten_newer_revision"]) is not bool
        or any(type(body[key]) is not str for key in ("section_id", "book_id"))
    ):
        raise BackupDatabaseError
    row = connection.execute(
        "SELECT page_uid FROM pages WHERE library_id=? AND page_id=?",
        (body["library_id"], body["page_id"]),
    ).fetchone()
    if row is None:
        raise BackupDatabaseError
    key = (body["library_id"], row[0])
    timeline = timelines.get(key)
    if timeline is None:
        timeline = load_page_state_timeline(
            connection, schema_revision=schema_revision, library_id=key[0], page_uid=key[1]
        )
        timelines[key] = timeline
    states = [
        state
        for state in timeline.states
        if state.deleted_at is None
        and state.revision_id == body["revision_id"]
        and state.revision_number == body["revision_number"]
        and state.updated_at <= created_at
        and page_current_etag(
            row[0], state.revision_id, state.revision_number, state.occurred_at, state.updated_at
        )
        == body["etag"]
    ]
    if len(states) != 1:
        raise BackupDatabaseError
    if save_fields <= body.keys() and (body["section_id"], body["book_id"]) != (
        states[0].section_id,
        states[0].book_id,
    ):
        raise BackupDatabaseError


def _skill_response(connection: sqlite3.Connection, body: dict[str, Any], created_at: int) -> None:
    if (
        set(body) != {"skill", "changed", "created", "replayed", "overwritten_newer_revision"}
        or type(body["skill"]) is not dict
        or any(
            type(body[key]) is not bool
            for key in ("changed", "created", "overwritten_newer_revision")
        )
        or body["replayed"] is not False
    ):
        raise BackupDatabaseError
    skill = body["skill"]
    if any(type(skill[key]) is not str for key in ("library_id", "id", "revision_id")):
        raise BackupDatabaseError
    row = connection.execute(
        "SELECT s.created_at,r.id,r.number,r.name,r.description,r.file_count,"
        "r.total_size_bytes,r.snapshot_sha256,r.created_at "
        "FROM library_skills s JOIN library_skill_revisions r "
        "ON r.library_id=s.library_id AND r.skill_id=s.id "
        "WHERE s.library_id=? AND s.id=? AND r.id=?",
        (skill["library_id"], skill["id"], skill["revision_id"]),
    ).fetchone()
    if row is None or row[8] > created_at:
        raise BackupDatabaseError
    expected = {
        "library_id": skill["library_id"],
        "id": skill["id"],
        "name": row[3],
        "description": row[4],
        "current_revision_id": row[1],
        "current_revision_number": row[2],
        "created_at": row[0],
        "updated_at": row[8],
        "revision_id": row[1],
        "revision_number": row[2],
        "file_count": row[5],
        "total_size_bytes": row[6],
        "snapshot_sha256": bytes(row[7]).hex(),
        "etag": f'"skill-v1-{skill["id"]}-{row[1]}"',
    }
    if set(skill) != set(expected) or any(
        type(skill[key]) is not type(value) or skill[key] != value
        for key, value in expected.items()
    ):
        raise BackupDatabaseError
    if body["created"] != (body["changed"] and row[2] == 1) or (
        body["created"] and body["overwritten_newer_revision"]
    ):
        raise BackupDatabaseError


def validate_workspace_graph(connection: sqlite3.Connection, *, skills: bool) -> None:
    for library_id, page_uid, revision_id, primary, title in connection.execute(
        "SELECT library_id,page_uid,revision_id,primary_file,display_title "
        "FROM revision_presentations"
    ):
        if not isinstance(title, str) or not title.strip() or "\x00" in title:
            raise BackupDatabaseError
        content = None
        if primary is not None:
            row = connection.execute(
                "SELECT content_bytes FROM revision_files WHERE library_id=? AND page_uid=? "
                "AND revision_id=? AND filename=?",
                (library_id, page_uid, revision_id, primary),
            ).fetchone()
            if row is None or preview_text(row[0]) is None:
                raise BackupDatabaseError
            content = row[0]
        # This format freezes the v1 write-time derivation. Future parser
        # changes need versioned validation, never rewritten historical titles.
        if title != extract_title(primary, content):
            raise BackupDatabaseError
    if not skills:
        return
    for (
        library_id,
        skill_id,
        revision_id,
        name,
        description,
        count,
        size,
        digest,
    ) in connection.execute(
        "SELECT library_id,skill_id,id,name,description,file_count,total_size_bytes,"
        "snapshot_sha256 "
        "FROM library_skill_revisions"
    ):
        # Freeze the v1 stored metadata contract for every immutable version.
        # Future writer rules require versioned validation, not changed history.
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or len(name) > 64
            or not isinstance(description, str)
            or len(description) > 4000
            or "\x00" in description
            or description != description.strip()
        ):
            raise BackupDatabaseError
        rows = connection.execute(
            "SELECT path,content_bytes,size_bytes,content_sha256 FROM library_skill_files "
            "WHERE library_id=? AND skill_id=? AND revision_id=? ORDER BY path",
            (library_id, skill_id, revision_id),
        ).fetchall()
        try:
            snapshot = build_skill_snapshot((path, content) for path, content, *_ in rows)
        except (ValueError, TypeError):
            raise BackupDatabaseError from None
        if (
            len(rows) != count
            or snapshot.total_size_bytes != size
            or snapshot.snapshot_sha256 != digest
            or any(
                len(content) != stored_size or hashlib.sha256(content).digest() != sha
                for _, content, stored_size, sha in rows
            )
        ):
            raise BackupDatabaseError
    if connection.execute(
        "SELECT 1 FROM library_skills s JOIN library_skill_revisions r "
        "ON r.library_id=s.library_id AND r.skill_id=s.id AND r.id=s.current_revision_id "
        "WHERE r.name!=s.name OR r.description!=s.description OR "
        "s.current_revision_number!=(SELECT max(number) FROM library_skill_revisions "
        "WHERE library_id=s.library_id AND skill_id=s.id) LIMIT 1"
    ).fetchone():
        raise BackupDatabaseError
    if connection.execute(
        "SELECT 1 FROM library_skill_revisions GROUP BY library_id,skill_id "
        "HAVING min(number)!=1 OR max(number)!=count(*) LIMIT 1"
    ).fetchone():
        raise BackupDatabaseError
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='admin_workspace_save_receipts'"
        ).fetchone()
        is None
    ):
        return
    schema_revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    timelines: dict[tuple[str, bytes], PageStateTimeline] = {}
    for identity, raw, digest, created_at in connection.execute(
        "SELECT identity_id,response_json,response_sha256,created_at "
        "FROM admin_workspace_save_receipts"
    ):
        if (
            connection.execute(
                "SELECT 1 FROM admin_master_identity WHERE identity_id=?", (identity,)
            ).fetchone()
            is None
        ):
            raise BackupDatabaseError
        try:
            # Bind the frozen operation flags as well as the resource references.
            # This is corruption detection, like file hashes, not a signature
            # against someone rewriting both a response and its checksum.
            if type(raw) is not str or hashlib.sha256(raw.encode("utf-8")).digest() != digest:
                raise BackupDatabaseError
            body = json.loads(raw)
            if type(body) is not dict:
                raise BackupDatabaseError
            if "skill" in body:
                _skill_response(connection, body, created_at)
            else:
                _page_response(connection, body, created_at, schema_revision, timelines)
        except (ValueError, TypeError, KeyError, PageMembershipHistoryError):
            raise BackupDatabaseError from None
