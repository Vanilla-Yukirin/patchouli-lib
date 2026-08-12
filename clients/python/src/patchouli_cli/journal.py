from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from patchouli_cli.errors import journal_error
from patchouli_cli.secure_fs import open_journal_directory
from patchouli_client import IdempotencyKey

_OPERATION_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.ASCII,
)
_RECORD_KEYS = {
    "version",
    "operation_id",
    "caller_id",
    "kind",
    "fingerprint",
    "idempotency_key",
    "status",
    "created_at",
    "completed_at",
    "request_id",
}


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    caller_id: str
    kind: str
    fingerprint: str = field(repr=False)
    idempotency_key: IdempotencyKey = field(repr=False)
    raw_key: str = field(repr=False)
    status: str
    created_at: str
    completed_at: str | None = None
    request_id: str | None = None


class OperationJournal:
    def __init__(self, root: Path, profile: str) -> None:
        try:
            self._directory = open_journal_directory(root, profile)
        except (OSError, PermissionError, ValueError) as exc:
            raise journal_error("operation journal directory could not be secured") from exc

    def prepare(
        self,
        *,
        caller_id: str,
        kind: str,
        fingerprint: str,
        operation_id: str | None,
    ) -> OperationRecord:
        caller = _validate_caller_id(caller_id)
        if operation_id is None:
            identifier = str(uuid.uuid4())
            raw_key = f"op_{secrets.token_urlsafe(32)}"
            record = OperationRecord(
                operation_id=identifier,
                caller_id=caller,
                kind=kind,
                fingerprint=fingerprint,
                idempotency_key=IdempotencyKey(raw_key),
                raw_key=raw_key,
                status="pending",
                created_at=_timestamp(),
            )
            self._create(record)
            return record

        record = self.preflight(
            kind=kind,
            fingerprint=fingerprint,
            operation_id=operation_id,
        )
        return self.validate_caller(record, caller_id=caller)

    def preflight(
        self,
        *,
        kind: str,
        fingerprint: str,
        operation_id: str,
    ) -> OperationRecord:
        identifier = _validate_operation_id(operation_id)
        record = self._load(identifier)
        if record.kind != kind or not secrets.compare_digest(record.fingerprint, fingerprint):
            raise journal_error(
                "operation journal does not match this endpoint, API version, route, "
                "metadata, content, or precondition"
            )
        return record

    def validate_caller(
        self,
        record: OperationRecord,
        *,
        caller_id: str,
    ) -> OperationRecord:
        caller = _validate_caller_id(caller_id)
        if record.caller_id != caller:
            raise journal_error("operation journal belongs to a different caller")
        return record

    def complete(self, record: OperationRecord, *, request_id: str) -> OperationRecord:
        completed = OperationRecord(
            operation_id=record.operation_id,
            caller_id=record.caller_id,
            kind=record.kind,
            fingerprint=record.fingerprint,
            idempotency_key=record.idempotency_key,
            raw_key=record.raw_key,
            status="succeeded",
            created_at=record.created_at,
            completed_at=_timestamp(),
            request_id=request_id,
        )
        self._replace(completed)
        return completed

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

    def _name(self, operation_id: str) -> str:
        return f"{operation_id}.json"

    def _create(self, record: OperationRecord) -> None:
        name = self._name(record.operation_id)
        try:
            with self._directory.create_file(name, secure=True) as file:
                file.write_all(_record_payload(record))
                file.sync()
            self._directory.sync()
            self._verify_record(name)
        except (OSError, PermissionError, ValueError) as exc:
            with suppress(OSError, PermissionError, ValueError):
                self._directory.unlink(name)
                self._directory.sync()
            raise journal_error("operation journal could not be created durably") from exc

    def _load(self, operation_id: str) -> OperationRecord:
        name = self._name(operation_id)
        try:
            with self._directory.open_file(name, secure=True) as file:
                data = file.read(16_384)
        except FileNotFoundError as exc:
            raise journal_error("operation journal record could not be inspected safely") from exc
        except (OSError, PermissionError, ValueError) as exc:
            raise journal_error(
                "operation journal record must be a secure regular non-reparse file"
            ) from exc
        if len(data) > 16_384:
            raise journal_error("operation journal record exceeds the safe size limit")
        try:
            parsed: object = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise journal_error("operation journal record is invalid") from exc
        return _parse_record(parsed, operation_id)

    def _replace(self, record: OperationRecord) -> None:
        target = self._name(record.operation_id)
        temporary = f".{record.operation_id}.{uuid.uuid4()}.tmp"
        replaced = False
        try:
            self._verify_record(target)
            with self._directory.create_file(temporary, secure=True) as file:
                file.write_all(_record_payload(record))
                file.sync()
            self._directory.sync()
            self._verify_record(temporary)
            self._directory.replace(temporary, target)
            replaced = True
            self._directory.sync()
            self._verify_record(target)
        except (OSError, PermissionError, ValueError) as exc:
            if not replaced:
                with suppress(OSError, PermissionError, ValueError):
                    self._directory.unlink(temporary)
                    self._directory.sync()
            raise journal_error("operation journal could not be updated durably") from exc

    def _verify_record(self, name: str) -> None:
        with self._directory.open_file(name, secure=True):
            pass


