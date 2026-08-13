from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from patchouli_cli.journal import (
    OperationJournal,
    OperationRecord,
    content_digest,
    operation_fingerprint,
)
from patchouli_client import (
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    BearerToken,
    ClientResponse,
    MarkdownContent,
    PageDocument,
    PatchouliClient,
)

ArchiveOperationKind = Literal["archive.create", "archive.revise"]


@dataclass(frozen=True, slots=True)
class ArchiveOperationResult:
    operation_id: str
    response: ClientResponse[PageDocument] = field(repr=False)


class ArchiveApplication:
    """Presentation-neutral archive orchestration over one typed client and journal."""

    __slots__ = ("_api_version", "_client", "_endpoint", "_journal", "_operation_id", "_token")

    def __init__(
        self,
        *,
        endpoint: str,
        api_version: str,
        client: PatchouliClient,
        token: BearerToken,
        journal: OperationJournal,
    ) -> None:
        self._endpoint = endpoint
        self._api_version = api_version
        self._client = client
        self._token = token
        self._journal = journal
        self._operation_id: str | None = None

    def __repr__(self) -> str:
        return "ArchiveApplication(endpoint=<redacted>, token=<redacted>)"

    @property
    def operation_id(self) -> str | None:
        """The safe operation UUID after a journal entry has been caller-validated."""
        return self._operation_id

    def reset_operation(self) -> None:
        """Clear presentation-visible operation state before validating a new call."""
        self._operation_id = None

    def create_archive(
        self,
        section_id: str,
        book_id: str,
        metadata: ArchiveCreateMetadata,
        content: MarkdownContent,
        *,
        operation_id: str | None = None,
    ) -> ArchiveOperationResult:
        fingerprint = self._fingerprint(
            kind="archive.create",
            section_id=section_id,
            resource_id=book_id,
            resource_field="book_id",
            metadata=metadata.to_wire(),
            content=content,
            if_match=None,
        )
        record = self._prepare(
            kind="archive.create",
            fingerprint=fingerprint,
            operation_id=operation_id,
        )
        response = self._client.create_archive(
            section_id,
            book_id,
            metadata,
            content,
            token=self._token,
            idempotency_key=record.idempotency_key,
        )
        self._journal.complete(record, request_id=response.metadata.request_id)
        return ArchiveOperationResult(operation_id=record.operation_id, response=response)

    def revise_archive(
        self,
        section_id: str,
        page_id: str,
        metadata: ArchiveRevisionMetadata,
        content: MarkdownContent,
        *,
        if_match: str,
        operation_id: str | None = None,
    ) -> ArchiveOperationResult:
        fingerprint = self._fingerprint(
            kind="archive.revise",
            section_id=section_id,
            resource_id=page_id,
            resource_field="page_id",
            metadata=metadata.to_wire(),
            content=content,
            if_match=if_match,
        )
        record = self._prepare(
            kind="archive.revise",
            fingerprint=fingerprint,
            operation_id=operation_id,
        )
        response = self._client.revise_archive(
            section_id,
            page_id,
            metadata,
            content,
            token=self._token,
            idempotency_key=record.idempotency_key,
            if_match=if_match,
        )
        self._journal.complete(record, request_id=response.metadata.request_id)
        return ArchiveOperationResult(operation_id=record.operation_id, response=response)

    def _prepare(
        self,
        *,
        kind: ArchiveOperationKind,
        fingerprint: str,
        operation_id: str | None,
    ) -> OperationRecord:
        self.reset_operation()
        if operation_id is None:
            caller_id = self._client.whoami(token=self._token).value.caller_id
            record = self._journal.prepare(
                caller_id=caller_id,
                kind=kind,
                fingerprint=fingerprint,
                operation_id=None,
            )
        else:
            record = self._journal.preflight(
                kind=kind,
                fingerprint=fingerprint,
                operation_id=operation_id,
            )
            caller_id = self._client.whoami(token=self._token).value.caller_id
            record = self._journal.validate_caller(record, caller_id=caller_id)
        self._operation_id = record.operation_id
        return record

    def _fingerprint(
        self,
        *,
        kind: ArchiveOperationKind,
        section_id: str,
        resource_id: str,
        resource_field: Literal["book_id", "page_id"],
        metadata: dict[str, object],
        content: MarkdownContent,
        if_match: str | None,
    ) -> str:
        value: dict[str, object] = {
            "kind": kind,
            "endpoint": self._endpoint,
            "api_version": self._api_version,
            "section_id": section_id,
            resource_field: resource_id,
            "metadata": metadata,
            "content_sha256": content_digest(content.body),
        }
        if if_match is not None:
            value["if_match"] = if_match
        return operation_fingerprint(value)
