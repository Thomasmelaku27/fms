"""Concurrent requests: no duplicate payments, no over-allocation, no double posting.

Each worker thread uses its own session (and therefore its own connection) so
the conditional updates and unique constraints are exercised for real, on
SQLite (serialised writers) and PostgreSQL (row locks) alike.
"""

import threading
import pytest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.models import JournalEntry
from app.modules.receivables import services
from app.modules.receivables.models import (
    AllocationRequest as AllocationRequestRecord,
    CustomerPayment,
    InvoiceAllocation,
    SalesInvoice,
)
from app.modules.receivables.schemas import AllocationIn, AllocationRequest, PaymentIn
from tests.receivables.conftest import API, D

WORKERS = 6


def _run_concurrently(fn, n):
    """Run ``fn(i)`` in ``n`` threads that all start at the same moment; returns results/exceptions."""
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001 - collected and asserted on by the test
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))


def _payment_in(ar, key, allocations=(), amount="100.00"):
    return PaymentIn(customer_id=ar.customer["id"], payment_date=date(2026, 1, 20), amount=Decimal(amount),
                     deposit_account_id=ar.accounts["1000"], idempotency_key=key,
                     allocations=[AllocationIn(invoice_id=i, amount=Decimal(a)) for i, a in allocations])


def _invariants(db, invoice_id):
    """The running sums must always equal the allocation rows."""
    invoice = db.get(SalesInvoice, invoice_id)
    allocated = db.scalar(select(func.coalesce(func.sum(InvoiceAllocation.amount), 0)).where(InvoiceAllocation.invoice_id == invoice_id))
    assert invoice.amount_settled == D(allocated)
    assert invoice.amount_settled <= invoice.total
    for payment in db.scalars(select(CustomerPayment)):
        allocated = db.scalar(select(func.coalesce(func.sum(InvoiceAllocation.amount), 0)).where(InvoiceAllocation.payment_id == payment.id))
        assert payment.amount_allocated == D(allocated) <= payment.amount
    return invoice


def test_same_payment_sent_concurrently_is_recorded_once(ar, client):
    inv = ar.issued()
    factory = client.app.state.session_factory
    data = _payment_in(ar, "dup-key", [(inv["id"], "60.00")])

    def send(_):
        with factory() as db:
            payment, created = services.register_payment(db, data)
            return payment.id, created

    results = _run_concurrently(send, WORKERS)
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == [], errors
    assert sum(created for _, created in results) == 1
    assert len({payment_id for payment_id, _ in results}) == 1

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CustomerPayment)) == 1
        assert db.scalar(select(func.count()).select_from(JournalEntry)) == 2  # invoice + one payment
        invoice = _invariants(db, inv["id"])
        assert invoice.amount_settled == D("60.00") and invoice.status.value == "partially_paid"
    assert ar.ledger("1000") == D("100.00")  # one payment of 100.00 posted, 60.00 of it allocated


def test_concurrent_allocations_never_exceed_the_invoice_balance(ar, client):
    inv = ar.issued()  # 100.00 outstanding
    payments = [ar.payment("25.00", key=f"p{i}") for i in range(WORKERS)]  # more than 100.00 on account
    factory = client.app.state.session_factory

    def allocate(i):
        with factory() as db:
            services.allocate_payment(db, payments[i]["id"], AllocationRequest(
                allocations=[AllocationIn(invoice_id=inv["id"], amount=Decimal("25.00"))]))
            return "ok"

    results = _run_concurrently(allocate, WORKERS)
    failures = [r for r in results if isinstance(r, Exception)]
    # losers are refused either because the remainder is too small or because the invoice is already paid
    assert all(isinstance(f, (services.OverAllocation, services.InvalidState)) for f in failures), failures
    assert results.count("ok") == 4 and len(failures) == WORKERS - 4  # exactly 4 x 25.00 fit

    with factory() as db:
        invoice = _invariants(db, inv["id"])
        assert invoice.amount_settled == D("100.00") and invoice.status.value == "paid"
        assert db.scalar(select(func.count()).select_from(InvoiceAllocation)) == 4
    assert ar.balance()["outstanding"] == "0.00"
    assert D(ar.balance()["unapplied_credit"]) == D("25.00") * (WORKERS - 4)


