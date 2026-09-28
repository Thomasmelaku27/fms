"""Shared fixtures.

Every test database is created by running the Alembic migrations (never
``create_all``), so the tests exercise exactly the schema, constraints and
triggers that production gets.

Tests that take ``db_url`` (directly or via ``engine`` / ``session`` / ``client``)
run once per backend:

* SQLite: a fresh file in the test's own pytest tmp dir; always runs.
* PostgreSQL: only when ``FMS_TEST_POSTGRES_URL`` is set (otherwise skipped).

PostgreSQL safety rules (see ``postgres_scratch_url`` / ``verify_scratch_database``):

* the scratch database gets a name unique to this run (pid + random token);
  if a database of that name already exists the run refuses to continue -
  it is never dropped or reused;
* creation is verified, and the run writes a marker (schema ``fms_test_run``,
  table ``marker`` holding the run token) inside the new database;
* every destructive step (resetting ``public`` before a test, dropping the
  database at the end) first re-reads that marker and the database name and
  aborts unless both prove the target was created by this very run;
* the database named in the URL is only used to issue ``CREATE DATABASE`` /
  ``DROP DATABASE``; nothing in it is ever read or modified.
"""

import os
import re
import secrets
import warnings
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("FMS_TEST_POSTGRES_URL")

# Identity of this test run; baked into the scratch database name and its marker.
RUN_TOKEN = f"{os.getpid()}-{secrets.token_hex(8)}"
MARKER_SCHEMA = "fms_test_run"


class ScratchDatabaseError(RuntimeError):
    """Raised instead of touching a database that this run cannot prove it created."""


def alembic_config(url: str) -> Config:
    """Alembic config for the project, pointed at ``url`` instead of FMS_DATABASE_URL."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def scratch_database_name(base: str) -> str:
    """``<base>_test_<pid>_<random>``: unique per run, never equal to the base name."""
    base = re.sub(r"[^a-z0-9_]", "_", (base or "fms").lower())[:24]
    return f"{base}_test_{os.getpid()}_{secrets.token_hex(4)}"


def verify_scratch_database(conn, expected_name: str, token: str = RUN_TOKEN) -> None:
    """Prove ``conn`` is on the scratch database created by this run, else raise.

    Read-only: checks the current database name and the marker token written
    right after ``CREATE DATABASE``. Any doubt (missing marker, other token,
    other name, unreadable) is an error - the caller must then leave the
    database alone.
    """
    try:
        name = conn.execute(text("SELECT current_database()")).scalar()
        if name != expected_name:
            raise ScratchDatabaseError(f"connected to {name!r}, expected scratch database {expected_name!r}")
        tokens = conn.execute(text(f"SELECT token FROM {MARKER_SCHEMA}.marker")).scalars().all()
    except SQLAlchemyError as exc:
        raise ScratchDatabaseError(f"cannot read the run marker in {expected_name!r}: {exc}") from exc
    if tokens != [token]:
        raise ScratchDatabaseError(
            f"database {expected_name!r} carries marker {tokens!r}, not this run's token; refusing to touch it"
        )


def _database_exists(conn, name: str) -> bool:
    return conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar() is not None


@pytest.fixture(scope="session")
def postgres_scratch_url() -> str | None:
    """Create a throw-away PostgreSQL database for this run (None if unavailable).

    Never drops anything it did not create: an existing database with the
    generated name aborts the run, and the final DROP only happens after the
    marker check proves ownership. On any doubt the database is left in place
    with a warning naming it.
    """
    if not POSTGRES_URL:
        yield None
        return
    admin_url = make_url(POSTGRES_URL)
    scratch_db = scratch_database_name(admin_url.database)
    admin = make_engine(str(admin_url)).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if _database_exists(conn, scratch_db):
            admin.dispose()
            raise ScratchDatabaseError(f"database {scratch_db!r} already exists; refusing to reuse or drop it")
        conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        if not _database_exists(conn, scratch_db):
            admin.dispose()
            raise ScratchDatabaseError(f"CREATE DATABASE {scratch_db!r} did not take effect")

    scratch_url = admin_url.set(database=scratch_db)
    engine = make_engine(str(scratch_url))
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {MARKER_SCHEMA}"))
        conn.execute(text(f"CREATE TABLE {MARKER_SCHEMA}.marker (token text NOT NULL)"))
        conn.execute(text(f"INSERT INTO {MARKER_SCHEMA}.marker (token) VALUES (:t)"), {"t": RUN_TOKEN})
    with engine.connect() as conn:
        verify_scratch_database(conn, scratch_db)
    engine.dispose()

    yield str(scratch_url)

    engine = make_engine(str(scratch_url))
    try:
        with engine.connect() as conn:
            verify_scratch_database(conn, scratch_db)
        owned = True
    except ScratchDatabaseError as exc:
        owned = False
        warnings.warn(f"leaving PostgreSQL database {scratch_db!r} in place: {exc}", stacklevel=1)
    finally:
        engine.dispose()
    if owned:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{scratch_db}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path, postgres_scratch_url) -> str:
    """URL of an empty database for the requested backend (not migrated yet)."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'test.db'}"
    if postgres_scratch_url is None:
        pytest.skip("set FMS_TEST_POSTGRES_URL to run the PostgreSQL variant")
    engine = make_engine(postgres_scratch_url)
    with engine.begin() as conn:
        # Reset only after proving this is our scratch database; the marker lives
        # in its own schema and survives the reset of ``public``.
        verify_scratch_database(conn, make_url(postgres_scratch_url).database)
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
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
