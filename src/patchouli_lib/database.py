import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url

SQLITE_BUSY_TIMEOUT_MS = 5_000


class DatabaseNotReadyError(RuntimeError):
    pass


def _ensure_sqlite_parent(database_url: str) -> None:
    database = make_url(database_url).database
    if database is None or database in {"", ":memory:"}:
        return
    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite_connection(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def build_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    engine = create_engine(
        database_url,
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1_000,
        },
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


@contextmanager
def immediate_transaction(engine: Engine) -> Iterator[Connection]:
    """Yield a connection inside a short SQLite ``BEGIN IMMEDIATE`` transaction."""
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def check_database(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            fts5_enabled = connection.exec_driver_sql(
                "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
            ).scalar_one()
            foreign_keys_enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    except Exception as exc:
        raise DatabaseNotReadyError("Database connectivity check failed.") from exc

    if not fts5_enabled:
        raise DatabaseNotReadyError("SQLite was built without FTS5 support.")
    if foreign_keys_enabled != 1:
        raise DatabaseNotReadyError("SQLite foreign-key enforcement is disabled.")
