"""The suite's disposable databases: unique per run, and only dropped when proven ours.

Nothing here touches the database named in ``FMS_TEST_POSTGRES_URL`` itself;
the PostgreSQL cases create their own scratch databases (and drop them again,
after checking the ownership stamp they wrote).
"""

import pytest
from sqlalchemy import make_url, text

from app.db import make_engine
from tests.conftest import (
    OWNER_SCHEMA,
    POSTGRES_URL,
    RUN_TAG,
    create_scratch_database,
    drop_scratch_database_if_owned,
    reset_public_schema_if_owned,
    scratch_database_name,
    scratch_database_owner,
    sqlite_scratch_path,
)

requires_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="set FMS_TEST_POSTGRES_URL")


# --- SQLite ---------------------------------------------------------------------------


def test_sqlite_scratch_path_is_new_and_inside_the_test_tmp_dir(tmp_path):
    path = sqlite_scratch_path(tmp_path)
    assert path.parent == tmp_path
    assert RUN_TAG in path.name  # traceable to this run
    assert not path.exists()


def test_sqlite_scratch_path_refuses_to_reuse_an_existing_file(tmp_path):
    path = sqlite_scratch_path(tmp_path)
    path.write_bytes(b"pretend this is somebody's data")
    with pytest.raises(RuntimeError, match="refusing to reuse"):
        sqlite_scratch_path(tmp_path)
    assert path.read_bytes() == b"pretend this is somebody's data"  # never deleted


def test_sqlite_db_url_fixture_creates_its_own_file(db_url, tmp_path):
    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite-specific")
    assert str(tmp_path) in db_url


# --- PostgreSQL ------------------------------------------------------------------------


def test_scratch_database_names_are_unique_per_run():
    assert scratch_database_name() == f"fms_test_{RUN_TAG}"
    assert scratch_database_name("other") != scratch_database_name()


@requires_postgres
def test_session_scratch_database_is_stamped_with_this_run(postgres_scratch_url):
    assert scratch_database_owner(postgres_scratch_url) == RUN_TAG
    assert scratch_database_name() in postgres_scratch_url


@requires_postgres
def test_owner_stamp_survives_the_per_test_reset(db_url):
    if db_url.startswith("sqlite"):
        pytest.skip("PostgreSQL-specific")
    # db_url has just dropped and recreated the public schema for this test
    assert scratch_database_owner(db_url) == RUN_TAG


@requires_postgres
def test_a_database_from_another_run_is_not_dropped():
    admin_url = make_url(POSTGRES_URL)
    foreign = scratch_database_name("foreignrun")
    create_scratch_database(admin_url, foreign, tag="foreignrun")
    try:
        assert drop_scratch_database_if_owned(admin_url, foreign) is False  # not ours: left alone
        assert scratch_database_owner(str(admin_url.set(database=foreign))) == "foreignrun"
    finally:
        # we created it in this test and can prove it, so we may remove it
        assert drop_scratch_database_if_owned(admin_url, foreign, tag="foreignrun") is True
    assert scratch_database_owner(str(admin_url.set(database=foreign))) is None


@requires_postgres
def test_an_unstamped_database_is_not_dropped():
    admin_url = make_url(POSTGRES_URL)
    name = scratch_database_name("unstamped")
    admin = make_engine(str(admin_url)).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))  # created here without a stamp
    try:
        assert drop_scratch_database_if_owned(admin_url, name) is False
        with make_engine(str(admin_url.set(database=name))).connect() as conn:
            assert conn.execute(text("SELECT to_regclass(:n)"), {"n": f"{OWNER_SCHEMA}.owner"}).scalar() is None
    finally:
        with admin.connect() as conn:  # this test created it, so this test removes it
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@requires_postgres
def test_creating_a_scratch_database_never_replaces_an_existing_one():
    admin_url = make_url(POSTGRES_URL)
    name = scratch_database_name("occupied")
    create_scratch_database(admin_url, name, tag="occupied")
    try:
        with make_engine(str(admin_url.set(database=name))).begin() as conn:
            conn.execute(text("CREATE TABLE precious (id integer)"))
        with pytest.raises(Exception, match="already exists"):
            create_scratch_database(admin_url, name, tag=RUN_TAG)
        # still intact and still owned by the other tag
        assert scratch_database_owner(str(admin_url.set(database=name))) == "occupied"
        with make_engine(str(admin_url.set(database=name))).connect() as conn:
            assert conn.execute(text("SELECT to_regclass('precious')")).scalar() is not None
    finally:
        assert drop_scratch_database_if_owned(admin_url, name, tag="occupied") is True


@requires_postgres
def test_the_configured_database_itself_is_never_used_as_scratch(postgres_scratch_url):
    configured = make_url(POSTGRES_URL).database
    assert make_url(postgres_scratch_url).database != configured
    assert scratch_database_owner(POSTGRES_URL) is None  # unstamped, therefore undroppable


@pytest.mark.parametrize("name", ["postgres", "fms", "template1", 'fms_test_x"; DROP DATABASE fms; --', "FMS_TEST_X"])
def test_only_scratch_style_names_can_ever_be_created_or_dropped(name):
    admin_url = make_url(POSTGRES_URL or "postgresql+psycopg://u@127.0.0.1:1/none")
    with pytest.raises(ValueError, match="unexpected scratch database name"):
        drop_scratch_database_if_owned(admin_url, name)
    with pytest.raises(ValueError, match="unexpected scratch database name"):
        create_scratch_database(admin_url, name)


@requires_postgres
def test_per_test_reset_refuses_a_database_from_another_run():
    admin_url = make_url(POSTGRES_URL)
    name = scratch_database_name("resetforeign")
    url = create_scratch_database(admin_url, name, tag="resetforeign")
    try:
        with make_engine(url).begin() as conn:
            conn.execute(text("CREATE TABLE public.precious (id integer)"))
        assert reset_public_schema_if_owned(url) is False  # stamped by another tag
        with make_engine(url).connect() as conn:
            assert conn.execute(text("SELECT to_regclass('public.precious')")).scalar() is not None
        assert reset_public_schema_if_owned(url, tag="resetforeign") is True  # its owner may
        with make_engine(url).connect() as conn:
            assert conn.execute(text("SELECT to_regclass('public.precious')")).scalar() is None
    finally:
        assert drop_scratch_database_if_owned(admin_url, name, tag="resetforeign") is True


@requires_postgres
def test_drop_of_a_missing_database_is_a_no_op():
    assert drop_scratch_database_if_owned(make_url(POSTGRES_URL), scratch_database_name("neverexisted")) is False
