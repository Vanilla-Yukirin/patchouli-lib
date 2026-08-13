from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from patchouli_lib.backup import BackupArtifactIdentity, BackupManifestError
from patchouli_lib.backup.manifest import (
    BACKUP_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
    canonical_utc_timestamp,
    parse_manifest,
    require_compatible_manifest,
)


def manifest() -> BackupManifestV1:
    return BackupManifestV1(
        schema_version=MANIFEST_SCHEMA_VERSION,
        backup_filename=BACKUP_FILENAME,
        byte_size=4096,
        sha256="a" * 64,
        created_at="2026-08-13T12:34:56.123456Z",
        app_version="0.1.0a0",
        schema_revision=SUPPORTED_SCHEMA_REVISION,
        sqlite_version="3.50.4",
        source_journal_mode="wal",
        artifact_identity="example.invalid/project@sha256",
        artifact_digest="sha256:" + "b" * 64,
    )


def test_manifest_is_exact_canonical_utf8_json() -> None:
    expected = manifest()
    wire = expected.canonical_bytes()

    assert wire.endswith(b"\n")
    assert parse_manifest(wire) == expected
    assert (
        canonical_utc_timestamp(datetime(2026, 8, 13, 20, 34, 56, 123456, tzinfo=UTC))
        == "2026-08-13T20:34:56.123456Z"
    )


@pytest.mark.parametrize(
    "wire",
    [
        b"{}\n",
        b'{"schema_version":NaN}\n',
        b'{"schema_version":1,"schema_version":1}\n',
        b"\xff",
        b"[]\n",
        b"[" * 1_100 + b"]" * 1_100,
        b" " * (16 * 1024 + 1),
    ],
)
def test_manifest_rejects_malformed_noncanonical_and_duplicate_json(wire: bytes) -> None:
    with pytest.raises(BackupManifestError, match="Backup manifest validation failed"):
        parse_manifest(wire)


def test_manifest_rejects_unknown_key_and_noncanonical_spacing() -> None:
    raw = manifest().canonical_bytes()
    with_unknown = raw[:-2] + b',"unknown":true}\n'
    spaced = raw.replace(b'"byte_size":4096', b'"byte_size": 4096')

    for wire in (with_unknown, spaced):
        with pytest.raises(BackupManifestError):
            parse_manifest(wire)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("backup_filename", "../database.sqlite"),
        ("byte_size", True),
        ("sha256", "A" * 64),
        ("created_at", "2026-08-13T12:34:56Z"),
        ("app_version", "bad version"),
        ("schema_revision", "forged"),
        ("sqlite_version", "unknown"),
        ("source_journal_mode", "memory"),
        ("artifact_identity", "private identity with spaces"),
        ("artifact_digest", "b" * 64),
    ],
)
def test_manifest_rejects_out_of_contract_values(field: str, value: object) -> None:
    values = asdict(manifest())
    with pytest.raises(BackupManifestError):
        BackupManifestV1(**(values | {field: value}))


def test_artifact_identity_and_app_compatibility_are_exact() -> None:
    identity = BackupArtifactIdentity("oci.example/project@sha256", "sha256:" + "c" * 64)
    assert identity.identity == "oci.example/project@sha256"

    require_compatible_manifest(manifest(), app_version="0.1.0a0")
    with pytest.raises(BackupManifestError):
        require_compatible_manifest(manifest(), app_version="0.1.0a1")
