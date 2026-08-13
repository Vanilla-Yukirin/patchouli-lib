"""Fixed, disclosure-safe errors for the experimental backup core."""

from __future__ import annotations


class BackupError(RuntimeError):
    """Base class for an experimental backup or restore failure."""


class BackupConfigurationError(BackupError):
    """The requested source, destination, or limit is unsafe."""

    def __init__(self) -> None:
        super().__init__("Backup configuration is invalid.")


class BackupManifestError(BackupError):
    """A manifest is malformed, incompatible, or does not bind the artifact."""

    def __init__(self) -> None:
        super().__init__("Backup manifest validation failed.")


class BackupDatabaseError(BackupError):
    """A database does not satisfy the supported schema and domain invariants."""

    def __init__(self) -> None:
        super().__init__("Backup database validation failed.")


class BackupOperationError(BackupError):
    """The online-copy or fail-closed publication operation failed."""

    def __init__(self) -> None:
        super().__init__("Backup operation failed.")


class BackupTimeoutError(BackupOperationError):
    """The bounded online-copy operation exceeded its deadline."""

    def __init__(self) -> None:
        RuntimeError.__init__(self, "Backup operation timed out.")


class BackupCancelledError(BackupOperationError):
    """The caller cancelled an online-copy operation."""

    def __init__(self) -> None:
        RuntimeError.__init__(self, "Backup operation was cancelled.")


__all__ = [
    "BackupCancelledError",
    "BackupConfigurationError",
    "BackupDatabaseError",
    "BackupError",
    "BackupManifestError",
    "BackupOperationError",
    "BackupTimeoutError",
]
