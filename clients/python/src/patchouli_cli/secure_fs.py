from __future__ import annotations

import importlib
import os
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast


class BackendFile(Protocol):
    name: str

    def read(self, max_bytes: int) -> bytes: ...

    def write_all(self, data: bytes) -> None: ...

    def sync(self) -> None: ...

    def close(self) -> None: ...


class BackendDirectory(Protocol):
    def open_child_directory(
        self, name: str, *, create: bool, secure: bool
    ) -> BackendDirectory: ...

    def open_file(self, name: str, *, secure: bool, trusted: bool) -> BackendFile: ...

    def create_file(self, name: str, *, secure: bool) -> BackendFile: ...

    def replace(self, source_name: str, target_name: str) -> None: ...

    def unlink(self, name: str) -> None: ...

    def sync(self) -> None: ...

    def verify_trusted(self) -> None: ...

    def close(self) -> None: ...


_backend = cast(
    Any,
    importlib.import_module(
        "patchouli_cli._windows_fs" if os.name == "nt" else "patchouli_cli._posix_fs"
    ),
)


class SecureFile:
    def __init__(self, backend: BackendFile) -> None:
        self._backend = backend
        self.name = backend.name

    def read(self, max_bytes: int) -> bytes:
        return self._backend.read(max_bytes)

    def write_all(self, data: bytes) -> None:
        self._backend.write_all(data)

    def sync(self) -> None:
        self._backend.sync()

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class SecureDirectory:
    def __init__(self, path: Path, backend: BackendDirectory) -> None:
        self.path = path
        self._backend = backend

    @classmethod
    def open(cls, path: Path, *, create: bool = False, secure: bool = False) -> SecureDirectory:
        absolute = Path(os.path.abspath(path))
        backend = cast(
            BackendDirectory,
            _backend.open_absolute_directory(
                absolute,
                create=create,
                secure=secure,
                trusted=False,
            ),
        )
        return cls(absolute, backend)

    def open_child(
        self, name: str, *, create: bool = False, secure: bool = False
    ) -> SecureDirectory:
        _validate_component(name)
        backend = self._backend.open_child_directory(name, create=create, secure=secure)
        return SecureDirectory(self.path / name, backend)

    def read_relative(self, path_value: str, *, max_bytes: int) -> bytes:
        parts = _relative_parts(path_value, self.path)
        with ExitStack() as stack:
            directory = self
            for part in parts[:-1]:
                directory = stack.enter_context(directory.open_child(part))
            with directory.open_file(parts[-1], secure=False, trusted=False) as file:
                data = file.read(max_bytes)
        if len(data) > max_bytes:
            raise ValueError("secure input exceeds its size limit")
        return data

    def open_file(self, name: str, *, secure: bool = False, trusted: bool = False) -> SecureFile:
        _validate_component(name)
        return SecureFile(self._backend.open_file(name, secure=secure, trusted=trusted))

    def create_file(self, name: str, *, secure: bool = True) -> SecureFile:
        _validate_component(name)
        return SecureFile(self._backend.create_file(name, secure=secure))

    def replace(self, source_name: str, target_name: str) -> None:
        _validate_component(source_name)
        _validate_component(target_name)
        self._backend.replace(source_name, target_name)

    def unlink(self, name: str) -> None:
        _validate_component(name)
        self._backend.unlink(name)

    def sync(self) -> None:
        self._backend.sync()

    def verify_trusted(self) -> None:
        self._backend.verify_trusted()

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_trusted_file(path: Path, *, max_bytes: int, required: bool) -> bytes | None:
    absolute = Path(os.path.abspath(path))
    try:
        backend = cast(
            BackendDirectory,
            _backend.open_absolute_directory(
                absolute.parent,
                create=False,
                secure=False,
                trusted=True,
            ),
        )
        parent = SecureDirectory(absolute.parent, backend)
    except FileNotFoundError:
        if not required:
            return None
        raise
    with parent:
        parent.verify_trusted()
        try:
            file = parent.open_file(absolute.name, trusted=True)
        except FileNotFoundError:
            if not required:
                return None
            raise
        with file:
            data = file.read(max_bytes)
    if len(data) > max_bytes:
        raise ValueError("trusted file exceeds its size limit")
    return data


def open_journal_directory(root: Path, profile: str) -> SecureDirectory:
    root_directory = SecureDirectory.open(root, create=True, secure=True)
    try:
        profile_directory = root_directory.open_child(profile, create=True, secure=True)
    finally:
        root_directory.close()
    return profile_directory


def current_user_only(path: Path, *, directory: bool) -> bool:
    return cast(bool, _backend.verify_current_user_only(path, directory=directory))


def _relative_parts(path_value: str, root: Path) -> tuple[str, ...]:
    requested = Path(path_value)
    if requested.is_absolute():
        candidate = Path(os.path.abspath(requested))
        try:
            requested = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("input file must remain inside the configured input root") from exc
    parts = tuple(requested.parts)
    if not parts:
        raise ValueError("input path must identify a regular file")
    for part in parts:
        _validate_component(part)
    return parts


def _validate_component(value: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or (os.name == "nt" and ":" in value)
    ):
        raise ValueError("secure path contains an invalid component")
