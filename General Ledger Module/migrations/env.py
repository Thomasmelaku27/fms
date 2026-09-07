"""Alembic environment.

The database URL is resolved in this order:

1. ``sqlalchemy.url`` set programmatically on the Alembic ``Config`` (used by the
   test suite to migrate throw-away databases);
2. the application settings (``FMS_DATABASE_URL`` env var or ``.env``), so the
   CLI always migrates the same database the app connects to.
"""

from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401  (registers tables on Base.metadata for autogenerate)
from app.config import get_settings
from app.db import Base, make_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(get_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode recreates tables instead.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
