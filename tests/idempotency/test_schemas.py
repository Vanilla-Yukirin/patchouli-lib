import hashlib

import pytest
from pydantic import ValidationError

from patchouli_lib.idempotency import (
    IDEMPOTENCY_KEY_DIGEST_DOMAIN,
    IdempotencyRequest,
    OriginalResponse,
    TransactionValidatedCaller,
    digest_idempotency_key,
    digest_request_fingerprint,
)
from patchouli_lib.idempotency.models import REPLAY_BODY_MAX_BYTES

from .conftest import CALLER_A, LIBRARY_A, ROUTE_TEMPLATE, request_for, response_for


def test_domain_separated_digest_vectors_are_stable() -> None:
    assert digest_idempotency_key("synthetic-operation-key").hex() == (
        "01c3ef393b6dc1b5750228c928701080e1aea062f37fdf4f398cc0a69a56b3ea"
    )
    assert digest_request_fingerprint(b"metadata", b"content", b"route").hex() == (
        "7c51b349065d122f091dacb9f73c37ca73e42e5311503e568015619b215f840d"
    )
    assert (
        digest_idempotency_key("synthetic-operation-key")
        != hashlib.sha256(b"synthetic-operation-key").digest()
    )
    assert IDEMPOTENCY_KEY_DIGEST_DOMAIN.endswith(b"\x00")


@pytest.mark.parametrize(
    "value",
    ["", " leading", "trailing ", "line\nbreak", "非ASCII", "x" * 257],
)
def test_idempotency_key_validation_is_fixed_and_secret_safe(value: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        digest_idempotency_key(value)
    if value:
        assert value not in str(exc_info.value)
    assert "Idempotency-Key" in str(exc_info.value)


def test_fingerprint_framing_distinguishes_part_boundaries() -> None:
    assert digest_request_fingerprint(b"ab", b"c") != digest_request_fingerprint(b"a", b"bc")
    with pytest.raises(ValueError, match="exact bytes"):
        digest_request_fingerprint(b"valid", "invalid")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "post"),
        ("method", "POST1"),
        ("route_template", "/api/v1/sections/expanded-private/pages"),
        ("route_template", "/api/v1/sections/{section_id}/pages/"),
        ("route_template", "/api/v1/sections/{SectionId}/pages"),
        ("key_digest", b"x" * 31),
        ("request_fingerprint", b"y" * 33),
    ],
)
def test_request_identity_rejects_noncanonical_or_wrong_length_values(
    field: str,
    value: object,
) -> None:
    values = request_for().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        IdempotencyRequest.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_status", 199),
        ("response_status", 300),
        ("response_media_type", "application/problem+json"),
        ("response_body", b"[]"),
        ("response_body", b'{"bad":NaN}'),
        ("response_body", b'{"nul":"\x00"}'),
        ("response_location", "https://example.invalid/private"),
        ("response_location", "/api/v1/pages/value?private=query"),
        ("response_etag", 'W/"weak"'),
        ("response_etag", '"two", "tags"'),
        ("original_request_id", "req_not-canonical"),
        ("original_request_timestamp", "2026-08-13T00:00:00Z"),
        ("original_request_timestamp", "2026-08-13T08:00:00.000000+08:00"),
    ],
)
def test_replay_response_rejects_unsafe_or_noncanonical_fields(
    field: str,
    value: object,
) -> None:
    values = response_for().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        OriginalResponse.model_validate(values)


def test_replay_body_bound_and_presentation_headers() -> None:
    with pytest.raises(ValidationError):
        OriginalResponse.model_validate(
            response_for().model_dump() | {"response_body": b"{" + b" " * REPLAY_BODY_MAX_BYTES}
        )

    response = response_for()
    assert "Idempotency-Replayed" not in response.model_dump()


def test_secret_safe_repr_and_validation_error_hide_sensitive_values() -> None:
    marker = "SYNTHETIC_PRIVATE_MARKER"
    request = request_for(key=marker)
    response = response_for(body=b'{"private":"SYNTHETIC_PRIVATE_MARKER"}')
    assert marker not in repr(request)
    assert marker not in repr(response)

    values = response.model_dump()
    values["response_etag"] = marker
    with pytest.raises(ValidationError) as exc_info:
        OriginalResponse.model_validate(values)
    assert marker not in str(exc_info.value)


def test_validated_caller_context_is_stable_caller_not_credential() -> None:
    context = TransactionValidatedCaller(library_id=LIBRARY_A, caller_id=CALLER_A)
    assert context.model_dump() == {"library_id": LIBRARY_A, "caller_id": CALLER_A}
    with pytest.raises(ValidationError):
        TransactionValidatedCaller.model_validate(
            {"library_id": LIBRARY_A, "caller_id": CALLER_A, "credential_id": "d" * 32}
        )


def test_valid_normalized_template_is_preserved_exactly() -> None:
    assert request_for().route_template == ROUTE_TEMPLATE
