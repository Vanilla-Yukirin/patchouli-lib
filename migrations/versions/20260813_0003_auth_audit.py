"""Create authentication, authorization, bootstrap, and audit storage.

Revision ID: 20260813_0003
Revises: 20260812_0002
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0003"
down_revision: str | None = "20260812_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_callers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("disabled_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_callers_id_lower_hex",
        ),
        sa.CheckConstraint(
            "kind IN ('operator', 'agent')",
            name="ck_auth_callers_kind",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200 AND name = trim(name)",
            name="ck_auth_callers_name",
        ),
        sa.CheckConstraint(
            "length(description) <= 4000",
            name="ck_auth_callers_description",
        ),
        sa.CheckConstraint(
            "policy_version >= 1",
            name="ck_auth_callers_policy_version",
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at "
            "AND (disabled_at IS NULL OR "
            "(disabled_at >= created_at AND disabled_at <= updated_at))",
            name="ck_auth_callers_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_auth_callers_library_id_libraries",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_callers"),
        sa.UniqueConstraint("id", "library_id", name="uq_auth_callers_id_library_id"),
        sa.UniqueConstraint(
            "library_id",
            "name",
            name="uq_auth_callers_library_id_name",
        ),
    )

    op.create_table(
        "auth_credentials",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.String(length=32), nullable=False),
        sa.Column("selector", sa.String(length=22), nullable=False),
        sa.Column("token_version", sa.BigInteger(), nullable=False),
        sa.Column("verifier", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("last_used_at", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.Column("rotated_at", sa.BigInteger(), nullable=True),
        sa.Column("rotated_to_credential_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_credentials_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(selector) = 22 AND selector NOT GLOB '*[^A-Za-z0-9_-]*'",
            name="ck_auth_credentials_selector",
        ),
        sa.CheckConstraint(
            "token_version = 1",
            name="ck_auth_credentials_token_version",
        ),
        sa.CheckConstraint(
            "length(verifier) = 32",
            name="ck_auth_credentials_verifier_length",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "(rotated_at IS NULL AND rotated_to_credential_id IS NULL) OR "
            "(rotated_at IS NOT NULL AND rotated_to_credential_id IS NOT NULL "
            "AND revoked_at = rotated_at)",
            name="ck_auth_credentials_rotation",
        ),
        sa.CheckConstraint(
            "rotated_to_credential_id IS NULL OR rotated_to_credential_id != id",
            name="ck_auth_credentials_rotation_target",
        ),
        sa.ForeignKeyConstraint(
            ["caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_auth_credentials_caller_library_callers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rotated_to_credential_id", "caller_id", "library_id"],
            [
                "auth_credentials.id",
                "auth_credentials.caller_id",
                "auth_credentials.library_id",
            ],
            name="fk_auth_credentials_rotated_to_caller_library_credentials",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_credentials"),
        sa.UniqueConstraint("selector", name="uq_auth_credentials_selector"),
        sa.UniqueConstraint(
            "id",
            "caller_id",
            "library_id",
            name="uq_auth_credentials_id_caller_id_library_id",
        ),
    )

    op.create_table(
        "auth_section_grants",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.String(length=32), nullable=False),
        sa.Column("section_id", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "action IN ('section:query', 'page:read', 'archive:write')",
            name="ck_auth_section_grants_action",
        ),
        sa.CheckConstraint(
            "created_at >= 0",
            name="ck_auth_section_grants_created_at",
        ),
        sa.ForeignKeyConstraint(
            ["caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_auth_section_grants_caller_library_callers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_auth_section_grants_section_library_sections",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "library_id",
            "caller_id",
            "section_id",
            "action",
            name="pk_auth_section_grants",
        ),
    )

    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("actor_caller_id", sa.String(length=32), nullable=False),
        sa.Column("actor_credential_id", sa.String(length=32), nullable=False),
        sa.Column("target_caller_id", sa.String(length=32), nullable=True),
        sa.Column("section_id", sa.String(length=32), nullable=True),
        sa.Column("section_action", sa.String(length=100), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("policy_version_before", sa.BigInteger(), nullable=True),
        sa.Column("policy_version_after", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_audit_events_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(action) BETWEEN 1 AND 100 AND action = trim(action)",
            name="ck_auth_audit_events_action",
        ),
        sa.CheckConstraint(
            "length(resource_type) BETWEEN 1 AND 100 AND resource_type = trim(resource_type)",
            name="ck_auth_audit_events_resource_type",
        ),
        sa.CheckConstraint(
            "length(resource_id) BETWEEN 1 AND 200 AND resource_id = trim(resource_id)",
            name="ck_auth_audit_events_resource_id",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed')",
            name="ck_auth_audit_events_outcome",
        ),
        sa.CheckConstraint(
            "((action IN ('auth.grant.add', 'auth.grant.remove') "
            "AND target_caller_id IS NOT NULL AND section_id IS NOT NULL "
            "AND section_action IN ('section:query', 'page:read', 'archive:write')) "
            "OR (action NOT IN ('auth.grant.add', 'auth.grant.remove') "
            "AND target_caller_id IS NULL AND section_id IS NULL "
            "AND section_action IS NULL))",
            name="ck_auth_audit_events_grant_identity",
        ),
        sa.CheckConstraint(
            "length(request_id) BETWEEN 1 AND 100 AND request_id = trim(request_id)",
            name="ck_auth_audit_events_request_id",
        ),
        sa.CheckConstraint(
            "occurred_at >= 0 AND "
            "(policy_version_before IS NULL OR policy_version_before >= 1) AND "
            "(policy_version_after IS NULL OR policy_version_after >= 1)",
            name="ck_auth_audit_events_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["actor_credential_id", "actor_caller_id", "library_id"],
            [
                "auth_credentials.id",
                "auth_credentials.caller_id",
                "auth_credentials.library_id",
            ],
            name="fk_auth_audit_actor_credential_caller_library_credentials",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_auth_audit_target_caller_library_callers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_auth_audit_section_library_sections",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_audit_events"),
    )
    op.create_index(
        "ix_auth_audit_events_library_request_id",
        "auth_audit_events",
        ["library_id", "request_id"],
        unique=False,
    )

    op.create_table(
        "operator_bootstrap_markers",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("operator_caller_id", sa.String(length=32), nullable=False),
        sa.Column("initial_credential_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "created_at >= 0",
            name="ck_operator_bootstrap_created_at",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_operator_bootstrap_library_id_libraries",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_operator_bootstrap_caller_library_callers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initial_credential_id", "operator_caller_id", "library_id"],
            [
                "auth_credentials.id",
                "auth_credentials.caller_id",
                "auth_credentials.library_id",
            ],
            name="fk_operator_bootstrap_credential_caller_library_credentials",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("library_id", name="pk_operator_bootstrap_markers"),
        sa.UniqueConstraint(
            "initial_credential_id",
            name="uq_operator_bootstrap_initial_credential_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("operator_bootstrap_markers")
    op.drop_index(
        "ix_auth_audit_events_library_request_id",
        table_name="auth_audit_events",
    )
    op.drop_table("auth_audit_events")
    op.drop_table("auth_section_grants")
    op.execute(
        sa.text(
            "UPDATE auth_credentials "
            "SET rotated_at = NULL, rotated_to_credential_id = NULL "
            "WHERE rotated_to_credential_id IS NOT NULL"
        )
    )
    op.drop_table("auth_credentials")
    op.drop_table("auth_callers")
