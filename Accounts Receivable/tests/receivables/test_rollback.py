"""Transactions are all-or-nothing: a failure anywhere leaves no partial AR or ledger data."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import JournalEntry, JournalLine
from app.modules.receivables import services
from app.modules.receivables.models import (
    AllocationRequest,
    CustomerAdjustment,
    CustomerPayment,
    InvoiceAllocation,
    SalesInvoice,
)
from app.modules.receivables.schemas import AdjustmentIn, AllocationIn, PaymentIn
from app.modules.receivables.schemas import AllocationRequest as AllocationRequest_schema
from tests.receivables.conftest import API, D


def _counts(db):
    return {
        "payments": db.scalar(select(func.count()).select_from(CustomerPayment)),
        "adjustments": db.scalar(select(func.count()).select_from(CustomerAdjustment)),
        "allocations": db.scalar(select(func.count()).select_from(InvoiceAllocation)),
        "allocation_requests": db.scalar(select(func.count()).select_from(AllocationRequest)),
        "entries": db.scalar(select(func.count()).select_from(JournalEntry)),
        "lines": db.scalar(select(func.count()).select_from(JournalLine)),
    }


@pytest.fixture
def db(client):
    with client.app.state.session_factory() as session:
        yield session


def _payment_in(ar, inv, allocations, key="k"):
    return PaymentIn(customer_id=ar.customer["id"], payment_date=date(2026, 1, 20), amount=Decimal("100.00"),
                     deposit_account_id=ar.accounts["1000"], idempotency_key=key,
                     allocations=[AllocationIn(invoice_id=i, amount=Decimal(a)) for i, a in allocations])


def test_unexpected_error_during_posting_rolls_everything_back(ar, db, monkeypatch):
    inv = ar.issued()
    before = _counts(db)

    def boom(db_, entry):
        raise RuntimeError("simulated crash while posting")

    monkeypatch.setattr(services, "post_entry", boom)
    with pytest.raises(RuntimeError):
        services.register_payment(db, _payment_in(ar, inv, [(inv["id"], "100.00")]))

    # the session is usable again and nothing was persisted, in this or any other session
    assert _counts(db) == before
    assert db.get(SalesInvoice, inv["id"]).amount_settled == D("0")
    assert ar.get_invoice(inv["id"])["status"] == "issued"
    assert ar.get_invoice(inv["id"])["allocations"] == []

    monkeypatch.undo()
    payment, created = services.register_payment(db, _payment_in(ar, inv, [(inv["id"], "100.00")]))
    assert created and payment.amount_allocated == D("100.00")
    assert ar.get_invoice(inv["id"])["status"] == "paid"


def test_failure_on_second_invoice_undoes_first_invoice_settlement(ar, db):
    a = ar.issued("A", lines=[ar.line("a", "1", "50.00")])
    b = ar.issued("B", lines=[ar.line("b", "1", "20.00")])
    before = _counts(db)
    with pytest.raises(services.OverAllocation) as exc_info:
        services.register_payment(db, _payment_in(ar, None, [(a["id"], "50.00"), (b["id"], "20.01")]))
    assert "exceeds the outstanding balance 20.00" in str(exc_info.value)

    assert _counts(db) == before
    assert ar.get_invoice(a["id"])["amount_settled"] == "0.00" and ar.get_invoice(a["id"])["status"] == "issued"
    assert ar.get_invoice(b["id"])["amount_settled"] == "0.00"
    assert ar.c.get(f"{API}/receivables/payments").json() == []
    assert ar.ledger("1000") == D("0")


def test_database_backstop_failure_is_rolled_back(ar, db, monkeypatch):
    """If the application check were bypassed, the allocation trigger still refuses and nothing sticks."""
    inv = ar.issued()  # 100.00
    pay = ar.payment("500.00")
    before = _counts(db)

    def unchecked_settle(db_, invoice_id, customer_id, amount):
        # Deliberately skip the balance check; the database trigger is the backstop.
        db_.execute(
            SalesInvoice.__table__.update()
            .where(SalesInvoice.id == invoice_id)
            .values(status="partially_paid")
        )

    monkeypatch.setattr(services, "_settle_invoice", unchecked_settle)
    with pytest.raises(services.ValidationFailed) as exc_info:
        services.allocate_payment(db, pay["id"], AllocationRequest_schema(allocations=[AllocationIn(invoice_id=inv["id"], amount=Decimal("100.01"))]))
    assert "allocation exceeds the invoice total" in str(exc_info.value)

    assert _counts(db) == before
    fresh = ar.get_invoice(inv["id"])
    assert fresh["status"] == "issued" and fresh["amount_settled"] == "0.00"
    assert ar.c.get(f"{API}/receivables/payments/{pay['id']}").json()["amount_allocated"] == "0.00"


def test_api_request_failing_mid_way_leaves_no_trace(ar, client, monkeypatch):
    inv = ar.issued()
    with client.app.state.session_factory() as db:
        before = _counts(db)
    monkeypatch.setattr(services, "post_entry", lambda db_, entry: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):  # TestClient re-raises unhandled server errors
        ar.c.post(f"{API}/receivables/adjustments", json={
            "customer_id": ar.customer["id"], "kind": "write_off", "adjustment_date": "2026-02-01",
            "amount": "10.00", "reason": "x", "invoice_id": inv["id"]})
    monkeypatch.undo()
    with client.app.state.session_factory() as db:
        assert _counts(db) == before
    assert ar.get_invoice(inv["id"])["amount_settled"] == "0.00"
    assert ar.c.get(f"{API}/receivables/adjustments").json() == []


def test_invoice_update_failure_keeps_old_lines(ar, db):
    inv = ar.invoice(lines=[ar.line("a", "1", "10.00")])
    r = ar.c.put(f"{API}/receivables/invoices/{inv['id']}", json={
        "invoice_number": "INV-1", "invoice_date": "2026-01-11", "due_date": "2026-02-10",
        "lines": [ar.line("ok", "1", "5.00"), ar.line("bad", "1", "5.00", account=9999)]})
    assert r.status_code == 422
    fresh = ar.get_invoice(inv["id"])
    assert [l["description"] for l in fresh["lines"]] == ["a"] and fresh["total"] == "10.00"


def test_service_replay_after_failure_is_consistent(ar, db):
    """A failed attempt does not 'use up' the idempotency key; the retry succeeds."""
    inv = ar.issued(lines=[ar.line("w", "1", "80.00")])
    data = _payment_in(ar, inv, [(inv["id"], "80.01")], key="retry")
    with pytest.raises(services.OverAllocation):
        services.register_payment(db, data)
    data = _payment_in(ar, inv, [(inv["id"], "80.00")], key="retry")
    payment, created = services.register_payment(db, data)
    assert created and payment.idempotency_key == "retry" and payment.unallocated_amount == D("20.00")
    again, created_again = services.register_payment(db, data)
    assert not created_again and again.id == payment.id
    assert db.scalar(select(func.count()).select_from(CustomerPayment)) == 1


def test_adjustment_in_schema_requires_invoice_for_write_off():
    with pytest.raises(ValueError):
        AdjustmentIn(customer_id=1, kind="write_off", adjustment_date=date(2026, 1, 1), amount=Decimal("1"), reason="r")


def test_rejected_allocation_batch_leaves_no_request_row(ar, db):
    """A batch is all-or-nothing, including the idempotency bookkeeping."""
    a = ar.issued("A", lines=[ar.line("a", "1", "50.00")])
    b = ar.issued("B", lines=[ar.line("b", "1", "20.00")])
    pay = ar.payment("100.00")
    before = _counts(db)

    r = ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations", json={
        "idempotency_key": "batch-1",
        "allocations": [{"invoice_id": a["id"], "amount": "50.00"}, {"invoice_id": b["id"], "amount": "20.01"}]})
    assert r.status_code == 422, r.text

    assert _counts(db) == before
    assert ar.get_invoice(a["id"])["amount_settled"] == "0.00"
    assert ar.c.get(f"{API}/receivables/payments/{pay['id']}").json()["amount_allocated"] == "0.00"

    # the key was not consumed by the failed attempt
    fixed = ar.allocate(pay["id"], [(a["id"], "50.00"), (b["id"], "20.00")], key="batch-1")
    assert fixed["amount_allocated"] == "70.00"
    assert ar.allocate(pay["id"], [(a["id"], "50.00"), (b["id"], "20.00")], key="batch-1", expect=200)["amount_allocated"] == "70.00"


def test_crash_during_allocation_rolls_back_the_request_row(ar, db, monkeypatch):
    inv = ar.issued()
    pay = ar.payment("100.00")
    before = _counts(db)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash while allocating")

    monkeypatch.setattr(services, "_settle_invoice", boom)
    with pytest.raises(RuntimeError):
        services.allocate_payment(db, pay["id"], AllocationRequest_schema(
            idempotency_key="crash", allocations=[AllocationIn(invoice_id=inv["id"], amount=Decimal("10.00"))]))
    monkeypatch.undo()

    assert _counts(db) == before
    assert ar.get_invoice(inv["id"])["amount_settled"] == "0.00"
    payment, created = services.allocate_payment(db, pay["id"], AllocationRequest_schema(
        idempotency_key="crash", allocations=[AllocationIn(invoice_id=inv["id"], amount=Decimal("10.00"))]))
    assert created and payment.amount_allocated == Decimal("10.00")
