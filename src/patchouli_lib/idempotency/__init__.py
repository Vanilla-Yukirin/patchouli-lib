"""Stable idempotency persistence and transaction-neutral service primitives."""

from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.repository import IdempotencyRepository
from patchouli_lib.idempotency.schemas import (
    IDEMPOTENCY_KEY_DIGEST_DOMAIN,
    MAX_IDEMPOTENCY_KEY_BYTES,
    REQUEST_FINGERPRINT_DOMAIN,
    IdempotencyRequest,
    NewIdempotencyRecord,
    OriginalResponse,
    ReplayResponse,
    StoredIdempotencyRecord,
    TransactionValidatedCaller,
    digest_idempotency_key,
    digest_request_fingerprint,
)
from patchouli_lib.idempotency.service import (
    IDEMPOTENCY_CONFLICT_DETAIL,
    IdempotencyConflictError,
    IdempotencyPersistenceError,
    IdempotencyService,
)

__all__ = [
    "IDEMPOTENCY_CONFLICT_DETAIL",
    "IDEMPOTENCY_KEY_DIGEST_DOMAIN",
    "MAX_IDEMPOTENCY_KEY_BYTES",
    "REQUEST_FINGERPRINT_DOMAIN",
    "IdempotencyConflictError",
    "IdempotencyPersistenceError",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "IdempotencyRequest",
    "IdempotencyService",
    "NewIdempotencyRecord",
    "OriginalResponse",
    "ReplayResponse",
    "StoredIdempotencyRecord",
    "TransactionValidatedCaller",
    "digest_idempotency_key",
    "digest_request_fingerprint",
]
