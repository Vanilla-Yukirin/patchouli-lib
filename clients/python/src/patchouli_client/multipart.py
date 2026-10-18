from __future__ import annotations

import json
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from patchouli_client.models import (
    MAX_FILE_SET_FILES,
    MAX_FILE_SET_PAGE_BYTES,
    FileSetFile,
    MarkdownContent,
    require_file_set_name,
)


@dataclass(frozen=True, slots=True)
class MultipartBody:
    media_type: str
    body: bytes


def build_archive_multipart(
    metadata: Mapping[str, object],
    content: MarkdownContent,
    *,
    boundary: str | None = None,
) -> MultipartBody:
    resolved_boundary = boundary or f"patchouli-{secrets.token_hex(16)}"
    if not resolved_boundary.isascii() or any(
        character in resolved_boundary for character in '\r\n"'
    ):
        raise ValueError("multipart boundary contains an unsafe character")

    metadata_bytes = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    marker = f"--{resolved_boundary}\r\n".encode()
    body = b"".join(
        (
            marker,
            b'Content-Disposition: form-data; name="metadata"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            metadata_bytes,
            b"\r\n",
            marker,
            b'Content-Disposition: form-data; name="content"\r\n',
            b"Content-Type: text/markdown;charset=utf-8\r\n\r\n",
            content.body,
            b"\r\n",
            f"--{resolved_boundary}--\r\n".encode(),
        )
    )
    return MultipartBody(
        media_type=f"multipart/form-data; boundary={resolved_boundary}",
        body=body,
    )


def build_file_set_multipart(
    metadata: Mapping[str, object],
    files: Sequence[FileSetFile],
    *,
    boundary: str | None = None,
) -> MultipartBody:
    """Build the same metadata + repeated-file wire shape for every file count."""
    resolved_boundary = boundary or f"patchouli-{secrets.token_hex(16)}"
    if (
        not 1 <= len(resolved_boundary) <= 70
        or not resolved_boundary.isascii()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for character in resolved_boundary
        )
    ):
        raise ValueError("multipart boundary contains an unsafe character")
    if not 1 <= len(files) <= MAX_FILE_SET_FILES or any(
        type(file) is not FileSetFile for file in files
    ):
        raise ValueError("file-set upload requires 1 to 64 typed files")
    normalized = [(require_file_set_name(file.filename), file.body) for file in files]
    collisions = [unicodedata.normalize("NFC", name.casefold()) for name, _ in normalized]
    if len(collisions) != len(set(collisions)):
        raise ValueError("file-set upload contains ambiguous duplicate filenames")
    if sum(len(content) for _, content in normalized) > MAX_FILE_SET_PAGE_BYTES:
        raise ValueError("file-set upload exceeds the page byte limit")
    metadata_bytes = json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")
    if not metadata_bytes or len(metadata_bytes) > 64 * 1024:
        raise ValueError("file-set metadata exceeds the byte limit")
    marker = f"--{resolved_boundary}\r\n".encode("ascii")
    parts = [
        marker,
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        metadata_bytes,
        b"\r\n",
    ]
    for name, content in sorted(normalized, key=lambda item: item[0].encode("utf-8")):
        parts.extend(
            (
                marker,
                b'Content-Disposition: form-data; name="file"; filename="'
                + name.encode("utf-8")
                + b'"\r\n',
                b"\r\n",
                content,
                b"\r\n",
            )
        )
    parts.append(f"--{resolved_boundary}--\r\n".encode("ascii"))
    return MultipartBody(
        media_type=f"multipart/form-data; boundary={resolved_boundary}",
        body=b"".join(parts),
    )
