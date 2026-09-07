"""Shared fixtures.

Every test database is created by running the Alembic migrations (never
``create_all``), so the tests exercise exactly the schema, constraints and
triggers that production gets.

Tests that take ``db_url`` (directly or via ``engine`` / ``session`` / ``client``)
run once per backend:

* SQLite: a fresh file in the test's tmp dir; always runs.
* PostgreSQL: only when ``FMS_TEST_POSTGRES_URL`` is set (otherwise skipped).
  Each test gets its own freshly created database with a unique name
  (``fms_test_<hex>``) on that server, which is dropped again when the test
  ends. The database named in the URL is only used to run ``CREATE DATABASE``
  / ``DROP DATABASE``; its contents are never touched or reset. The role
  therefore needs the CREATEDB privilege.
"""

import os
import secrets
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("FMS_TEST_POSTGRES_URL")
TEST_DB_PREFIX = "fms_test_"


def alembic_config(url: str) -> Config:
    """Alembic config for the project, pointed at ``url`` instead of FMS_DATABASE_URL."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


class PostgresDatabases:
    """Creates and drops uniquely named databases on the configured server."""

    def __init__(self, admin_url: str) -> None:
        self.admin_url = make_url(admin_url)
        self.engine = make_engine(admin_url).execution_options(isolation_level="AUTOCOMMIT")

    def create(self) -> str:
        name = f"{TEST_DB_PREFIX}{secrets.token_hex(4)}"
        with self.engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        return str(self.admin_url.set(database=name))

    def drop(self, url: str) -> None:
        name = make_url(url).database
        assert name.startswith(TEST_DB_PREFIX), name  # never drop anything we did not create
        with self.engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    def dispose(self) -> None:
        self.engine.dispose()


@pytest.fixture(scope="session")
def postgres_databases() -> PostgresDatabases | None:
    if not POSTGRES_URL:
        yield None
        return
    databases = PostgresDatabases(POSTGRES_URL)
    yield databases
    databases.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path, postgres_databases) -> str:
    """URL of an empty, private database for the requested backend (not migrated yet)."""
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'test.db'}"
        return
    if postgres_databases is None:
        pytest.skip("set FMS_TEST_POSTGRES_URL to run the PostgreSQL variant")
    url = postgres_databases.create()
    yield url
    postgres_databases.drop(url)


@pytest.fixture
def migrated_db_url(db_url) -> str:
    migrate(db_url)
    return db_url


@pytest.fixture
def engine(migrated_db_url):
    engine = make_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def client(migrated_db_url):
    app = create_app(Settings(database_url=migrated_db_url))
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()