def test_concurrent_allocations_of_one_payment_never_exceed_its_amount(ar, client):
    invoices = [ar.issued(f"I{i}", lines=[ar.line("x", "1", "40.00")]) for i in range(WORKERS)]
    pay = ar.payment("100.00")
    factory = client.app.state.session_factory

    def allocate(i):
        with factory() as db:
            services.allocate_payment(db, pay["id"], AllocationRequest(
                allocations=[AllocationIn(invoice_id=invoices[i]["id"], amount=Decimal("40.00"))]))
            return "ok"

    results = _run_concurrently(allocate, WORKERS)
    assert results.count("ok") == 2  # 80.00 of 100.00; a third 40.00 does not fit
    assert all(isinstance(r, services.OverAllocation) for r in results if r != "ok"), results
    with factory() as db:
        payment = db.get(CustomerPayment, pay["id"])
        assert payment.amount_allocated == D("80.00")
        for invoice in invoices:
            _invariants(db, invoice["id"])


def test_interleaved_allocations_to_shared_invoices_do_not_deadlock(ar, client):
    """Different payments allocating to the same invoices in different orders."""
    a = ar.issued("A", lines=[ar.line("a", "1", "1000.00")])
    b = ar.issued("B", lines=[ar.line("b", "1", "1000.00")])
    payments = [ar.payment("20.00", key=f"p{i}") for i in range(WORKERS)]
    factory = client.app.state.session_factory

    def allocate(i):
        order = [a, b] if i % 2 else [b, a]
        with factory() as db:
            services.allocate_payment(db, payments[i]["id"], AllocationRequest(
                allocations=[AllocationIn(invoice_id=order[0]["id"], amount=Decimal("10.00")),
                             AllocationIn(invoice_id=order[1]["id"], amount=Decimal("10.00"))]))
            return "ok"

    results = _run_concurrently(allocate, WORKERS)
    assert results == ["ok"] * WORKERS, results
    with factory() as db:
        assert _invariants(db, a["id"]).amount_settled == D("10.00") * WORKERS
        assert _invariants(db, b["id"]).amount_settled == D("10.00") * WORKERS


def test_concurrent_issue_posts_exactly_once(ar, client):
    inv = ar.invoice()
    factory = client.app.state.session_factory

    def issue(_):
        with factory() as db:
            return services.issue_invoice(db, inv["id"]).journal_entry_id

    results = _run_concurrently(issue, WORKERS)
    entry_ids = [r for r in results if not isinstance(r, Exception)]
    assert len(entry_ids) == 1
    assert all(isinstance(r, services.InvalidState) for r in results if isinstance(r, Exception))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(JournalEntry)) == 1
    assert ar.ledger("1200") == D("100.00")
    assert ar.get_invoice(inv["id"])["journal_entry_id"] == entry_ids[0]


def test_void_racing_with_a_payment(ar, client):
    """Either the void wins (payment allocation rejected) or the payment wins (void rejected)."""
    inv = ar.issued()
    factory = client.app.state.session_factory

    def void(_):
        with factory() as db:
            services.void_invoice(db, inv["id"], date(2026, 1, 20))
            return "void"

    def pay(_):
        with factory() as db:
            services.register_payment(db, _payment_in(ar, "race", [(inv["id"], "100.00")]))
            return "paid"

    results = _run_concurrently(lambda i: (void if i == 0 else pay)(i), 2)
    outcomes = sorted(r if isinstance(r, str) else type(r).__name__ for r in results)
    assert outcomes in (["InvalidState", "paid"], ["InvalidState", "void"]), results
    fresh = ar.get_invoice(inv["id"])
    if "void" in outcomes:
        assert fresh["status"] == "void" and fresh["amount_settled"] == "0.00"
        assert ar.c.get(f"{API}/receivables/payments").json() == []
    else:
        assert fresh["status"] == "paid" and fresh["void_journal_entry_id"] is None


def test_concurrent_api_requests_with_same_key(ar):
    """Through the HTTP layer: the same POST fired from several threads yields one payment."""
    inv = ar.issued()
    body = {"customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "100.00",
            "deposit_account_id": ar.accounts["1000"], "idempotency_key": "http-dup",
            "allocations": [{"invoice_id": inv["id"], "amount": "100.00"}]}

    def post(_):
        r = ar.c.post(f"{API}/receivables/payments", json=body)
        return r.status_code, r.json().get("id")

    results = _run_concurrently(post, 4)
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == [], errors
    assert sorted(code for code, _ in results) == [200, 200, 200, 201]
    assert len({payment_id for _, payment_id in results}) == 1
    assert len(ar.c.get(f"{API}/receivables/payments").json()) == 1
    assert ar.get_invoice(inv["id"])["status"] == "paid"
    assert ar.get_invoice(inv["id"])["amount_settled"] == "100.00"


