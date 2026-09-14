"""Shared fixtures.

Every test database is created by running the Alembic migrations (never
``create_all``), so the tests exercise exactly the schema, constraints and
triggers that production gets.

Tests that take ``db_url`` (directly or via ``engine`` / ``session`` / ``client``)
run once per backend:

* SQLite: a fresh file in the test's tmp dir; always runs.
* PostgreSQL: only when ``FMS_TEST_POSTGRES_URL`` is set (otherwise skipped).
  A uniquely named, ownership-tagged scratch database is created on that server
  for the test session and its ``public`` schema is reset before each test; the
  database in the URL itself is never modified and no other database is ever
  dropped (``tests/payables_support/dbscratch.py``).
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("FMS_TEST_POSTGRES_URL")


def alembic_config(url: str) -> Config:
    """Alembic config for the project, pointed at ``url`` instead of FMS_DATABASE_URL."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


@pytest.fixture(scope="session")
def postgres_scratch_db():
    """A uniquely named, owned PostgreSQL database for the session (None if unavailable).

    Created only if the generated name is free, tagged with an ownership token, and
    dropped at the end only after the token is re-verified -- never a predictable name
    with ``DROP DATABASE IF EXISTS``. See ``tests/payables_support/dbscratch.py``.
    """
    if not POSTGRES_URL:
        yield None
        return
    from tests.payables_support import dbscratch

    with dbscratch.scratch_database(POSTGRES_URL, label="session") as db:
        yield db


@pytest.fixture(scope="session")
def postgres_scratch_url(postgres_scratch_db) -> str | None:
    return postgres_scratch_db.url if postgres_scratch_db else None


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path, postgres_scratch_url) -> str:
    """URL of an empty database for the requested backend (not migrated yet)."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'test.db'}"
    if postgres_scratch_url is None:
        pytest.skip("set FMS_TEST_POSTGRES_URL to run the PostgreSQL variant")
    from tests.payables_support import dbscratch

    dbscratch.reset_schema(request.getfixturevalue("postgres_scratch_db"))
    return postgres_scratch_url


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
