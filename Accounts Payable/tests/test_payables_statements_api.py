"""Payment reversal / correction, supplier statements, reconciliation and as-of
consistency of outstanding / aging reports (SQLite and PostgreSQL).

Ends with the end-to-end scenario: bill approval -> partial payment -> credit
adjustment -> final settlement, checked against the ledger at every step.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JournalEntry, JournalLine
from app.modules.payables.models import PaymentAllocation, VendorPayment

API = "/api/v1/payables"


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {
        "ap": _account(client, "2000", "liability"),
        "ap2": _account(client, "2010", "liability"),
        "expense": _account(client, "6000", "expense"),
        "cash": _account(client, "1000", "asset"),
        "cash2": _account(client, "1010", "asset"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]})
    assert r.status_code == 201, r.text
    return r.json()


def _bill(client, vendor, accounts, number, total, invoice_date, due_date=None, payable_account_id=None):
    payload = {
        "vendor_id": vendor["id"],
        "invoice_number": number,
        "invoice_date": invoice_date,
        "total": total,
        "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}],
    }
    if due_date:
        payload["due_date"] = due_date
    if payable_account_id:
        payload["payable_account_id"] = payable_account_id
    r = client.post(f"{API}/bills", json=payload)
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _pay(client, vendor, accounts, allocations, date_, amount=None, **extra):
    amount = amount or str(sum(Decimal(a["amount"]) for a in allocations))
    r = client.post(
        f"{API}/payments",
        json={"vendor_id": vendor["id"], "payment_date": date_, "amount": amount, "cash_account_id": accounts["cash"],
              "recorded_by": "bob", "allocations": allocations, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _credit(client, vendor, accounts, number, total, date_, apply_to=None, apply_date=None):
    r = client.post(
        f"{API}/credits",
        json={"vendor_id": vendor["id"], "credit_number": number, "credit_date": date_, "total": total,
              "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": total}]},
    )
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    credit = a.json()
    if apply_to is not None:
        ap = client.post(f"{API}/credits/{credit['id']}/applications",
                         json={"bill_id": apply_to, "amount": total, "actor": "bob", "application_date": apply_date or date_})
        assert ap.status_code == 200, ap.text
        credit = ap.json()
    return credit


def _statement(client, vendor, date_from, date_to):
    r = client.get(f"{API}/vendors/{vendor['id']}/statement?date_from={date_from}&date_to={date_to}")
    assert r.status_code == 200, r.text
    return r.json()


def _outstanding(client, as_of, vendor_id=None):
    q = f"as_of={as_of}" + (f"&vendor_id={vendor_id}" if vendor_id else "")
    return client.get(f"{API}/outstanding?{q}").json()


def _recon(client, as_of):
    return client.get(f"{API}/reconciliation?as_of={as_of}").json()


def _journal(client):
    return client.get("/api/v1/journal-entries").json()


def _count(client, model):
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


# --- payment reversal ---------------------------------------------------------------------------


def test_reverse_payment_posts_mirror_entry_and_restores_bills(client, vendor, accounts):
    one = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    two = _bill(client, vendor, accounts, "B2", "40.00", "2026-01-10", payable_account_id=accounts["ap2"])
    pay = _pay(client, vendor, accounts, [{"bill_id": one["id"], "amount": "60.00"}, {"bill_id": two["id"], "amount": "40.00"}],
               "2026-01-20", reference="RUN-7")
    assert client.get(f"{API}/bills/{two['id']}").json()["status"] == "paid"

    r = client.post(f"{API}/payments/{pay['id']}/reverse",
                    json={"actor": "carol", "reason": "Bounced", "reversal_date": "2026-01-25"})
    assert r.status_code == 200, r.text
    reversed_ = r.json()
    assert reversed_["status"] == "reversed"
    assert (reversed_["reversed_by"], reversed_["reversal_date"], reversed_["reversal_reason"]) == ("carol", "2026-01-25", "Bounced")
    assert reversed_["reversed_at"] is not None
    # Original allocations are preserved (ids, bills, amounts) but no longer in force.
    assert [(a["id"], a["bill_id"], a["amount"]) for a in reversed_["allocations"]] == [
        (a["id"], a["bill_id"], a["amount"]) for a in pay["allocations"]
    ]
    assert all(not a["is_active"] for a in reversed_["allocations"])
    assert reversed_["journal_entry_id"] == pay["journal_entry_id"]

    original = client.get(f"/api/v1/journal-entries/{pay['journal_entry_id']}").json()
    reversal = client.get(f"/api/v1/journal-entries/{reversed_['reversal_journal_entry_id']}").json()
    assert original["status"] == "posted" and original["lines"] == original["lines"]
    assert reversal["status"] == "posted"
    assert reversal["entry_date"] == "2026-01-25"
    assert reversal["reference"] == f"AP-PAY-REV-{pay['id']}"
    assert reversal["description"].startswith("Reversal of vendor payment RUN-7")
    assert [(l["account_id"], l["debit"], l["credit"]) for l in reversal["lines"]] == [
        (accounts["ap"], "0.00", "60.00"),
        (accounts["ap2"], "0.00", "40.00"),
        (accounts["cash"], "100.00", "0.00"),
    ]

    for bill, expected in ((one, ("approved", "100.00")), (two, ("approved", "40.00"))):
        current = client.get(f"{API}/bills/{bill['id']}").json()
        assert (current["status"], current["outstanding"], current["amount_paid"]) == (*expected, "0.00")
    assert client.get(f"{API}/payments?status=reversed").json()[0]["id"] == pay["id"]
    assert client.get(f"{API}/payments?status=recorded").json() == []
    # Trial balance: cash and AP back where they were after the approvals.
    rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert rows["1000"]["debit"] == rows["1000"]["credit"] == "0.00"
    assert rows["2000"]["credit"] == "100.00" and rows["2010"]["credit"] == "40.00"


def test_reversal_date_validation(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    pay = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "60.00"}], "2026-01-20")
    r = client.post(f"{API}/payments/{pay['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-01-19"})
    assert r.status_code == 422 and "cannot precede" in r.json()["detail"]
    assert client.post(f"{API}/payments/{pay['id']}/reverse", json={"actor": "c"}).status_code == 422
    assert client.post(f"{API}/payments/999/reverse", json={"actor": "c", "reason": "r"}).status_code == 404
    assert client.get(f"{API}/payments/{pay['id']}").json()["status"] == "recorded"


def test_reversed_payment_can_be_re_recorded_and_bill_settled(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    pay = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "100.00"}], "2026-01-20")
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    assert client.post(f"{API}/payments/{pay['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-01-21"}).status_code == 200
    again = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "100.00"}], "2026-01-22")
    assert again["status"] == "recorded"
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    assert _count(client, VendorPayment) == 2 and _count(client, PaymentAllocation) == 2


# --- payment correction --------------------------------------------------------------------------


def test_correct_payment_reverses_and_replaces_atomically(client, vendor, accounts):
    one = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    two = _bill(client, vendor, accounts, "B2", "50.00", "2026-01-10")
    wrong = _pay(client, vendor, accounts, [{"bill_id": one["id"], "amount": "50.00"}], "2026-01-20", reference="CHQ-1")

    r = client.post(
        f"{API}/payments/{wrong['id']}/correct",
        json={
            "actor": "carol",
            "reason": "Allocated to the wrong bill and bank",
            "reversal_date": "2026-01-22",
            "replacement": {
                "vendor_id": vendor["id"], "payment_date": "2026-01-22", "amount": "50.00", "method": "cheque",
                "reference": "CHQ-1", "cash_account_id": accounts["cash2"], "recorded_by": "carol",
                "allocations": [{"bill_id": two["id"], "amount": "50.00"}],
            },
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversed"]["id"] == wrong["id"] and body["reversed"]["status"] == "reversed"
    replacement = body["replacement"]
    assert replacement["status"] == "recorded"
    assert replacement["corrects_payment_id"] == wrong["id"]
    assert replacement["cash_account_id"] == accounts["cash2"]
    assert replacement["journal_entry_id"] not in (wrong["journal_entry_id"], body["reversed"]["reversal_journal_entry_id"])

    assert client.get(f"{API}/bills/{one['id']}").json()["outstanding"] == "100.00"
    assert client.get(f"{API}/bills/{two['id']}").json()["status"] == "paid"
    assert client.get(f"{API}/payments/{replacement['id']}").json() == replacement

    entries = _journal(client)
    assert [e["reference"] for e in entries] == ["AP-BILL-1", "AP-BILL-2", f"AP-PAY-{wrong['id']}",
                                                f"AP-PAY-REV-{wrong['id']}", f"AP-PAY-{replacement['id']}"]
    assert all(e["status"] == "posted" for e in entries)
    rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert rows["1000"]["credit"] == "0.00" and rows["1010"]["credit"] == "50.00"
    assert rows["2000"]["credit"] == "100.00"


def test_replacement_cannot_predate_the_reversal(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    wrong = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "50.00"}], "2026-01-20")
    r = client.post(
        f"{API}/payments/{wrong['id']}/correct",
        json={"actor": "c", "reason": "r", "reversal_date": "2026-01-25",
              "replacement": {"vendor_id": vendor["id"], "payment_date": "2026-01-20", "amount": "50.00",
                              "cash_account_id": accounts["cash"], "recorded_by": "c",
                              "allocations": [{"bill_id": bill["id"], "amount": "50.00"}]}},
    )
    assert r.status_code == 422 and "cannot be dated before the reversal" in r.json()["detail"]
    assert client.get(f"{API}/payments/{wrong['id']}").json()["status"] == "recorded"
    assert len(_journal(client)) == 2


# --- statements --------------------------------------------------------------------------------


def test_vendor_statement_with_opening_balance_and_all_transaction_types(client, vendor, accounts):
    old = _bill(client, vendor, accounts, "OLD", "200.00", "2025-12-01")
    _pay(client, vendor, accounts, [{"bill_id": old["id"], "amount": "50.00"}], "2025-12-15")
    jan = _bill(client, vendor, accounts, "JAN", "100.00", "2026-01-05", due_date="2026-02-04")
    p = _pay(client, vendor, accounts, [{"bill_id": jan["id"], "amount": "30.00"}], "2026-01-10", reference="P-30")
    _credit(client, vendor, accounts, "CN-1", "20.00", "2026-01-12", apply_to=jan["id"], apply_date="2026-01-13")
    assert client.post(f"{API}/payments/{p['id']}/reverse",
                       json={"actor": "c", "reason": "bounced", "reversal_date": "2026-01-15"}).status_code == 200
    stray = _credit(client, vendor, accounts, "CN-2", "5.00", "2026-01-18")
    assert client.post(f"{API}/credits/{stray['id']}/reverse",
                       json={"actor": "c", "reason": "duplicate", "reversal_date": "2026-01-19"}).status_code == 200
    _pay(client, vendor, accounts, [{"bill_id": jan["id"], "amount": "80.00"}], "2026-01-25", reference="P-80")
    _bill(client, vendor, accounts, "FEB", "999.00", "2026-02-03")  # outside the period
    unapproved = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "DRAFT",
                                                   "invoice_date": "2026-01-20", "total": "1.00"})
    assert unapproved.status_code == 201  # drafts never appear on statements

    statement = _statement(client, vendor, "2026-01-01", "2026-01-31")
    assert statement["opening_balance"] == "150.00"  # 200 - 50 before January
    assert [(l["date"], l["type"], l["reference"], l["debit"], l["credit"], l["balance"]) for l in statement["lines"]] == [
        ("2026-01-05", "bill", "JAN", "0.00", "100.00", "250.00"),
        ("2026-01-10", "payment", "P-30", "30.00", "0.00", "220.00"),
        ("2026-01-12", "credit", "CN-1", "20.00", "0.00", "200.00"),
        ("2026-01-15", "payment_reversal", "P-30", "0.00", "30.00", "230.00"),
        ("2026-01-18", "credit", "CN-2", "5.00", "0.00", "225.00"),
        ("2026-01-19", "credit_reversal", "CN-2", "0.00", "5.00", "230.00"),
        ("2026-01-25", "payment", "P-80", "80.00", "0.00", "150.00"),
    ]
    assert statement["totals"] == {"bills": "100.00", "credits": "25.00", "payments": "110.00",
                                   "discounts": "0.00", "payment_reversals": "30.00",
                                   "credit_reversals": "5.00", "discount_reversals": "0.00"}
    assert statement["closing_balance"] == "150.00"
    # Reconciles with the open-item view: OLD 150 outstanding, JAN 0 outstanding (20 credit + 80 paid).
    assert statement["outstanding_bills"] == "150.00"
    assert statement["unapplied_credits"] == "0.00"
    assert statement["difference"] == "0.00"
    assert all(l["journal_entry_id"] is not None for l in statement["lines"])

    # A later window starts from the January closing balance.
    feb = _statement(client, vendor, "2026-02-01", "2026-02-28")
    assert feb["opening_balance"] == "150.00"
    assert [l["reference"] for l in feb["lines"]] == ["FEB"]
    assert feb["closing_balance"] == "1149.00"
    assert client.get(f"{API}/vendors/{vendor['id']}/statement?date_from=2026-02-28&date_to=2026-02-01").status_code == 422
    assert client.get(f"{API}/vendors/999/statement?date_from=2026-01-01&date_to=2026-01-31").status_code == 404


def test_statement_reconciliation_shows_unapplied_credits(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    _credit(client, vendor, accounts, "CN-1", "30.00", "2026-01-12")  # approved, not applied
    statement = _statement(client, vendor, "2026-01-01", "2026-01-31")
    assert statement["closing_balance"] == "70.00"
    assert statement["outstanding_bills"] == "100.00"
    assert statement["unapplied_credits"] == "30.00"
    assert statement["difference"] == "0.00"
    assert _outstanding(client, "2026-01-31")["total_outstanding"] == "100.00"
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "approved"


# --- reconciliation with the ledger -----------------------------------------------------------


def test_reconciliation_matches_ledger_and_flags_manual_postings(client, vendor, accounts):
    other = client.post(f"{API}/vendors", json={"code": "BETA", "name": "Beta", "payable_account_id": accounts["ap2"]}).json()
    a = _bill(client, vendor, accounts, "A1", "100.00", "2026-01-10")
    b = _bill(client, other, accounts, "B1", "40.00", "2026-01-11")
    p = _pay(client, vendor, accounts, [{"bill_id": a["id"], "amount": "30.00"}], "2026-01-15")
    _credit(client, vendor, accounts, "CN", "10.00", "2026-01-16", apply_to=a["id"])
    assert client.post(f"{API}/payments/{p['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-01-20"}).status_code == 200
    _pay(client, other, accounts, [{"bill_id": b["id"], "amount": "40.00"}], "2026-01-21")

    for as_of, expected in (("2026-01-09", {"2000": "0.00", "2010": "0.00"}),
                            ("2026-01-15", {"2000": "70.00", "2010": "40.00"}),
                            ("2026-01-19", {"2000": "60.00", "2010": "40.00"}),
                            ("2026-01-31", {"2000": "90.00", "2010": "0.00"})):
        report = _recon(client, as_of)
        rows = {r["account_code"]: r for r in report["rows"]}
        assert {code: rows[code]["ledger_balance"] for code in expected} == expected, as_of
        assert all(r["difference"] == "0.00" and r["ledger_balance"] == r["subledger_balance"] for r in report["rows"]), as_of
        assert report["total_difference"] == "0.00"

    # A manual journal entry on the control account shows up as a difference.
    manual = client.post("/api/v1/journal-entries", json={
        "entry_date": "2026-01-25", "description": "manual accrual",
        "lines": [{"account_id": accounts["expense"], "debit": "7.00"}, {"account_id": accounts["ap"], "credit": "7.00"}]})
    assert client.post(f"/api/v1/journal-entries/{manual.json()['id']}/post").status_code == 200
    report = _recon(client, "2026-01-31")
    rows = {r["account_code"]: r for r in report["rows"]}
    assert rows["2000"]["ledger_balance"] == "97.00" and rows["2000"]["subledger_balance"] == "90.00"
    assert rows["2000"]["difference"] == "7.00" and report["total_difference"] == "7.00"
    assert _recon(client, "2026-01-24")["total_difference"] == "0.00"


# --- outstanding and aging consistency with reversals and credits -----------------------------------


def test_outstanding_and_aging_as_of_with_reversal_and_credit(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-01", due_date="2026-01-31")
    p = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "60.00"}], "2026-01-10")
    assert client.post(f"{API}/payments/{p['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-02-05"}).status_code == 200
    _credit(client, vendor, accounts, "CN", "25.00", "2026-02-10", apply_to=bill["id"], apply_date="2026-02-12")
    _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "75.00"}], "2026-03-20")

    expectations = {
        "2026-01-05": ("100.00", "current"),  # before the payment
        "2026-01-20": ("40.00", "current"),  # payment in force
        "2026-02-04": ("40.00", "days_1_30"),  # overdue, payment still in force
        "2026-02-06": ("100.00", "days_1_30"),  # reversed on the 5th
        "2026-02-11": ("100.00", "days_1_30"),  # credit approved but applied on the 12th
        "2026-02-15": ("75.00", "days_1_30"),
        "2026-03-10": ("75.00", "days_31_60"),
        "2026-03-20": (None, None),  # settled
    }
    for as_of, (outstanding, bucket) in expectations.items():
        report = _outstanding(client, as_of)
        aging = client.get(f"{API}/aging?as_of={as_of}").json()
        if outstanding is None:
            assert report["rows"] == [] and aging["rows"] == [], as_of
            continue
        assert report["total_outstanding"] == outstanding, as_of
        assert aging["rows"][0][bucket] == outstanding, as_of
        assert aging["totals"]["total"] == outstanding, as_of
        # Statement closing balance equals the open-item view whenever no credit is unapplied.
        statement = _statement(client, vendor, "2025-01-01", as_of)
        assert statement["closing_balance"] == str(Decimal(outstanding) - Decimal(statement["unapplied_credits"])), as_of
        assert statement["difference"] == "0.00", as_of
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"


# --- end-to-end scenario -----------------------------------------------------------------------


def test_end_to_end_bill_partial_payment_credit_adjustment_final_settlement(client, vendor, accounts):
    """Approve a bill, pay part of it, adjust it with a credit, settle the rest."""

    def ap_balance():
        rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
        row = rows.get("2000", {"credit": "0.00", "debit": "0.00"})
        return str(Decimal(row["credit"]) - Decimal(row["debit"]))

    # 1. Draft and approve a bill of 1,000.00 (two lines).
    r = client.post(f"{API}/bills", json={
        "vendor_id": vendor["id"], "invoice_number": "INV-2026-042", "invoice_date": "2026-03-01", "due_date": "2026-03-31",
        "total": "1000.00",
        "lines": [{"description": "Consulting days", "account_id": accounts["expense"], "quantity": "4", "unit_price": "225.00"},
                  {"description": "Travel", "account_id": accounts["expense"], "unit_price": "100.00"}]})
    assert r.status_code == 201, r.text
    bill_id = r.json()["id"]
    assert ap_balance() == "0.00"  # drafts do not touch the ledger
    approved = client.post(f"{API}/bills/{bill_id}/approve", json={"actor": "alice"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert ap_balance() == "1000.00"
    assert _outstanding(client, "2026-03-01")["total_outstanding"] == "1000.00"

    # 2. Partial payment of 400.00.
    partial = _pay(client, vendor, accounts, [{"bill_id": bill_id, "amount": "400.00"}], "2026-03-10",
                   reference="TRF-1", idempotency_key="e2e-partial")
    bill = client.get(f"{API}/bills/{bill_id}").json()
    assert (bill["status"], bill["amount_paid"], bill["outstanding"]) == ("partially_paid", "400.00", "600.00")
    assert ap_balance() == "600.00"
    # Resending the same payment is harmless.
    repeat = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-03-10", "amount": "400.00",
                                                   "cash_account_id": accounts["cash"], "recorded_by": "bob", "reference": "TRF-1",
                                                   "idempotency_key": "e2e-partial", "allocations": [{"bill_id": bill_id, "amount": "400.00"}]})
    assert repeat.status_code == 200 and repeat.json()["id"] == partial["id"]

    # 3. The supplier agrees a 150.00 credit adjustment for the travel overcharge; apply it.
    credit = client.post(f"{API}/credits", json={
        "vendor_id": vendor["id"], "kind": "adjustment", "credit_number": "ADJ-042", "credit_date": "2026-03-15",
        "total": "150.00", "related_bill_id": bill_id, "description": "Travel overcharge",
        "lines": [{"description": "Travel overcharge", "account_id": accounts["expense"], "unit_price": "150.00"}]})
    assert credit.status_code == 201, credit.text
    credit_id = credit.json()["id"]
    assert client.post(f"{API}/credits/{credit_id}/approve", json={"actor": "alice"}).status_code == 200
    assert ap_balance() == "450.00"  # the credit is in the ledger as soon as it is approved
    assert _outstanding(client, "2026-03-15")["total_outstanding"] == "600.00"  # but not yet applied to the bill
    applied = client.post(f"{API}/credits/{credit_id}/applications",
                          json={"bill_id": bill_id, "amount": "150.00", "actor": "bob", "application_date": "2026-03-16"})
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    bill = client.get(f"{API}/bills/{bill_id}").json()
    assert (bill["status"], bill["amount_paid"], bill["credits_applied"], bill["outstanding"]) == ("partially_paid", "400.00", "150.00", "450.00")
    assert ap_balance() == "450.00"  # applying a credit has no ledger effect
    assert _outstanding(client, "2026-03-16")["total_outstanding"] == "450.00"
    aging = client.get(f"{API}/aging?as_of=2026-04-10").json()
    assert aging["rows"][0]["days_1_30"] == "450.00"

    # 4. Final settlement: anything more than the outstanding amount is refused ...
    too_much = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-04-12", "amount": "450.01",
                                                     "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                     "allocations": [{"bill_id": bill_id, "amount": "450.01"}]})
    assert too_much.status_code == 422
    # ... and the exact remainder closes the bill.
    final = _pay(client, vendor, accounts, [{"bill_id": bill_id, "amount": "450.00"}], "2026-04-12", reference="TRF-2")
    bill = client.get(f"{API}/bills/{bill_id}").json()
    assert (bill["status"], bill["amount_settled"], bill["outstanding"]) == ("paid", "1000.00", "0.00")
    assert ap_balance() == "0.00"
    assert _outstanding(client, "2026-04-12")["rows"] == []
    assert client.get(f"{API}/aging?as_of=2026-04-30").json()["rows"] == []
    assert client.get(f"{API}/payment-schedule").json()["rows"] == []

    # 5. Statement for the quarter and ledger reconciliation.
    statement = _statement(client, vendor, "2026-03-01", "2026-04-30")
    assert statement["opening_balance"] == "0.00"
    assert [(l["type"], l["credit"], l["debit"], l["balance"]) for l in statement["lines"]] == [
        ("bill", "1000.00", "0.00", "1000.00"),
        ("payment", "0.00", "400.00", "600.00"),
        ("credit", "0.00", "150.00", "450.00"),
        ("payment", "0.00", "450.00", "0.00"),
    ]
    assert statement["closing_balance"] == "0.00" and statement["difference"] == "0.00"
    recon = _recon(client, "2026-04-30")
    assert recon["total_ledger"] == recon["total_subledger"] == "0.00" and recon["total_difference"] == "0.00"

    # 6. Every ledger entry is posted, balanced, and was never altered.
    entries = _journal(client)
    assert [e["reference"] for e in entries] == [f"AP-BILL-{bill_id}", f"AP-PAY-{partial['id']}",
                                                f"AP-CREDIT-{credit_id}", f"AP-PAY-{final['id']}"]
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == 4
        for entry in session.scalars(select(JournalEntry)):
            assert entry.status.value == "posted"
            assert sum(l.debit for l in entry.lines) == sum(l.credit for l in entry.lines)
        assert session.scalar(select(func.count()).select_from(JournalLine)) == 3 + 2 + 2 + 2
    cash_rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert cash_rows["1000"]["credit"] == "850.00" and cash_rows["6000"]["debit"] == "850.00"
