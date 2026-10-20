"""Manual browser saves: merge explicit edits into the latest immutable snapshot."""

from __future__ import annotations

import hashlib
import hmac
import json
from email import policy
from email.parser import BytesParser
from typing import Any
from uuid import uuid4

from fastapi import Request
from sqlalchemy import BigInteger, CheckConstraint, Connection, LargeBinary, Text, insert, select
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.admin.json_api.common import ApiError, BrowserAuth, body_json, decode_json
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.content.file_manifest import (
    MAX_PAGE_BYTES,
    build_file_manifest,
    normalize_file_name,
)
from patchouli_lib.content.file_set_create_core import FileSetPageCreateCore
from patchouli_lib.content.file_set_service import FileSetRevisionService
from patchouli_lib.content.models import Revision
from patchouli_lib.content.presentation import presentation_for, read_presentation
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveSourceInput, NewPageSource
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.identifiers import generate_revision_id, parse_occurrence_time
from patchouli_lib.library.models import Book
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.models import Base
from patchouli_lib.retrieval.file_set_read import read_verified_revision_snapshot


class WorkspaceSaveReceipt(Base):
    __tablename__ = "admin_workspace_save_receipts"
    __table_args__ = (
        CheckConstraint("length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'"),
        CheckConstraint("typeof(key_digest) = 'blob' AND length(key_digest) = 32"),
        CheckConstraint(
            "typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32"
        ),
        CheckConstraint("typeof(created_at) = 'integer' AND created_at >= 0"),
        CheckConstraint("json_valid(response_json)"),
    )
    identity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    key_digest: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


def operation_key(request: Request) -> bytes:
    values = request.headers.getlist("idempotency-key")
    try:
        if len(values) != 1:
            raise ValueError
        return digest_idempotency_key(values[0])
    except (ValueError, UnicodeError):
        raise ApiError(422, "operation_key_required") from None


