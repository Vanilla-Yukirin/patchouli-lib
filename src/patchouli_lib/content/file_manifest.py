"""Pure, bounded building blocks for a flat Page file snapshot.

This module does not define an HTTP or storage contract. Callers must still
authorize a Page mutation and persist the complete snapshot atomically.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

MAX_FILES_PER_PAGE = 64
MAX_FILENAME_BYTES = 255
MAX_RAW_FILENAME_CHARS = 512
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PAGE_BYTES = 64 * 1024 * 1024

_SNAPSHOT_DOMAIN = b"patchouli-page-file-snapshot-v1\x00"
_WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*')
_WINDOWS_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"})
_WINDOWS_DEVICE_PREFIXES = ("COM", "LPT")
_WINDOWS_DEVICE_SUFFIXES = frozenset("0123456789\u00b9\u00b2\u00b3")


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """One normalized, immutable file and its verified content digest."""

    name: str
    content: bytes = field(repr=False)
    content_size_bytes: int
    content_sha256: bytes

    def __post_init__(self) -> None:
        if normalize_file_name(self.name) != self.name:
            raise ValueError("Manifest file name must already be normalized.")
        if type(self.content) is not bytes:
            raise TypeError("File content must be exact bytes.")
        if len(self.content) > MAX_FILE_BYTES:
            raise ValueError("Page file content exceeds the configured limit.")
        if type(self.content_size_bytes) is not int or self.content_size_bytes != len(self.content):
            raise ValueError("File size does not match its content.")
        if (
            type(self.content_sha256) is not bytes
            or len(self.content_sha256) != 32
            or self.content_sha256 != hashlib.sha256(self.content).digest()
        ):
            raise ValueError("File digest does not match its content.")


@dataclass(frozen=True, slots=True)
class FileManifest:
    """A deterministically ordered complete Page snapshot."""

    files: tuple[ManifestFile, ...] = field(repr=False)
    total_size_bytes: int
    snapshot_sha256: bytes

    def __post_init__(self) -> None:
        if type(self.files) is not tuple or not 1 <= len(self.files) <= MAX_FILES_PER_PAGE:
            raise ValueError("Page must contain a bounded file tuple.")
        if any(type(entry) is not ManifestFile for entry in self.files):
            raise TypeError("Page files must be validated manifest entries.")
        for entry in self.files:
            entry.__post_init__()
        names = [entry.name for entry in self.files]
        if names != sorted(names, key=lambda name: name.encode("utf-8")):
            raise ValueError("Page files must be sorted by normalized name.")
        collision_keys = [unicodedata.normalize("NFC", name.casefold()) for name in names]
        if len(collision_keys) != len(set(collision_keys)):
            raise ValueError("Page contains duplicate or ambiguous file names.")
        total_size = sum(entry.content_size_bytes for entry in self.files)
        if total_size > MAX_PAGE_BYTES:
            raise ValueError("Page file content exceeds the configured limit.")
        if type(self.total_size_bytes) is not int or self.total_size_bytes != total_size:
            raise ValueError("Page size does not match its files.")
        if (
            type(self.snapshot_sha256) is not bytes
            or len(self.snapshot_sha256) != 32
            or self.snapshot_sha256 != _snapshot_digest(self.files)
        ):
            raise ValueError("Page snapshot digest does not match its files.")


def normalize_file_name(value: str) -> str:
    """Return a safe flat filename, or reject ambiguous cross-platform names."""

    if type(value) is not str:
        raise TypeError("File name must be text.")
    if len(value) > MAX_RAW_FILENAME_CHARS:
        raise ValueError("File name is too long.")
    name = unicodedata.normalize("NFC", value)
    if not name or name in {".", ".."}:
        raise ValueError("File name must identify one file.")
    if name[0] == " " or any(part.endswith(" ") for part in name.split(".")):
        raise ValueError("File name must not have ambiguous edge characters.")
    if name[-1] == ".":
        raise ValueError("File name must not have ambiguous edge characters.")
    if any(character in _WINDOWS_FORBIDDEN for character in name):
        raise ValueError("File name must not contain path or device separators.")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in name):
        raise ValueError("File name must not contain control or formatting characters.")

    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_DEVICE_NAMES or (
        len(stem) == 4
        and stem[:3] in _WINDOWS_DEVICE_PREFIXES
        and stem[3] in _WINDOWS_DEVICE_SUFFIXES
    ):
        raise ValueError("File name is reserved by a common filesystem.")
    if len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise ValueError("File name is too long.")
    return name


def _snapshot_digest(files: tuple[ManifestFile, ...]) -> bytes:
    digest = hashlib.sha256()
    digest.update(_SNAPSHOT_DOMAIN)
    digest.update(len(files).to_bytes(8, "big"))
    for entry in files:
        name_bytes = entry.name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(entry.content_size_bytes.to_bytes(8, "big"))
        digest.update(entry.content_sha256)
    return digest.digest()


def build_file_manifest(files: Iterable[tuple[str, bytes]]) -> FileManifest:
    """Validate exact file bytes and hash a canonical, complete file list.

    The digest includes a domain separator, file count, length-prefixed UTF-8
    names, byte lengths, and SHA-256 of each file. It is not an authenticator.
    """

    if isinstance(files, (str, bytes, bytearray)):
        raise TypeError("Files must be an iterable of name and bytes pairs.")

    entries: list[ManifestFile] = []
    seen_names: set[str] = set()
    total_size = 0
    for item in files:
        if len(entries) >= MAX_FILES_PER_PAGE:
            raise ValueError("Page contains too many files.")
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("Each file must be a name and bytes pair.")
        raw_name, content = item
        name = normalize_file_name(raw_name)
        collision_key = unicodedata.normalize("NFC", name.casefold())
        if collision_key in seen_names:
            raise ValueError("Page contains duplicate or ambiguous file names.")
        if type(content) is not bytes:
            raise TypeError("File content must be exact bytes.")
        size = len(content)
        if size > MAX_FILE_BYTES or total_size + size > MAX_PAGE_BYTES:
            raise ValueError("Page file content exceeds the configured limit.")
        total_size += size
        seen_names.add(collision_key)
        entries.append(
            ManifestFile(
                name=name,
                content=content,
                content_size_bytes=size,
                content_sha256=hashlib.sha256(content).digest(),
            )
        )

    if not entries:
        raise ValueError("Page must contain at least one file.")

    ordered = tuple(sorted(entries, key=lambda entry: entry.name.encode("utf-8")))
    return FileManifest(
        files=ordered,
        total_size_bytes=total_size,
        snapshot_sha256=_snapshot_digest(ordered),
    )


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_FILENAME_BYTES",
    "MAX_RAW_FILENAME_CHARS",
    "MAX_FILES_PER_PAGE",
    "MAX_PAGE_BYTES",
    "FileManifest",
    "ManifestFile",
    "build_file_manifest",
    "normalize_file_name",
]
