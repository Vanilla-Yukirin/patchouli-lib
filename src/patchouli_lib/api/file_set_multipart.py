"""Bounded parser for the proposed, unified Page file-set upload format.

This helper is not registered as a public route. One Markdown file and several
mixed files use the same multipart shape; uploaded MIME labels are never trusted
as evidence of a file's contents.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import parse_options_header

from patchouli_lib.api.errors import ApplicationProblem
from patchouli_lib.content.file_manifest import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
    FileManifest,
    build_file_manifest,
    normalize_file_name,
)

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

MAX_FILE_SET_METADATA_BYTES: Final = 64 * 1024
# The budget includes bounded MIME headers, delimiters, and one JSON part.
# It is an internal implementation ceiling, not an accepted public quota.
MAX_FILE_SET_MULTIPART_BYTES: Final = MAX_PAGE_BYTES + MAX_FILE_SET_METADATA_BYTES + 256 * 1024

_CONTENT_DISPOSITION = b"content-disposition"
_CONTENT_LENGTH = b"content-length"
_CONTENT_TYPE = b"content-type"
_MIME_TOKEN_BYTES = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_MAX_MIME_HEADER_BYTES = 1_024
_MAX_PART_HEADER_BYTES = 1_024


@dataclass(frozen=True, slots=True)
class ParsedFileSetUpload:
    """Raw bounded metadata and a normalized, verified complete snapshot."""

    metadata: bytes
    manifest: FileManifest


class _MalformedMultipart(ValueError):
    pass


class _UnsupportedPartMedia(ValueError):
    pass


class _PayloadTooLarge(ValueError):
    pass


def _problem(status: int, code: str, title: str, detail: str) -> ApplicationProblem:
    return ApplicationProblem(status_code=status, code=code, title=title, detail=detail)


def _validation_problem() -> ApplicationProblem:
    return _problem(
        422,
        "request_validation_failed",
        "Request validation failed",
        "The request did not satisfy the required schema.",
    )


def _size_problem() -> ApplicationProblem:
    return _problem(
        413, "content_too_large", "Content too large", "The request content is too large."
    )


def _media_problem() -> ApplicationProblem:
    return _problem(
        415, "unsupported_media_type", "Unsupported media type", "The media type is not supported."
    )


def _header_values(request: Request, expected: bytes) -> tuple[bytes, ...]:
    headers: Sequence[tuple[bytes, bytes]] = request.scope.get("headers", ())
    return tuple(value for name, value in headers if name.lower() == expected)


def _require_unique_mime_parameters(value: bytes) -> None:
    """Reject duplicate parameters without splitting quoted semicolons."""

    if not value or len(value) > _MAX_MIME_HEADER_BYTES:
        raise ValueError("Invalid bounded MIME header.")
    position = value.find(b";")
    if position < 0:
        return
    seen: set[bytes] = set()
    length = len(value)
    while position < length:
        if value[position] != ord(";"):
            raise ValueError("Invalid MIME parameter separator.")
        position += 1
        while position < length and value[position] in b" \t":
            position += 1
        name_start = position
        while position < length and value[position] in _MIME_TOKEN_BYTES:
            position += 1
        if position == name_start:
            raise ValueError("Invalid MIME parameter name.")
        name = value[name_start:position].lower()
        # python-multipart may discard an RFC 5987 filename* when filename is
        # also present. Reject extended/continued names before normalization so
        # one request cannot carry two conflicting filenames.
        if b"*" in name:
            raise ValueError("Extended MIME parameters are not supported.")
        while position < length and value[position] in b" \t":
            position += 1
        if position >= length or value[position] != ord("="):
            raise ValueError("Invalid MIME parameter assignment.")
        position += 1
        while position < length and value[position] in b" \t":
            position += 1
        if position >= length:
            raise ValueError("Missing MIME parameter value.")
        if value[position] == ord('"'):
            position += 1
            while position < length:
                current = value[position]
                if current == ord("\\"):
                    if position + 1 >= length or value[position + 1] in b"\x00\r\n":
                        raise ValueError("Invalid MIME quoted escape.")
                    position += 2
                    continue
                if current == ord('"'):
                    position += 1
                    break
                if current in b"\r\n" or current < 0x20 or current == 0x7F:
                    raise ValueError("Invalid MIME quoted value.")
                position += 1
            else:
                raise ValueError("Unterminated MIME quoted value.")
        else:
            value_start = position
            while position < length and value[position] in _MIME_TOKEN_BYTES:
                position += 1
            if position == value_start:
                raise ValueError("Invalid MIME token value.")
        while position < length and value[position] in b" \t":
            position += 1
        if position < length and value[position] != ord(";"):
            raise ValueError("Invalid MIME parameter suffix.")
        if name in seen:
            raise ValueError("Duplicate MIME parameter.")
        seen.add(name)


def _parameters(value: bytes) -> tuple[bytes, dict[bytes, bytes]]:
    _require_unique_mime_parameters(value)
    media, raw = parse_options_header(value)
    normalized = {name.lower(): parameter for name, parameter in raw.items()}
    if len(normalized) != len(raw):
        raise ValueError("Ambiguous MIME parameters.")
    return media.lower(), normalized


def _boundary(request: Request) -> bytes:
    values = _header_values(request, _CONTENT_TYPE)
    if len(values) != 1:
        raise _media_problem()
    try:
        media, parameters = _parameters(values[0])
    except (AssertionError, UnicodeError, ValueError):
        raise _media_problem() from None
    if media != b"multipart/form-data" or set(parameters) != {b"boundary"}:
        raise _media_problem()
    boundary = parameters[b"boundary"]
    if not 1 <= len(boundary) <= 70 or any(value < 0x20 or value > 0x7E for value in boundary):
        raise _validation_problem()
    return boundary


def _declared_length(request: Request) -> int | None:
    values = _header_values(request, _CONTENT_LENGTH)
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit() or len(values[0]) > 20:
        raise _validation_problem()
    return int(values[0])


class _FileSetCollector:
    def __init__(self) -> None:
        self.metadata: bytes | None = None
        self.files: list[tuple[str, bytes]] = []
        self.ended = False
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._part_name: bytes | None = None
        self._file_name: str | None = None
        self._part_data = bytearray()
        self._total_file_bytes = 0
        self._collision_keys: set[str] = set()

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        if self.ended or len(self.files) >= MAX_FILES_PER_PAGE:
            raise _PayloadTooLarge
        self._header_name.clear()
        self._header_value.clear()
        self._headers.clear()
        self._part_name = None
        self._file_name = None
        self._part_data.clear()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        if len(self._header_name) + end - start > _MAX_PART_HEADER_BYTES:
            raise _MalformedMultipart
        self._header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        if len(self._header_value) + end - start > _MAX_PART_HEADER_BYTES:
            raise _MalformedMultipart
        self._header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        name = bytes(self._header_name).lower()
        if not name or name in self._headers:
            raise _MalformedMultipart
        self._headers[name] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        if _CONTENT_DISPOSITION not in self._headers or set(self._headers) - {
            _CONTENT_DISPOSITION,
            _CONTENT_TYPE,
        }:
            raise _MalformedMultipart
        try:
            disposition, parameters = _parameters(self._headers[_CONTENT_DISPOSITION])
        except (AssertionError, UnicodeError, ValueError):
            raise _MalformedMultipart from None
        name = parameters.get(b"name")
        if disposition != b"form-data" or name not in {b"metadata", b"file"}:
            raise _MalformedMultipart
        if self.metadata is None and not self.files:
            if name != b"metadata" or set(parameters) != {b"name"}:
                raise _MalformedMultipart
        elif name != b"file" or set(parameters) != {b"name", b"filename"}:
            raise _MalformedMultipart

        media_header = self._headers.get(_CONTENT_TYPE)
        if name == b"metadata":
            if media_header is None:
                raise _UnsupportedPartMedia
            try:
                media, media_parameters = _parameters(media_header)
            except (AssertionError, UnicodeError, ValueError):
                raise _UnsupportedPartMedia from None
            if b"charset" in media_parameters:
                media_parameters[b"charset"] = media_parameters[b"charset"].lower()
            if media != b"application/json" or media_parameters not in (
                {},
                {b"charset": b"utf-8"},
            ):
                raise _UnsupportedPartMedia
        else:
            if len(self.files) >= MAX_FILES_PER_PAGE:
                raise _PayloadTooLarge
            try:
                raw_filename = parameters[b"filename"].decode("utf-8", errors="strict")
                filename = normalize_file_name(raw_filename)
            except (KeyError, UnicodeError, ValueError, TypeError):
                raise _MalformedMultipart from None
            collision_key = unicodedata.normalize("NFC", filename.casefold())
            if collision_key in self._collision_keys:
                raise _MalformedMultipart
            self._collision_keys.add(collision_key)
            self._file_name = filename
            if media_header is not None:
                try:
                    media, media_parameters = _parameters(media_header)
                except (AssertionError, UnicodeError, ValueError):
                    raise _UnsupportedPartMedia from None
                if not _valid_media_label(media, media_parameters):
                    raise _UnsupportedPartMedia
        self._part_name = name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._part_name is None:
            raise _MalformedMultipart
        incoming = end - start
        if self._part_name == b"metadata":
            if len(self._part_data) + incoming > MAX_FILE_SET_METADATA_BYTES:
                raise _PayloadTooLarge
        elif (
            len(self._part_data) + incoming > MAX_FILE_BYTES
            or self._total_file_bytes + len(self._part_data) + incoming > MAX_PAGE_BYTES
        ):
            raise _PayloadTooLarge
        self._part_data.extend(data[start:end])

    def on_part_end(self) -> None:
        if self._part_name == b"metadata":
            if self.metadata is not None or not self._part_data:
                raise _MalformedMultipart
            self.metadata = bytes(self._part_data)
        elif self._part_name == b"file" and self._file_name is not None:
            content = bytes(self._part_data)
            self._total_file_bytes += len(content)
            self.files.append((self._file_name, content))
        else:
            raise _MalformedMultipart

    def on_end(self) -> None:
        self.ended = True


def _valid_media_label(media: bytes, parameters: Mapping[bytes, bytes]) -> bool:
    """Only validate the label's syntax; never use it as a content-type authority."""

    parts = media.split(b"/")
    if len(parts) != 2 or not all(part and set(part) <= _MIME_TOKEN_BYTES for part in parts):
        return False
    if parts[0] == b"multipart":
        return False
    if set(parameters) - {b"charset"}:
        return False
    charset = parameters.get(b"charset")
    return charset is None or charset.lower() == b"utf-8"


