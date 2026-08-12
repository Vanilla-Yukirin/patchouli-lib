from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import cast

_O_DIRECTORY = cast(int, os.__dict__["O_DIRECTORY"])
_O_NOFOLLOW = cast(int, os.__dict__["O_NOFOLLOW"])
_O_CLOEXEC = cast(int, os.__dict__["O_CLOEXEC"])
_geteuid = cast(Callable[[], int], os.__dict__["geteuid"])
_DIRECTORY_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC


def _sync_fd(descriptor: int) -> None:
    os.fsync(descriptor)


class PosixFile:
    def __init__(self, descriptor: int, name: str) -> None:
        self._descriptor = descriptor
        self.name = name
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(self._descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def write_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(self._descriptor, remaining)
            if written <= 0:
                raise OSError("secure file write made no progress")
            remaining = remaining[written:]

    def sync(self) -> None:
        _sync_fd(self._descriptor)

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True


class PosixDirectory:
    def __init__(self, descriptor: int, *, created: bool = False) -> None:
        self._descriptor = descriptor
        self._closed = False
        self.created = created

    def open_child_directory(self, name: str, *, create: bool, secure: bool) -> PosixDirectory:
        created = False
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=self._descriptor)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(name, mode=0o700, dir_fd=self._descriptor)
            self.sync()
            created = True
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=self._descriptor)
        child = PosixDirectory(descriptor, created=created)
        try:
            child._verify_directory()
            if secure:
                child._secure_current_user()
                if created:
                    child.sync()
            return child
        except BaseException:
            child.close()
            raise

    def open_file(self, name: str, *, secure: bool, trusted: bool) -> PosixFile:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=self._descriptor)
        file = PosixFile(descriptor, name)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("opened object is not a regular file")
            if secure:
                _verify_current_user_file(info)
            if trusted:
                _verify_trusted_object(info, directory=False)
            return file
        except BaseException:
            file.close()
            raise

    def create_file(self, name: str, *, secure: bool) -> PosixFile:
        descriptor = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=self._descriptor)
        file = PosixFile(descriptor, name)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("created object is not a regular file")
            if secure:
                os.fchmod(descriptor, 0o600)
                _verify_current_user_file(os.fstat(descriptor))
            return file
        except BaseException:
            file.close()
            raise

    def replace(self, source_name: str, target_name: str) -> None:
        os.replace(
            source_name,
            target_name,
            src_dir_fd=self._descriptor,
            dst_dir_fd=self._descriptor,
        )

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self._descriptor)

    def sync(self) -> None:
        _sync_fd(self._descriptor)

    def verify_trusted(self) -> None:
        _verify_trusted_object(os.fstat(self._descriptor), directory=True)

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True

    def _verify_directory(self) -> None:
        if not stat.S_ISDIR(os.fstat(self._descriptor).st_mode):
            raise OSError("opened object is not a directory")

    def _secure_current_user(self) -> None:
        info = os.fstat(self._descriptor)
        if info.st_uid != _geteuid():
            raise PermissionError("secure directory is not owned by the current user")
        if _geteuid() == 0 and not self.created and stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("root will not change permissions on an existing directory")
        os.fchmod(self._descriptor, 0o700)
        secured = os.fstat(self._descriptor)
        if stat.S_IMODE(secured.st_mode) != 0o700:
            raise PermissionError("secure directory permissions could not be established")


def open_absolute_directory(
    path: Path, *, create: bool, secure: bool, trusted: bool
) -> PosixDirectory:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise OSError("secure path must be absolute")
    current = PosixDirectory(os.open("/", _DIRECTORY_FLAGS))
    created_chain = False
    try:
        if trusted:
            current.verify_trusted()
        for index, part in enumerate(absolute.parts[1:]):
            final = index == len(absolute.parts[1:]) - 1
            child = current.open_child_directory(
                part,
                create=create,
                secure=secure and (final or created_chain),
            )
            if trusted:
                child.verify_trusted()
            created_chain = created_chain or child.created
            current.close()
            current = child
        if secure:
            current._secure_current_user()
            current.sync()
        return current
    except BaseException:
        current.close()
        raise


def verify_current_user_only(path: Path, *, directory: bool) -> bool:
    absolute = Path(os.path.abspath(path))
    parent = open_absolute_directory(absolute.parent, create=False, secure=False, trusted=False)
    try:
        if directory:
            child = parent.open_child_directory(absolute.name, create=False, secure=False)
            try:
                info = os.fstat(child._descriptor)
                return info.st_uid == _geteuid() and stat.S_IMODE(info.st_mode) == 0o700
            finally:
                child.close()
        file = parent.open_file(absolute.name, secure=False, trusted=False)
        try:
            info = os.fstat(file._descriptor)
            return info.st_uid == _geteuid() and stat.S_IMODE(info.st_mode) == 0o600
        finally:
            file.close()
    finally:
        parent.close()


def _verify_current_user_file(info: os.stat_result) -> None:
    if info.st_uid != _geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("secure file is not current-user-only")


def _verify_trusted_object(info: os.stat_result, *, directory: bool) -> None:
    if info.st_uid not in {0, _geteuid()}:
        raise PermissionError("trusted path is not owned by the current user or root")
    writable = info.st_mode & 0o022
    sticky_root = directory and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    if writable and not sticky_root:
        raise PermissionError("trusted path is group- or world-writable")
