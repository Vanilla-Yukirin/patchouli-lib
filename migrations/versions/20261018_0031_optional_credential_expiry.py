"""Allow Agent credentials without an expiry while preserving existing data.

Revision ID: 20261018_0031
Revises: 20261003_0030
Create Date: 2026-10-18
"""

import re
import sqlite3
from collections.abc import Sequence

from alembic import op

revision: str = "20261018_0031"
down_revision: str | None = "20261003_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_rebuild() -> None:
    # As in 0013, commit any completed migration prefix before SQLite's
    # FK-off rebuild. This disposable connection is closed by env.py; normal
    # application connections continue to enable foreign-key enforcement.
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Credential expiry migration requires SQLite.")
    if driver.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("Cannot disable foreign keys for credential rebuild.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    _check_foreign_keys()


def _check_foreign_keys() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Credential relationships are inconsistent.")


def _replace_once(sql: str, old: str, new: str) -> str:
    if sql.count(old) != 1:
        raise RuntimeError("Unexpected credential schema prevents a safe rebuild.")
    return sql.replace(old, new, 1)


def _rebuild(*, optional: bool) -> None:
    connection = op.get_bind()
    _begin_rebuild()
    if (
        not optional
        and connection.exec_driver_sql(
            "SELECT 1 FROM auth_credentials WHERE expires_at IS NULL LIMIT 1"
        ).first()
    ):
        raise RuntimeError("Cannot downgrade while credentials without expiry exist.")

    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'auth_credentials'"
    ).scalar_one()
    changes = (
        ("expires_at BIGINT NOT NULL", "expires_at BIGINT"),
        ("expires_at > created_at", "(expires_at IS NULL OR expires_at > created_at)"),
        ("last_used_at < expires_at", "(expires_at IS NULL OR last_used_at < expires_at)"),
    )
    for before, after in changes:
        sql = _replace_once(sql, before if optional else after, after if optional else before)

    # DROP and recreate at the original name preserves exact historical schema
    # spelling on downgrade and leaves composite/self foreign-key targets intact.
    # Related triggers must be absent during the temporary missing-table window.
    triggers = [
        (name, definition)
        for name, table, definition in connection.exec_driver_sql(
            "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type = 'trigger'"
        )
        if table == "auth_credentials"
        or (definition and re.search(r"\bauth_credentials\b", definition, re.IGNORECASE))
    ]
    indexes = list(
        connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' "
            "AND tbl_name = 'auth_credentials' AND sql IS NOT NULL"
        ).scalars()
    )
    quote = connection.dialect.identifier_preparer.quote
    for name, _definition in triggers:
        connection.exec_driver_sql(f"DROP TRIGGER {quote(name)}")
    connection.exec_driver_sql(
        "CREATE TEMP TABLE credential_expiry_backup AS SELECT * FROM auth_credentials"
    )
    connection.exec_driver_sql("DROP TABLE auth_credentials")
    connection.exec_driver_sql(sql)
    connection.exec_driver_sql(
        "INSERT INTO auth_credentials SELECT * FROM credential_expiry_backup"
    )
    for definition in indexes:
        connection.exec_driver_sql(definition)
    for _name, definition in triggers:
        connection.exec_driver_sql(definition)
    connection.exec_driver_sql("DROP TABLE credential_expiry_backup")
    _check_foreign_keys()


def upgrade() -> None:
    _rebuild(optional=True)


def downgrade() -> None:
    _rebuild(optional=False)
