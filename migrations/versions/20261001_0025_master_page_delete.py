"""Permit audited master-session Page soft deletion without rewriting history.

Revision ID: 20261001_0025
Revises: 20260930_0024
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_0025"
down_revision: str | None = "20260930_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGER = "trg_page_lifecycle_guards_master_audit"
_OLD_CHECK = "NEW.action IS NOT 'restore'"
_OLD_ACTION = "audit.action = 'content.archive.restore'"
_NEW_CHECK = "NEW.action NOT IN ('delete', 'restore')"
_NEW_ACTION = "audit.action = 'content.archive.' || NEW.action"


def _replace_trigger(*, upgrading: bool) -> None:
    connection = op.get_bind()
    # SQLite DDL needs an explicit transaction. Check before dropping anything
    # and retain the old trigger if any subsequent step fails.
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    expected_revision = down_revision if upgrading else revision
    if connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() != (
        expected_revision
    ):
        raise RuntimeError("Unexpected schema revision for master Page deletion.")
    if connection.exec_driver_sql("SELECT 1 FROM page_lifecycle_guards LIMIT 1").first():
        raise RuntimeError("Cannot change Page lifecycle with a pending guard.")
    if not upgrading and (
        connection.exec_driver_sql(
            "SELECT 1 FROM page_lifecycle_events "
            "WHERE master_audit_event_id IS NOT NULL AND action = 'delete' LIMIT 1"
        ).first()
        or connection.exec_driver_sql(
            "SELECT 1 FROM admin_master_audit_events "
            "WHERE action = 'content.archive.delete' LIMIT 1"
        ).first()
    ):
        raise RuntimeError("Cannot downgrade master-attributed Page deletion history.")
    source = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (_TRIGGER,)
    ).scalar_one()
    old_check, new_check = (_OLD_CHECK, _NEW_CHECK) if upgrading else (_NEW_CHECK, _OLD_CHECK)
    old_action, new_action = (_OLD_ACTION, _NEW_ACTION) if upgrading else (_NEW_ACTION, _OLD_ACTION)
    if not isinstance(source, str) or source.count(old_check) != 1 or source.count(old_action) != 1:
        raise RuntimeError("Unexpected master Page lifecycle trigger.")
    replacement = source.replace(old_check, new_check, 1).replace(old_action, new_action, 1)
    connection.exec_driver_sql(f"DROP TRIGGER {_TRIGGER}")
    connection.exec_driver_sql(replacement)


def upgrade() -> None:
    _replace_trigger(upgrading=True)


def downgrade() -> None:
    _replace_trigger(upgrading=False)
