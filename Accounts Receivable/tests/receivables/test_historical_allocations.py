"""Allocations carry an effective date; "as of" reports only count what was in force then.

Applying an old payment today must not change what the aging report, the
customer balance or a statement said about an earlier date.
"""

from datetime import date, timedelta

from tests.receivables.conftest import API, D

TODAY = date.today()
PRIOR = "2026-02-20"  # after the invoice is due, before any late allocation


def _scene(ar):
    """An invoice from January and a payment received in January, not yet applied."""
    inv = ar.issued(lines=[ar.line("w", "1", "100.00")], invoice_date="2026-01-10", due_date="2026-02-09")
    pay = ar.payment("100.00", payment_date="2026-01-20", key="p1")
    return inv, pay


def _snapshot(ar, as_of):
    return {
        "balance": ar.balance(as_of=as_of),
        "aging": ar.aging(as_of=as_of),
        "overdue": ar.overdue(as_of=as_of),
    }


def test_late_allocation_leaves_earlier_reports_untouched(ar):
    inv, pay = _scene(ar)
    before = _snapshot(ar, PRIOR)
    assert before["balance"]["outstanding"] == "100.00"
    assert before["balance"]["unapplied_credit"] == "100.00"
    assert before["balance"]["balance"] == "0.00"
    assert [i["invoice_number"] for i in before["overdue"]["invoices"]] == ["INV-1"]

    ar.allocate(pay["id"], [(inv["id"], "100.00")], key="late")  # effective today by default

    assert _snapshot(ar, PRIOR) == before  # the past is exactly as it was reported
    today = ar.balance(as_of=TODAY.isoformat())
    assert today["outstanding"] == "0.00" and today["unapplied_credit"] == "0.00"
    assert ar.aging(as_of=TODAY.isoformat())["rows"] == []
    assert ar.overdue(as_of=TODAY.isoformat())["invoices"] == []
    # the invoice itself is settled now, whichever date the report asks about
    assert ar.get_invoice(inv["id"])["status"] == "paid"


def test_allocation_records_both_dates(ar):
    inv, pay = _scene(ar)
    allocated = ar.allocate(pay["id"], [(inv["id"], "40.00")], key="late")
    row = allocated["allocations"][0]
    assert row["allocation_date"] == "2026-01-20"      # settlement date of the documents
    assert row["effective_date"] == TODAY.isoformat()  # when it entered the books
    assert ar.get_invoice(inv["id"])["allocations"][0]["effective_date"] == TODAY.isoformat()


def test_allocation_made_with_the_payment_takes_effect_with_it(ar):
    """Nothing changes for allocations captured when the payment is registered."""
    inv = ar.issued(lines=[ar.line("w", "1", "100.00")], invoice_date="2026-01-10", due_date="2026-02-09")
    pay = ar.payment("100.00", [(inv["id"], "100.00")], payment_date="2026-01-20")
    assert pay["allocations"][0]["effective_date"] == "2026-01-20"
    assert ar.balance(as_of="2026-01-25")["outstanding"] == "0.00"
    assert ar.balance(as_of=PRIOR)["balance"] == "0.00"
    assert ar.overdue(as_of=PRIOR)["invoices"] == []


def test_explicit_effective_date_is_honoured(ar):
    inv, pay = _scene(ar)
    ar.allocate(pay["id"], [(inv["id"], "100.00")], key="late", effective_date="2026-03-01")
    assert ar.balance(as_of="2026-02-28")["outstanding"] == "100.00"
    assert ar.balance(as_of="2026-03-01")["outstanding"] == "0.00"
    assert ar.aging(as_of="2026-02-28")["totals"]["total_outstanding"] == "100.00"
    assert ar.aging(as_of="2026-03-01")["rows"] == []


