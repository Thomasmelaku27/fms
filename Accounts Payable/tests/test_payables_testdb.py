"""Safety of the disposable PostgreSQL database utility (PostgreSQL only).

The utility must never drop a database it did not create in this run: ownership is
proven by a token stored in the database comment, names are unique per run, and
collisions, interrupted cleanup and failed creation all leave foreign databases alone.
Administrative cleanup of leaks is separate and drops only databases proven abandoned
(dead creator process on this host, old enough, nobody connected) or explicitly handed
over with their token.
"""

import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db import make_engine
from tests.conftest import POSTGRES_URL
from tests.payables_support import dbscratch

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set FMS_TEST_POSTGRES_URL")


def _admin():
    return make_engine(POSTGRES_URL).execution_options(isolation_level="AUTOCOMMIT")


def _exists(name):
    with _admin().connect() as conn:
        return conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar() is not None


@pytest.fixture
def foreign_database():
    """A database that looks like ours (same prefix) but was created by 'someone else'."""
    name = dbscratch.generate_name("foreign")
    with _admin().connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        conn.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{dbscratch.OWNER_PREFIX}someone-else'"))
    yield name
    with _admin().connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def test_create_tags_and_drop_verifies_ownership():
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="unit")
    assert db.name.startswith(dbscratch.NAME_PREFIX) and _exists(db.name)
    with _admin().connect() as conn:
        comment = conn.execute(text("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :n"), {"n": db.name}).scalar()
    assert comment == db.owner_comment
    assert db in dbscratch.owned_databases()
    assert dbscratch.drop_scratch_database(db) is True
    assert not _exists(db.name)
    assert dbscratch.drop_scratch_database(db) is False  # already gone: nothing to do, no error


def test_refuses_to_drop_a_database_it_does_not_own(foreign_database):
    impostor = dbscratch.ScratchDatabase(admin_url=POSTGRES_URL, name=foreign_database, token="ab" * 16,
                                         ownership=dbscratch.Ownership("ab" * 16, "h", 1, "b", None))
    with pytest.raises(dbscratch.ScratchOwnershipError):
        dbscratch.drop_scratch_database(impostor)
    with pytest.raises(dbscratch.ScratchOwnershipError):
        dbscratch.reset_schema(impostor)
    assert _exists(foreign_database)
    # The context manager swallows the ownership refusal (the database is left alone).
    assert any(entry.name == foreign_database for entry in dbscratch.list_scratch_databases(POSTGRES_URL))
    assert dbscratch.drop_listed_scratch_databases(POSTGRES_URL, [foreign_database]) == []  # confirm=False: nothing
    # Confirmed, but the comment is not a parsable ownership record: ownership cannot be established.
    decisions = dbscratch.administrative_cleanup(POSTGRES_URL, [foreign_database], confirm=True, operator="admin")
    assert decisions[0].action == "refused" and "cannot be established" in decisions[0].reason
    assert _exists(foreign_database)


def test_name_collision_picks_another_name_and_touches_nothing(foreign_database):
    names = iter([foreign_database, dbscratch.generate_name("retry")])
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="x", name_factory=lambda label: next(names))
    try:
        assert db.name != foreign_database and _exists(foreign_database) and _exists(db.name)
    finally:
        dbscratch.drop_scratch_database(db)
    with pytest.raises(dbscratch.ScratchNameCollision):
        dbscratch.create_scratch_database(POSTGRES_URL, label="x", attempts=1, name_factory=lambda label: foreign_database)
    assert _exists(foreign_database)


