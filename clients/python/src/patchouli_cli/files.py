from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self, TextIO

from patchouli_cli.errors import input_error
from patchouli_cli.secure_fs import SecureDirectory

MAX_METADATA_BYTES = 64 * 1024
MAX_QUERY_BYTES = 4_096
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
InputStream = BinaryIO | TextIO


class InputRoot:
    def __init__(self, path: Path, directory: SecureDirectory) -> None:
        self.path = path
        self._directory = directory

    def read(self, path_value: str, *, max_bytes: int) -> bytes:
        try:
            return self._directory.read_relative(path_value, max_bytes=max_bytes)
        except ValueError as exc:
            raise input_error(str(exc)) from exc
        except FileNotFoundError as exc:
            raise input_error("input file could not be inspected safely") from exc
        except OSError as exc:
            raise input_error(
                "input file must be a regular file and not a symlink or reparse point"
            ) from exc

    def close(self) -> None:
        self._directory.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_input_root(value: str | None, environ: Mapping[str, str]) -> InputRoot:
    configured = value or environ.get("PATCHOULI_INPUT_ROOT")
    root = Path(configured) if configured else Path.cwd()
    root = Path(os.path.abspath(root))
    try:
        directory = SecureDirectory.open(root)
    except FileNotFoundError as exc:
        raise input_error("input root could not be inspected safely") from exc
    except OSError as exc:
        raise input_error(
            "input root must identify a real directory and must not traverse symlinks "
            "or reparse points"
        ) from exc
    return InputRoot(root, directory)


def resolve_input_root(value: str | None, environ: Mapping[str, str]) -> Path:
    with open_input_root(value, environ) as root:
        return root.path


def read_file(path_value: str, *, root: Path, max_bytes: int) -> bytes:
    with open_input_root(str(root), {}) as opened:
        return opened.read(path_value, max_bytes=max_bytes)


def read_stdin(stdin: InputStream, *, max_bytes: int) -> bytes:
    value = stdin.read(max_bytes + 1)
    if isinstance(value, str):
        try:
            data = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise input_error("stdin input must be valid UTF-8") from exc
    else:
        data = value
    if len(data) > max_bytes:
        raise input_error("stdin input exceeds the command's safe size limit")
    return data


def decode_text(data: bytes, *, label: str, trim_terminal_newline: bool = False) -> str:
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise input_error(f"{label} must be valid UTF-8") from exc
    if "\x00" in value:
        raise input_error(f"{label} must not contain NUL")
    if trim_terminal_newline:
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if not value:
        raise input_error(f"{label} must not be empty")
    return value
