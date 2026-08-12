"""Transaction-neutral persistence for durable idempotency records."""

from sqlalchemy import Connection, insert, select

from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.schemas import (
    IdempotencyRequest,
    NewIdempotencyRecord,
    StoredIdempotencyRecord,
    TransactionValidatedCaller,
)


class IdempotencyRepository:
    """Read and insert records without authenticating or committing."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get(
        self,
        caller: TransactionValidatedCaller,
        request: IdempotencyRequest,
    ) -> StoredIdempotencyRecord | None:
        statement = select(IdempotencyRecord.__table__).where(
            IdempotencyRecord.library_id == caller.library_id,
            IdempotencyRecord.caller_id == caller.caller_id,
            IdempotencyRecord.method == request.method,
            IdempotencyRecord.route_template == request.route_template,
            IdempotencyRecord.key_digest == request.key_digest,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else StoredIdempotencyRecord.model_validate(dict(row))

    def add(self, record: NewIdempotencyRecord) -> StoredIdempotencyRecord:
        values = record.model_dump()
        self._connection.execute(insert(IdempotencyRecord), values)
        return StoredIdempotencyRecord.model_validate(values)


__all__ = ["IdempotencyRepository"]
