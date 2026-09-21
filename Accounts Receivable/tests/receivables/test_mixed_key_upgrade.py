"""Upgrading historical data in which one allocation key served a payment *and* a credit batch.

The original ``fmsreceivables02`` failed on such data (cleanly on PostgreSQL,
half-way on SQLite). Its controlled revision must upgrade it without losing
an allocation, without modifying any historical row, without touching the
ledger, and must complete a database the original left half-upgraded.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app
from app.modules.receivables import services
from app.modules.receivables.schemas import AllocationIn
from app.modules.receivables.schemas import AllocationRequest as AllocationRequestIn
from app.modules.receivables.upgrade_checks import half_applied_fmsreceivables02, mixed_source_allocation_keys
from tests.conftest import alembic_config, migrate
from tests.receivables.legacy_data import PAYMENT_REQUEST, SHARED_KEY_ALLOCATIONS, build_legacy_database

API = "/api/v1/receivables"
HISTORY_TABLES = {
    "invoice_allocations": "id, invoice_id, payment_id, adjustment_id, amount, allocation_date, idempotency_key",
    "customer_payments": "id, customer_id, payment_date, amount, amount_allocated, idempotency_key, request_hash",
    "customer_adjustments": "id, customer_id, kind, adjustment_date, amount, amount_allocated, idempotency_key, request_hash",
    "sales_invoices": "id, invoice_number, total, amount_settled, status, journal_entry_id",
    "journal_entries": "id, entry_date, description, status",
    "journal_lines": "id, entry_id, account_id, debit, credit",
}


def history_snapshot(url):
    """Every historical row that an upgrade must leave exactly as it is."""
    engine = make_engine(url)
    with engine.connect() as conn:
        snapshot = {
            table: sorted(tuple(str(v) for v in row) for row in conn.execute(text(f"SELECT {columns} FROM {table}")))
            for table, columns in HISTORY_TABLES.items()
        }
    engine.dispose()
    return snapshot


def _triggers_enforced(url):
    engine = make_engine(url)
    with engine.connect() as conn:
        for sql in ("UPDATE invoice_allocations SET amount = 1", "DELETE FROM invoice_allocations"):
            with pytest.raises(IntegrityError, match="invoice allocations cannot be modified"):
                conn.execute(text(sql))
            conn.rollback()
    engine.dispose()


@pytest.fixture
def mixed_url(db_url):
    build_legacy_database(db_url, allocations=SHARED_KEY_ALLOCATIONS)  # at fmsreceivables01
    return db_url


@pytest.fixture
def upgraded_client(mixed_url):
    migrate(mixed_url, "head")
    app = create_app(Settings(database_url=mixed_url))
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


# --- The upgrade itself -------------------------------------------------------------------


def test_upgrade_succeeds_and_preserves_every_historical_row(mixed_url):
    before = history_snapshot(mixed_url)
    assert len(before["invoice_allocations"]) == 4

    migrate(mixed_url, "head")

    assert history_snapshot(mixed_url) == before  # allocations, documents and ledger untouched
    engine = make_engine(mixed_url)
    with Session(engine) as db:
        assert compare_metadata(MigrationContext.configure(db.connection()), Base.metadata) == []
        assert db.connection().execute(text("SELECT version_num FROM alembic_version")).scalar() == "fmsreceivables03"
        # the mixed key has no request row; the other legacy batch is classified as before
        rows = db.execute(text("SELECT idempotency_key, fingerprint_origin FROM allocation_requests")).all()
        assert rows == [("alloc-multi", "unverifiable")]
        # effective dates were backfilled for every row, including the mixed key's
        assert db.execute(text("SELECT COUNT(*) FROM invoice_allocations WHERE effective_date <> allocation_date")).scalar() == 0
    engine.dispose()
    _triggers_enforced(mixed_url)


def test_end_state_equals_the_original_backfill_when_no_key_is_mixed(db_url):
    """For the data the original fmsreceivables02 could handle, the revised body is equivalent."""
    build_legacy_database(db_url)  # no mixed keys
    migrate(db_url, "fmsreceivables02")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT idempotency_key, payment_id, adjustment_id, request_hash, effective_date "
            "FROM allocation_requests ORDER BY idempotency_key")).all()
        assert [tuple(str(v) for v in r) for r in rows] == [
            ("alloc-credit", "None", "1", "None", "2026-01-25"),   # what the original INSERT produced
            ("alloc-multi", "1", "None", "None", "2026-01-20"),
            ("alloc-single", "1", "None", "None", "2026-01-20"),
        ]
        assert conn.execute(text("SELECT COUNT(*) FROM invoice_allocations WHERE effective_date <> allocation_date")).scalar() == 0
    engine.dispose()


def test_running_the_upgrade_on_an_already_upgraded_database_changes_nothing(mixed_url):
    migrate(mixed_url, "head")
    before = history_snapshot(mixed_url)
    engine = make_engine(mixed_url)
    with engine.connect() as conn:
        requests_before = conn.execute(text("SELECT * FROM allocation_requests ORDER BY id")).all()
    engine.dispose()
    command.downgrade(alembic_config(mixed_url), "fmsreceivables02")
    migrate(mixed_url, "head")
    assert history_snapshot(mixed_url) == before
    engine = make_engine(mixed_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT * FROM allocation_requests ORDER BY id")).all() == requests_before
    engine.dispose()


def test_pre_upgrade_check_reports_mixed_keys_and_stays_read_only(mixed_url):
    before = history_snapshot(mixed_url)
    engine = make_engine(mixed_url)
    with engine.connect() as conn:
        assert mixed_source_allocation_keys(conn) == ["shared"]
        assert half_applied_fmsreceivables02(conn) == []
    engine.dispose()
    assert history_snapshot(mixed_url) == before


# --- Partial failure: rollback (PostgreSQL) or resume (SQLite) -------------------------------


BLOCK_SQLITE = ["CREATE TRIGGER trg_test_block BEFORE UPDATE ON invoice_allocations BEGIN SELECT RAISE(ABORT, 'test block'); END"]
BLOCK_PG = [
    "CREATE FUNCTION fms_test_block() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test block'; END $$",
    "CREATE TRIGGER trg_test_block BEFORE UPDATE ON invoice_allocations FOR EACH ROW EXECUTE FUNCTION fms_test_block()",
]


def test_failure_half_way_through_fmsreceivables02(mixed_url):
    """A fault injected into the backfill: PostgreSQL rolls back, SQLite is left half-way and then resumes."""
    before = history_snapshot(mixed_url)
    engine = make_engine(mixed_url)
    sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        for statement in BLOCK_SQLITE if sqlite else BLOCK_PG:
            conn.execute(text(statement))

    with pytest.raises(Exception, match="test block"):
        migrate(mixed_url, "fmsreceivables02")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "fmsreceivables01"
        findings = half_applied_fmsreceivables02(conn)
        tables = set(inspect(conn).get_table_names())
        if sqlite:
            # exactly the state the original revision left behind on a real failure
            assert "allocation_requests" in tables and findings
            assert "append-only UPDATE trigger on invoice_allocations is missing" in findings
        else:
            assert "allocation_requests" not in tables and findings == []  # fully rolled back
            assert conn.execute(text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_invoice_allocations_guard'")).scalar() == "O"
    assert history_snapshot(mixed_url) == before

    # remove the injected fault and simply upgrade again
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER trg_test_block" + ("" if sqlite else " ON invoice_allocations")))
        if not sqlite:
            conn.execute(text("DROP FUNCTION fms_test_block()"))
    engine.dispose()
    migrate(mixed_url, "head")

    assert history_snapshot(mixed_url) == before
    engine = make_engine(mixed_url)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        assert half_applied_fmsreceivables02(conn) == []
        assert conn.execute(text("SELECT idempotency_key FROM allocation_requests")).scalars().all() == ["alloc-multi"]
        assert conn.execute(text("SELECT COUNT(*) FROM invoice_allocations WHERE effective_date <> allocation_date")).scalar() == 0
    engine.dispose()
    _triggers_enforced(mixed_url)


# --- Behaviour after the upgrade ------------------------------------------------------------------


def _body(key, invoice_id, amount):
    return {"idempotency_key": key, "allocations": [{"invoice_id": invoice_id, "amount": amount}]}


def test_reuse_of_the_mixed_key_is_refused_for_every_source(upgraded_client, mixed_url):
    before = history_snapshot(mixed_url)
    for source, invoice_id, amount in (("payments", 1, "20.00"), ("payments", 1, "25.00"), ("adjustments", 4, "15.00"), ("adjustments", 2, "1.00")):
        r = upgraded_client.post(f"{API}/{source}/1/allocations", json=_body("shared", invoice_id, amount))
        assert r.status_code == 409, (source, r.text)
        assert "cannot be verified" in r.json()["detail"]
    assert history_snapshot(mixed_url) == before


def test_new_keys_and_legacy_payment_replay_still_work(upgraded_client, mixed_url):
    assert upgraded_client.post(f"{API}/payments/1/allocations", json=_body("new-key", 1, "5.00")).status_code == 201
    assert upgraded_client.post(f"{API}/payments/1/allocations", json=_body("new-key", 1, "5.00")).status_code == 200
    assert upgraded_client.post(f"{API}/payments/1/allocations", json=_body("new-key", 1, "6.00")).status_code == 409
    replay = upgraded_client.post(f"{API}/payments", json=PAYMENT_REQUEST)
    assert replay.status_code == 200 and replay.json()["id"] == 1
    assert upgraded_client.post(f"{API}/payments", json=PAYMENT_REQUEST | {"amount": "250.00"}).status_code == 409
    assert upgraded_client.get(f"{API}/customers/1/balance").status_code == 200  # reporting works on the upgraded data


def test_concurrent_requests_on_the_mixed_key_and_on_new_keys(upgraded_client, mixed_url):
    factory = upgraded_client.app.state.session_factory
    before = history_snapshot(mixed_url)
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        with factory() as db:
            try:
                if i % 2 == 0:  # reuse of the mixed key, alternating sources
                    if i % 4 == 0:
                        services.allocate_payment(db, 1, AllocationRequestIn(
                            idempotency_key="shared", allocations=[AllocationIn(invoice_id=1, amount=Decimal("20.00"))]))
                    else:
                        services.allocate_adjustment(db, 1, AllocationRequestIn(
                            idempotency_key="shared", allocations=[AllocationIn(invoice_id=4, amount=Decimal("15.00"))]))
                    return "APPLIED"
                _, created = services.allocate_payment(db, 1, AllocationRequestIn(  # distinct new keys
                    idempotency_key=f"fresh-{i}", allocations=[AllocationIn(invoice_id=2, amount=Decimal("1.00"))]))
                return "created" if created else "replayed"
            except Exception as exc:  # noqa: BLE001
                return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert all(isinstance(r, services.IdempotencyConflict) for i, r in enumerate(results) if i % 2 == 0), results
    assert [r for i, r in enumerate(results) if i % 2] == ["created"] * 4

    after = history_snapshot(mixed_url)
    untouched = {k: v for k, v in before.items() if k not in ("invoice_allocations", "customer_payments", "sales_invoices")}
    assert {k: v for k, v in after.items() if k in untouched} == untouched
    assert len(after["invoice_allocations"]) == len(before["invoice_allocations"]) + 4
    assert all(row in after["invoice_allocations"] for row in before["invoice_allocations"])
