"""Experimental portable backup and new-destination restore primitives.

The current public design still lists a supported backup/restore policy as an
open decision. These APIs are therefore a reviewed prototype, not a stable
operational restore contract. They intentionally omit cutover and deployment.
"""

from patchouli_lib.backup.errors import (
    BackupCancelledError,
    BackupConfigurationError,
    BackupDatabaseError,
    BackupError,
    BackupManifestError,
    BackupOperationError,
    BackupTimeoutError,
)
from patchouli_lib.backup.manifest import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_REVISION,
    BackupArtifactIdentity,
    BackupManifestV1,
    parse_manifest,
)
from patchouli_lib.backup.service import (
    BackupResult,
    RestoreResult,
    create_backup,
    restore_backup,
    verify_backup_bundle,
)
from patchouli_lib.backup.validation import DatabaseValidationReport, validate_database

__all__ = [
    "BACKUP_FILENAME",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_REVISION",
    "BackupArtifactIdentity",
    "BackupCancelledError",
    "BackupConfigurationError",
    "BackupDatabaseError",
    "BackupError",
    "BackupManifestError",
    "BackupManifestV1",
    "BackupOperationError",
    "BackupResult",
    "BackupTimeoutError",
    "DatabaseValidationReport",
    "RestoreResult",
    "create_backup",
    "parse_manifest",
    "restore_backup",
    "validate_database",
    "verify_backup_bundle",
]
