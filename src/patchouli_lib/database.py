from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url


class DatabaseNotReadyError(RuntimeError):
    pass


def _ensure_sqlite_parent(database_url: str) -> None:
    database = make_url(database_url).database
    if database is None or database in {"", ":memory:"}:
        return
    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    return create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )


def check_database(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            fts5_enabled = connection.exec_driver_sql(
                "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
            ).scalar_one()
    except Exception as exc:
        raise DatabaseNotReadyError("Database connectivity check failed.") from exc

    if not fts5_enabled:
        raise DatabaseNotReadyError("SQLite was built without FTS5 support.")
