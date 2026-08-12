from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

_WinDLL = cast(Any, ctypes.__dict__["WinDLL"])
_get_last_error = cast(Callable[[], int], ctypes.__dict__["get_last_error"])
_kernel32 = _WinDLL("kernel32", use_last_error=True)
_advapi32 = _WinDLL("advapi32", use_last_error=True)
_ntdll = _WinDLL("ntdll", use_last_error=True)

_INVALID_HANDLE = ctypes.c_void_p(-1).value
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_SYNCHRONIZE = 0x00100000
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_LIST_DIRECTORY = 0x0001
_FILE_DELETE_CHILD = 0x0040
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_NORMAL = 0x0080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_OPEN_EXISTING = 3
_OBJ_CASE_INSENSITIVE = 0x00000040
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_FILE_RENAME_INFORMATION = 10
_FILE_DISPOSITION_INFORMATION = 13
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ACL_REVISION = 2
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERIT_ONLY_ACE = 0x08
_WIN_LOCAL_SYSTEM_SID = 22
_WIN_BUILTIN_ADMINISTRATORS_SID = 26
_WIN_CREATOR_OWNER_RIGHTS_SID = 71
_DANGEROUS_WRITE_MASK = (
    0x0002
    | 0x0004
    | 0x0010
    | 0x0040
    | 0x0100
    | _DELETE
    | _WRITE_DAC
    | 0x00080000
    | _GENERIC_WRITE
    | 0x10000000
)


def _flush_handle(handle: int) -> None:
    if not _kernel32.FlushFileBuffers(handle):
        _raise_last_error("secure filesystem buffers could not be flushed")


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", wintypes.LPVOID), ("Information", ctypes.c_size_t)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("CreationTime", wintypes.FILETIME),
        ("LastAccessTime", wintypes.FILETIME),
        ("LastWriteTime", wintypes.FILETIME),
        ("VolumeSerialNumber", wintypes.DWORD),
        ("FileSizeHigh", wintypes.DWORD),
        ("FileSizeLow", wintypes.DWORD),
        ("NumberOfLinks", wintypes.DWORD),
        ("FileIndexHigh", wintypes.DWORD),
        ("FileIndexLow", wintypes.DWORD),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _SecurityDescriptorControl(ctypes.c_ushort):
    pass


class _SecurityDescriptor(ctypes.Structure):
    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("Control", ctypes.c_ushort),
        ("Owner", wintypes.LPVOID),
        ("Group", wintypes.LPVOID),
        ("Sacl", wintypes.LPVOID),
        ("Dacl", wintypes.LPVOID),
    ]


_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_ByHandleFileInformation),
]
_kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
_kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = list(_kernel32.ReadFile.argtypes)
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
_kernel32.FlushFileBuffers.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.LocalFree.argtypes = [wintypes.LPVOID]
_kernel32.LocalFree.restype = wintypes.LPVOID
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.GetLengthSid.argtypes = [wintypes.LPVOID]
_advapi32.GetLengthSid.restype = wintypes.DWORD
_advapi32.CopySid.argtypes = [wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
_advapi32.CopySid.restype = wintypes.BOOL
_advapi32.InitializeAcl.argtypes = [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD]
_advapi32.InitializeAcl.restype = wintypes.BOOL
_advapi32.AddAccessAllowedAceEx.argtypes = [
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
]
_advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
_advapi32.InitializeSecurityDescriptor.argtypes = [wintypes.LPVOID, wintypes.DWORD]
_advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
_advapi32.SetSecurityDescriptorOwner.argtypes = [
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
]
_advapi32.SetSecurityDescriptorOwner.restype = wintypes.BOOL
_advapi32.SetSecurityDescriptorDacl.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPVOID,
    wintypes.BOOL,
]
_advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
_advapi32.SetSecurityDescriptorControl.argtypes = [
    wintypes.LPVOID,
    ctypes.c_ushort,
    ctypes.c_ushort,
]
_advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL
_advapi32.SetSecurityInfo.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.LPVOID,
]
_advapi32.SetSecurityInfo.restype = wintypes.DWORD
_advapi32.GetSecurityInfo.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.LPVOID),
]
_advapi32.GetSecurityInfo.restype = wintypes.DWORD
_advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
_advapi32.EqualSid.restype = wintypes.BOOL
_advapi32.IsWellKnownSid.argtypes = [wintypes.LPVOID, wintypes.DWORD]
_advapi32.IsWellKnownSid.restype = wintypes.BOOL
_advapi32.GetSecurityDescriptorControl.argtypes = [
    wintypes.LPVOID,
    ctypes.POINTER(_SecurityDescriptorControl),
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
_advapi32.GetAclInformation.argtypes = [
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
]
_advapi32.GetAclInformation.restype = wintypes.BOOL
_advapi32.GetAce.argtypes = [
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID),
]
_advapi32.GetAce.restype = wintypes.BOOL
_ntdll.NtCreateFile.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    ctypes.POINTER(_ObjectAttributes),
    ctypes.POINTER(_IoStatusBlock),
    wintypes.LPVOID,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.LPVOID,
    wintypes.ULONG,
]
_ntdll.NtCreateFile.restype = wintypes.LONG
_ntdll.NtSetInformationFile.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_IoStatusBlock),
    wintypes.LPVOID,
    wintypes.ULONG,
    wintypes.ULONG,
]
_ntdll.NtSetInformationFile.restype = wintypes.LONG
_ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
_ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


