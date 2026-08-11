from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

import httpx

from patchouli_client.errors import TransportError
from patchouli_client.secrets import BearerToken, IdempotencyKey

Sleep = Callable[[float], None]
RandomValue = Callable[[], float]

APPROVED_RETRY_STATUSES = frozenset({408, 429, 502, 503, 504})
_TRANSIENT_REQUEST_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class OperationKind(Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_seconds: float = 0.25
    backoff_cap_seconds: float = 2.0
    retry_statuses: frozenset[int] = APPROVED_RETRY_STATUSES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("retry policy must allow between one and ten attempts")
        if (
            not math.isfinite(self.backoff_base_seconds)
            or not math.isfinite(self.backoff_cap_seconds)
            or self.backoff_base_seconds < 0
            or self.backoff_cap_seconds < 0
        ):
            raise ValueError("retry backoff must be finite and non-negative")
        try:
            retry_statuses = frozenset(self.retry_statuses)
        except TypeError as exc:
            raise ValueError("retry statuses must be a set of HTTP status codes") from exc
        if (
            any(
                isinstance(status, bool) or not isinstance(status, int) for status in retry_statuses
            )
            or not retry_statuses <= APPROVED_RETRY_STATUSES
        ):
            raise ValueError("retry statuses must be a subset of the approved transient statuses")
        object.__setattr__(self, "retry_statuses", retry_statuses)


class Transport:
    def __init__(
        self,
        base_url: str,
        *,
        http_transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Sleep | None = None,
        random_value: RandomValue | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base URL must be an HTTPS origin without user information")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("base URL must contain only an HTTPS origin")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=5.0),
            transport=http_transport,
        )
        self._retry_policy: RetryPolicy = retry_policy or RetryPolicy()
        self._sleep: Sleep = sleep or time.sleep
        self._random_value: RandomValue = random_value or random.random

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def send(
        self,
        method: str,
        path: str,
        *,
        token: BearerToken,
        operation: OperationKind,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, object] | None = None,
        body: bytes | None = None,
        replayable: bool = True,
        idempotency_key: IdempotencyKey | None = None,
    ) -> httpx.Response:
        parsed_path = urlsplit(path)
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or not parsed_path.path.startswith("/api/v1/")
        ):
            raise ValueError("request path must stay inside the /api/v1 namespace")
        supplied_headers = {key.lower() for key in (headers or {})}
        if "authorization" in supplied_headers or "idempotency-key" in supplied_headers:
            raise ValueError("secret transport headers must use their typed parameters")
        if operation is OperationKind.WRITE and idempotency_key is None:
            raise ValueError("write operations require an idempotency key")

        request_headers = dict(headers or {})
        request_headers["Authorization"] = token._authorization_value()
        if idempotency_key is not None:
            request_headers["Idempotency-Key"] = idempotency_key._header_value()

        can_retry = operation is OperationKind.READ or (replayable and idempotency_key is not None)
        attempts = 0
        while attempts < self._retry_policy.max_attempts:
            attempts += 1
            try:
                response = self._client.request(
                    method,
                    path,
                    headers=request_headers,
                    params=params,
                    json=json_body,
                    content=body,
                )
            except httpx.RequestError as exc:
                if (
                    not isinstance(exc, _TRANSIENT_REQUEST_ERRORS)
                    or not can_retry
                    or attempts >= self._retry_policy.max_attempts
                ):
                    raise TransportError(operation=operation.value, attempts=attempts) from None
                self._sleep(self._delay(attempts))
                continue

            if (
                response.status_code in self._retry_policy.retry_statuses
                and can_retry
                and attempts < self._retry_policy.max_attempts
            ):
                response.close()
                self._sleep(self._delay(attempts))
                continue
            return response

        raise AssertionError("retry loop terminated without response or transport error")

    def _delay(self, attempts: int) -> float:
        ceiling: float = min(
            self._retry_policy.backoff_cap_seconds,
            self._retry_policy.backoff_base_seconds * (2.0 ** (attempts - 1)),
        )
        random_fraction = self._random_value()
        if not math.isfinite(random_fraction) or random_fraction < 0.0:
            random_fraction = 0.0
        elif random_fraction > 1.0:
            random_fraction = 1.0
        return ceiling * random_fraction
