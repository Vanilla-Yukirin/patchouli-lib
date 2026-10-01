from logging.config import fileConfig

from alembic import context

from patchouli_lib.admin import file_set_receipts as master_file_set_models
from patchouli_lib.auth import models as auth_models
from patchouli_lib.config import Settings
from patchouli_lib.content import models as content_models
from patchouli_lib.database import build_engine
from patchouli_lib.idempotency import models as idempotency_models
from patchouli_lib.library import models as library_models
from patchouli_lib.models import Base
from patchouli_lib.request_log import models as request_log_models
from patchouli_lib.tags import models as tag_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
if library_models.Library.metadata is not Base.metadata:
    raise RuntimeError("Library models must use the shared SQLAlchemy metadata.")
if auth_models.Caller.metadata is not Base.metadata:
    raise RuntimeError("Authentication models must use the shared SQLAlchemy metadata.")
if content_models.Page.metadata is not Base.metadata:
    raise RuntimeError("Content models must use the shared SQLAlchemy metadata.")
if idempotency_models.IdempotencyRecord.metadata is not Base.metadata:
    raise RuntimeError("Idempotency models must use the shared SQLAlchemy metadata.")
if tag_models.Tag.metadata is not Base.metadata:
    raise RuntimeError("Tag models must use the shared SQLAlchemy metadata.")
if request_log_models.RequestLogRecord.metadata is not Base.metadata:
    raise RuntimeError("Request log models must use the shared SQLAlchemy metadata.")
if master_file_set_models.MasterFileSetReceiptRow.metadata is not Base.metadata:
    raise RuntimeError("Master file-set models must use the shared SQLAlchemy metadata.")
target_metadata = Base.metadata


def _include_object(
    _object: object, name: str | None, type_: str, reflected: bool, _compare_to: object
) -> bool:
    # Search-v2 is a raw-SQL/FTS5 projection owned by its exact migration.
    # Alembic autogenerate cannot represent the virtual table or its shadows.
    return not (
        reflected and type_ == "table" and isinstance(name, str) and name.startswith("search_")
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = build_engine(settings.database_url)

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                include_object=_include_object,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