class _CurrentUserSecurity:
    def __init__(self, *, directory: bool) -> None:
        self.sid = _current_user_sid()
        sid_pointer = ctypes.cast(self.sid, wintypes.LPVOID)
        sid_length = _advapi32.GetLengthSid(sid_pointer)
        if not sid_length:
            _raise_last_error("current user SID length could not be read")
        acl_size = 8 + 8 + sid_length
        self.acl = ctypes.create_string_buffer(acl_size)
        if not _advapi32.InitializeAcl(self.acl, acl_size, _ACL_REVISION):
            _raise_last_error("current-user ACL could not be initialized")
        flags = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        if not _advapi32.AddAccessAllowedAceEx(
            self.acl,
            _ACL_REVISION,
            flags,
            _FILE_ALL_ACCESS,
            sid_pointer,
        ):
            _raise_last_error("current-user ACL entry could not be created")
        self.descriptor = ctypes.create_string_buffer(ctypes.sizeof(_SecurityDescriptor))
        if not _advapi32.InitializeSecurityDescriptor(self.descriptor, 1):
            _raise_last_error("security descriptor could not be initialized")
        if not _advapi32.SetSecurityDescriptorOwner(self.descriptor, sid_pointer, False):
            _raise_last_error("security descriptor owner could not be set")
        if not _advapi32.SetSecurityDescriptorDacl(self.descriptor, True, self.acl, False):
            _raise_last_error("security descriptor DACL could not be set")
        if not _advapi32.SetSecurityDescriptorControl(
            self.descriptor,
            _SE_DACL_PROTECTED,
            _SE_DACL_PROTECTED,
        ):
            _raise_last_error("security descriptor inheritance could not be disabled")


