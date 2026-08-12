from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    CONFIG = 3
    CREDENTIAL = 4
    JOURNAL = 5
    AUTH = 10
    SCOPE = 11
    NOT_FOUND = 12
    CONFLICT = 13
    PRECONDITION = 14
    VALIDATION = 15
    SERVICE = 16
    TRANSPORT = 17
    EDGE_GATE = 18
    PROTOCOL = 19
    INTERNAL = 70
    INTERRUPTED = 130


class CliError(Exception):
    """A deliberately safe diagnostic with a deterministic process status."""

    def __init__(self, exit_code: ExitCode, category: str, code: str, message: str) -> None:
        self.exit_code = exit_code
        self.category = category
        self.code = code
        self.public_message = message
        super().__init__(message)

    def __repr__(self) -> str:
        return (
            f"CliError(exit_code={int(self.exit_code)}, category={self.category!r}, "
            f"code={self.code!r})"
        )


def usage_error(message: str = "invalid command arguments; use --help") -> CliError:
    return CliError(ExitCode.USAGE, "usage", "invalid_arguments", message)


def config_error(message: str) -> CliError:
    return CliError(ExitCode.CONFIG, "config", "invalid_config", message)


def credential_error(message: str) -> CliError:
    return CliError(ExitCode.CREDENTIAL, "credential", "credential_unavailable", message)


def journal_error(message: str) -> CliError:
    return CliError(ExitCode.JOURNAL, "journal", "journal_error", message)


def input_error(message: str) -> CliError:
    return CliError(ExitCode.VALIDATION, "validation", "invalid_input", message)
