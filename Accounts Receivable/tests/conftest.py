"""Shared fixtures.

Every test database is created by running the Alembic migrations (never
``create_all``), so the tests exercise exactly the schema, constraints and
triggers that production gets.

Tests that take ``db_url`` (directly or via ``engine`` / ``session`` / ``client``)
run once per backend:

* SQLite: a new file, with a name unique to this run, inside the test's own
  ``tmp_path``; always runs. Existing files are never opened or deleted (pytest
  owns the retention of its tmp directories).
* PostgreSQL: only when ``FMS_TEST_POSTGRES_URL`` is set (otherwise skipped).

Disposable resources are created, never reused, and only destroyed when they
can be proven to belong to this run:

* the scratch database is named ``fms_test_<run tag>`` (unique per process and
  run), so it cannot collide with an existing database; ``CREATE DATABASE``
  fails loudly rather than dropping something that is already there;
* immediately after creation it is stamped with the run tag in its own
  ``fms_test_owner`` schema (which the per-test ``public`` reset leaves alone);
* the per-test reset verifies the stamp inside the same transaction that
  empties ``public``; the final ``DROP DATABASE`` pins the database OID, reads
  the stamp from inside that database and re-checks the OID right before
  dropping. A database whose stamp is missing, unreadable or from another run
  is left in place (with a warning) instead.

A local PostgreSQL server for these variants can be provided with
``python -m tests.scratch_postgres -- python -m pytest`` (see that module), which
applies the same "verify the exact resource right before destroying it" rule
to the cluster's data directory.

The database named in ``FMS_TEST_POSTGRES_URL`` itself is only ever connected
to in order to issue ``CREATE DATABASE``/``DROP DATABASE``; nothing in it is
modified.
"""

import os
import re
import uuid
import warnings
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("FMS_TEST_POSTGRES_URL")

#: Identifies the resources created by this test run (and nothing else).
RUN_TAG = f"{os.getpid()}_{uuid.uuid4().hex[:12]}"
#: Schema holding the ownership stamp; survives the per-test reset of ``public``.
OWNER_SCHEMA = "fms_test_owner"
SAFE_DB_NAME = re.compile(r"fms_test_[a-z0-9_]{1,50}")


def alembic_config(url: str) -> Config:
    """Alembic config for the project, pointed at ``url`` instead of FMS_DATABASE_URL."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def sqlite_scratch_path(tmp_path: Path) -> Path:
    """A new SQLite file in the test's tmp dir; never an existing one."""
    path = tmp_path / f"fms_test_{RUN_TAG}.db"
    if path.exists():  # pragma: no cover - would mean the name was not unique
        raise RuntimeError(f"refusing to reuse an existing database file: {path}")
    return path


def scratch_database_name(tag: str = RUN_TAG) -> str:
    return f"fms_test_{tag}"


def _admin_engine(admin_url):
    return make_engine(str(admin_url)).execution_options(isolation_level="AUTOCOMMIT")


def _scratch_url(admin_url, name: str) -> str:
    return str(admin_url.set(database=name))


def stamp_scratch_database(url: str, tag: str) -> None:
    """Mark a freshly created database as belonging to ``tag``."""
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {OWNER_SCHEMA}"))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {OWNER_SCHEMA}.owner (tag text primary key)"))
        conn.execute(text(f"INSERT INTO {OWNER_SCHEMA}.owner (tag) VALUES (:tag)"), {"tag": tag})
    engine.dispose()


def _read_owner(conn, lock: bool = False) -> str | None:
    if conn.execute(text("SELECT to_regclass(:name)"), {"name": f"{OWNER_SCHEMA}.owner"}).scalar() is None:
        return None
    query = f"SELECT tag FROM {OWNER_SCHEMA}.owner" + (" FOR SHARE" if lock else "")
    return conn.execute(text(query)).scalars().first()


def scratch_database_owner(url: str) -> str | None:
    """The run tag stamped on a database.

    ``None`` whenever ownership cannot be established - no stamp, or the
    database cannot even be reached - which callers treat as "not ours".
    """
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            return _read_owner(conn)
    except OperationalError:
        return None
    finally:
        engine.dispose()


def _checked_name(name: str) -> str:
    if not SAFE_DB_NAME.fullmatch(name):
        raise ValueError(f"unexpected scratch database name: {name!r}")
    return name


def create_scratch_database(admin_url, name: str, tag: str = RUN_TAG) -> str:
    """Create ``name`` (failing if it exists) and stamp it; returns its URL."""
    name = _checked_name(name)
    admin = _admin_engine(admin_url)
    try:
        with admin.connect() as conn:
            # Deliberately no DROP: a clash means the name belongs to someone else.
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    url = _scratch_url(admin_url, name)
    stamp_scratch_database(url, tag)
    return url


def _database_oid(conn, name: str) -> int | None:
    return conn.execute(text("SELECT oid FROM pg_database WHERE datname = :n"), {"n": name}).scalar()


def drop_scratch_database_if_owned(admin_url, name: str, tag: str = RUN_TAG) -> bool:
    """Drop ``name`` only if it is provably the database stamped with ``tag``.

    The OID of the database is pinned before the stamp is read (from inside
    that very database) and re-checked on the admin connection right before
    ``DROP DATABASE``, so a database renamed or recreated in between is never
    the one dropped. Returns whether it was dropped.
    """
    name = _checked_name(name)
    admin = _admin_engine(admin_url)
    try:
        with admin.connect() as conn:
            oid = _database_oid(conn, name)
            if oid is None:
                return False
            scratch = make_engine(_scratch_url(admin_url, name))
            try:
                with scratch.connect() as inner:
                    same_db = inner.execute(
                        text("SELECT oid FROM pg_database WHERE datname = current_database()")
                    ).scalar() == oid
                    owner = _read_owner(inner)
            except OperationalError:
                return False
            finally:
                scratch.dispose()
            if not same_db or owner != tag or _database_oid(conn, name) != oid:
                return False
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            return True
    finally:
        admin.dispose()


def reset_public_schema_if_owned(url: str, tag: str = RUN_TAG) -> bool:
    """Empty the ``public`` schema, verifying ownership in the same transaction first."""
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            if _read_owner(conn, lock=True) != tag:
                return False  # transaction ends without touching anything
            conn.execute(text("DROP SCHEMA public CASCADE"))  # the owner stamp lives elsewhere
            conn.execute(text("CREATE SCHEMA public"))
            return True
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def postgres_scratch_url() -> str | None:
    """Create a throw-away PostgreSQL database for the session (None if unavailable)."""
    if not POSTGRES_URL:
        yield None
        return
    admin_url = make_url(POSTGRES_URL)
    scratch_db = scratch_database_name()
    yield create_scratch_database(admin_url, scratch_db)
    if not drop_scratch_database_if_owned(admin_url, scratch_db):
        warnings.warn(
            f'leaving PostgreSQL database "{scratch_db}" in place: it is not stamped as created '
            f"by this run ({RUN_TAG})",
            stacklevel=1,
        )


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request, tmp_path, postgres_scratch_url) -> str:
    """URL of an empty database for the requested backend (not migrated yet)."""
    if request.param == "sqlite":
        return f"sqlite:///{sqlite_scratch_path(tmp_path)}"
    if postgres_scratch_url is None:
        pytest.skip("set FMS_TEST_POSTGRES_URL to run the PostgreSQL variant")
    if not reset_public_schema_if_owned(postgres_scratch_url):
        pytest.fail(f"refusing to reset {postgres_scratch_url}: it was not created by this run")
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
