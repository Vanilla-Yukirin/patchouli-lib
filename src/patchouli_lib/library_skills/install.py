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


def _entry_key(path: Path) -> str | None:
    # WindowsPath equality folds case even in case-sensitive directories. Compare
    # canonical directory entries, not lexical Path equality or hard-link identity.
    try:
        return str(path.resolve(strict=True))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _contains_entry(path: Path, candidates: frozenset[Path] | set[Path]) -> bool:
    entry = _entry_key(path)
    return entry is not None and any(_entry_key(candidate) == entry for candidate in candidates)


def _target(
    root: Path, resource_path: str, *, obsolete_files: frozenset[Path] = frozenset()
) -> Path:
    normalized = normalize_skill_path(resource_path)
    if normalized != resource_path:
        raise SkillValidationError("Install paths must be canonical.")
    candidate = root.joinpath(*normalized.split("/"))
    for component in (candidate, *candidate.parents):
        if component == root.parent:
            break
        if component.is_symlink() or component.is_junction():
            raise SkillConflictError("A local package path is a symbolic link or junction.")
        if (
            component != candidate
            and component.exists()
            and not component.is_dir()
            and not _contains_entry(component, obsolete_files)
        ):
            raise SkillConflictError("A local file conflicts with a package directory.")
    return candidate


def _directory_plan(
    directory: Path, package_directories: set[Path], obsolete_files: frozenset[Path]
) -> set[Path]:
    pending = [directory]
    removable: set[Path] = set()
    while pending:
        parent = pending.pop()
        if not _contains_entry(parent, package_directories):
            raise SkillConflictError("An untracked local directory would be overwritten.")
        removable.add(parent)
        for child in parent.iterdir():
            if child.is_symlink() or child.is_junction():
                raise SkillConflictError("A local package path is a symbolic link or junction.")
            if child.is_dir():
                pending.append(child)
            elif not _contains_entry(child, obsolete_files):
                raise SkillConflictError("An untracked local file would be overwritten.")
    return removable


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
    if (
        _has_link(root)
        or _has_link(receipt_path)
        or receipt_path.is_relative_to(root)
        or root.is_relative_to(receipt_path)
    ):
        raise SkillConflictError("The install destination or receipt is unsafe.")
    if root.name != manifest.get("name"):
        raise SkillConflictError("Confirm the new Skill folder name before installing.")
    if any(
        parent.exists() and not parent.is_dir()
        for parent in (root, *root.parents, *receipt_path.parents)
    ) or (receipt_path.exists() and not receipt_path.is_file()):
        raise SkillConflictError("The install destination or receipt conflicts with a local path.")
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
    tracked_files: set[Path] = set()
    package_directories: set[Path] = set()
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
            tracked_files.add(local)
            for parent in local.parents:
                if parent == root:
                    break
                package_directories.add(parent)
    expected_targets = {root.joinpath(*path.split("/")) for path in expected}
    obsolete_files = frozenset(
        local for local in tracked_files if not _contains_entry(local, expected_targets)
    )
    remove_directories: set[Path] = set()
    for path in expected:
        local = _target(root, path, obsolete_files=obsolete_files)
        if local.is_dir():
            remove_directories.update(_directory_plan(local, package_directories, obsolete_files))
        elif local.exists() and not _contains_entry(local, tracked_files):
            raise SkillConflictError("An untracked local file would be overwritten.")
        elif local.exists() and not local.is_file():
            raise SkillConflictError("A local directory conflicts with the package.")
    if old_revision == manifest["revision_id"] and previous == expected:
        return "unchanged"
    receipt = {
        **identity,
        "revision_id": manifest["revision_id"],
        "snapshot_sha256": manifest["snapshot_sha256"],
        "files": expected,
    }
    receipt_body = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
    for local in obsolete_files:
        local.unlink()
    for directory in sorted(remove_directories, key=lambda path: len(path.parts), reverse=True):
        directory.rmdir()
    for path, body in snapshot.files:
        _write(_target(root, path), body)
    # Do not leave retired empty package directories as future "untracked" collisions.
    # Unknown child directories/files keep their parent nonempty and are preserved.
    for directory in sorted(package_directories, key=lambda path: len(path.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    _write(receipt_path, receipt_body)
    return "installed"
