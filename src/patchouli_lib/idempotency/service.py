"""Transaction-neutral idempotency lookup and successful-response recording."""

from sqlalchemy.exc import SQLAlchemyError

from patchouli_lib.idempotency.repository import IdempotencyRepository
from patchouli_lib.idempotency.schemas import (
    IdempotencyRequest,
    NewIdempotencyRecord,
    OriginalResponse,
    ReplayResponse,
    StoredIdempotencyRecord,
    TransactionValidatedCaller,
)

IDEMPOTENCY_CONFLICT_DETAIL = "The idempotency key was already used for a different request."


class IdempotencyConflictError(RuntimeError):
    """A namespace already belongs to a different semantic request."""

    def __init__(self) -> None:
        super().__init__(IDEMPOTENCY_CONFLICT_DETAIL)


class IdempotencyPersistenceError(RuntimeError):
    """A replay record could not be persisted without exposing its response."""

    def __init__(self) -> None:
        super().__init__("Idempotency response could not be persisted.")


class IdempotencyService:
    """Resolve and record replay state inside one caller-owned write transaction.

    The caller must first revalidate the active caller, presented credential, and
    exact Section authorization in the same short ``BEGIN IMMEDIATE`` transaction.
    This service never authenticates, mutates content, emits audit rows, or commits.
    """

    def __init__(self, repository: IdempotencyRepository) -> None:
        self._repository = repository

    def lookup(
        self,
        caller: TransactionValidatedCaller,
        request: IdempotencyRequest,
    ) -> ReplayResponse | None:
        stored = self._repository.get(caller, request)
        if stored is None:
            return None
        self._require_matching_fingerprint(stored, request)
        return self._as_replay(stored)

    def record_success(
        self,
        caller: TransactionValidatedCaller,
        request: IdempotencyRequest,
        response: OriginalResponse,
    ) -> StoredIdempotencyRecord:
        """Recheck the namespace and insert one eligible success without committing."""
        existing = self._repository.get(caller, request)
        if existing is not None:
            self._require_matching_fingerprint(existing, request)
            return existing
        try:
            stored = self._repository.add(
                NewIdempotencyRecord(
                    library_id=caller.library_id,
                    caller_id=caller.caller_id,
                    **request.model_dump(),
                    **response.model_dump(),
                )
            )
        except SQLAlchemyError:
            pass
        else:
            return stored
        raise IdempotencyPersistenceError

    @staticmethod
    def _require_matching_fingerprint(
        stored: StoredIdempotencyRecord,
        request: IdempotencyRequest,
    ) -> None:
        if stored.request_fingerprint != request.request_fingerprint:
            raise IdempotencyConflictError

    @staticmethod
    def _as_replay(stored: StoredIdempotencyRecord) -> ReplayResponse:
        return ReplayResponse.model_validate(
            stored.model_dump(
                include={
                    "response_status",
                    "response_media_type",
                    "response_body",
                    "response_location",
                    "response_etag",
                    "original_request_id",
                    "original_request_timestamp",
                }
            )
        )


__all__ = [
    "IDEMPOTENCY_CONFLICT_DETAIL",
    "IdempotencyConflictError",
    "IdempotencyPersistenceError",
    "IdempotencyService",
]
