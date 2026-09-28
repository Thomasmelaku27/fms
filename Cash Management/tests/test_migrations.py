"""Alembic setup: migrations build the schema, the app no longer does."""

import os
import sqlite3
import subprocess
import sys

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app import models  # noqa: F401  (register tables on Base.metadata)
from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app
from tests.conftest import PROJECT_ROOT, alembic_config, migrate

EXPECTED_TABLES = {"accounts", "journal_entries", "journal_lines"}


def test_upgrade_head_creates_schema_matching_models(db_url):
    migrate(db_url)

    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert EXPECTED_TABLES <= set(inspect(conn).get_table_names())

        # The models and the migrations must describe the same schema, otherwise
        # `alembic revision --autogenerate` would produce spurious changes.
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []

        head = ScriptDirectory.from_config(alembic_config(db_url)).get_current_head()
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
    engine.dispose()


def test_downgrade_base_removes_schema_and_upgrade_again_works(db_url):
    migrate(db_url)
    command.downgrade(alembic_config(db_url), "base")

    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert not EXPECTED_TABLES & set(inspect(conn).get_table_names())
        if engine.dialect.name == "postgresql":
            leftover = (
                conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'fms\\_%'"))
                .scalars()
                .all()
            )
            assert leftover == [], "trigger functions must be dropped on downgrade"
    engine.dispose()

    migrate(db_url)  # a full cycle leaves a usable schema behind


def test_create_app_does_not_create_tables(tmp_path):
    db_path = tmp_path / "app.db"
    app = create_app(Settings(database_url=f"sqlite:///{db_path}"))
    app.state.engine.dispose()

    assert inspect(make_engine(f"sqlite:///{db_path}")).get_table_names() == []


def test_alembic_cli_reads_url_from_fms_database_url(tmp_path):
    """`alembic upgrade head` targets the database configured through app settings."""
    db_path = tmp_path / "cli.db"
    env = {**os.environ, "FMS_DATABASE_URL": f"sqlite:///{db_path}"}

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    tables = {
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert EXPECTED_TABLES <= tables
