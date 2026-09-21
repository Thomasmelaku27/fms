"""Upgrading a database with pre-fingerprint data: verification, replay, conflicts.

The rule under test: a request reusing a key is only ever treated as a retry
when it can be *verified* against what was originally recorded. Legacy
allocation batches that cannot be rebuilt reliably answer every reuse with
409; legacy payments/adjustments replay when resent exactly as recorded.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app
from app.modules.receivables import services
from app.modules.receivables.models import AllocationRequest, CustomerAdjustment, CustomerPayment, InvoiceAllocation, SalesInvoice
from app.modules.receivables.schemas import AdjustmentIn, AllocationIn, PaymentIn
from app.modules.receivables.schemas import AllocationRequest as AllocationRequestIn
from tests.conftest import alembic_config, migrate
from tests.receivables.legacy_data import ADJUSTMENT_REQUEST, PAYMENT_REQUEST, build_legacy_database, v1_fingerprint

API = "/api/v1/receivables"


@pytest.fixture
def legacy_url(db_url):
    build_legacy_database(db_url, revision="head")
    return db_url


@pytest.fixture
def legacy_client(legacy_url):
    app = create_app(Settings(database_url=legacy_url))
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


def _state(url):
    """Everything a replay or a conflict must leave unchanged."""
    engine = make_engine(url)
    with Session(engine) as db:
        state = {
            "payments": db.scalar(select(func.count()).select_from(CustomerPayment)),
            "adjustments": db.scalar(select(func.count()).select_from(CustomerAdjustment)),
            "allocations": sorted((a.idempotency_key or "", a.invoice_id, str(a.amount)) for a in db.scalars(select(InvoiceAllocation))),
            "requests": sorted((r.idempotency_key, r.fingerprint_origin) for r in db.scalars(select(AllocationRequest))),
            "settled": sorted((i.id, str(i.amount_settled)) for i in db.scalars(select(SalesInvoice))),
            "allocated": str(db.get(CustomerPayment, 1).amount_allocated),
        }
    engine.dispose()
    return state


def _alloc_body(key, invoice_id, amount, **extra):
    return {"idempotency_key": key, "allocations": [{"invoice_id": invoice_id, "amount": amount}], **extra}


# --- Migration / upgrade regression ---------------------------------------------------------


def test_upgrade_classifies_every_legacy_allocation_request(db_url):
    build_legacy_database(db_url, revision="fmsreceivables02")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        # the state this fix addresses: fmsreceivables02 left every legacy hash NULL
        assert conn.execute(text("SELECT COUNT(*) FROM allocation_requests WHERE request_hash IS NULL")).scalar() == 3
    engine.dispose()

    migrate(db_url, "head")

    engine = make_engine(db_url)
    with Session(engine) as db:
        requests = {r.idempotency_key: r for r in db.scalars(select(AllocationRequest))}
        assert {k: r.fingerprint_origin for k, r in requests.items()} == {
            "alloc-single": "reconstructed", "alloc-multi": "unverifiable", "alloc-credit": "reconstructed"}
        assert requests["alloc-multi"].request_hash is None
        # the migration's reconstruction equals what the application computes for that request
        payment = db.get(CustomerPayment, 1)
        credit = db.get(CustomerAdjustment, 1)
        assert requests["alloc-single"].request_hash == services.allocation_fingerprint(
            payment, AllocationRequestIn(idempotency_key="alloc-single", allocations=[AllocationIn(invoice_id=1, amount=Decimal("20.00"))]))
        assert requests["alloc-credit"].request_hash == services.allocation_fingerprint(
            credit, AllocationRequestIn(idempotency_key="alloc-credit", allocations=[AllocationIn(invoice_id=4, amount=Decimal("15"))]))
        # earlier backfills are unchanged: effective dates equal allocation dates
        assert all(a.effective_date == a.allocation_date for a in db.scalars(select(InvoiceAllocation)))
        # the schema matches the models exactly
        assert compare_metadata(MigrationContext.configure(db.connection()), Base.metadata) == []
    engine.dispose()


def test_upgrade_keeps_the_append_only_guard(legacy_url):
    engine = make_engine(legacy_url)
    with engine.connect() as conn:
        with pytest.raises(IntegrityError, match="invoice allocations cannot be modified"):
            conn.execute(text("UPDATE invoice_allocations SET amount = 1"))
        conn.rollback()
        with pytest.raises(IntegrityError, match="invoice allocations cannot be modified"):
            conn.execute(text("DELETE FROM invoice_allocations"))
        conn.rollback()
    engine.dispose()


@pytest.mark.parametrize(
    ("origin", "hash_", "constraint"),
    [
        ("request", None, "hash_matches_origin"),
        ("reconstructed", None, "hash_matches_origin"),
        ("unverifiable", "abc", "hash_matches_origin"),
        ("guessed", "abc", "fingerprint_origin"),
    ],
)
def test_database_rejects_inconsistent_fingerprint_state(legacy_url, origin, hash_, constraint):
    engine = make_engine(legacy_url)
    with engine.connect() as conn:
        with pytest.raises(IntegrityError, match=constraint):
            conn.execute(text(
                "INSERT INTO allocation_requests (idempotency_key, payment_id, request_hash, fingerprint_origin, effective_date) "
                "VALUES ('x', 1, :h, :o, '2026-02-01')"), {"h": hash_, "o": origin})
        conn.rollback()
        with pytest.raises(IntegrityError, match="hash_matches_origin"):
            conn.execute(text("UPDATE allocation_requests SET request_hash = NULL WHERE idempotency_key = 'alloc-single'"))
        conn.rollback()
    engine.dispose()


def test_downgrade_and_upgrade_again_is_stable(legacy_url):
    before = _state(legacy_url)
    command.downgrade(alembic_config(legacy_url), "fmsreceivables02")
    engine = make_engine(legacy_url)
    with engine.connect() as conn:
        assert "fingerprint_origin" not in {c["name"] for c in inspect(conn).get_columns("allocation_requests")}
        # exactly the fmsreceivables02 state again: every legacy fingerprint is NULL
        hashes = dict(conn.execute(text("SELECT idempotency_key, request_hash FROM allocation_requests")).all())
        assert hashes == {"alloc-single": None, "alloc-multi": None, "alloc-credit": None}
    engine.dispose()
    migrate(legacy_url, "head")
    assert _state(legacy_url) == before


def test_fresh_database_upgrade_has_nothing_to_classify(migrated_db_url):
    engine = make_engine(migrated_db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM allocation_requests")).scalar() == 0
    engine.dispose()


# --- The reported case and its neighbours -------------------------------------------------


def test_modified_request_on_a_legacy_key_is_a_conflict(legacy_client, legacy_url):
    """Recorded as 20.00 before the upgrade; the same key now sent with 25.00."""
    before = _state(legacy_url)
    r = legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("alloc-single", 1, "25.00"))
    assert r.status_code == 409, r.text
    assert _state(legacy_url) == before


def test_identical_request_on_a_reconstructed_legacy_key_replays(legacy_client, legacy_url):
    before = _state(legacy_url)
    for amount in ("20.00", "20"):
        r = legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("alloc-single", 1, amount))
        assert r.status_code == 200, r.text
        assert r.json()["amount_allocated"] == "40.00"
    r = legacy_client.post(f"{API}/adjustments/1/allocations", json=_alloc_body("alloc-credit", 4, "15.00"))
    assert r.status_code == 200, r.text
    assert _state(legacy_url) == before


@pytest.mark.parametrize(
    "body",
    [
        _alloc_body("alloc-single", 1, "20.00", effective_date="2026-02-01"),  # adds a field the original lacked
        _alloc_body("alloc-single", 2, "20.00"),                               # other invoice
        {"idempotency_key": "alloc-single", "allocations": [{"invoice_id": 1, "amount": "20.00"}, {"invoice_id": 2, "amount": "1.00"}]},
    ],
)
def test_any_other_change_on_a_reconstructed_key_is_a_conflict(legacy_client, legacy_url, body):
    before = _state(legacy_url)
    assert legacy_client.post(f"{API}/payments/1/allocations", json=body).status_code == 409
    assert _state(legacy_url) == before


def test_unverifiable_legacy_key_is_never_treated_as_a_retry(legacy_client, legacy_url):
    """Even the request that probably *was* the original cannot be proven so: 409."""
    before = _state(legacy_url)
    original = {"idempotency_key": "alloc-multi", "allocations": [{"invoice_id": 2, "amount": "10.00"}, {"invoice_id": 3, "amount": "10.00"}]}
    for body in (original, _alloc_body("alloc-multi", 2, "10.00"), _alloc_body("alloc-multi", 2, "25.00")):
        r = legacy_client.post(f"{API}/payments/1/allocations", json=body)
        assert r.status_code == 409, r.text
        assert "cannot be verified" in r.json()["detail"]
    assert _state(legacy_url) == before


def test_unverifiable_key_conflicts_for_other_sources_too(legacy_client, legacy_url):
    before = _state(legacy_url)
    r = legacy_client.post(f"{API}/adjustments/1/allocations", json=_alloc_body("alloc-multi", 4, "1.00"))
    assert r.status_code == 409
    assert _state(legacy_url) == before


# --- Legacy payments and adjustments (verifiable old fingerprint) -----------------------------


def test_legacy_payment_with_its_old_fingerprint_replays(legacy_client, legacy_url):
    before = _state(legacy_url)
    r = legacy_client.post(f"{API}/payments", json=PAYMENT_REQUEST)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == 1 and r.json()["amount"] == "200.00"
    assert _state(legacy_url) == before


@pytest.mark.parametrize(
    "change",
    [{"amount": "250.00"}, {"reference": "CHQ-2"}, {"payment_date": "2026-01-21"},
     {"allocations": [{"invoice_id": 1, "amount": "5.00"}]},
     # v1 hashed the amount exactly as received; an equivalent but different
     # spelling cannot be verified against it, so it is refused rather than assumed equal
     {"amount": "200"}],
)
def test_modified_legacy_payment_is_a_conflict(legacy_client, legacy_url, change):
    before = _state(legacy_url)
    r = legacy_client.post(f"{API}/payments", json=PAYMENT_REQUEST | change)
    assert r.status_code == 409, r.text
    assert _state(legacy_url) == before


def test_legacy_adjustment_with_its_old_fingerprint_replays_and_changes_conflict(legacy_client, legacy_url):
    before = _state(legacy_url)
    assert legacy_client.post(f"{API}/adjustments", json=ADJUSTMENT_REQUEST).status_code == 200
    assert legacy_client.post(f"{API}/adjustments", json=ADJUSTMENT_REQUEST | {"amount": "55.00"}).status_code == 409
    assert legacy_client.post(f"{API}/adjustments", json=ADJUSTMENT_REQUEST | {"reason": "other"}).status_code == 409
    assert _state(legacy_url) == before


def test_frozen_legacy_encoding_equals_the_original_formula():
    """The explicit v1 functions reproduce what fmsreceivables01-era code stored."""
    for body in (PAYMENT_REQUEST, PAYMENT_REQUEST | {"amount": "200", "reference": None,
                                                       "allocations": [{"invoice_id": 3, "amount": "1.5"}]}):
        model = PaymentIn(**body)
        assert services.legacy_payment_fingerprint(model) == v1_fingerprint(model)
    for body in (ADJUSTMENT_REQUEST, ADJUSTMENT_REQUEST | {"kind": "write_off", "invoice_id": 2, "amount": "7"}):
        model = AdjustmentIn(**body)
        assert services.legacy_adjustment_fingerprint(model) == v1_fingerprint(model)


def test_new_requests_after_the_upgrade_are_fully_verifiable(legacy_client, legacy_url):
    r = legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("after-upgrade", 1, "5.00"))
    assert r.status_code == 201
    assert legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("after-upgrade", 1, "5.00")).status_code == 200
    assert legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("after-upgrade", 1, "6.00")).status_code == 409
    engine = make_engine(legacy_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT fingerprint_origin FROM allocation_requests WHERE idempotency_key = 'after-upgrade'")).scalar() == "request"
    engine.dispose()


# --- Concurrent retries -------------------------------------------------------------------------


def _concurrently(fn, n=6):
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))


def test_concurrent_retries_against_legacy_keys(legacy_client, legacy_url):
    factory = legacy_client.app.state.session_factory
    before = _state(legacy_url)

    def send(i):
        with factory() as db:
            if i % 3 == 0:  # identical retry of a reconstructed batch
                _, created = services.allocate_payment(db, 1, AllocationRequestIn(
                    idempotency_key="alloc-single", allocations=[AllocationIn(invoice_id=1, amount=Decimal("20.00"))]))
                return "replayed" if not created else "CREATED"
            if i % 3 == 1:  # modified retry of the same batch
                services.allocate_payment(db, 1, AllocationRequestIn(
                    idempotency_key="alloc-single", allocations=[AllocationIn(invoice_id=1, amount=Decimal("25.00"))]))
                return "ACCEPTED"
            services.allocate_payment(db, 1, AllocationRequestIn(  # anything on an unverifiable batch
                idempotency_key="alloc-multi", allocations=[AllocationIn(invoice_id=2, amount=Decimal("10.00")),
                                                            AllocationIn(invoice_id=3, amount=Decimal("10.00"))]))
            return "ACCEPTED"

    results = _concurrently(send, 9)
    assert [r for i, r in enumerate(results) if i % 3 == 0] == ["replayed"] * 3
    assert all(isinstance(r, services.IdempotencyConflict) for i, r in enumerate(results) if i % 3), results
    assert _state(legacy_url) == before


def test_concurrent_identical_legacy_payment_retries(legacy_client, legacy_url):
    factory = legacy_client.app.state.session_factory
    before = _state(legacy_url)

    def send(_):
        with factory() as db:
            payment, created = services.register_payment(db, PaymentIn(**PAYMENT_REQUEST))
            return payment.id, created

    results = _concurrently(send)
    assert results == [(1, False)] * 6
    assert _state(legacy_url) == before


# --- Rollback -------------------------------------------------------------------------------------


def test_conflict_leaves_the_session_clean_and_usable(legacy_client, legacy_url):
    factory = legacy_client.app.state.session_factory
    before = _state(legacy_url)
    with factory() as db:
        with pytest.raises(services.IdempotencyConflict):
            services.allocate_payment(db, 1, AllocationRequestIn(
                idempotency_key="alloc-multi", allocations=[AllocationIn(invoice_id=2, amount=Decimal("10.00"))]))
        assert not db.new and not db.dirty  # nothing half-done is pending
        # same session, new key: works and is the only change
        payment, created = services.allocate_payment(db, 1, AllocationRequestIn(
            idempotency_key="fresh", allocations=[AllocationIn(invoice_id=2, amount=Decimal("1.00"))]))
        assert created and payment.amount_allocated == Decimal("41.00")
    after = _state(legacy_url)
    assert after["allocations"] == sorted(before["allocations"] + [("fresh", 2, "1.00")])


def test_failed_new_request_on_upgraded_database_rolls_back_its_request_row(legacy_client, legacy_url):
    before = _state(legacy_url)
    r = legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("too-much", 1, "80.01"))  # 80.00 left on INV-1
    assert r.status_code == 422
    assert _state(legacy_url) == before
    assert legacy_client.post(f"{API}/payments/1/allocations", json=_alloc_body("too-much", 1, "80.00")).status_code == 201
