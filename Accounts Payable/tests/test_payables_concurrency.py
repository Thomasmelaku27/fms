"""Truly concurrent requests against one database (SQLite and PostgreSQL).

Two HTTP requests race through the real stack in separate threads. Whatever the
interleaving, the invariants must hold: a credit is never over-applied, a bill
never over-settled, an invoice number never duplicated, an idempotent payment
never recorded twice. On PostgreSQL the ``FOR UPDATE`` locks serialise the
writers; on SQLite the single-writer lock does; the database triggers are the
backstop in both cases.
"""

import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.modules.payables.models import CreditApplication, PaymentAllocation, SupplierBill, VendorPayment

API = "/api/v1/payables"


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}


@pytest.fixture
def vendor(client, accounts):
    r = client.post(f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]})
    assert r.status_code == 201, r.text
    return r.json()


def _bill(client, vendor, accounts, number, total):
    r = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": number, "invoice_date": "2026-01-10", "due_date": "2026-02-09",
                                          "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _credit(client, vendor, accounts, total):
    r = client.post(f"{API}/credits", json={"vendor_id": vendor["id"], "credit_number": "CN", "credit_date": "2026-01-12", "total": total,
                                            "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": total}]})
    assert client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"}).status_code == 200
    return r.json()


def _race(requests):
    """Fire the given zero-argument callables at the same moment; return their responses."""
    barrier = threading.Barrier(len(requests))
    results = [None] * len(requests)

    def worker(index, fn):
        barrier.wait(timeout=10)
        results[index] = fn()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(r is not None for r in results), "a request did not finish"
    return results


def _count(client, model):
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_concurrent_credit_applications_never_over_apply(client, vendor, accounts):
    one = _bill(client, vendor, accounts, "B1", "100.00")
    two = _bill(client, vendor, accounts, "B2", "100.00")
    credit = _credit(client, vendor, accounts, "50.00")
    apply = lambda bill: (lambda: client.post(f"{API}/credits/{credit['id']}/applications",
                                              json={"bill_id": bill["id"], "amount": "30.00", "actor": "t", "application_date": "2026-01-20"}))
    responses = _race([apply(one), apply(two)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 422], [r.text for r in responses]
    assert "exceeds the remaining amount" in [r for r in responses if r.status_code == 422][0].json()["detail"]
    assert client.get(f"{API}/credits/{credit['id']}").json()["amount_applied"] == "30.00"
    assert _count(client, CreditApplication) == 1


def test_concurrent_payments_on_one_bill_never_over_settle(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    pay = lambda: client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-20", "amount": "70.00",
                                                       "cash_account_id": accounts["cash"], "recorded_by": "t",
                                                       "allocations": [{"bill_id": bill["id"], "amount": "70.00"}]})
    responses = _race([pay, pay])
    assert sorted(r.status_code for r in responses) == [201, 422], [r.text for r in responses]
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["amount_paid"], current["outstanding"]) == ("partially_paid", "70.00", "30.00")
    assert _count(client, VendorPayment) == 1 and _count(client, PaymentAllocation) == 1
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == 2  # approval + one payment


def test_concurrent_advance_applications_never_exceed_the_remainder(client, vendor, accounts):
    one = _bill(client, vendor, accounts, "B1", "100.00")
    two = _bill(client, vendor, accounts, "B2", "100.00")
    advance = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-15", "amount": "50.00",
                                                   "cash_account_id": accounts["cash"], "recorded_by": "t"}).json()
    apply = lambda bill: (lambda: client.post(f"{API}/payments/{advance['id']}/applications",
                                              json={"bill_id": bill["id"], "amount": "40.00", "actor": "t", "allocation_date": "2026-01-20"}))
    responses = _race([apply(one), apply(two)])
    assert sorted(r.status_code for r in responses) == [200, 422], [r.text for r in responses]
    assert client.get(f"{API}/payments/{advance['id']}").json()["unapplied_amount"] == "10.00"
    with Session(client.app.state.engine) as session:
        bills = session.scalars(select(SupplierBill)).all()
        assert sorted(b.outstanding for b in bills) == [60, 100]


def test_concurrent_duplicate_bill_submissions(client, vendor, accounts):
    payload = {"vendor_id": vendor["id"], "invoice_number": "INV-RACE", "invoice_date": "2026-01-10", "total": "10.00",
               "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "10.00"}]}
    submit = lambda variant: (lambda: client.post(f"{API}/bills", json={**payload, "invoice_number": variant}))
    responses = _race([submit("INV-RACE"), submit("inv-race"), submit(" INV-RACE ")])
    assert sorted(r.status_code for r in responses) == [201, 409, 409], [r.text for r in responses]
    assert len(client.get(f"{API}/bills").json()) == 1


def test_concurrent_idempotent_payment_submissions_record_once(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    payload = {"vendor_id": vendor["id"], "payment_date": "2026-01-20", "amount": "40.00", "cash_account_id": accounts["cash"],
               "recorded_by": "t", "idempotency_key": "race-key", "allocations": [{"bill_id": bill["id"], "amount": "40.00"}]}
    submit = lambda: client.post(f"{API}/payments", json=payload)
    responses = _race([submit, submit, submit])
    assert sorted(r.status_code for r in responses) == [200, 200, 201], [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert _count(client, VendorPayment) == 1
    assert client.get(f"{API}/bills/{bill['id']}").json()["amount_paid"] == "40.00"