def test_concurrent_sessions_get_distinct_databases_and_only_drop_their_own():
    created = []
    lock = threading.Lock()

    def worker():
        with dbscratch.scratch_database(POSTGRES_URL, label="par") as db:
            with lock:
                created.append(db.name)
            assert _exists(db.name)
            with make_engine(db.url).begin() as conn:
                conn.execute(text("CREATE TABLE t (x int)"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(created) == 4 and len(set(created)) == 4
    assert not any(_exists(n) for n in created)


def test_interrupted_run_drops_only_its_own_database(foreign_database):
    mine = None
    with pytest.raises(RuntimeError):
        with dbscratch.scratch_database(POSTGRES_URL, label="boom") as db:
            mine = db.name
            raise RuntimeError("test interrupted")
    assert mine and not _exists(mine)
    assert _exists(foreign_database)


def test_failed_creation_drops_nothing(monkeypatch, foreign_database):
    original = dbscratch._admin_engine

    class Exploding:
        def __init__(self, engine):
            self._engine = engine

        def connect(self):
            conn = self._engine.connect()
            real_execute = conn.execute

            def execute(statement, *args, **kwargs):
                if "CREATE DATABASE" in str(statement):
                    raise RuntimeError("no privilege to create databases")
                return real_execute(statement, *args, **kwargs)

            conn.execute = execute
            return conn

        def dispose(self):
            self._engine.dispose()

    monkeypatch.setattr(dbscratch, "_admin_engine", lambda url: Exploding(original(url)))
    before = dbscratch.owned_databases()
    with pytest.raises(RuntimeError, match="no privilege"):
        dbscratch.create_scratch_database(POSTGRES_URL, label="fail")
    assert dbscratch.owned_databases() == before
    assert _exists(foreign_database)


def test_session_fixture_database_is_owned_and_unique(postgres_scratch_db):
    assert postgres_scratch_db.name.startswith(dbscratch.NAME_PREFIX + "session_")
    dbscratch.verify_ownership(postgres_scratch_db)
    other = dbscratch.create_scratch_database(POSTGRES_URL, label="session")
    try:
        assert other.name != postgres_scratch_db.name
    finally:
        dbscratch.drop_scratch_database(other)


# --- administrative cleanup: ownership must be established, never inferred from a name ---------------


def _comment_of(name):
    with _admin().connect() as conn:
        return conn.execute(text("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :n"), {"n": name}).scalar()


def _set_comment(name, comment):
    with _admin().connect() as conn:
        conn.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{comment}'"))


def test_ownership_token_mismatch_is_refused_even_with_a_valid_comment():
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="tok")
    try:
        forged = dbscratch.ScratchDatabase(admin_url=POSTGRES_URL, name=db.name, token="cd" * 16,
                                           ownership=dbscratch.Ownership("cd" * 16, db.ownership.host, db.ownership.pid, db.ownership.boot, db.ownership.created))
        with pytest.raises(dbscratch.ScratchOwnershipError):
            dbscratch.drop_scratch_database(forged)
        # Handing over the wrong token administratively does not help either (live process -> refused).
        decisions = dbscratch.administrative_cleanup(POSTGRES_URL, [db.name], confirm=True, operator="admin", tokens={db.name: "cd" * 16})
        assert decisions[0].action == "refused" and "still alive" in decisions[0].reason
        assert _exists(db.name)
    finally:
        assert dbscratch.drop_scratch_database(db) is True


def test_cross_run_cleanup_is_refused_while_the_creator_lives_and_allowed_with_its_token():
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="cross")
    # Another run (simulated: same process, different registry) cannot drop it by name ...
    refused = dbscratch.administrative_cleanup(POSTGRES_URL, [db.name], confirm=True, operator="other-run")
    assert refused[0].action == "refused" and "still alive" in refused[0].reason and _exists(db.name)
    # ... unless the owning run hands over the token explicitly.
    handed = dbscratch.administrative_cleanup(POSTGRES_URL, [db.name], confirm=True, operator="other-run", tokens={db.name: db.token})
    assert handed[0].action == "dropped" and "token supplied" in handed[0].reason
    assert not _exists(db.name)


def test_connected_database_is_never_force_dropped_administratively():
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="conn")
    engine = make_engine(db.url)
    try:
        with engine.connect() as held:
            held.execute(text("SELECT 1"))
            decisions = dbscratch.administrative_cleanup(POSTGRES_URL, [db.name], confirm=True, operator="admin", tokens={db.name: db.token})
            assert decisions[0].action == "refused" and "connected" in decisions[0].reason
            assert _exists(db.name)
    finally:
        engine.dispose()
        assert dbscratch.drop_scratch_database(db) is True  # the owner may force its own sessions away


