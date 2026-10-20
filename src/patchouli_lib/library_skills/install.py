"""Local package installation primitives; never fetch, print secrets or execute files."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from patchouli_lib.library_skills.service import (
    SkillConflictError,
    SkillValidationError,
    build_skill_snapshot,
    normalize_skill_path,
)


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _has_link(path: Path) -> bool:
    return any(part.is_symlink() or part.is_junction() for part in (path, *path.parents))


def _target(root: Path, resource_path: str) -> Path:
    normalized = normalize_skill_path(resource_path)
    if normalized != resource_path:
        raise SkillValidationError("Install paths must be canonical.")
    candidate = root.joinpath(*normalized.split("/"))
    for component in (candidate, *candidate.parents):
        if component == root.parent:
            break
        if component.is_symlink() or component.is_junction():
            raise SkillConflictError("A local package path is a symbolic link or junction.")
        if component != candidate and component.exists() and not component.is_dir():
            raise SkillConflictError("A local file conflicts with a package directory.")
    return candidate


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending: Path | None = None
    try:
        with NamedTemporaryFile(
            prefix=".patchouli-install-", dir=path.parent, delete=False
        ) as stream:
            pending = Path(stream.name)
            stream.write(body)
        os.replace(pending, path)
    finally:
        if pending is not None and pending.exists():
            pending.unlink()


def install_skill_package(
    destination: Path,
    manifest: Mapping[str, Any],
    downloaded: Mapping[str, bytes],
    *,
    service_origin: str,
    receipt_path: Path,
) -> str:
    """Install explicitly downloaded bytes, refusing any unreviewed local divergence.

    The host chooses/authorizes both paths and fetches every manifest entry with
    its own secret handling. The receipt is outside the package; neither local
    connection settings nor unknown extra files are replaced or deleted.
    """
    root = destination.absolute()
    receipt_path = receipt_path.absolute()
    if _has_link(root) or _has_link(receipt_path) or receipt_path.is_relative_to(root):
        raise SkillConflictError("The install destination or receipt is unsafe.")
    if root.name != manifest.get("name"):
        raise SkillConflictError("Confirm the new Skill folder name before installing.")
    snapshot = build_skill_snapshot(downloaded.items())
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != len(downloaded):
        raise SkillValidationError("Package manifest file set differs.")
    expected: dict[str, str] = {}
    for entry in entries:
        path = entry["path"]
        if path in expected or path not in downloaded:
            raise SkillValidationError("Package manifest paths differ.")
        body = downloaded[path]
        if (
            type(entry["bytes"]) is not int
            or len(body) != entry["bytes"]
            or _digest(body) != entry["sha256"]
        ):
            raise SkillValidationError("Package bytes or digest differ.")
        expected[path] = entry["sha256"]
    if snapshot.snapshot_sha256.hex() != manifest.get("snapshot_sha256"):
        raise SkillValidationError("Package snapshot digest differs.")
    identity = {
        "service_origin": service_origin,
        "library_id": manifest["library_id"],
        "skill_id": manifest["skill_id"],
    }
    previous: dict[str, str] = {}
    old_revision: object = None
    if receipt_path.exists():
        old = json.loads(receipt_path.read_text(encoding="utf-8"))
        if any(old.get(key) != value for key, value in identity.items()):
            raise SkillConflictError("The install receipt belongs to another Skill.")
        previous = old["files"]
        old_revision = old.get("revision_id")
        for path, digest in previous.items():
            local = _target(root, path)
            if not local.is_file() or _digest(local.read_bytes()) != digest:
                raise SkillConflictError("Local Skill changes need an explicit merge decision.")
    for path in expected:
        local = _target(root, path)
        if local.exists() and path not in previous:
            raise SkillConflictError("An untracked local file would be overwritten.")
        if local.exists() and not local.is_file():
            raise SkillConflictError("A local directory conflicts with the package.")
    if old_revision == manifest["revision_id"] and previous == expected:
        return "unchanged"
    for path, body in snapshot.files:
        _write(_target(root, path), body)
    for path in previous.keys() - expected.keys():
        _target(root, path).unlink()
    receipt = {
        **identity,
        "revision_id": manifest["revision_id"],
        "snapshot_sha256": manifest["snapshot_sha256"],
        "files": expected,
    }
    _write(receipt_path, (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode())
    return "installed"
