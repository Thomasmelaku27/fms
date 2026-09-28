"""The PostgreSQL scratch database is only ever reset or dropped when it is provably ours."""

import os
import re

import pytest
from sqlalchemy import make_url, text

from app.db import make_engine
from tests.conftest import (
    MARKER_SCHEMA,
    RUN_TOKEN,
    ScratchDatabaseError,
    scratch_database_name,
    verify_scratch_database,
)


def test_scratch_name_is_unique_per_run_and_never_the_base_name():
    a = scratch_database_name("fms")
    b = scratch_database_name("fms")
    assert a != b
    assert a != "fms" and not a.endswith("_test")
    assert re.fullmatch(rf"fms_test_{os.getpid()}_[0-9a-f]{{8}}", a)
    assert len(scratch_database_name("x" * 100)) <= 63  # PostgreSQL identifier limit
    assert scratch_database_name("My-DB.name") .startswith("my_db_name_test_")


@pytest.fixture
def scratch(postgres_scratch_url):
    if postgres_scratch_url is None:
        pytest.skip("set FMS_TEST_POSTGRES_URL to run the PostgreSQL variant")
    engine = make_engine(postgres_scratch_url)
    yield engine, make_url(postgres_scratch_url).database
    engine.dispose()


def test_scratch_database_carries_this_runs_marker(scratch):
    engine, name = scratch
    with engine.connect() as conn:
        verify_scratch_database(conn, name)  # does not raise
        assert conn.execute(text(f"SELECT token FROM {MARKER_SCHEMA}.marker")).scalar() == RUN_TOKEN
        assert conn.execute(text("SELECT current_database()")).scalar() == name


def test_verification_refuses_other_names_tokens_and_missing_markers(scratch, db_url):
    engine, name = scratch
    with engine.connect() as conn:
        with pytest.raises(ScratchDatabaseError, match="expected scratch database"):
            verify_scratch_database(conn, "some_other_database")
        with pytest.raises(ScratchDatabaseError, match="not this run's token"):
            verify_scratch_database(conn, name, token="not-our-token")
    # Hide the marker inside a transaction that is rolled back: verification must fail closed.
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text(f"ALTER TABLE {MARKER_SCHEMA}.marker RENAME TO hidden"))
        with pytest.raises(ScratchDatabaseError, match="cannot read the run marker"):
            verify_scratch_database(conn, name)
        trans.rollback()
    with engine.connect() as conn:
        verify_scratch_database(conn, name)  # marker intact again


def test_public_schema_reset_keeps_the_marker(db_url):
    """``db_url`` resets ``public`` before each PostgreSQL test; the marker must survive it."""
    if not db_url.startswith("postgresql"):
        pytest.skip("PostgreSQL only")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        verify_scratch_database(conn, make_url(db_url).database)
        assert conn.execute(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalar() == 0
    engine.dispose()