def operation_fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record_payload(record: OperationRecord) -> bytes:
    return json.dumps(
        {
            "version": 2,
            "operation_id": record.operation_id,
            "caller_id": record.caller_id,
            "kind": record.kind,
            "fingerprint": record.fingerprint,
            "idempotency_key": record.raw_key,
            "status": record.status,
            "created_at": record.created_at,
            "completed_at": record.completed_at,
            "request_id": record.request_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_record(value: object, expected_id: str) -> OperationRecord:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise journal_error("operation journal record is invalid")
    data = cast(dict[str, object], value)
    if set(data) != _RECORD_KEYS or data.get("version") != 2:
        raise journal_error("operation journal record has an unsupported schema")
    required_strings = (
        "operation_id",
        "caller_id",
        "kind",
        "fingerprint",
        "idempotency_key",
        "status",
        "created_at",
    )
    if any(not isinstance(data.get(key), str) for key in required_strings):
        raise journal_error("operation journal record contains invalid fields")
    operation_id = cast(str, data["operation_id"])
    if operation_id != expected_id or _OPERATION_PATTERN.fullmatch(operation_id) is None:
        raise journal_error("operation journal record identity is invalid")
    caller_id = _validate_caller_id(cast(str, data["caller_id"]))
    fingerprint = cast(str, data["fingerprint"])
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint, re.ASCII) is None:
        raise journal_error("operation journal fingerprint is invalid")
    status = cast(str, data["status"])
    if status not in {"pending", "succeeded"}:
        raise journal_error("operation journal status is invalid")
    completed_at = data["completed_at"]
    request_id = data["request_id"]
    if completed_at is not None and not isinstance(completed_at, str):
        raise journal_error("operation journal completion timestamp is invalid")
    if request_id is not None and not isinstance(request_id, str):
        raise journal_error("operation journal request ID is invalid")
    raw_key = cast(str, data["idempotency_key"])
    try:
        key = IdempotencyKey(raw_key)
    except ValueError as exc:
        raise journal_error("operation journal idempotency key is invalid") from exc
    return OperationRecord(
        operation_id=operation_id,
        caller_id=caller_id,
        kind=cast(str, data["kind"]),
        fingerprint=fingerprint,
        idempotency_key=key,
        raw_key=raw_key,
        status=status,
        created_at=cast(str, data["created_at"]),
        completed_at=completed_at,
        request_id=request_id,
    )


def _validate_operation_id(value: str) -> str:
    if _OPERATION_PATTERN.fullmatch(value) is None:
        raise journal_error("operation ID must be a canonical version-4 UUID")
    return value


def _validate_caller_id(value: str) -> str:
    if not value or len(json.dumps(value, ensure_ascii=True).encode("ascii")) > 4_096:
        raise journal_error("operation caller ID is invalid")
    return value


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