async def parse_file_set_multipart(request: Request) -> ParsedFileSetUpload:
    """Read a complete 1..64-file snapshot from a bounded multipart stream.

    The first part is ``metadata`` JSON bytes; each subsequent ``file`` part
    has one UTF-8 flat filename. Business metadata schema remains the caller's
    responsibility. The returned manifest owns all file bytes and hashes.
    """

    boundary = _boundary(request)
    length = _declared_length(request)
    if length is not None and length > MAX_FILE_SET_MULTIPART_BYTES:
        raise _size_problem()
    collector = _FileSetCollector()
    try:
        parser = MultipartParser(
            boundary,
            callbacks=collector.callbacks(),
            max_size=MAX_FILE_SET_MULTIPART_BYTES,
            max_header_count=2,
            max_header_size=_MAX_PART_HEADER_BYTES,
        )
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_FILE_SET_MULTIPART_BYTES:
                raise _PayloadTooLarge
            parser.write(chunk)
    except _PayloadTooLarge:
        raise _size_problem() from None
    except _UnsupportedPartMedia:
        raise _media_problem() from None
    except (
        _MalformedMultipart,
        FormParserError,
        MultipartParseError,
        AssertionError,
        UnicodeError,
        ValueError,
    ):
        raise _validation_problem() from None
    if not collector.ended or collector.metadata is None or not collector.files:
        raise _validation_problem()
    if length is not None and total != length:
        raise _validation_problem()
    try:
        manifest = build_file_manifest(collector.files)
    except (TypeError, ValueError):
        raise _validation_problem() from None
    return ParsedFileSetUpload(metadata=collector.metadata, manifest=manifest)


__all__ = [
    "MAX_FILE_SET_METADATA_BYTES",
    "MAX_FILE_SET_MULTIPART_BYTES",
    "ParsedFileSetUpload",
    "parse_file_set_multipart",
]