def test_distinct_concurrent_payments_are_all_recorded(ar, client):
    inv = ar.issued(lines=[ar.line("x", "1", str(100 * WORKERS))])
    factory = client.app.state.session_factory

    def send(i):
        with factory() as db:
            payment, created = services.register_payment(db, _payment_in(ar, f"distinct-{i}", [(inv["id"], "100.00")]))
            assert created
            return payment.id

    results = _run_concurrently(send, WORKERS)
    assert all(not isinstance(r, Exception) for r in results), results
    assert len(set(results)) == WORKERS
    with factory() as db:
        invoice = _invariants(db, inv["id"])
        assert invoice.amount_settled == D(100 * WORKERS) and invoice.status.value == "paid"
    assert ar.ledger("1000") == D(100 * WORKERS) and ar.ledger("1200") == D("0")


# --- Idempotent allocation batches -------------------------------------------------------


def _allocation_request(inv_id, amount, key, effective_date=None):
    return AllocationRequest(idempotency_key=key, effective_date=effective_date,
                             allocations=[AllocationIn(invoice_id=inv_id, amount=Decimal(amount))])


def test_same_allocation_request_sent_concurrently_is_applied_once(ar, client):
    inv = ar.issued()
    pay = ar.payment("100.00")
    factory = client.app.state.session_factory

    def send(_):
        with factory() as db:
            payment, created = services.allocate_payment(db, pay["id"], _allocation_request(inv["id"], "40.00", "dup"))
            return payment.amount_allocated, created

    results = _run_concurrently(send, WORKERS)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert sum(created for _, created in results) == 1
    assert {str(allocated) for allocated, _ in results} == {"40.00"}

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceAllocation)) == 1
        assert db.scalar(select(func.count()).select_from(AllocationRequestRecord)) == 1
        assert _invariants(db, inv["id"]).amount_settled == D("40.00")


def test_concurrent_requests_reusing_a_key_with_different_payloads(ar, client):
    """Exactly one payload wins; every other request is rejected, not silently replayed."""
    inv = ar.issued()
    pay = ar.payment("100.00")
    factory = client.app.state.session_factory
    amounts = [Decimal(10 + i) for i in range(WORKERS)]

    def send(i):
        with factory() as db:
            payment, created = services.allocate_payment(db, pay["id"], _allocation_request(inv["id"], amounts[i], "same-key"))
            return payment.amount_allocated, created

    results = _run_concurrently(send, WORKERS)
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1 and winners[0][1] is True
    assert all(isinstance(r, services.IdempotencyConflict) for r in losers), losers

    with factory() as db:
        allocations = list(db.scalars(select(InvoiceAllocation)))
        assert len(allocations) == 1
        assert allocations[0].amount in amounts
        assert _invariants(db, inv["id"]).amount_settled == allocations[0].amount


def test_failed_allocation_does_not_consume_the_key(ar, client):
    """A rejected request leaves no trace, so the corrected retry may reuse the key."""
    inv = ar.issued()  # 100.00
    pay = ar.payment("500.00")
    factory = client.app.state.session_factory
    with factory() as db:
        with pytest.raises(services.OverAllocation):
            services.allocate_payment(db, pay["id"], _allocation_request(inv["id"], "100.01", "retry-key"))
        assert db.scalar(select(func.count()).select_from(AllocationRequestRecord)) == 0
        assert db.scalar(select(func.count()).select_from(InvoiceAllocation)) == 0

        payment, created = services.allocate_payment(db, pay["id"], _allocation_request(inv["id"], "100.00", "retry-key"))
        assert created and payment.amount_allocated == D("100.00")
        replay, created_again = services.allocate_payment(db, pay["id"], _allocation_request(inv["id"], "100.00", "retry-key"))
        assert not created_again and replay.amount_allocated == D("100.00")
        assert db.scalar(select(func.count()).select_from(InvoiceAllocation)) == 1