def test_crashed_process_leak_is_recoverable_only_when_proven_abandoned(tmp_path):
    """A child process creates a database and dies without cleanup."""
    script = (
        "import sys; from tests.payables_support import dbscratch; "
        "db = dbscratch.create_scratch_database(sys.argv[1], label='crash'); import os; print(db.name, os.getpid()); sys.stdout.flush(); os._exit(137)"
    )
    proc = subprocess.run([sys.executable, "-c", script, POSTGRES_URL], capture_output=True, text=True, cwd=os.getcwd(), timeout=60)
    name, child_pid = proc.stdout.strip().splitlines()[-1].split()
    assert proc.returncode == 137 and name.startswith(dbscratch.NAME_PREFIX) and _exists(name)
    listing = {entry.name: entry for entry in dbscratch.list_scratch_databases(POSTGRES_URL)}[name]
    assert listing.ownership is not None and listing.ownership.pid == int(child_pid) and listing.ownership.host
    # Too young: refused. Then, with the grace period elapsed and the pid dead: dropped.
    young = dbscratch.administrative_cleanup(POSTGRES_URL, [name], confirm=True, operator="admin")
    assert young[0].action == "refused" and "grace" in young[0].reason and _exists(name)
    aged = dbscratch.administrative_cleanup(POSTGRES_URL, [name], confirm=True, operator="admin", grace=timedelta(0))
    assert aged[0].action == "dropped" and f"dead process {child_pid}" in aged[0].reason
    assert not _exists(name)


def test_databases_from_another_host_or_boot_are_refused():
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="host")
    try:
        foreign = dbscratch.Ownership(db.token, "other-host", 1, "other-boot", datetime.now(UTC) - timedelta(days=1))
        _set_comment(db.name, foreign.comment())
        decisions = dbscratch.administrative_cleanup(POSTGRES_URL, [db.name], confirm=True, operator="admin", grace=timedelta(0))
        assert decisions[0].action == "refused" and "another host" in decisions[0].reason
        assert _exists(db.name)
        # Our own cleanup also refuses now: the comment is no longer ours.
        with pytest.raises(dbscratch.ScratchOwnershipError):
            dbscratch.drop_scratch_database(db)
    finally:
        _set_comment(db.name, db.owner_comment)
        assert dbscratch.drop_scratch_database(db) is True


def test_pattern_based_and_unlisted_deletion_is_impossible(foreign_database):
    db = dbscratch.create_scratch_database(POSTGRES_URL, label="pat")
    try:
        # Only listed names are considered; a non-scratch name is refused outright.
        decisions = dbscratch.administrative_cleanup(POSTGRES_URL, ["postgres", "fms", db.name + "x"], confirm=True, operator="admin")
        assert {d.action for d in decisions} == {"refused"}
        with pytest.raises(dbscratch.ScratchDatabaseError):
            dbscratch._quote("postgres")
        assert _exists(db.name) and _exists(foreign_database)
    finally:
        dbscratch.drop_scratch_database(db)


def test_classify_abandoned_decisions():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    own = dbscratch.Ownership("ab" * 16, "host", 4242, "boot", now - timedelta(hours=1))
    listing = dbscratch.ScratchListing("fmsscratch_x", own.comment(), own, 0)
    dead = lambda pid: False  # noqa: E731
    alive = lambda pid: True  # noqa: E731
    assert dbscratch.classify_abandoned(listing, now=now, host="host", boot="boot", pid_alive=dead) is None
    assert "alive" in dbscratch.classify_abandoned(listing, now=now, host="host", boot="boot", pid_alive=alive)
    assert "another host" in dbscratch.classify_abandoned(listing, now=now, host="elsewhere", boot="boot", pid_alive=dead)
    assert "grace" in dbscratch.classify_abandoned(listing, now=now - timedelta(minutes=59), host="host", boot="boot", pid_alive=dead)
    busy = dbscratch.ScratchListing("fmsscratch_x", own.comment(), own, 2)
    assert "connected" in dbscratch.classify_abandoned(busy, now=now, host="host", boot="boot", pid_alive=dead)
    assert "cannot be established" in dbscratch.classify_abandoned(dbscratch.ScratchListing("fmsscratch_y", "garbage", None, 0), now=now)
    assert dbscratch.parse_ownership(dbscratch.OWNER_PREFIX + "zz;host=h") is None
    assert dbscratch.parse_ownership(own.comment()) == own
