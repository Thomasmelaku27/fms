"""Supplier credit notes and adjustments through the API (SQLite and PostgreSQL)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.payables import services
from app.modules.payables.models import CreditApplication, SupplierCredit, SupplierCreditLine

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
        "expense2": _account(client, "6100", "expense"),
        "cash": _account(client, "1000", "asset"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(
        f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _bill(client, vendor, accounts, number="INV-1", total="150.00", approve=True):
    r = client.post(
        f"{API}/bills",
        json={
            "vendor_id": vendor["id"],
            "invoice_number": number,
            "invoice_date": "2026-01-10",
            "due_date": "2026-02-09",
            "total": total,
            "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}],
        },
    )
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _credit_payload(vendor, accounts, **overrides):
    payload = {
        "vendor_id": vendor["id"],
        "kind": "credit_note",
        "credit_number": "CN-1",
        "credit_date": "2026-01-15",
        "total": "30.00",
        "lines": [
            {"description": "Returned goods", "account_id": accounts["expense"], "quantity": "2", "unit_price": "10.00"},
            {"description": "Discount", "account_id": accounts["expense2"], "unit_price": "10.00"},
        ],
    }
    payload.update(overrides)
    return payload


def _credit(client, vendor, accounts, approve=True, **overrides):
    r = client.post(f"{API}/credits", json=_credit_payload(vendor, accounts, **overrides))
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _apply(client, credit_id, bill_id, amount, date_="2026-01-20", actor="bob"):
    return client.post(
        f"{API}/credits/{credit_id}/applications",
        json={"bill_id": bill_id, "amount": amount, "actor": actor, "application_date": date_},
    )


def _count(client, model) -> int:
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _journal(client):
    return client.get("/api/v1/journal-entries").json()


# --- drafting ---------------------------------------------------------------------------------


def test_create_credit_note_draft(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    r = client.post(
        f"{API}/credits",
        json=_credit_payload(vendor, accounts, related_bill_id=bill["id"], description="Damaged items"),
    )
    assert r.status_code == 201, r.text
    credit = r.json()
    assert credit["status"] == "draft"
    assert credit["kind"] == "credit_note"
    assert credit["payable_account_id"] == accounts["ap"]  # vendor default
    assert credit["related_bill_id"] == bill["id"]
    assert credit["lines_total"] == "30.00"
    assert credit["amount_applied"] == "0.00" and credit["remaining"] == "30.00"
    assert [(l["line_no"], l["line_total"]) for l in credit["lines"]] == [(1, "20.00"), (2, "10.00")]
    assert credit["journal_entry_id"] is None
    assert client.get(f"{API}/credits/{credit['id']}").json() == credit
    assert client.get(f"{API}/credits?vendor_id={vendor['id']}&status=draft").json() == [credit]
    assert client.get(f"{API}/credits/999").status_code == 404


def test_adjustment_kind_and_draft_editing(client, vendor, accounts):
    credit = _credit(client, vendor, accounts, approve=False, kind="adjustment", credit_number="ADJ-1")
    assert credit["kind"] == "adjustment"
    r = client.put(
        f"{API}/credits/{credit['id']}",
        json={"total": "12.50", "description": "Price correction",
              "lines": [{"description": "Overcharge", "account_id": accounts["expense"], "unit_price": "12.50"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == "12.50"
    assert [l["line_total"] for l in r.json()["lines"]] == ["12.50"]
    assert _count(client, SupplierCreditLine) == 1

    bad = client.put(f"{API}/credits/{credit['id']}", json={"total": "99.00"})
    assert bad.status_code == 422 and "do not equal credit total" in bad.json()["detail"]

    assert client.delete(f"{API}/credits/{credit['id']}").status_code == 204
    assert client.get(f"{API}/credits/{credit['id']}").status_code == 404
    assert _count(client, SupplierCreditLine) == 0


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"total": "31.00"}, "do not equal credit total"),
        ({"total": "0"}, "greater than 0"),
        ({"payable_account_id": "EXPENSE"}, "not a liability"),
        ({"related_bill_id": 999}, "does not exist"),
        ({"vendor_id": 999}, "Vendor does not exist"),
        ({"kind": "refund"}, "kind"),
        ({"lines": [{"description": "x", "account_id": 0, "unit_price": "30"}]}, "Unknown line"),
    ],
)
def test_credit_validation(client, vendor, accounts, overrides, fragment):
    if overrides.get("payable_account_id") == "EXPENSE":
        overrides = {"payable_account_id": accounts["expense"]}
    r = client.post(f"{API}/credits", json=_credit_payload(vendor, accounts, **overrides))
    assert r.status_code == 422, r.text
    assert fragment in r.text
    assert _count(client, SupplierCredit) == 0


def test_related_bill_must_belong_to_the_vendor(client, vendor, accounts):
    other = client.post(f"{API}/vendors", json={"code": "OTHER", "name": "Other", "payable_account_id": accounts["ap"]}).json()
    theirs = _bill(client, other, accounts, number="O-1")
    r = client.post(f"{API}/credits", json=_credit_payload(vendor, accounts, related_bill_id=theirs["id"]))
    assert r.status_code == 422
    assert "another vendor" in r.json()["detail"]


def test_duplicate_credit_number_protection(client, vendor, accounts, monkeypatch):
    _credit(client, vendor, accounts, approve=False)
    for number in ("CN-1", "cn-1", " CN-1\t"):
        r = client.post(f"{API}/credits", json=_credit_payload(vendor, accounts, credit_number=number))
        assert r.status_code == 409, (number, r.text)
    # Database layer: bypass the API check, the UNIQUE constraint still wins and rolls back lines.
    monkeypatch.setattr(services.credits, "_assert_no_duplicate_credit", lambda *a, **k: None)
    r = client.post(f"{API}/credits", json=_credit_payload(vendor, accounts, credit_number="cn-1"))
    assert r.status_code == 409
    assert _count(client, SupplierCredit) == 1
    assert _count(client, SupplierCreditLine) == 2


# --- approval, rejection, reopen ----------------------------------------------------------------


def test_approval_posts_mirror_image_entry(client, vendor, accounts):
    credit = _credit(client, vendor, accounts)
    assert credit["status"] == "approved"
    assert credit["approved_by"] == "alice" and credit["approved_at"] is not None
    entry = client.get(f"/api/v1/journal-entries/{credit['journal_entry_id']}").json()
    assert entry["status"] == "posted"
    assert entry["entry_date"] == "2026-01-15"
    assert entry["reference"] == f"AP-CREDIT-{credit['id']}"
    assert [(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]] == [
        (accounts["ap"], "30.00", "0.00"),
        (accounts["expense"], "0.00", "20.00"),
        (accounts["expense2"], "0.00", "10.00"),
    ]
    rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert rows["2000"]["debit"] == "30.00"


def test_approval_requirements(client, accounts):
    vendor = client.post(f"{API}/vendors", json={"code": "NOAP", "name": "No AP"}).json()
    empty = client.post(
        f"{API}/credits",
        json={"vendor_id": vendor["id"], "credit_number": "E", "credit_date": "2026-01-15", "total": "5.00"},
    ).json()
    r = client.post(f"{API}/credits/{empty['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 422
    assert "credit has no lines" in r.json()["detail"]
    assert "no payable account" in r.json()["detail"]
    assert _journal(client) == []


def test_reject_reopen_resubmit(client, vendor, accounts):
    credit = _credit(client, vendor, accounts, approve=False)
    cid = credit["id"]
    r = client.post(f"{API}/credits/{cid}/reject", json={"actor": "carol", "reason": "Not agreed"})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["rejected_by"], r.json()["rejection_reason"]) == ("rejected", "carol", "Not agreed")
    assert r.json()["rejected_at"] is not None
    assert client.post(f"{API}/credits/{cid}/approve", json={"actor": "alice"}).status_code == 409
    assert client.put(f"{API}/credits/{cid}", json={"total": "1.00"}).status_code == 409
    r = client.post(f"{API}/credits/{cid}/reopen", json={"actor": "dave"})
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["rejected_by"] is None
    assert client.post(f"{API}/credits/{cid}/approve", json={"actor": "alice"}).status_code == 200


# --- applying credits to bills ----------------------------------------------------------------


def test_apply_credit_settles_bill_without_ledger_entry(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    credit = _credit(client, vendor, accounts)
    entries_before = len(_journal(client))

    r = _apply(client, credit["id"], bill["id"], "30.00")
    assert r.status_code == 200, r.text
    applied = r.json()
    assert applied["status"] == "applied"
    assert applied["amount_applied"] == "30.00" and applied["remaining"] == "0.00"
    assert len(applied["applications"]) == 1
    app = applied["applications"][0]
    assert (app["bill_id"], app["amount"], app["application_date"], app["applied_by"]) == (bill["id"], "30.00", "2026-01-20", "bob")

    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert current["status"] == "partially_paid"
    assert (current["amount_paid"], current["credits_applied"], current["amount_settled"], current["outstanding"]) == ("0.00", "30.00", "30.00", "120.00")
    assert len(_journal(client)) == entries_before  # no ledger effect

    # The payment schedule and payment-schedule report see the credit as settlement.
    schedule = client.get(f"{API}/bills/{bill['id']}/schedule").json()
    assert schedule["installments"][0]["amount_remaining"] == "120.00"
    assert client.get(f"{API}/payment-schedule").json()["total_remaining"] == "120.00"


def test_partial_applications_across_bills_and_unapply(client, vendor, accounts):
    one = _bill(client, vendor, accounts, number="B1", total="20.00")
    two = _bill(client, vendor, accounts, number="B2", total="50.00")
    credit = _credit(client, vendor, accounts, total="30.00")

    assert _apply(client, credit["id"], one["id"], "20.00").status_code == 200
    assert client.get(f"{API}/bills/{one['id']}").json()["status"] == "paid"
    r = _apply(client, credit["id"], two["id"], "10.00")
    assert r.status_code == 200
    assert r.json()["status"] == "applied"
    assert client.get(f"{API}/bills/{two['id']}").json()["outstanding"] == "40.00"

    application_id = r.json()["applications"][0]["id"]
    r = client.post(f"{API}/credits/{credit['id']}/applications/{application_id}/unapply", json={"actor": "bob"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "partially_applied" and r.json()["remaining"] == "20.00"
    assert client.get(f"{API}/bills/{one['id']}").json()["status"] == "approved"
    assert client.get(f"{API}/bills/{one['id']}").json()["outstanding"] == "20.00"
    # The application row is kept (closed with a date); unapplying it again is a conflict.
    assert client.post(f"{API}/credits/{credit['id']}/applications/{application_id}/unapply", json={"actor": "bob"}).status_code == 409
    assert _count(client, CreditApplication) == 2


@pytest.mark.parametrize(
    ("setup", "amount", "fragment"),
    [
        ("draft_bill", "10.00", "is draft and cannot receive credits"),
        ("paid_bill", "1.00", "is paid and cannot receive credits"),
        ("other_vendor", "10.00", "belongs to another vendor"),
        ("draft_credit", "10.00", "is draft and cannot be applied"),
        ("ok", "31.00", "exceeds the remaining amount"),
        ("ok", "0", "greater than 0"),
        ("small_bill", "30.00", "exceeds the outstanding amount"),
        ("early", "10.00", "cannot precede"),
        ("missing_bill", "10.00", "does not exist"),
    ],
)
def test_invalid_applications_are_rejected_without_side_effects(client, vendor, accounts, setup, amount, fragment):
    credit = _credit(client, vendor, accounts, approve=setup != "draft_credit")
    bill = _bill(client, vendor, accounts, approve=setup != "draft_bill")
    bill_id = bill["id"]
    date_ = "2026-01-20"
    if setup == "paid_bill":
        r = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-18", "amount": "150.00",
                                                  "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                  "allocations": [{"bill_id": bill_id, "amount": "150.00"}]})
        assert r.status_code == 201
    elif setup == "other_vendor":
        other = client.post(f"{API}/vendors", json={"code": "OTHER", "name": "Other", "payable_account_id": accounts["ap"]}).json()
        bill_id = _bill(client, other, accounts, number="O-1")["id"]
    elif setup == "small_bill":
        bill_id = _bill(client, vendor, accounts, number="SMALL", total="25.00")["id"]
    elif setup == "early":
        date_ = "2026-01-01"
    elif setup == "missing_bill":
        bill_id = 999

    before = client.get(f"{API}/bills/{bill['id']}").json()
    r = _apply(client, credit["id"], bill_id, amount, date_=date_)
    assert r.status_code == 422, r.text
    assert fragment in r.text
    assert _count(client, CreditApplication) == 0
    assert client.get(f"{API}/credits/{credit['id']}").json()["amount_applied"] == "0.00"
    assert client.get(f"{API}/bills/{bill['id']}").json() == before


def test_payments_and_credits_share_the_bill_ceiling(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, total="100.00")
    credit = _credit(client, vendor, accounts, total="30.00")
    assert _apply(client, credit["id"], bill["id"], "30.00").status_code == 200
    pay = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-21", "amount": "80.00",
                                                "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                "allocations": [{"bill_id": bill["id"], "amount": "80.00"}]})
    assert pay.status_code == 422 and "exceeds its outstanding amount 70.00" in pay.json()["detail"]
    pay = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-21", "amount": "70.00",
                                                "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                "allocations": [{"bill_id": bill["id"], "amount": "70.00"}]})
    assert pay.status_code == 201
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["amount_paid"], current["credits_applied"], current["outstanding"]) == ("paid", "70.00", "30.00", "0.00")


# --- reversal ----------------------------------------------------------------------------------


def test_reverse_unapplied_credit(client, vendor, accounts):
    credit = _credit(client, vendor, accounts)
    r = client.post(f"{API}/credits/{credit['id']}/reverse",
                    json={"actor": "carol", "reason": "Issued in error", "reversal_date": "2026-01-31"})
    assert r.status_code == 200, r.text
    reversed_ = r.json()
    assert reversed_["status"] == "reversed"
    assert (reversed_["reversed_by"], reversed_["reversal_date"], reversed_["reversal_reason"]) == ("carol", "2026-01-31", "Issued in error")
    assert reversed_["reversed_at"] is not None
    original = client.get(f"/api/v1/journal-entries/{credit['journal_entry_id']}").json()
    reversal = client.get(f"/api/v1/journal-entries/{reversed_['reversal_journal_entry_id']}").json()
    assert original["status"] == reversal["status"] == "posted"
    assert reversal["entry_date"] == "2026-01-31"
    assert reversal["reference"] == f"AP-CREDIT-REV-{credit['id']}"
    assert [(l["account_id"], l["debit"], l["credit"]) for l in reversal["lines"]] == [
        (l["account_id"], l["credit"], l["debit"]) for l in original["lines"]
    ]
    # The credit can no longer be applied and the AP account nets to zero.
    bill = _bill(client, vendor, accounts)
    assert _apply(client, credit["id"], bill["id"], "10.00").status_code == 422
    rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert rows["2000"]["credit"] == "150.00"  # only the bill remains


def test_reverse_requires_unapplied_credit_and_valid_date(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    credit = _credit(client, vendor, accounts)
    assert _apply(client, credit["id"], bill["id"], "10.00").status_code == 200
    r = client.post(f"{API}/credits/{credit['id']}/reverse", json={"actor": "c", "reason": "r"})
    assert r.status_code == 409 and "unapply first" in r.json()["detail"]
    application_id = client.get(f"{API}/credits/{credit['id']}").json()["applications"][0]["id"]
    assert client.post(f"{API}/credits/{credit['id']}/applications/{application_id}/unapply", json={"actor": "b"}).status_code == 200
    early = client.post(f"{API}/credits/{credit['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-01-01"})
    assert early.status_code == 422 and "cannot precede" in early.json()["detail"]
    assert client.post(f"{API}/credits/{credit['id']}/reverse", json={"actor": "c", "reason": "r"}).status_code == 200
    draft = _credit(client, vendor, accounts, approve=False, credit_number="CN-2")
    assert client.post(f"{API}/credits/{draft['id']}/reverse", json={"actor": "c", "reason": "r"}).status_code == 409