def test_effective_date_cannot_precede_the_documents(ar):
    inv, pay = _scene(ar)  # invoice 2026-01-10, payment 2026-01-20
    for bad in ("2026-01-19", "2026-01-05", "2025-12-31"):
        r = ar.allocate_raw(pay["id"], {"idempotency_key": None, "effective_date": bad,
                                        "allocations": [{"invoice_id": inv["id"], "amount": "10.00"}]})
        assert r.status_code == 422, (bad, r.text)
        assert "precedes" in r.json()["detail"]
    assert ar.get_invoice(inv["id"])["amount_settled"] == "0.00"
    # the earliest acceptable date is the later of the two documents
    assert ar.allocate(pay["id"], [(inv["id"], "10.00")], effective_date="2026-01-20")["amount_allocated"] == "10.00"

    later_invoice = ar.issued("LATER", lines=[ar.line("x", "1", "10.00")], invoice_date="2026-05-01", due_date="2026-06-01")
    r = ar.allocate_raw(pay["id"], {"effective_date": "2026-04-30", "allocations": [{"invoice_id": later_invoice["id"], "amount": "10.00"}]})
    assert r.status_code == 422 and "2026-05-01" in r.json()["detail"]


def test_replay_does_not_move_the_allocation_in_time(ar, client, monkeypatch):
    """A retry the next day returns the original effective date instead of re-resolving it."""
    inv, pay = _scene(ar)
    first = ar.allocate(pay["id"], [(inv["id"], "50.00")], key="late")
    assert first["allocations"][0]["effective_date"] == TODAY.isoformat()

    from app.modules.receivables import services

    class _Tomorrow(date):
        @classmethod
        def today(cls):
            return TODAY + timedelta(days=1)

    monkeypatch.setattr(services, "date", _Tomorrow)
    replay = ar.allocate(pay["id"], [(inv["id"], "50.00")], key="late", expect=200)
    assert replay["allocations"][0]["effective_date"] == TODAY.isoformat()
    assert replay["amount_allocated"] == "50.00"


def test_balance_matches_the_posted_ledger_at_every_date(ar):
    """outstanding - unapplied credit equals the receivables account, before and after."""
    inv, pay = _scene(ar)
    dates = ["2026-01-15", "2026-01-25", PRIOR, TODAY.isoformat()]
    for as_of in dates:
        assert D(ar.balance(as_of=as_of)["balance"]) == ar.ledger_as_of("1200", as_of), as_of
    ar.allocate(pay["id"], [(inv["id"], "100.00")], key="late")
    for as_of in dates:
        assert D(ar.balance(as_of=as_of)["balance"]) == ar.ledger_as_of("1200", as_of), as_of


def test_late_credit_note_allocation_also_uses_its_effective_date(ar):
    inv = ar.issued(lines=[ar.line("w", "1", "100.00")], invoice_date="2026-01-10", due_date="2026-02-09")
    credit = ar.adjustment("credit", "40.00", adjustment_date="2026-01-15", reason="goodwill")
    before = _snapshot(ar, PRIOR)
    assert before["balance"]["unapplied_credit"] == "40.00"
    ar.allocate(credit["id"], [(inv["id"], "40.00")], key="c-late", kind="adjustments")
    assert _snapshot(ar, PRIOR) == before
    assert ar.balance(as_of=TODAY.isoformat())["outstanding"] == "60.00"


def test_aging_buckets_use_the_effective_date(ar):
    """A late allocation must not retro-empty an old bucket."""
    inv = ar.issued(lines=[ar.line("w", "1", "100.00")], invoice_date="2025-09-01", due_date="2025-09-15")
    pay = ar.payment("100.00", payment_date="2025-10-01", key="p1")
    old = ar.aging(as_of="2025-11-01")
    assert old["rows"][0]["buckets"] == ["0.00", "100.00", "0.00", "0.00"]  # 47 days overdue
    ar.allocate(pay["id"], [(inv["id"], "100.00")], key="late")
    assert ar.aging(as_of="2025-11-01") == old
    assert ar.aging(as_of=TODAY.isoformat())["rows"] == []


def test_statement_is_unaffected_by_when_money_is_applied(ar):
    """Statements list documents, so allocating later never rewrites their lines."""
    inv, pay = _scene(ar)
    statement = ar.c.get(f"{API}/receivables/customers/{ar.customer['id']}/statement", params={"end_date": PRIOR}).json()
    ar.allocate(pay["id"], [(inv["id"], "100.00")], key="late")
    assert ar.c.get(f"{API}/receivables/customers/{ar.customer['id']}/statement", params={"end_date": PRIOR}).json() == statement
    assert statement["closing_balance"] == "0.00"
