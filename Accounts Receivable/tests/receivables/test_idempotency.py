"""Idempotency keys behave the same way for payments, adjustments and allocations.

The rule under test, everywhere: retrying the *identical* request replays the
original result (``200``, nothing changes); the same key with *any* other
request is rejected as a conflict (``409``) and changes nothing either.
"""

import pytest
from sqlalchemy import func, select

from app.modules.receivables.models import AllocationRequest, CustomerPayment, InvoiceAllocation
from tests.receivables.conftest import API


def _payment_body(ar, key="k1", **overrides):
    return {
        "customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "100.00",
        "deposit_account_id": ar.accounts["1000"], "reference": "CHQ-1", "idempotency_key": key,
        "allocations": [],
    } | overrides


def _adjustment_body(ar, key="a1", **overrides):
    return {
        "customer_id": ar.customer["id"], "kind": "credit", "adjustment_date": "2026-01-25",
        "amount": "25.00", "reason": "goodwill", "invoice_id": None, "idempotency_key": key,
    } | overrides


def _counts(db):
    return (
        db.scalar(select(func.count()).select_from(CustomerPayment)),
        db.scalar(select(func.count()).select_from(InvoiceAllocation)),
        db.scalar(select(func.count()).select_from(AllocationRequest)),
    )


# --- Payments -------------------------------------------------------------------------


def test_payment_replay_ignores_only_formatting(ar):
    """The same money written differently is the same request, not a conflict."""
    inv = ar.issued()
    first = ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, amount="100.00", allocations=[{"invoice_id": inv["id"], "amount": "60.00"}]))
    assert first.status_code == 201
    for amount, allocation in (("100", "60"), ("100.0", "60.000"), ("100.00", "60.00")):
        again = ar.c.post(f"{API}/receivables/payments", json=_payment_body(
            ar, amount=amount, allocations=[{"invoice_id": inv["id"], "amount": allocation}]))
        assert again.status_code == 200, again.text
        assert again.json()["id"] == first.json()["id"]
    assert len(ar.c.get(f"{API}/receivables/payments").json()) == 1
    assert ar.get_invoice(inv["id"])["amount_settled"] == "60.00"


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "100.01"},
        {"payment_date": "2026-01-21"},
        {"reference": "CHQ-2"},
        {"reference": None},
    ],
)
def test_payment_key_reused_with_a_different_field_is_a_conflict(ar, client, change):
    ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar))
    with client.app.state.session_factory() as db:
        before = _counts(db)
    r = ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, **change))
    assert r.status_code == 409, r.text
    assert "different payment" in r.json()["detail"]
    with client.app.state.session_factory() as db:
        assert _counts(db) == before


def test_payment_key_reused_with_different_allocations_is_a_conflict(ar, client):
    a = ar.issued("A")
    b = ar.issued("B")
    original = [{"invoice_id": a["id"], "amount": "40.00"}]
    assert ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, allocations=original)).status_code == 201
    for allocations in (
        [{"invoice_id": a["id"], "amount": "50.00"}],                                   # other amount
        [{"invoice_id": b["id"], "amount": "40.00"}],                                   # other invoice
        [{"invoice_id": a["id"], "amount": "40.00"}, {"invoice_id": b["id"], "amount": "10.00"}],  # extra line
        [],                                                                              # none at all
    ):
        r = ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, allocations=allocations))
        assert r.status_code == 409, (allocations, r.text)
    assert ar.get_invoice(a["id"])["amount_settled"] == "40.00"
    assert ar.get_invoice(b["id"])["amount_settled"] == "0.00"
    with client.app.state.session_factory() as db:
        assert _counts(db)[0] == 1


def test_allocations_in_another_order_are_the_same_request(ar):
    a = ar.issued("A")
    b = ar.issued("B")
    pair = [{"invoice_id": a["id"], "amount": "10.00"}, {"invoice_id": b["id"], "amount": "20.00"}]
    first = ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, allocations=pair))
    assert first.status_code == 201
    again = ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, allocations=list(reversed(pair))))
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]


# --- Allocation batches -----------------------------------------------------------------


def test_allocation_replay_is_identical_and_changes_nothing(ar):
    inv = ar.issued()
    pay = ar.payment("100.00")
    first = ar.allocate(pay["id"], [(inv["id"], "20.00")], key="alloc-1")
    for _ in range(3):
        again = ar.allocate(pay["id"], [(inv["id"], "20.00")], key="alloc-1", expect=200)
        assert again["allocations"] == first["allocations"]
        assert again["amount_allocated"] == "20.00"
    assert ar.get_invoice(inv["id"])["amount_settled"] == "20.00"
    assert len(ar.get_invoice(inv["id"])["allocations"]) == 1