def fingerprint(target: str, metadata: dict[str, Any], uploads: list[tuple[str, bytes]]) -> bytes:
    return hashlib.sha256(
        json.dumps(
            [
                target,
                metadata,
                [[name, hashlib.sha256(body).hexdigest()] for name, body in uploads],
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).digest()


def replay(
    connection: Connection, session: MasterAdminSession, key: bytes, digest: bytes
) -> dict[str, Any] | None:
    row = (
        connection.execute(
            select(WorkspaceSaveReceipt.__table__).where(
                WorkspaceSaveReceipt.identity_id == session.identity_id,
                WorkspaceSaveReceipt.key_digest == key,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if not hmac.compare_digest(row["request_fingerprint"], digest):
        raise ApiError(409, "operation_key_conflict")
    return {**json.loads(row["response_json"]), "replayed": True}


def remember(
    connection: Connection,
    session: MasterAdminSession,
    key: bytes,
    digest: bytes,
    response: dict[str, Any],
    *,
    operation_at: int,
) -> None:
    connection.execute(
        insert(WorkspaceSaveReceipt).values(
            identity_id=session.identity_id,
            key_digest=key,
            request_fingerprint=digest,
            response_json=json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            created_at=max(utc_microseconds(), operation_at),
        )
    )


def audit(
    connection: Connection,
    session: MasterAdminSession,
    action: str,
    kind: str,
    target: str,
    *,
    operation_at: int = 0,
) -> None:
    MasterAuditRepository(connection).add_success(
        identity_id=session.identity_id,
        session_generation=session.session_generation,
        session_fingerprint=session.audit_fingerprint(),
        action=action,
        target_type=kind,
        target_id=target,
        occurred_at=max(utc_microseconds(), operation_at),
        event_id=uuid4().hex,
    )


async def save_input(request: Request) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip() == "application/json":
        return await body_json(request), []
    if (
        not content_type.startswith("multipart/form-data;")
        or "\r" in content_type
        or "\n" in content_type
    ):
        raise ApiError(415, "multipart_or_json_required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PAGE_BYTES + 2 * 1024 * 1024 + 256 * 1024:
            raise ApiError(413, "body_too_large")
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        if not message.is_multipart() or message.defects:
            raise ValueError
        metadata = None
        uploads: list[tuple[str, bytes]] = []
        for part in message.iter_parts():
            if part.defects or part.is_multipart() or part.get_content_disposition() != "form-data":
                raise ValueError
            field = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise ValueError
            if field == "metadata" and metadata is None and len(payload) <= 2 * 1024 * 1024:
                metadata = decode_json(payload)
            elif field in {"file", "files"} and len(uploads) < 64:
                filename = part.get_filename()
                if not isinstance(filename, str):
                    raise ValueError
                uploads.append((filename, payload))
            else:
                raise ValueError
        if metadata is None:
            raise ValueError
        return metadata, uploads
    except (ValueError, UnicodeError):
        raise ApiError(422, "invalid_multipart") from None


def merge_files(
    current: list[tuple[str, bytes]],
    metadata: dict[str, Any],
    uploads: list[tuple[str, bytes]],
    *,
    package: bool = False,
) -> list[tuple[str, bytes]]:
    """Apply removals, simultaneous snapshot renames, then explicit replacements."""

    from patchouli_lib.library_skills.service import normalize_skill_path

    normalize = normalize_skill_path if package else normalize_file_name
    files = dict(current)
    edits = metadata.get("edits", [])
    removes = metadata.get("remove", [])
    renames = metadata.get("renames", [])
    if not all(isinstance(item, list) for item in (edits, removes, renames)):
        raise ApiError(422, "invalid_file_changes")
    removed: set[str] = set()
    for name in removes:
        if (
            not isinstance(name, str)
            or normalize(name) != name
            or name in removed
            or name not in files
        ):
            raise ApiError(409, "file_changed")
        removed.add(name)
        del files[name]
    moves: dict[str, str] = {}
    destinations: set[str] = set()
    for rename in renames:
        if not isinstance(rename, dict) or set(rename) != {"from", "to"}:
            raise ApiError(422, "invalid_rename")
        old, new = rename["from"], rename["to"]
        if (
            not isinstance(old, str)
            or not isinstance(new, str)
            or normalize(old) != old
            or normalize(new) != new
        ):
            raise ApiError(422, "invalid_file_name")
        if old in moves or new in destinations or old not in files:
            raise ApiError(409, "file_changed")
        moves[old] = new
        destinations.add(new)
    if any(name in files and name not in moves for name in destinations):
        raise ApiError(409, "file_changed")
    moved = {new: files[old] for old, new in moves.items()}
    for old in moves:
        del files[old]
    files.update(moved)
    incoming: list[tuple[str, bytes]] = []
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or set(edit) != {"name", "content"}
            or not isinstance(edit["content"], str)
        ):
            raise ApiError(422, "invalid_text_edit")
        incoming.append((edit["name"], edit["content"].encode("utf-8")))
    incoming.extend(uploads)
    seen: set[str] = set()
    for name, content in incoming:
        if not isinstance(name, str) or normalize(name) != name or name in seen:
            raise ApiError(422, "duplicate_file_change")
        seen.add(name)
        files[name] = content
    return list(files.items())


def save_page(
    auth: BrowserAuth,
    session: MasterAdminSession,
    key: bytes,
    metadata: dict[str, Any],
    uploads: list[tuple[str, bytes]],
    *,
    library_id: str | None = None,
    page_id: str | None = None,
    book_id: str | None = None,
) -> dict[str, Any]:
    allowed = {"primary_file", "base_etag", "edits", "remove", "renames", "occurred_at", "mode"}
    if (
        set(metadata) - allowed
        or "primary_file" not in metadata
        or metadata.get("mode", "overwrite_latest") != "overwrite_latest"
    ):
        raise ApiError(422, "invalid_save_metadata")
    primary = metadata["primary_file"]
    if primary is not None and not isinstance(primary, str):
        raise ApiError(422, "invalid_primary_file")
    base = metadata.get("base_etag")
    if base is not None and not isinstance(base, str):
        raise ApiError(422, "invalid_base_etag")
    digest = fingerprint(f"page:{library_id}:{page_id}:{book_id}", metadata, uploads)
    with immediate_transaction(auth.engine) as connection:
        auth.authorize(connection, session)
        previous = replay(connection, session, key, digest)
        if previous is not None:
            return previous
        content = ContentRepository(connection)
        now = utc_microseconds()
        overwritten = False
        if page_id is None:
            row = connection.execute(
                select(Book.library_id, Book.section_id).where(Book.id == book_id)
            ).first()
            if row is None:
                raise ApiError(404, "book_not_found")
            book = LibraryRepository(connection).get_book(
                row.library_id, row.section_id, str(book_id)
            )
            if book is None:
                raise ApiError(404, "book_not_found")
            files = merge_files([], metadata, uploads)
            manifest = build_file_manifest(files)
            presentation = presentation_for(manifest, primary)
            occurred = metadata.get("occurred_at")
            if occurred is not None and not isinstance(occurred, str):
                raise ApiError(422, "invalid_occurrence")
            page = FileSetPageCreateCore(connection).create_page(
                book=book,
                title=presentation.display_title,
                occurred_at=now
                if occurred is None
                else parse_occurrence_time(occurred).utc_microseconds,
                operation_at=now,
                manifest=manifest,
                source=ArchiveSourceInput(kind="admin_web"),
                primary_file=primary,
            )
            changed = True
        else:
            target = content.get_page(str(library_id), page_id)
            if target is None or target.deleted_at is not None:
                raise ApiError(404, "page_not_found")
            page = target
            etag = page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            )
            overwritten = base is not None and base != etag
            current = content.get_current_file_manifest(page)
            files = merge_files(
                [(entry.name, entry.content) for entry in current.files], metadata, uploads
            )
            revision_id = generate_revision_id()
            result = FileSetRevisionService(connection).append_existing_page(
                library_id=page.library_id,
                page_id=page.page_id,
                expected_etag=etag,
                files=files,
                revision_id=revision_id,
                revision_at=max(now, page.updated_at + 1),
                primary_file=primary,
                source=NewPageSource(
                    library_id=page.library_id,
                    source_id=uuid4().hex,
                    page_uid=page.page_uid,
                    revision_id=revision_id,
                    revision_number=page.current_revision_number + 1,
                    kind="admin_web",
                    created_at=now,
                ),
            )
            page, manifest, changed = result.page, result.manifest, result.changed
        if changed:
            audit(
                connection,
                session,
                "content.page.workspace_save",
                "page",
                f"{page.library_id}:{page.page_id}:{page.current_revision_id}",
                operation_at=page.updated_at,
            )
        response = {
            "library_id": page.library_id,
            "section_id": page.section_id,
            "book_id": page.book_id,
            "page_id": page.page_id,
            "revision_id": page.current_revision_id,
            "revision_number": page.current_revision_number,
            "etag": page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            ),
            "changed": changed,
            "replayed": False,
            "overwritten_newer_revision": overwritten,
        }
        remember(connection, session, key, digest, response, operation_at=page.updated_at)
        return response


def restore_revision(
    auth: BrowserAuth,
    session: MasterAdminSession,
    key: bytes,
    library_id: str,
    page_id: str,
    revision_number: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if (
        set(metadata) != {"expected_etag", "confirm_restore"}
        or metadata["confirm_restore"] != "yes"
        or not isinstance(metadata["expected_etag"], str)
    ):
        raise ApiError(422, "restore_confirmation_required")
    digest = fingerprint(f"restore:{library_id}:{page_id}:{revision_number}", metadata, [])
    with immediate_transaction(auth.engine) as connection:
        auth.authorize(connection, session)
        previous = replay(connection, session, key, digest)
        if previous is not None:
            return previous
        content = ContentRepository(connection)
        page = content.get_page(library_id, page_id)
        if page is None or page.deleted_at is not None:
            raise ApiError(404, "page_not_found")
        source_id = connection.scalar(
            select(Revision.revision_id).where(
                Revision.library_id == library_id,
                Revision.page_uid == page.page_uid,
                Revision.revision_number == revision_number,
            )
        )
        if source_id is None:
            raise ApiError(404, "revision_not_found")
        snapshot = read_verified_revision_snapshot(connection, page, source_id)
        presentation = read_presentation(connection, library_id, page.page_uid, source_id)
        now, revision_id = utc_microseconds(), generate_revision_id()
        result = FileSetRevisionService(connection).append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=metadata["expected_etag"],
            files=[(entry.name, entry.content) for entry in snapshot.manifest.files],
            revision_id=revision_id,
            revision_at=max(now, page.updated_at + 1),
            primary_file=presentation.primary_file,
            presentation_override=presentation,
            source=NewPageSource(
                library_id=library_id,
                source_id=uuid4().hex,
                page_uid=page.page_uid,
                revision_id=revision_id,
                revision_number=page.current_revision_number + 1,
                kind="revision_restore",
                locator=source_id,
                created_at=now,
            ),
        )
        if result.changed:
            audit(
                connection,
                session,
                "content.page.revision_restore",
                "page",
                f"{library_id}:{page_id}:{result.page.current_revision_id}",
                operation_at=result.page.updated_at,
            )
        response = {
            "library_id": library_id,
            "page_id": page_id,
            "revision_id": result.page.current_revision_id,
            "revision_number": result.page.current_revision_number,
            "etag": result.etag,
            "changed": result.changed,
            "replayed": False,
        }
        remember(connection, session, key, digest, response, operation_at=result.page.updated_at)
        return response
