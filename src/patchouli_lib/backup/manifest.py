"""Canonical manifest primitives for the experimental backup bundle format.

The manifest SHA-256 detects accidental corruption. It is not an authenticator:
operators must protect the complete bundle with authenticated storage, a MAC,
or a signature before trusting artifacts from an adversarial location.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final

from patchouli_lib.backup.errors import BackupManifestError

MANIFEST_SCHEMA_VERSION: Final = 1
BACKUP_FILENAME: Final = "database.sqlite"
MANIFEST_FILENAME: Final = "manifest.json"
SUPPORTED_SCHEMA_REVISION: Final = "20260813_0006"
MAX_MANIFEST_BYTES: Final = 16 * 1024
MAX_IDENTITY_BYTES: Final = 256
MAX_APP_VERSION_BYTES: Final = 100
MAX_SQLITE_VERSION_BYTES: Final = 40
MAX_DATABASE_BYTES: Final = (1 << 63) - 1

_SHA256_PATTERN: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_ARTIFACT_DIGEST_PATTERN: Final = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_IDENTITY_PATTERN: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+:/@-]*\Z")
_APP_VERSION_PATTERN: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")
_SQLITE_VERSION_PATTERN: Final = re.compile(r"\A[0-9]+(?:\.[0-9]+){1,3}\Z")
_UTC_PATTERN: Final = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_JOURNAL_MODES: Final = frozenset({"delete", "persist", "truncate", "wal"})
_MANIFEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "backup_filename",
        "byte_size",
        "sha256",
        "created_at",
        "app_version",
        "schema_revision",
        "sqlite_version",
        "source_journal_mode",
        "artifact_identity",
        "artifact_digest",
    }
)


def _require_bounded_ascii(value: object, maximum: int, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise BackupManifestError
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise BackupManifestError from None
    if not 1 <= len(encoded) <= maximum or pattern.fullmatch(value) is None:
        raise BackupManifestError
    return value


def canonical_utc_timestamp(value: datetime) -> str:
    """Return one exact microsecond UTC wire timestamp."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BackupManifestError
    resolved = value.astimezone(UTC)
    return resolved.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_PATTERN.fullmatch(value) is None:
        raise BackupManifestError
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise BackupManifestError from None
    if canonical_utc_timestamp(parsed) != value:
        raise BackupManifestError
    return value


@dataclass(frozen=True, slots=True)
class BackupArtifactIdentity:
    """Caller-supplied, non-secret identity for the producing source artifact."""

    identity: str
    digest: str

    def __post_init__(self) -> None:
        _require_bounded_ascii(self.identity, MAX_IDENTITY_BYTES, _IDENTITY_PATTERN)
        if (
            not isinstance(self.digest, str)
            or _ARTIFACT_DIGEST_PATTERN.fullmatch(self.digest) is None
        ):
            raise BackupManifestError


@dataclass(frozen=True, slots=True)
class BackupManifestV1:
    """Exact v1 metadata for one self-contained SQLite backup artifact."""

    schema_version: int
    backup_filename: str
    byte_size: int
    sha256: str
    created_at: str
    app_version: str
    schema_revision: str
    sqlite_version: str
    source_journal_mode: str
    artifact_identity: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise BackupManifestError
        if self.backup_filename != BACKUP_FILENAME:
            raise BackupManifestError
        if type(self.byte_size) is not int or not 1 <= self.byte_size <= MAX_DATABASE_BYTES:
            raise BackupManifestError
        if not isinstance(self.sha256, str) or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise BackupManifestError
        validate_utc_timestamp(self.created_at)
        _require_bounded_ascii(self.app_version, MAX_APP_VERSION_BYTES, _APP_VERSION_PATTERN)
        if self.schema_revision != SUPPORTED_SCHEMA_REVISION:
            raise BackupManifestError
        _require_bounded_ascii(
            self.sqlite_version,
            MAX_SQLITE_VERSION_BYTES,
            _SQLITE_VERSION_PATTERN,
        )
        if self.source_journal_mode not in _JOURNAL_MODES:
            raise BackupManifestError
        BackupArtifactIdentity(self.artifact_identity, self.artifact_digest)

    def canonical_bytes(self) -> bytes:
        """Serialize exact UTF-8 JSON with stable ordering and one trailing newline."""

        return (
            json.dumps(
                asdict(self),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BackupManifestError
        result[key] = value
    return result


def parse_manifest(data: bytes) -> BackupManifestV1:
    """Parse an exact, bounded, duplicate-free canonical v1 manifest."""

    if type(data) is not bytes or not 1 <= len(data) <= MAX_MANIFEST_BYTES:
        raise BackupManifestError
    try:
        text = data.decode("utf-8", errors="strict")
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(BackupManifestError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        BackupManifestError,
        RecursionError,
        ValueError,
    ):
        raise BackupManifestError from None
    if not isinstance(raw, dict) or frozenset(raw) != _MANIFEST_KEYS:
        raise BackupManifestError
    try:
        manifest = BackupManifestV1(**raw)
    except (TypeError, BackupManifestError):
        raise BackupManifestError from None
    if manifest.canonical_bytes() != data:
        raise BackupManifestError
    return manifest


def require_compatible_manifest(
    manifest: BackupManifestV1,
    *,
    app_version: str,
    schema_revision: str = SUPPORTED_SCHEMA_REVISION,
) -> None:
    """Fail closed unless application and schema compatibility are exact."""

    _require_bounded_ascii(app_version, MAX_APP_VERSION_BYTES, _APP_VERSION_PATTERN)
    if schema_revision != SUPPORTED_SCHEMA_REVISION:
        raise BackupManifestError
    if manifest.app_version != app_version or manifest.schema_revision != schema_revision:
        raise BackupManifestError


__all__ = [
    "BACKUP_FILENAME",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_MANIFEST_BYTES",
    "SUPPORTED_SCHEMA_REVISION",
    "BackupArtifactIdentity",
    "BackupManifestV1",
    "canonical_utc_timestamp",
    "parse_manifest",
    "require_compatible_manifest",
    "validate_utc_timestamp",
]
