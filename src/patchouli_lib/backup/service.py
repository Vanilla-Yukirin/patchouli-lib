"""Experimental, CLI-neutral SQLite backup and new-destination restore services.

This prototype does not perform live cutover, PITR quarantine, migration,
credential reactivation, or deployment-side sidecar/open-handle management.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Final

from sqlalchemy import Engine

from patchouli_lib.backup.errors import (
    BackupCancelledError,
    BackupConfigurationError,
    BackupDatabaseError,
    BackupManifestError,
    BackupOperationError,
    BackupTimeoutError,
)
from patchouli_lib.backup.manifest import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    SUPPORTED_SCHEMA_REVISION,
    BackupArtifactIdentity,
    BackupManifestV1,
    canonical_utc_timestamp,
    parse_manifest,
    require_compatible_manifest,
)
from patchouli_lib.backup.validation import validate_database

DEFAULT_BUSY_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_OPERATION_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_PAGES_PER_STEP: Final = 256
MIN_SECRET_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_MAX_HASH_CHUNK: Final = 1024 * 1024
_BUNDLE_NAMES: Final = frozenset({BACKUP_FILENAME, MANIFEST_FILENAME})

CancelCheck = Callable[[], bool]
Clock = Callable[[], datetime]
PathIdentity = tuple[int, int]


@dataclass(frozen=True, slots=True)
class BackupResult:
    bundle_path: Path
    database_path: Path
    manifest_path: Path
    manifest: BackupManifestV1


@dataclass(frozen=True, slots=True)
class RestoreResult:
    destination_path: Path
    manifest: BackupManifestV1


@dataclass(frozen=True, slots=True)
class _SourceBinding:
    configured_path: Path
    resolved_path: Path
    identity: PathIdentity


def _validated_limits(
    *,
    busy_timeout_seconds: float,
    operation_timeout_seconds: float,
    pages_per_step: int,
) -> tuple[float, float, int]:
    if (
        isinstance(busy_timeout_seconds, bool)
        or not isinstance(busy_timeout_seconds, (int, float))
        or not 0.001 <= busy_timeout_seconds <= 60.0
        or isinstance(operation_timeout_seconds, bool)
        or not isinstance(operation_timeout_seconds, (int, float))
        or not 0.001 <= operation_timeout_seconds <= 3600.0
        or type(pages_per_step) is not int
        or not 1 <= pages_per_step <= 65_536
    ):
        raise BackupConfigurationError
    return float(busy_timeout_seconds), float(operation_timeout_seconds), pages_per_step


def _canonical_path(path: Path, *, must_exist: bool) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupConfigurationError
    try:
        if path.is_symlink():
            raise BackupConfigurationError
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError):
        raise BackupConfigurationError from None
    if must_exist and not resolved.is_file():
        raise BackupConfigurationError
    return resolved


def _reject_reparse_components(path: Path) -> None:
    try:
        candidates = (path, *path.parents)
    except RecursionError:
        raise BackupConfigurationError from None
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise BackupConfigurationError from None
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or file_attributes & reparse_attribute:
            raise BackupConfigurationError


def _reject_existing_destination(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise BackupConfigurationError from None
    raise BackupConfigurationError


def _configured_source_identity(path: Path) -> PathIdentity:
    try:
        metadata = path.lstat()
    except (OSError, RecursionError):
        raise BackupConfigurationError from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _is_reparse(metadata)
        or metadata.st_nlink != 1
        or metadata.st_size == 0
        or metadata.st_ino == 0
    ):
        raise BackupConfigurationError
    return metadata.st_dev, metadata.st_ino


def _engine_source(engine: Engine) -> _SourceBinding:
    if not isinstance(engine, Engine) or engine.dialect.name != "sqlite":
        raise BackupConfigurationError
    url = engine.url
    database = url.database
    if (
        url.query
        or database is None
        or database in {"", ":memory:"}
        or not Path(database).is_absolute()
    ):
        raise BackupConfigurationError
    configured_source = Path(database)
    _reject_reparse_components(configured_source)
    source = _canonical_path(configured_source, must_exist=True)
    _reject_reparse_components(source)
    return _SourceBinding(
        configured_path=configured_source,
        resolved_path=source,
        identity=_configured_source_identity(source),
    )


def _revalidate_engine_source(
    binding: _SourceBinding,
    connection: sqlite3.Connection,
) -> None:
    try:
        _reject_reparse_components(binding.configured_path)
        resolved = _canonical_path(binding.configured_path, must_exist=True)
        _reject_reparse_components(resolved)
        if resolved != binding.resolved_path:
            raise BackupConfigurationError
        if _configured_source_identity(resolved) != binding.identity:
            raise BackupConfigurationError

        main_rows = [row for row in connection.execute("PRAGMA database_list") if row[1] == "main"]
        if len(main_rows) != 1 or not isinstance(main_rows[0][2], str) or not main_rows[0][2]:
            raise BackupConfigurationError
        connection_path = Path(main_rows[0][2])
        if not connection_path.is_absolute():
            raise BackupConfigurationError
        _reject_reparse_components(connection_path)
        connection_source = _canonical_path(connection_path, must_exist=True)
        _reject_reparse_components(connection_source)
        if _configured_source_identity(connection_source) != binding.identity:
            raise BackupConfigurationError
    except BackupConfigurationError:
        raise
    except (OSError, RecursionError, sqlite3.Error):
        raise BackupConfigurationError from None


def _same_or_nested(candidate: Path, authority: Path) -> bool:
    try:
        candidate.relative_to(authority)
    except ValueError:
        return False
    return True


def _sqlite_backup(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    operation_timeout_seconds: float,
    pages_per_step: int,
    cancel_check: CancelCheck | None,
) -> None:
    deadline = time.monotonic() + operation_timeout_seconds

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if cancel_check is not None and cancel_check():
            raise BackupCancelledError
        if time.monotonic() > deadline:
            raise BackupTimeoutError

    try:
        source.backup(destination, pages=pages_per_step, progress=progress, sleep=0.01)
    except (BackupCancelledError, BackupTimeoutError):
        raise
    except sqlite3.Error:
        raise BackupOperationError from None


def _is_reparse(metadata: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_attribute)


def _identity(metadata: os.stat_result) -> PathIdentity:
    if metadata.st_ino == 0:
        raise BackupOperationError
    return metadata.st_dev, metadata.st_ino


def _directory_identity(path: Path) -> PathIdentity:
    try:
        metadata = path.lstat()
    except (OSError, RecursionError):
        raise BackupOperationError from None
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
        raise BackupOperationError
    return _identity(metadata)


def _regular_file_identity(path: Path, *, require_single_link: bool) -> PathIdentity:
    try:
        metadata = path.lstat()
    except (OSError, RecursionError):
        raise BackupOperationError from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _is_reparse(metadata)
        or (require_single_link and metadata.st_nlink != 1)
    ):
        raise BackupOperationError
    return _identity(metadata)


def _open_verified_read(path: Path) -> tuple[int, PathIdentity]:
    expected = _regular_file_identity(path, require_single_link=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except (OSError, RecursionError):
        raise BackupOperationError from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or _is_reparse(opened)
        or opened.st_nlink != 1
        or _identity(opened) != expected
    ):
        os.close(descriptor)
        raise BackupOperationError
    return descriptor, expected


def _require_unchanged_file(path: Path, expected: PathIdentity) -> None:
    if _regular_file_identity(path, require_single_link=True) != expected:
        raise BackupOperationError


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    descriptor = -1
    try:
        descriptor, identity = _open_verified_read(path)
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            while chunk := source.read(_MAX_HASH_CHUNK):
                size += len(chunk)
                digest.update(chunk)
        _require_unchanged_file(path, identity)
    except (OSError, RecursionError):
        raise BackupOperationError from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return size, digest.hexdigest()


def _read_manifest_file(path: Path) -> bytes:
    descriptor = -1
    try:
        descriptor, identity = _open_verified_read(path)
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(MAX_MANIFEST_BYTES + 1)
        _require_unchanged_file(path, identity)
    except (OSError, RecursionError):
        raise BackupOperationError from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not 1 <= len(data) <= MAX_MANIFEST_BYTES:
        raise BackupManifestError
    return data


def _restrict(path: Path, *, directory: bool = False) -> None:
    # Windows ACLs and some filesystems do not implement POSIX modes. The
    # deployment layer remains responsible for an authenticated private store.
    with suppress(OSError):
        path.chmod(0o700 if directory else MIN_SECRET_FILE_MODE)


def _flush_file(path: Path) -> None:
    descriptor = -1
    try:
        # Windows requires a writable handle for FlushFileBuffers even though
        # this helper does not modify the already-complete artifact bytes.
        identity = _regular_file_identity(path, require_single_link=True)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        if _identity(os.fstat(descriptor)) != identity:
            raise BackupOperationError
        os.fsync(descriptor)
        _require_unchanged_file(path, identity)
    except (OSError, RecursionError):
        raise BackupOperationError from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_new_file(path: Path, data: bytes) -> None:
    descriptor = -1
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, MIN_SECRET_FILE_MODE)
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise BackupOperationError
            written += count
    except (OSError, RecursionError):
        raise BackupOperationError from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_verified_file(
    source_path: Path,
    destination_path: Path,
    *,
    operation_timeout_seconds: float,
    cancel_check: CancelCheck | None,
) -> None:
    source_descriptor = -1
    destination_descriptor = -1
    deadline = time.monotonic() + operation_timeout_seconds
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_descriptor, source_identity = _open_verified_read(source_path)
        destination_descriptor = os.open(
            destination_path,
            destination_flags,
            MIN_SECRET_FILE_MODE,
        )
        while chunk := os.read(source_descriptor, _MAX_HASH_CHUNK):
            if cancel_check is not None and cancel_check():
                raise BackupCancelledError
            if time.monotonic() > deadline:
                raise BackupTimeoutError
            written = 0
            while written < len(chunk):
                count = os.write(destination_descriptor, chunk[written:])
                if count <= 0:
                    raise BackupOperationError
                written += count
        _require_unchanged_file(source_path, source_identity)
    except (BackupCancelledError, BackupOperationError, BackupTimeoutError):
        raise
    except (OSError, RecursionError):
        raise BackupOperationError from None
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _unlink_owned_leaf(path: Path, identity: PathIdentity) -> None:
    try:
        if _regular_file_identity(path, require_single_link=False) != identity:
            raise BackupOperationError
        path.unlink()
    except FileNotFoundError:
        return
    except BackupOperationError:
        raise
    except (OSError, RecursionError):
        raise BackupOperationError from None


def _bounded_directory_names(path: Path, maximum: int) -> set[str]:
    names: set[str] = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if len(names) >= maximum:
                    raise BackupOperationError
                names.add(entry.name)
    except BackupOperationError:
        raise
    except (OSError, RecursionError):
        raise BackupOperationError from None
    return names


def _cleanup(path: Path, identity: PathIdentity, expected_names: frozenset[str]) -> None:
    """Delete only known regular leaves when the temporary directory is unchanged."""

    for attempt in range(4):
        try:
            try:
                current_identity = _directory_identity(path)
            except BackupOperationError:
                if not path.exists():
                    return
                raise
            if current_identity != identity:
                raise BackupOperationError
            names = _bounded_directory_names(path, len(expected_names) + 1)
            if not names.issubset(expected_names):
                raise BackupOperationError
            leaves = {
                name: _regular_file_identity(path / name, require_single_link=False)
                for name in names
            }
            for name, leaf_identity in leaves.items():
                if _directory_identity(path) != identity:
                    raise BackupOperationError
                child = path / name
                if _regular_file_identity(child, require_single_link=False) != leaf_identity:
                    raise BackupOperationError
                child.unlink()
            if _directory_identity(path) != identity:
                raise BackupOperationError
            path.rmdir()
            return
        except FileNotFoundError:
            if not path.exists():
                return
        except BackupOperationError:
            raise
        except OSError:
            if attempt < 3:
                time.sleep(0.01 * (attempt + 1))
    raise BackupOperationError from None


def _publish_bundle(
    staging: Path,
    staging_identity: PathIdentity,
    destination: Path,
) -> PathIdentity:
    """Publish database first and manifest last without overwriting any path.

    The manifest is the logical discovery marker. This process-level ordering
    is not crash-durable directory publication; a later deployment layer must
    define filesystem support and directory-fsync policy before operational use.
    """

    destination_identity: PathIdentity | None = None
    try:
        if _directory_identity(staging) != staging_identity:
            raise BackupOperationError
        destination.mkdir(mode=0o700)
        destination_identity = _directory_identity(destination)
        database_target = destination / BACKUP_FILENAME
        os.link(staging / BACKUP_FILENAME, database_target)
        manifest_target = destination / MANIFEST_FILENAME
        os.link(staging / MANIFEST_FILENAME, manifest_target)
        if _bounded_directory_names(destination, 3) != _BUNDLE_NAMES:
            raise BackupOperationError
        return destination_identity
    except (OSError, RecursionError):
        if destination_identity is not None:
            _cleanup(destination, destination_identity, _BUNDLE_NAMES)
        raise BackupOperationError from None
    except BackupOperationError:
        if destination_identity is not None:
            _cleanup(destination, destination_identity, _BUNDLE_NAMES)
        raise


def create_backup(
    engine: Engine,
    destination_bundle: Path,
    *,
    artifact_identity: BackupArtifactIdentity,
    app_version: str,
    clock: Clock = lambda: datetime.now(UTC),
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    pages_per_step: int = DEFAULT_PAGES_PER_STEP,
    cancel_check: CancelCheck | None = None,
) -> BackupResult:
    """Create one validated, self-contained bundle with a manifest-last marker.

    The bundle directory must not exist. The source is copied through SQLite's
    online backup API; a live database file is never copied as raw bytes. The
    manifest is a logical discovery marker because two independent file links
    cannot be published atomically as a pair. This core makes no crash-durable
    directory-publication claim. The configured source and its ancestors must
    remain on an operator-controlled filesystem during this call. Revalidation
    closes ordinary alias swaps, but perfect malicious swap-back resistance
    requires platform-specific handle-relative traversal outside this prototype.
    """

    busy_timeout, operation_timeout, pages = _validated_limits(
        busy_timeout_seconds=busy_timeout_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
        pages_per_step=pages_per_step,
    )
    if not isinstance(artifact_identity, BackupArtifactIdentity):
        raise BackupConfigurationError
    source_binding = _engine_source(engine)
    source = source_binding.resolved_path
    _reject_reparse_components(source)
    bundle = _canonical_path(destination_bundle, must_exist=False)
    _reject_reparse_components(destination_bundle)
    _reject_existing_destination(bundle)
    parent = bundle.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BackupConfigurationError
    if _same_or_nested(bundle, source) or _same_or_nested(source, bundle):
        raise BackupConfigurationError
    try:
        if source.samefile(parent):
            raise BackupConfigurationError
    except OSError:
        pass

    staging = Path(mkdtemp(prefix=f".{bundle.name}.staging-", dir=parent))
    staging_identity = _directory_identity(staging)
    _restrict(staging, directory=True)
    database_path = staging / BACKUP_FILENAME
    manifest_path = staging / MANIFEST_FILENAME
    try:
        raw = engine.raw_connection()
        try:
            source_connection = raw.driver_connection
            if not isinstance(source_connection, sqlite3.Connection):
                raise BackupConfigurationError
            journal_row = source_connection.execute("PRAGMA journal_mode").fetchone()
            if journal_row is None or not isinstance(journal_row[0], str):
                raise BackupOperationError
            source_journal_mode = journal_row[0].lower()
            with closing(sqlite3.connect(database_path, timeout=busy_timeout)) as destination:
                destination.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
                _revalidate_engine_source(source_binding, source_connection)
                _sqlite_backup(
                    source_connection,
                    destination,
                    operation_timeout_seconds=operation_timeout,
                    pages_per_step=pages,
                    cancel_check=cancel_check,
                )
                # A backup artifact is portable and never depends on source WAL sidecars.
                destination.execute("PRAGMA journal_mode = DELETE")
                destination.commit()
        finally:
            raw.close()

        _restrict(database_path)
        report = validate_database(database_path)
        byte_size, sha256 = _hash_file(database_path)
        created_at = canonical_utc_timestamp(clock())
        manifest = BackupManifestV1(
            schema_version=MANIFEST_SCHEMA_VERSION,
            backup_filename=BACKUP_FILENAME,
            byte_size=byte_size,
            sha256=sha256,
            created_at=created_at,
            app_version=app_version,
            schema_revision=report.schema_revision,
            sqlite_version=report.sqlite_version,
            source_journal_mode=source_journal_mode,
            artifact_identity=artifact_identity.identity,
            artifact_digest=artifact_identity.digest,
        )
        _flush_file(database_path)
        _write_new_file(manifest_path, manifest.canonical_bytes())
        _restrict(manifest_path)
        _flush_file(manifest_path)
        bundle_identity = _publish_bundle(staging, staging_identity, bundle)
        try:
            _cleanup(staging, staging_identity, _BUNDLE_NAMES)
        except BackupOperationError:
            _cleanup(bundle, bundle_identity, _BUNDLE_NAMES)
            raise
        return BackupResult(
            bundle_path=bundle,
            database_path=bundle / BACKUP_FILENAME,
            manifest_path=bundle / MANIFEST_FILENAME,
            manifest=manifest,
        )
    except BaseException:
        _cleanup(staging, staging_identity, _BUNDLE_NAMES)
        raise


def _require_manifest_binding(database_path: Path, manifest: BackupManifestV1) -> None:
    size, digest = _hash_file(database_path)
    if size != manifest.byte_size or digest != manifest.sha256:
        raise BackupManifestError


def _load_bundle(bundle: Path) -> tuple[Path, Path, BackupManifestV1]:
    try:
        resolved = _canonical_path(bundle, must_exist=False)
        _reject_reparse_components(bundle)
        bundle_identity = _directory_identity(resolved)
        if _bounded_directory_names(resolved, 3) != _BUNDLE_NAMES:
            raise BackupManifestError
        database_path = resolved / BACKUP_FILENAME
        manifest_path = resolved / MANIFEST_FILENAME
        database_identity = _regular_file_identity(database_path, require_single_link=True)
        manifest_identity = _regular_file_identity(manifest_path, require_single_link=True)
        if database_identity == manifest_identity:
            raise BackupManifestError
        manifest = parse_manifest(_read_manifest_file(manifest_path))
        _require_manifest_binding(database_path, manifest)
        if _directory_identity(resolved) != bundle_identity:
            raise BackupManifestError
    except BackupManifestError:
        raise
    except (BackupConfigurationError, BackupOperationError, OSError, RecursionError):
        raise BackupManifestError from None
    return resolved, database_path, manifest


def verify_backup_bundle(
    bundle: Path,
    *,
    app_version: str,
    schema_revision: str = SUPPORTED_SCHEMA_REVISION,
) -> BackupManifestV1:
    """Verify manifest binding, exact compatibility, and all database invariants."""

    _resolved, database_path, manifest = _load_bundle(bundle)
    require_compatible_manifest(
        manifest,
        app_version=app_version,
        schema_revision=schema_revision,
    )
    report = validate_database(database_path)
    if report.schema_revision != manifest.schema_revision:
        raise BackupManifestError
    _require_manifest_binding(database_path, manifest)
    return manifest


def restore_backup(
    bundle: Path,
    destination: Path,
    *,
    app_version: str,
    schema_revision: str = SUPPORTED_SCHEMA_REVISION,
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    pages_per_step: int = DEFAULT_PAGES_PER_STEP,
    cancel_check: CancelCheck | None = None,
) -> RestoreResult:
    """Restore a verified bundle into one new empty destination atomically.

    The method never overwrites an existing path and never runs Alembic. It is
    not permission to activate credentials from the restored historical state.
    """

    _busy_timeout, operation_timeout, _pages = _validated_limits(
        busy_timeout_seconds=busy_timeout_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
        pages_per_step=pages_per_step,
    )
    bundle_path, database_path, manifest = _load_bundle(bundle)
    require_compatible_manifest(
        manifest,
        app_version=app_version,
        schema_revision=schema_revision,
    )
    validate_database(database_path)
    _require_manifest_binding(database_path, manifest)
    target = _canonical_path(destination, must_exist=False)
    _reject_reparse_components(destination)
    _reject_existing_destination(target)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise BackupConfigurationError
    if _same_or_nested(target, bundle_path):
        raise BackupConfigurationError
    try:
        if target.samefile(database_path) or target.samefile(bundle):
            raise BackupConfigurationError
    except FileNotFoundError:
        pass
    except OSError:
        raise BackupConfigurationError from None

    temporary = Path(mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    temporary_identity = _directory_identity(temporary)
    staged_database = temporary / "restored.sqlite"
    temporary_names = frozenset({staged_database.name})
    published_identity: PathIdentity | None = None
    try:
        # The bundle was validated through a read-only SQLite connection under
        # the immutable-artifact assumption and has no sidecars. A bounded
        # no-follow byte copy plus source/staged hashes rechecks its exact
        # manifest binding around the copy window.
        _copy_verified_file(
            database_path,
            staged_database,
            operation_timeout_seconds=operation_timeout,
            cancel_check=cancel_check,
        )
        current_size, current_digest = _hash_file(database_path)
        if current_size != manifest.byte_size or current_digest != manifest.sha256:
            raise BackupManifestError
        _restrict(staged_database)
        restored_report = validate_database(staged_database)
        if restored_report.schema_revision != manifest.schema_revision:
            raise BackupDatabaseError
        restored_size, restored_digest = _hash_file(staged_database)
        if restored_size != manifest.byte_size or restored_digest != manifest.sha256:
            raise BackupDatabaseError
        _flush_file(staged_database)
        _reject_existing_destination(target)
        try:
            os.link(staged_database, target)
        except OSError:
            raise BackupOperationError from None
        published_identity = _regular_file_identity(target, require_single_link=False)
    except BaseException:
        _cleanup(temporary, temporary_identity, temporary_names)
        raise
    try:
        _cleanup(temporary, temporary_identity, temporary_names)
    except BackupOperationError:
        if published_identity is not None:
            _unlink_owned_leaf(target, published_identity)
        raise
    return RestoreResult(destination_path=target, manifest=manifest)


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_SECONDS",
    "DEFAULT_OPERATION_TIMEOUT_SECONDS",
    "DEFAULT_PAGES_PER_STEP",
    "BackupResult",
    "RestoreResult",
    "create_backup",
    "restore_backup",
    "verify_backup_bundle",
]