class WindowsFile:
    def __init__(self, handle: int, name: str) -> None:
        self._handle = handle
        self.name = name
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            count = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(count)
            read = wintypes.DWORD()
            if not _kernel32.ReadFile(self._handle, buffer, count, ctypes.byref(read), None):
                _raise_last_error("secure file could not be read")
            if read.value == 0:
                break
            chunks.append(buffer.raw[: read.value])
            remaining -= read.value
        return b"".join(chunks)

    def write_all(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 64 * 1024]
            written = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(chunk)
            if not _kernel32.WriteFile(
                self._handle,
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                _raise_last_error("secure file could not be written")
            if written.value == 0:
                raise OSError("secure file write made no progress")
            offset += written.value

    def sync(self) -> None:
        _flush_handle(self._handle)

    def close(self) -> None:
        if not self._closed:
            if not _kernel32.CloseHandle(self._handle):
                _raise_last_error("secure file handle could not be closed")
            self._closed = True


class WindowsDirectory:
    def __init__(self, handle: int, *, write_capable: bool = False) -> None:
        self._handle = handle
        self._closed = False
        self.write_capable = write_capable

    def open_child_directory(self, name: str, *, create: bool, secure: bool) -> WindowsDirectory:
        _validate_component(name)
        access = _secure_directory_access() if secure else _read_directory_access()
        try:
            handle = _nt_open(
                self._handle,
                name,
                access=access,
                disposition=_FILE_OPEN,
                directory=True,
                security=None,
            )
            created = False
        except FileNotFoundError:
            if not create:
                raise
            security = _CurrentUserSecurity(directory=True)
            handle = _nt_open(
                self._handle,
                name,
                access=_secure_directory_access(),
                disposition=_FILE_CREATE,
                directory=True,
                security=security,
            )
            self.sync()
            created = True
        child = WindowsDirectory(handle, write_capable=secure or created)
        try:
            _verify_kind(handle, directory=True)
            if secure:
                _establish_current_user_only(handle, directory=True)
                if created:
                    child.sync()
            return child
        except BaseException:
            child.close()
            raise

    def open_file(self, name: str, *, secure: bool, trusted: bool) -> WindowsFile:
        _validate_component(name)
        handle = _nt_open(
            self._handle,
            name,
            access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
            disposition=_FILE_OPEN,
            directory=False,
            security=None,
        )
        file = WindowsFile(handle, name)
        try:
            _verify_kind(handle, directory=False)
            if secure:
                _verify_current_user_only(handle, directory=False)
            if trusted:
                _verify_trusted_security(handle, directory=False)
            return file
        except BaseException:
            file.close()
            raise

    def create_file(self, name: str, *, secure: bool) -> WindowsFile:
        _validate_component(name)
        security = _CurrentUserSecurity(directory=False) if secure else None
        handle = _nt_open(
            self._handle,
            name,
            access=(
                _GENERIC_READ | _GENERIC_WRITE | _DELETE | _READ_CONTROL | _WRITE_DAC | _SYNCHRONIZE
            ),
            disposition=_FILE_CREATE,
            directory=False,
            security=security,
        )
        file = WindowsFile(handle, name)
        try:
            _verify_kind(handle, directory=False)
            if secure:
                _verify_current_user_only(handle, directory=False)
            return file
        except BaseException:
            file.close()
            raise

    def replace(self, source_name: str, target_name: str) -> None:
        source = self.open_file_for_delete(source_name)
        renamed = False
        try:
            encoded = target_name.encode("utf-16-le")
            buffer = ctypes.create_string_buffer(20 + len(encoded))
            buffer[0] = b"\x01"
            struct.pack_into("P", buffer, 8, self._handle)
            struct.pack_into("I", buffer, 16, len(encoded))
            ctypes.memmove(ctypes.addressof(buffer) + 20, encoded, len(encoded))
            status = _ntdll.NtSetInformationFile(
                source._handle,
                ctypes.byref(_IoStatusBlock()),
                buffer,
                len(buffer),
                _FILE_RENAME_INFORMATION,
            )
            _check_status(status, "secure file could not be replaced")
            renamed = True
            _verify_current_user_only(source._handle, directory=False)
        finally:
            if not renamed:
                _mark_delete(source._handle)
            source.close()

    def unlink(self, name: str) -> None:
        file = self.open_file_for_delete(name)
        try:
            _mark_delete(file._handle)
        finally:
            file.close()

    def open_file_for_delete(self, name: str) -> WindowsFile:
        _validate_component(name)
        handle = _nt_open(
            self._handle,
            name,
            access=_DELETE | _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            disposition=_FILE_OPEN,
            directory=False,
            security=None,
        )
        _verify_kind(handle, directory=False)
        return WindowsFile(handle, name)

    def sync(self) -> None:
        _flush_handle(self._handle)

    def verify_trusted(self) -> None:
        _verify_trusted_security(self._handle, directory=True)

    def close(self) -> None:
        if not self._closed:
            if not _kernel32.CloseHandle(self._handle):
                _raise_last_error("secure directory handle could not be closed")
            self._closed = True


def open_absolute_directory(
    path: Path, *, create: bool, secure: bool, trusted: bool
) -> WindowsDirectory:
    drive, parts = _absolute_parts(path)
    root_access = _read_directory_access()
    root = _open_drive_root(drive, root_access)
    stack: list[tuple[WindowsDirectory, str | None]] = [(WindowsDirectory(root), None)]
    created_chain = False
    try:
        if trusted:
            _verify_trusted_security(root, directory=True, allow_other_owner=True)
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            should_secure = secure and (final or created_chain)
            current = stack[-1][0]
            try:
                child = current.open_child_directory(part, create=False, secure=should_secure)
                created = False
            except FileNotFoundError:
                if not create:
                    raise
                if not current.write_capable:
                    upgraded = _reopen_stack_directory(stack)
                    stack[-1] = (upgraded, stack[-1][1])
                    current = upgraded
                child = current.open_child_directory(part, create=True, secure=True)
                created = True
            created_chain = created_chain or created
            if trusted:
                child.verify_trusted()
            stack.append((child, part))
        result = stack.pop()[0]
        if secure and not result.write_capable:
            stack.append((result, parts[-1] if parts else None))
            result = _reopen_stack_directory(stack)
            stack.pop()
        if secure:
            _establish_current_user_only(result._handle, directory=True)
            result.sync()
        return result
    except BaseException:
        raise
    finally:
        for directory, _ in reversed(stack):
            directory.close()


def verify_current_user_only(path: Path, *, directory: bool) -> bool:
    absolute = Path(os.path.abspath(path))
    parent = open_absolute_directory(absolute.parent, create=False, secure=False, trusted=False)
    try:
        if directory:
            child = parent.open_child_directory(absolute.name, create=False, secure=False)
            try:
                _verify_current_user_only(child._handle, directory=True)
            finally:
                child.close()
        else:
            file = parent.open_file(absolute.name, secure=False, trusted=False)
            try:
                _verify_current_user_only(file._handle, directory=False)
            finally:
                file.close()
        return True
    except (OSError, PermissionError):
        return False
    finally:
        parent.close()


def _absolute_parts(path: Path) -> tuple[str, tuple[str, ...]]:
    value = os.path.abspath(path)
    if value.startswith("\\\\") or value.startswith("\\?\\"):
        raise OSError("secure filesystem paths must use a local drive")
    drive, tail = os.path.splitdrive(value)
    if not drive or not tail.startswith(("\\", "/")):
        raise OSError("secure filesystem path must be drive-absolute")
    parts = tuple(part for part in tail.replace("/", "\\").split("\\") if part)
    for part in parts:
        _validate_component(part)
    return drive, parts


def _open_drive_root(drive: str, access: int) -> int:
    handle = _kernel32.CreateFileW(
        f"{drive}\\",
        access,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE:
        _raise_last_error("drive root could not be opened safely")
    _verify_kind(handle, directory=True)
    return cast(int, handle)


def _nt_open(
    root: int,
    name: str,
    *,
    access: int,
    disposition: int,
    directory: bool,
    security: _CurrentUserSecurity | None,
) -> int:
    buffer = ctypes.create_unicode_buffer(name)
    byte_length = len(name.encode("utf-16-le"))
    string = _UnicodeString(byte_length, byte_length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    descriptor = None if security is None else ctypes.cast(security.descriptor, wintypes.LPVOID)
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        root,
        ctypes.pointer(string),
        _OBJ_CASE_INSENSITIVE,
        descriptor,
        None,
    )
    output = wintypes.HANDLE()
    status_block = _IoStatusBlock()
    options = (
        (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE)
        | _FILE_SYNCHRONOUS_IO_NONALERT
        | _FILE_OPEN_REPARSE_POINT
    )
    status = _ntdll.NtCreateFile(
        ctypes.byref(output),
        access,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        _FILE_SHARE_ALL,
        disposition,
        options,
        None,
        0,
    )
    _check_status(status, "filesystem object could not be opened safely")
    if output.value is None:
        raise OSError("filesystem object returned an invalid handle")
    return output.value


def _check_status(status: int, message: str) -> None:
    unsigned = ctypes.c_ulong(status).value
    if status >= 0:
        return
    if unsigned in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
        raise FileNotFoundError(message)
    if unsigned == _STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError(message)
    error = _ntdll.RtlNtStatusToDosError(status)
    raise OSError(error, message)


def _verify_kind(handle: int, *, directory: bool) -> _ByHandleFileInformation:
    information = _ByHandleFileInformation()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        _raise_last_error("opened object identity could not be verified")
    if information.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("opened object is a reparse point")
    is_directory = bool(information.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
    if is_directory != directory:
        raise OSError("opened object has an unexpected filesystem type")
    return information


def _file_identity(handle: int) -> tuple[int, int, int]:
    info = _verify_kind(handle, directory=True)
    return info.VolumeSerialNumber, info.FileIndexHigh, info.FileIndexLow


def _reopen_stack_directory(stack: list[tuple[WindowsDirectory, str | None]]) -> WindowsDirectory:
    current, name = stack[-1]
    if name is None:
        raise PermissionError("drive root cannot be upgraded for journal creation")
    parent = stack[-2][0]
    handle = _nt_open(
        parent._handle,
        name,
        access=_secure_directory_access(),
        disposition=_FILE_OPEN,
        directory=True,
        security=None,
    )
    upgraded = WindowsDirectory(handle, write_capable=True)
    if _file_identity(handle) != _file_identity(current._handle):
        upgraded.close()
        raise OSError("directory identity changed during secure traversal")
    current.close()
    return upgraded


def _secure_directory_access() -> int:
    return _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _WRITE_DAC | _SYNCHRONIZE


def _read_directory_access() -> int:
    return (
        _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _SYNCHRONIZE
    )


def _current_user_sid() -> ctypes.Array[ctypes.c_char]:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        _raise_last_error("current process token could not be opened")
    try:
        required = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if not required.value:
            _raise_last_error("current token user could not be sized")
        token_user = ctypes.create_string_buffer(required.value)
        if not _advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            token_user,
            required,
            ctypes.byref(required),
        ):
            _raise_last_error("current token user could not be read")
        sid_pointer = ctypes.cast(token_user, ctypes.POINTER(wintypes.LPVOID)).contents.value
        length = _advapi32.GetLengthSid(sid_pointer)
        sid = ctypes.create_string_buffer(length)
        if not _advapi32.CopySid(length, sid, sid_pointer):
            _raise_last_error("current user SID could not be copied")
        return sid
    finally:
        _kernel32.CloseHandle(token)


def _establish_current_user_only(handle: int, *, directory: bool) -> None:
    _verify_owner_is_current_user(handle)
    security = _CurrentUserSecurity(directory=directory)
    result = _advapi32.SetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        security.acl,
        None,
    )
    if result:
        raise OSError(result, "current-user-only DACL could not be established")
    _verify_current_user_only(handle, directory=directory)


def _verify_owner_is_current_user(handle: int) -> None:
    owner, _, descriptor = _security_info(handle)
    try:
        current = _current_user_sid()
        if not _advapi32.EqualSid(owner, current):
            raise PermissionError("secure storage is not owned by the current user")
    finally:
        _kernel32.LocalFree(descriptor)


def _verify_current_user_only(handle: int, *, directory: bool) -> None:
    owner, dacl, descriptor = _security_info(handle)
    try:
        current = _current_user_sid()
        if not _advapi32.EqualSid(owner, current):
            raise PermissionError("secure storage is not owned by the current user")
        control = _SecurityDescriptorControl()
        revision = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            _raise_last_error("security descriptor control could not be read")
        if not control.value & _SE_DACL_PROTECTED:
            raise PermissionError("secure storage DACL inherits access")
        entries = _acl_entries(dacl)
        if len(entries) != 1:
            raise PermissionError("secure storage DACL is not current-user-only")
        ace_type, ace_flags, mask, sid = entries[0]
        expected_flags = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        if (
            ace_type != _ACCESS_ALLOWED_ACE_TYPE
            or ace_flags != expected_flags
            or mask != _FILE_ALL_ACCESS
            or not _advapi32.EqualSid(sid, current)
        ):
            raise PermissionError("secure storage DACL is not current-user-only")
    finally:
        _kernel32.LocalFree(descriptor)


def _verify_trusted_security(
    handle: int, *, directory: bool, allow_other_owner: bool = False
) -> None:
    owner, dacl, descriptor = _security_info(handle)
    try:
        current = _current_user_sid()
        if not allow_other_owner and not (
            _advapi32.EqualSid(owner, current)
            or _advapi32.IsWellKnownSid(owner, _WIN_LOCAL_SYSTEM_SID)
            or _advapi32.IsWellKnownSid(owner, _WIN_BUILTIN_ADMINISTRATORS_SID)
        ):
            raise PermissionError("trusted path has an untrusted Windows owner")
        dangerous_mask = (
            (_FILE_DELETE_CHILD | _DELETE | _WRITE_DAC | 0x00080000 | 0x10000000)
            if directory
            else _DANGEROUS_WRITE_MASK
        )
        for ace_type, ace_flags, mask, sid in _acl_entries(dacl):
            if ace_flags & _INHERIT_ONLY_ACE:
                continue
            if not mask & dangerous_mask or ace_type == _ACCESS_DENIED_ACE_TYPE:
                continue
            if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
                raise PermissionError("trusted path has an unrecognized permissive ACL entry")
            if not (
                _advapi32.EqualSid(sid, current)
                or _advapi32.IsWellKnownSid(sid, _WIN_LOCAL_SYSTEM_SID)
                or _advapi32.IsWellKnownSid(sid, _WIN_BUILTIN_ADMINISTRATORS_SID)
                or _advapi32.IsWellKnownSid(sid, _WIN_CREATOR_OWNER_RIGHTS_SID)
            ):
                raise PermissionError("trusted path grants write access to an untrusted principal")
    finally:
        _kernel32.LocalFree(descriptor)


def _security_info(handle: int) -> tuple[int, int, int]:
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    result = _advapi32.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise OSError(result, "filesystem security descriptor could not be read")
    if not owner.value or not dacl.value or not descriptor.value:
        _kernel32.LocalFree(descriptor)
        raise PermissionError("filesystem object has an unsafe null owner or DACL")
    return owner.value, dacl.value, descriptor.value


def _acl_entries(dacl: int) -> list[tuple[int, int, int, int]]:
    information = _AclSizeInformation()
    if not _advapi32.GetAclInformation(
        dacl,
        ctypes.byref(information),
        ctypes.sizeof(information),
        _ACL_SIZE_INFORMATION,
    ):
        _raise_last_error("filesystem ACL could not be inspected")
    entries: list[tuple[int, int, int, int]] = []
    for index in range(information.AceCount):
        ace = wintypes.LPVOID()
        if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
            _raise_last_error("filesystem ACL entry could not be read")
        address = cast(int, ace.value)
        header = ctypes.string_at(address, 4)
        ace_type, ace_flags, _ = struct.unpack("BBH", header)
        mask = struct.unpack("I", ctypes.string_at(address + 4, 4))[0]
        sid = address + 8
        entries.append((ace_type, ace_flags, mask, sid))
    return entries


def _mark_delete(handle: int) -> None:
    value = ctypes.c_ubyte(1)
    status = _ntdll.NtSetInformationFile(
        handle,
        ctypes.byref(_IoStatusBlock()),
        ctypes.byref(value),
        ctypes.sizeof(value),
        _FILE_DISPOSITION_INFORMATION,
    )
    _check_status(status, "temporary secure file could not be removed")


def _validate_component(name: str) -> None:
    if not name or name in {".", ".."} or any(character in name for character in "\\/:\x00"):
        raise OSError("secure path contains an invalid component")


def _raise_last_error(message: str) -> None:
    error = _get_last_error()
    raise OSError(error, message)
