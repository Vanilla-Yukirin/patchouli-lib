from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from patchouli_client.models import MarkdownContent


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
