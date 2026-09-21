"""``fmsreceivables02`` interrupted at any point must complete on the next ``alembic upgrade head``.

The interruptions are produced by running the revision's *own* steps (loaded
through Alembic, executed under an ``Operations`` context) up to a given
point and committing, which is exactly the state a non-transactional SQLite
upgrade leaves behind when it dies there. On PostgreSQL a real failure rolls
back, but the same partial states are exercised for symmetry.
"""

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.autogenerate import compare_metadata
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app
from tests.conftest import alembic_config, migrate
from tests.receivables.legacy_data import LEGACY_ALLOCATIONS, PAYMENT_REQUEST, SHARED_KEY_ALLOCATIONS, build_legacy_database
from tests.receivables.test_mixed_key_upgrade import _triggers_enforced, history_snapshot

API = "/api/v1/receivables"


def _revision_module(url):
    return ScriptDirectory.from_config(alembic_config(url)).get_revision("fmsreceivables02").module


def run_steps(url, count):
    """Apply the first ``count`` steps of fmsreceivables02 and commit - an interruption after step ``count``."""
    module = _revision_module(url)
    engine = make_engine(url)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            for _, step in module.STEPS[:count]:
                step()
    engine.dispose()
    return [name for name, _ in module.STEPS[:count]]


def _final_state_ok(url, expected_requests):
    engine = make_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "fmsreceivables03"
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        assert {i["name"] for i in inspect(conn).get_indexes("allocation_requests")} >= {
            "ix_allocation_requests_payment_id", "ix_allocation_requests_adjustment_id"}
        rows = conn.execute(text("SELECT idempotency_key, fingerprint_origin FROM allocation_requests ORDER BY idempotency_key")).all()
        assert [tuple(r) for r in rows] == expected_requests
        assert conn.execute(text("SELECT COUNT(*) FROM invoice_allocations WHERE effective_date <> allocation_date")).scalar() == 0
    engine.dispose()
    _triggers_enforced(url)


STEP_COUNT = 7  # every step except the final verification
DATASETS = {
    "plain": (LEGACY_ALLOCATIONS, [("alloc-credit", "reconstructed"), ("alloc-multi", "unverifiable"), ("alloc-single", "reconstructed")]),
    "shared-key": (SHARED_KEY_ALLOCATIONS, [("alloc-multi", "unverifiable")]),
}


@pytest.mark.parametrize("dataset", sorted(DATASETS))
@pytest.mark.parametrize("interrupted_after", range(1, STEP_COUNT + 1))
def test_upgrade_completes_after_an_interruption(db_url, dataset, interrupted_after):
    allocations, expected_requests = DATASETS[dataset]
    build_legacy_database(db_url, allocations=allocations)  # at fmsreceivables01
    before = history_snapshot(db_url)

    done = run_steps(db_url, interrupted_after)
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "fmsreceivables01"
    engine.dispose()

    migrate(db_url, "head")

    assert history_snapshot(db_url) == before, done
    _final_state_ok(db_url, expected_requests)


def test_the_reported_case_table_created_but_indexes_missing(db_url):
    """Exactly the independent reproduction: table present, both indexes absent."""
    build_legacy_database(db_url, allocations=SHARED_KEY_ALLOCATIONS)
    assert run_steps(db_url, 1) == ["create table allocation_requests"]
    engine = make_engine(db_url)
    with engine.connect() as conn:  # PostgreSQL also lists the unique constraint's backing index here
        names = {i["name"] for i in inspect(conn).get_indexes("allocation_requests")}
        assert names.isdisjoint({"ix_allocation_requests_payment_id", "ix_allocation_requests_adjustment_id"})
    engine.dispose()

    migrate(db_url, "head")
    _final_state_ok(db_url, [("alloc-multi", "unverifiable")])


def test_an_interrupted_then_resumed_upgrade_behaves_like_a_clean_one(db_url):
    build_legacy_database(db_url, allocations=SHARED_KEY_ALLOCATIONS)
    run_steps(db_url, 6)  # right before the append-only guard is restored
    migrate(db_url, "head")
    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as client:
        for source, invoice_id in (("payments", 1), ("adjustments", 4)):
            r = client.post(f"{API}/{source}/1/allocations", json={
                "idempotency_key": "shared", "allocations": [{"invoice_id": invoice_id, "amount": "1.00"}]})
            assert r.status_code == 409 and "cannot be verified" in r.json()["detail"]
        assert client.post(f"{API}/payments", json=PAYMENT_REQUEST).status_code == 200  # legacy replay
        assert client.post(f"{API}/payments", json=PAYMENT_REQUEST | {"amount": "1.00"}).status_code == 409
        assert client.post(f"{API}/payments/1/allocations", json={
            "idempotency_key": "fresh", "allocations": [{"invoice_id": 1, "amount": "1.00"}]}).status_code == 201
        assert client.get(f"{API}/customers/1/balance", params={"as_of": "2026-02-01"}).status_code == 200
    app.state.engine.dispose()


def test_running_the_full_upgrade_twice_over_is_harmless(db_url):
    build_legacy_database(db_url, allocations=SHARED_KEY_ALLOCATIONS)
    run_steps(db_url, STEP_COUNT)  # everything applied, version never stamped
    before = history_snapshot(db_url)
    migrate(db_url, "head")
    assert history_snapshot(db_url) == before
    _final_state_ok(db_url, [("alloc-multi", "unverifiable")])


def test_a_foreign_allocation_requests_table_stops_the_upgrade(db_url):
    build_legacy_database(db_url)
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE allocation_requests (id INTEGER PRIMARY KEY, idempotency_key VARCHAR(64))"))
    before = history_snapshot(db_url)

    with pytest.raises(RuntimeError, match="not the one this revision creates"):
        migrate(db_url, "head")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "fmsreceivables01"
        columns = {c["name"] for c in inspect(conn).get_columns("allocation_requests")}
        assert columns == {"id", "idempotency_key"}  # the foreign table is left exactly as it was
        assert "effective_date" not in {c["name"] for c in inspect(conn).get_columns("invoice_allocations")}
    engine.dispose()
    assert history_snapshot(db_url) == before
    _triggers_enforced(db_url)


def test_end_state_verification_catches_a_missing_trigger(db_url):
    """The final step refuses to stamp the revision while anything it owns is missing."""
    build_legacy_database(db_url)
    engine = make_engine(db_url)
    if engine.dialect.name != "sqlite":
        engine.dispose()
        pytest.skip("trigger-level interruptions only exist on SQLite")
    run_steps(db_url, STEP_COUNT)
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER trg_invoice_allocations_no_delete"))  # damage outside this revision's steps
    engine.dispose()
    with pytest.raises(RuntimeError, match="trg_invoice_allocations_no_delete is missing"):
        migrate(db_url, "head")
