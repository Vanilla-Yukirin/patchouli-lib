"""Read-only validation of the additive workspace graph in exact backup formats."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.content.presentation import preview_text
from patchouli_lib.library_skills.service import build_skill_snapshot


def validate_workspace_graph(connection: sqlite3.Connection, *, skills: bool) -> None:
    for library_id, page_uid, revision_id, primary, title in connection.execute(
        "SELECT library_id,page_uid,revision_id,primary_file,display_title "
        "FROM revision_presentations"
    ):
        if not isinstance(title, str) or not title.strip() or "\x00" in title:
            raise BackupDatabaseError
        if primary is not None:
            row = connection.execute(
                "SELECT content_bytes FROM revision_files WHERE library_id=? AND page_uid=? "
                "AND revision_id=? AND filename=?",
                (library_id, page_uid, revision_id, primary),
            ).fetchone()
            if row is None or preview_text(row[0]) is None:
                raise BackupDatabaseError
    if not skills:
        return
    for library_id, skill_id, revision_id, count, size, digest in connection.execute(
        "SELECT library_id,skill_id,id,file_count,total_size_bytes,snapshot_sha256 "
        "FROM library_skill_revisions"
    ):
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
    for identity, raw, created_at in connection.execute(
        "SELECT identity_id,response_json,created_at FROM admin_workspace_save_receipts"
    ):
        if (
            connection.execute(
                "SELECT 1 FROM admin_master_identity WHERE identity_id=?", (identity,)
            ).fetchone()
            is None
        ):
            raise BackupDatabaseError
        try:
            body = json.loads(raw)
            if "skill" in body:
                body = body["skill"]
                body = {**body, "id": body.get("id", body.get("skill_id"))}
            library_id, revision_id, number = (
                body["library_id"],
                body["revision_id"],
                body["revision_number"],
            )
            if "page_id" in body:
                row = connection.execute(
                    "SELECT r.created_at FROM revisions r JOIN pages p "
                    "ON p.library_id=r.library_id AND p.page_uid=r.page_uid "
                    "WHERE p.library_id=? AND p.page_id=? "
                    "AND r.revision_id=? AND r.revision_number=?",
                    (library_id, body["page_id"], revision_id, number),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT created_at FROM library_skill_revisions "
                    "WHERE library_id=? AND skill_id=? AND id=? AND number=?",
                    (library_id, body["id"], revision_id, number),
                ).fetchone()
            if row is None or row[0] > created_at:
                raise BackupDatabaseError
        except (ValueError, TypeError, KeyError):
            raise BackupDatabaseError from None
