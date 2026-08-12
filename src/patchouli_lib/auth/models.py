from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.auth.tokens import TOKEN_VERSION, VERIFIER_BYTES
from patchouli_lib.library.models import NAME_MAX_LENGTH, OPAQUE_ID_LENGTH
from patchouli_lib.models import Base

SELECTOR_LENGTH = 22
KIND_MAX_LENGTH = 16
ACTION_MAX_LENGTH = 100
REQUEST_ID_MAX_LENGTH = 100
RESOURCE_ID_MAX_LENGTH = 200


class Caller(Base):
    __tablename__ = "auth_callers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_auth_callers_library_id_libraries",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_callers_id_lower_hex",
        ),
        CheckConstraint(
            "kind IN ('operator', 'agent')",
            name="ck_auth_callers_kind",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_auth_callers_name",
        ),
        CheckConstraint(
            "length(description) <= 4000",
            name="ck_auth_callers_description",
        ),
        CheckConstraint(
            "policy_version >= 1",
            name="ck_auth_callers_policy_version",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at "
            "AND (disabled_at IS NULL OR "
            "(disabled_at >= created_at AND disabled_at <= updated_at))",
            name="ck_auth_callers_timestamps",
        ),
        UniqueConstraint("id", "library_id", name="uq_auth_callers_id_library_id"),
        UniqueConstraint(
            "library_id",
            "name",
            name="uq_auth_callers_library_id_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(KIND_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    disabled_at: Mapped[int | None] = mapped_column(BigInteger)


class Credential(Base):
    __tablename__ = "auth_credentials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_auth_credentials_caller_library_callers",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["rotated_to_credential_id", "caller_id", "library_id"],
            [
                "auth_credentials.id",
                "auth_credentials.caller_id",
                "auth_credentials.library_id",
            ],
            name="fk_auth_credentials_rotated_to_caller_library_credentials",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_credentials_id_lower_hex",
        ),
        CheckConstraint(
            "length(selector) = 22 AND selector NOT GLOB '*[^A-Za-z0-9_-]*'",
            name="ck_auth_credentials_selector",
        ),
        CheckConstraint(
            f"token_version = {TOKEN_VERSION}",
            name="ck_auth_credentials_token_version",
        ),
        CheckConstraint(
            f"length(verifier) = {VERIFIER_BYTES}",
            name="ck_auth_credentials_verifier_length",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at AND expires_at > created_at "
            "AND (last_used_at IS NULL OR "
            "(last_used_at >= created_at AND last_used_at < expires_at "
            "AND last_used_at <= updated_at)) "
            "AND (revoked_at IS NULL OR "
            "(revoked_at >= created_at AND revoked_at <= updated_at)) "
            "AND (rotated_at IS NULL OR "
            "(rotated_at >= created_at AND rotated_at <= updated_at))",
            name="ck_auth_credentials_timestamps",
        ),
        CheckConstraint(
            "(rotated_at IS NULL AND rotated_to_credential_id IS NULL) OR "
            "(rotated_at IS NOT NULL AND rotated_to_credential_id IS NOT NULL "
            "AND revoked_at = rotated_at)",
            name="ck_auth_credentials_rotation",
        ),
        CheckConstraint(
            "rotated_to_credential_id IS NULL OR rotated_to_credential_id != id",
            name="ck_auth_credentials_rotation_target",
        ),
        UniqueConstraint("selector", name="uq_auth_credentials_selector"),
        UniqueConstraint(
            "id",
            "caller_id",
            "library_id",
            name="uq_auth_credentials_id_caller_id_library_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    selector: Mapped[str] = mapped_column(String(SELECTOR_LENGTH), nullable=False)
    token_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verifier: Mapped[bytes] = mapped_column(LargeBinary(VERIFIER_BYTES), nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger)
    rotated_at: Mapped[int | None] = mapped_column(BigInteger)
    rotated_to_credential_id: Mapped[str | None] = mapped_column(String(OPAQUE_ID_LENGTH))


class SectionGrant(Base):
    __tablename__ = "auth_section_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_auth_section_grants_caller_library_callers",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_auth_section_grants_section_library_sections",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('section:query', 'page:read', 'archive:write')",
            name="ck_auth_section_grants_action",
        ),
        CheckConstraint("created_at >= 0", name="ck_auth_section_grants_created_at"),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    caller_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    section_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    action: Mapped[str] = mapped_column(String(ACTION_MAX_LENGTH), primary_key=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class AuditEvent(Base):
    __tablename__ = "auth_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["actor_credential_id", "actor_caller_id", "library_id"],
            [
                "auth_credentials.id",
                "auth_credentials.caller_id",
                "auth_credentials.library_id",
            ],
            name="fk_auth_audit_actor_credential_caller_library_credentials",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_auth_audit_target_caller_library_callers",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_auth_audit_section_library_sections",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_audit_events_id_lower_hex",
        ),
        CheckConstraint(
            "length(action) BETWEEN 1 AND 100 AND action = trim(action)",
            name="ck_auth_audit_events_action",
        ),
        CheckConstraint(
            "length(resource_type) BETWEEN 1 AND 100 AND resource_type = trim(resource_type)",
            name="ck_auth_audit_events_resource_type",
        ),
        CheckConstraint(
            "length(resource_id) BETWEEN 1 AND 200 AND resource_id = trim(resource_id)",
            name="ck_auth_audit_events_resource_id",
        ),
        CheckConstraint(
            "outcome IN ('succeeded', 'failed')",
            name="ck_auth_audit_events_outcome",
        ),
        CheckConstraint(
            "((action IN ('auth.grant.add', 'auth.grant.remove') "
            "AND target_caller_id IS NOT NULL AND section_id IS NOT NULL "
            "AND section_action IN ('section:query', 'page:read', 'archive:write')) "
            "OR (action NOT IN ('auth.grant.add', 'auth.grant.remove') "
            "AND target_caller_id IS NULL AND section_id IS NULL "
            "AND section_action IS NULL))",
            name="ck_auth_audit_events_grant_identity",
        ),
        CheckConstraint(
            "length(request_id) BETWEEN 1 AND 100 AND request_id = trim(request_id)",
            name="ck_auth_audit_events_request_id",
        ),
        CheckConstraint(
            "occurred_at >= 0 AND "
            "(policy_version_before IS NULL OR policy_version_before >= 1) AND "
            "(policy_version_after IS NULL OR policy_version_after >= 1)",
            name="ck_auth_audit_events_metadata",
        ),
        Index("ix_auth_audit_events_library_request_id", "library_id", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), primary_key=True)
    library_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    actor_caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    actor_credential_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    target_caller_id: Mapped[str | None] = mapped_column(String(OPAQUE_ID_LENGTH))
    section_id: Mapped[str | None] = mapped_column(String(OPAQUE_ID_LENGTH))
    section_action: Mapped[str | None] = mapped_column(String(ACTION_MAX_LENGTH))
    action: Mapped[str] = mapped_column(String(ACTION_MAX_LENGTH), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(ACTION_MAX_LENGTH), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(RESOURCE_ID_MAX_LENGTH), nullable=False)
    outcome: Mapped[str] = mapped_column(String(KIND_MAX_LENGTH), nullable=False)
    request_id: Mapped[str] = mapped_column(String(REQUEST_ID_MAX_LENGTH), nullable=False)
    policy_version_before: Mapped[int | None] = mapped_column(BigInteger)
    policy_version_after: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class BootstrapMarker(Base):
    __tablename__ = "operator_bootstrap_markers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_operator_bootstrap_library_id_libraries",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operator_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_operator_bootstrap_caller_library_callers",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initial_credential_id", "operator_caller_id", "library_id"],
            [
                "auth_credentials.id",
                "auth_credentials.caller_id",
                "auth_credentials.library_id",
            ],
            name="fk_operator_bootstrap_credential_caller_library_credentials",
            ondelete="RESTRICT",
        ),
        CheckConstraint("created_at >= 0", name="ck_operator_bootstrap_created_at"),
        UniqueConstraint(
            "initial_credential_id",
            name="uq_operator_bootstrap_initial_credential_id",
        ),
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    operator_caller_id: Mapped[str] = mapped_column(String(OPAQUE_ID_LENGTH), nullable=False)
    initial_credential_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        nullable=False,
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["AuditEvent", "BootstrapMarker", "Caller", "Credential", "SectionGrant"]
