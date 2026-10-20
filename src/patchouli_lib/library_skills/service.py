"""Transaction-neutral, authorization-gated complete Skill package versions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from sqlalchemy import Connection, insert, select, update

from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.content.file_manifest import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
    normalize_file_name,
)
from patchouli_lib.library_skills.models import LibrarySkill, LibrarySkillFile, LibrarySkillRevision

type SkillAction = Literal["metadata", "read", "write"]
type SkillAuthorization = Callable[[str, SkillAction], None]


class SkillValidationError(ValueError):
    pass


class SkillNotFoundError(ValueError):
    pass


class SkillConflictError(ValueError):
    pass


class SkillPreconditionError(ValueError):
    pass


def normalize_skill_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        raise SkillValidationError("Invalid package path.")
    parts = value.split("/")
    if len(parts) > 16:
        raise SkillValidationError("Package path is too deep.")
    try:
        return "/".join(normalize_file_name(part) for part in parts)
    except (TypeError, ValueError, UnicodeError):
        raise SkillValidationError("Invalid package path.") from None


@dataclass(frozen=True)
class SkillSnapshot:
    files: tuple[tuple[str, bytes], ...] = field(repr=False)
    snapshot_sha256: bytes
    total_size_bytes: int


def build_skill_snapshot(files: Iterable[tuple[str, bytes]]) -> SkillSnapshot:
    entries: dict[str, bytes] = {}
    seen: set[str] = set()
    size = 0
    try:
        for path, content in files:
            if len(entries) >= MAX_FILES_PER_PAGE or type(content) is not bytes:
                raise ValueError
            path = normalize_skill_path(path)
            key = unicodedata.normalize("NFC", path.casefold())
            if key in seen or len(content) > MAX_FILE_BYTES:
                raise ValueError
            seen.add(key)
            size += len(content)
            if size > MAX_PAGE_BYTES:
                raise ValueError
            entries[path] = content
        for path in seen:
            if any("/".join(path.split("/")[:i]) in seen for i in range(1, len(path.split("/")))):
                raise ValueError
        entry = entries["SKILL.md"].decode("utf-8")
        if not entry.strip() or "\x00" in entry:
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SkillValidationError(
            "A safe complete package with UTF-8 SKILL.md is required."
        ) from None
    ordered = tuple(sorted(entries.items(), key=lambda item: item[0].encode("utf-8")))
    identity = [[path, len(body), hashlib.sha256(body).hexdigest()] for path, body in ordered]
    digest = hashlib.sha256(
        b"patchouli-library-skill-v1\0"
        + json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).digest()
    return SkillSnapshot(ordered, digest, size)


@dataclass(frozen=True)
class SkillSaveResult:
    skill: dict[str, object]
    changed: bool
    created: bool


class LibrarySkillService:
    """Caller owns the transaction and audit; each operation rechecks authority."""

    def __init__(
        self,
        connection: Connection,
        *,
        authorize: SkillAuthorization,
        clock: Callable[[], int] = utc_microseconds,
    ) -> None:
        self.connection = connection
        self.authorize = authorize
        self.clock = clock

    def _current(self, library_id: str, skill_id: str) -> dict[str, object]:
        row = (
            self.connection.execute(
                select(LibrarySkill.__table__).where(
                    LibrarySkill.library_id == library_id, LibrarySkill.id == skill_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SkillNotFoundError("Skill was not found.")
        return dict(row)

    def _version(self, library_id: str, skill_id: str, revision_id: str) -> dict[str, object]:
        row = (
            self.connection.execute(
                select(LibrarySkillRevision.__table__).where(
                    LibrarySkillRevision.library_id == library_id,
                    LibrarySkillRevision.skill_id == skill_id,
                    LibrarySkillRevision.id == revision_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SkillNotFoundError("Skill version was not found.")
        result = dict(row)
        result["snapshot_sha256"] = bytes(row["snapshot_sha256"]).hex()
        result["revision_id"] = result.pop("id")
        result["revision_number"] = result.pop("number")
        return result

    @staticmethod
    def _etag(current: dict[str, object]) -> str:
        return '"skill-v1-' + str(current["id"]) + "-" + str(current["current_revision_id"]) + '"'

    def _summary(self, current: dict[str, object]) -> dict[str, object]:
        version = self._version(
            str(current["library_id"]), str(current["id"]), str(current["current_revision_id"])
        )
        return {
            **current,
            "revision_id": version["revision_id"],
            "revision_number": version["revision_number"],
            "file_count": version["file_count"],
            "total_size_bytes": version["total_size_bytes"],
            "snapshot_sha256": version["snapshot_sha256"],
            "etag": self._etag(current),
        }

    def list(self, library_id: str, *, limit: int = 20, offset: int = 0) -> dict[str, object]:
        self.authorize(library_id, "metadata")
        if not 1 <= limit <= 100 or not 0 <= offset <= (1 << 63) - 1:
            raise SkillValidationError("Invalid pagination.")
        rows = (
            self.connection.execute(
                select(LibrarySkill.__table__)
                .where(LibrarySkill.library_id == library_id)
                .order_by(LibrarySkill.name, LibrarySkill.id)
                .limit(limit + 1)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        return {
            "items": [self._summary(dict(row)) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def get(self, library_id: str, skill_id: str) -> dict[str, object]:
        self.authorize(library_id, "metadata")
        return self._summary(self._current(library_id, skill_id))

    def versions(
        self, library_id: str, skill_id: str, *, limit: int = 20, offset: int = 0
    ) -> dict[str, object]:
        self.authorize(library_id, "metadata")
        self._current(library_id, skill_id)
        if not 1 <= limit <= 100 or not 0 <= offset <= (1 << 63) - 1:
            raise SkillValidationError("Invalid pagination.")
        identifiers = list(
            self.connection.scalars(
                select(LibrarySkillRevision.id)
                .where(
                    LibrarySkillRevision.library_id == library_id,
                    LibrarySkillRevision.skill_id == skill_id,
                )
                .order_by(LibrarySkillRevision.number.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "items": [self._version(library_id, skill_id, value) for value in identifiers[:limit]],
            "next_offset": offset + limit if len(identifiers) > limit else None,
        }

    def manifest(
        self, library_id: str, skill_id: str, *, revision_id: str | None = None
    ) -> dict[str, object]:
        self.authorize(library_id, "read")
        current = self._current(library_id, skill_id)
        revision_id = revision_id or str(current["current_revision_id"])
        version = self._version(library_id, skill_id, revision_id)
        rows = self.connection.execute(
            select(
                LibrarySkillFile.path, LibrarySkillFile.size_bytes, LibrarySkillFile.content_sha256
            )
            .where(
                LibrarySkillFile.library_id == library_id,
                LibrarySkillFile.skill_id == skill_id,
                LibrarySkillFile.revision_id == revision_id,
            )
            .order_by(LibrarySkillFile.path)
        ).all()
        return {
            **version,
            "primary_file": "SKILL.md",
            "version": revision_id,
            "files": [
                {"path": path, "bytes": size, "sha256": bytes(digest).hex()}
                for path, size, digest in rows
            ],
        }

    def read_file(
        self, library_id: str, skill_id: str, resource_path: str, *, revision_id: str | None = None
    ) -> bytes:
        self.authorize(library_id, "read")
        current = self._current(library_id, skill_id)
        path = normalize_skill_path(resource_path)
        row = self.connection.execute(
            select(
                LibrarySkillFile.content_bytes,
                LibrarySkillFile.size_bytes,
                LibrarySkillFile.content_sha256,
            ).where(
                LibrarySkillFile.library_id == library_id,
                LibrarySkillFile.skill_id == skill_id,
                LibrarySkillFile.revision_id == (revision_id or current["current_revision_id"]),
                LibrarySkillFile.path == path,
            )
        ).one_or_none()
        if row is None:
            raise SkillNotFoundError("Skill file was not found.")
        body = bytes(row[0])
        if len(body) != row[1] or hashlib.sha256(body).digest() != row[2]:
            raise SkillValidationError("Stored Skill file integrity failed.")
        return body

    @staticmethod
    def _metadata(name: str, description: str) -> tuple[str, str]:
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or len(name) > 64
        ):
            raise SkillValidationError("Skill name must be a portable lowercase folder name.")
        if not isinstance(description, str) or len(description) > 4000 or "\x00" in description:
            raise SkillValidationError("Invalid Skill description.")
        return name, description.strip()

    def create(
        self, library_id: str, *, name: str, description: str, files: Iterable[tuple[str, bytes]]
    ) -> SkillSaveResult:
        self.authorize(library_id, "write")
        name, description = self._metadata(name, description)
        snapshot = build_skill_snapshot(files)
        existing = self.connection.execute(
            select(LibrarySkill.id).where(
                LibrarySkill.library_id == library_id, LibrarySkill.name == name
            )
        ).scalar_one_or_none()
        if existing is not None:
            current = self._current(library_id, existing)
            version = self._version(library_id, existing, str(current["current_revision_id"]))
            if (
                current["description"] != description
                or version["snapshot_sha256"] != snapshot.snapshot_sha256.hex()
            ):
                raise SkillConflictError("Skill name already identifies different content.")
            return SkillSaveResult(self._summary(current), False, False)
        now = self.clock()
        current = {
            "library_id": library_id,
            "id": uuid4().hex,
            "name": name,
            "description": description,
            "current_revision_id": "rev_" + uuid4().hex,
            "current_revision_number": 1,
            "created_at": now,
            "updated_at": now,
        }
        self.connection.execute(insert(LibrarySkill), current)
        self._insert_version(current, snapshot)
        return SkillSaveResult(self._summary(current), True, True)

    def revise(
        self,
        library_id: str,
        skill_id: str,
        *,
        name: str,
        description: str,
        files: Iterable[tuple[str, bytes]],
        expected_etag: str | None,
    ) -> SkillSaveResult:
        self.authorize(library_id, "write")
        name, description = self._metadata(name, description)
        snapshot = build_skill_snapshot(files)
        current = self._current(library_id, skill_id)
        if expected_etag is not None and expected_etag != self._etag(current):
            raise SkillPreconditionError("Skill version changed.")
        version = self._version(library_id, skill_id, str(current["current_revision_id"]))
        if (
            current["name"] == name
            and current["description"] == description
            and version["snapshot_sha256"] == snapshot.snapshot_sha256.hex()
        ):
            return SkillSaveResult(self._summary(current), False, False)
        conflict = self.connection.execute(
            select(LibrarySkill.id).where(
                LibrarySkill.library_id == library_id,
                LibrarySkill.name == name,
                LibrarySkill.id != skill_id,
            )
        ).first()
        if conflict is not None:
            raise SkillConflictError("Skill name already exists.")
        next_current = {
            **current,
            "name": name,
            "description": description,
            "current_revision_id": "rev_" + uuid4().hex,
            "current_revision_number": int(str(current["current_revision_number"])) + 1,
            "updated_at": max(self.clock(), int(str(current["updated_at"])) + 1),
        }
        self._insert_version(next_current, snapshot)
        self.connection.execute(
            update(LibrarySkill)
            .where(LibrarySkill.library_id == library_id, LibrarySkill.id == skill_id)
            .values(
                name=name,
                description=description,
                current_revision_id=next_current["current_revision_id"],
                current_revision_number=next_current["current_revision_number"],
                updated_at=next_current["updated_at"],
            )
        )
        return SkillSaveResult(self._summary(next_current), True, False)

    def _insert_version(self, current: dict[str, object], snapshot: SkillSnapshot) -> None:
        self.connection.execute(
            insert(LibrarySkillRevision),
            {
                "library_id": current["library_id"],
                "skill_id": current["id"],
                "id": current["current_revision_id"],
                "number": current["current_revision_number"],
                "name": current["name"],
                "description": current["description"],
                "file_count": len(snapshot.files),
                "total_size_bytes": snapshot.total_size_bytes,
                "snapshot_sha256": snapshot.snapshot_sha256,
                "created_at": current["updated_at"],
            },
        )
        self.connection.execute(
            insert(LibrarySkillFile),
            [
                {
                    "library_id": current["library_id"],
                    "skill_id": current["id"],
                    "revision_id": current["current_revision_id"],
                    "path": path,
                    "content_bytes": body,
                    "size_bytes": len(body),
                    "content_sha256": hashlib.sha256(body).digest(),
                }
                for path, body in snapshot.files
            ],
        )
