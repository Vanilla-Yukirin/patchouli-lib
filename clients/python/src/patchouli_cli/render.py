from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import TextIO

from patchouli_cli.errors import CliError
from patchouli_client.headers import ResponseMetadata
from patchouli_client.models import Page, format_rfc3339_utc


def emit_success(
    stream: TextIO,
    *,
    output: str,
    operation: str,
    value: object,
    metadata: ResponseMetadata | None,
    operation_id: str | None = None,
) -> None:
    envelope: dict[str, object] = {
        "ok": True,
        "operation": operation,
        "data": to_jsonable(value),
    }
    meta: dict[str, object] = {}
    if metadata is not None:
        meta = {
            "request_id": metadata.request_id,
            "cache_control": list(metadata.cache_control.directives),
            "etag": metadata.etag,
            "location": metadata.location,
            "idempotency_replayed": metadata.idempotency_replayed,
        }
    if operation_id is not None:
        meta["operation_id"] = operation_id
    if meta:
        envelope["metadata"] = meta

    if output == "json":
        stream.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
        return
    stream.write(f"operation: {operation}\n")
    for key, item in meta.items():
        if item is not None:
            stream.write(f"{key}: {str(item).lower() if isinstance(item, bool) else item}\n")
    stream.write(json.dumps(envelope["data"], ensure_ascii=False, indent=2) + "\n")


def emit_error(
    stream: TextIO,
    *,
    output: str,
    error: CliError,
    request_id: str | None = None,
    operation_id: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "category": error.category,
        "code": error.code,
        "message": error.public_message,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    if operation_id is not None:
        payload["operation_id"] = operation_id
    if output == "json":
        stream.write(
            json.dumps({"ok": False, "error": payload}, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        return
    suffix = ""
    if request_id is not None:
        suffix += f" request_id={request_id}"
    if operation_id is not None:
        suffix += f" operation_id={operation_id}"
    stream.write(f"error[{error.code}]: {error.public_message}{suffix}\n")


def to_jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return format_rfc3339_utc(value)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Page):
        return {
            "section_id": value.section_id,
            "book_id": value.book_id,
            "page_id": value.page_id,
            "title": value.title,
            "type": value.page_type,
            "occurred_at": to_jsonable(value.occurred_at),
            "current_revision_id": value.current_revision_id,
            "current_revision_number": value.current_revision_number,
        }
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [to_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    raise TypeError("unsupported CLI output value")