def test_same_allocation_key_with_a_different_amount_is_a_conflict(ar):
    """The reported case: 20.00 under a key, then 25.00 under the same key."""
    inv = ar.issued()
    pay = ar.payment("100.00")
    ar.allocate(pay["id"], [(inv["id"], "20.00")], key="alloc-1")
    r = ar.allocate_raw(pay["id"], {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": inv["id"], "amount": "25.00"}]})
    assert r.status_code == 409, r.text
    assert "different allocation request" in r.json()["detail"]
    # neither the 20.00 nor anything else moved
    fresh = ar.get_invoice(inv["id"])
    assert fresh["amount_settled"] == "20.00" and len(fresh["allocations"]) == 1
    assert ar.c.get(f"{API}/receivables/payments/{pay['id']}").json()["amount_allocated"] == "20.00"
    # the identical request still replays
    assert ar.allocate(pay["id"], [(inv["id"], "20.00")], key="alloc-1", expect=200)["amount_allocated"] == "20.00"


def test_allocation_key_with_different_invoices_or_source_is_a_conflict(ar):
    a = ar.issued("A")
    b = ar.issued("B")
    pay = ar.payment("100.00", key="p1")
    other = ar.payment("100.00", key="p2")
    ar.allocate(pay["id"], [(a["id"], "10.00")], key="alloc-1")

    for body in (
        {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": b["id"], "amount": "10.00"}]},
        {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": a["id"], "amount": "10.00"}, {"invoice_id": b["id"], "amount": "5.00"}]},
        {"idempotency_key": "alloc-1", "effective_date": "2026-06-01", "allocations": [{"invoice_id": a["id"], "amount": "10.00"}]},
    ):
        assert ar.allocate_raw(pay["id"], body).status_code == 409, body
    # same key against a different payment
    assert ar.allocate_raw(other["id"], {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": a["id"], "amount": "10.00"}]}).status_code == 409
    # ... and against an adjustment
    credit = ar.adjustment("credit", "10.00")
    assert ar.allocate_raw(credit["id"], {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": a["id"], "amount": "10.00"}]}, kind="adjustments").status_code == 409

    assert ar.get_invoice(a["id"])["amount_settled"] == "10.00"
    assert ar.get_invoice(b["id"])["amount_settled"] == "0.00"
    assert ar.c.get(f"{API}/receivables/payments/{other['id']}").json()["amount_allocated"] == "0.00"


def test_allocation_amount_formatting_is_not_a_conflict(ar):
    inv = ar.issued()
    pay = ar.payment("100.00")
    ar.allocate(pay["id"], [(inv["id"], "20.00")], key="alloc-1")
    for amount in ("20", "20.0", "20.00"):
        assert ar.allocate_raw(pay["id"], {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": inv["id"], "amount": amount}]}).status_code == 200
    assert ar.get_invoice(inv["id"])["amount_settled"] == "20.00"


def test_credit_allocation_keys_follow_the_same_rule(ar):
    inv = ar.issued()
    credit = ar.adjustment("credit", "50.00")
    first = ar.allocate(credit["id"], [(inv["id"], "30.00")], key="c-1", kind="adjustments")
    assert ar.allocate(credit["id"], [(inv["id"], "30.00")], key="c-1", kind="adjustments", expect=200)["allocations"] == first["allocations"]
    assert ar.allocate_raw(credit["id"], {"idempotency_key": "c-1", "allocations": [{"invoice_id": inv["id"], "amount": "31.00"}]}, kind="adjustments").status_code == 409
    assert ar.get_invoice(inv["id"])["amount_settled"] == "30.00"


def test_allocation_without_a_key_is_never_treated_as_a_replay(ar):
    inv = ar.issued()
    pay = ar.payment("100.00")
    ar.allocate(pay["id"], [(inv["id"], "10.00")])
    ar.allocate(pay["id"], [(inv["id"], "10.00")])
    assert ar.get_invoice(inv["id"])["amount_settled"] == "20.00"


# --- Adjustments -------------------------------------------------------------------------


def test_adjustment_key_rules_match_payments(ar, client):
    first = ar.c.post(f"{API}/receivables/adjustments", json=_adjustment_body(ar, amount="25.00"))
    assert first.status_code == 201
    same = ar.c.post(f"{API}/receivables/adjustments", json=_adjustment_body(ar, amount="25"))
    assert same.status_code == 200 and same.json()["id"] == first.json()["id"]
    for change in ({"amount": "26.00"}, {"reason": "other"}, {"adjustment_date": "2026-01-26"}, {"kind": "write_off", "invoice_id": ar.issued("X")["id"]}):
        r = ar.c.post(f"{API}/receivables/adjustments", json=_adjustment_body(ar, **change))
        assert r.status_code == 409, (change, r.text)
    assert len(ar.c.get(f"{API}/receivables/adjustments").json()) == 1


def test_keys_are_scoped_per_operation(ar):
    """A payment, an adjustment and an allocation batch may each use the same key."""
    inv = ar.issued()
    pay = ar.c.post(f"{API}/receivables/payments", json=_payment_body(ar, key="shared"))
    assert pay.status_code == 201
    assert ar.c.post(f"{API}/receivables/adjustments", json=_adjustment_body(ar, key="shared")).status_code == 201
    assert ar.allocate(pay.json()["id"], [(inv["id"], "10.00")], key="shared")["amount_allocated"] == "10.00"
