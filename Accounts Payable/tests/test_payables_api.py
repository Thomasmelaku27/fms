"""End-to-end API tests for the accounts payable workflow (SQLite and PostgreSQL).

Covers the happy path (draft -> approve -> schedule -> partial payments -> paid),
validation, duplicate protection, repeat requests (approving twice, re-sending a
payment, duplicate bills) and failure rollback (ledger refuses the posting, the
database rejects a row behind the application's back, unexpected errors).
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JournalEntry, JournalLine
from app.modules.payables import services
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import (
    BillPaymentSchedule,
    PaymentAllocation,
    SupplierBill,
    SupplierBillLine,
    VendorPayment,
)

API = "/api/v1/payables"


# --- helpers -------------------------------------------------------------------------


def _account(client, code, type_, name=None):
    r = client.post("/api/v1/accounts", json={"code": code, "name": name or code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {
        "ap": _account(client, "2000", "liability", "Accounts payable"),
        "expense": _account(client, "6000", "expense", "Office supplies"),
        "expense2": _account(client, "6100", "expense", "Consulting"),
        "cash": _account(client, "1000", "asset", "Main bank"),
        "revenue": _account(client, "4000", "revenue", "Sales"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(
        f"{API}/vendors",
        json={"code": "ACME", "name": "Acme Ltd", "payable_account_id": accounts["ap"]},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _lines(accounts, *amounts):
    return [
        {"description": f"Item {i}", "account_id": accounts["expense"], "unit_price": amount}
        for i, amount in enumerate(amounts, start=1)
    ]


def _bill(client, vendor, accounts, *, number="INV-1", total="150.00", lines=None, **extra):
    payload = {
        "vendor_id": vendor["id"],
        "invoice_number": number,
        "invoice_date": "2026-01-10",
        "due_date": "2026-02-09",
        "total": total,
        "lines": _lines(accounts, "75.00", "75.00") if lines is None else lines,
        **extra,
    }
    r = client.post(f"{API}/bills", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _approve(client, bill_id, actor="alice"):
    return client.post(f"{API}/bills/{bill_id}/approve", json={"actor": actor})


def _approved_bill(client, vendor, accounts, **kw):
    bill = _bill(client, vendor, accounts, **kw)
    r = _approve(client, bill["id"])
    assert r.status_code == 200, r.text
    return r.json()


def _pay(client, vendor, accounts, allocations, *, amount=None, date_="2026-01-20", **extra):
    if amount is None:
        amount = str(sum(float(a["amount"]) for a in allocations))
    return client.post(
        f"{API}/payments",
        json={
            "vendor_id": vendor["id"],
            "payment_date": date_,
            "amount": amount,
            "cash_account_id": accounts["cash"],
            "recorded_by": "bob",
            "allocations": allocations,
            **extra,
        },
    )


def _count(client, model) -> int:
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _journal(client):
    return client.get("/api/v1/journal-entries").json()


# --- vendors ----------------------------------------------------------------------------


def test_vendor_crud_and_account_policy(client, accounts):
    r = client.post(f"{API}/vendors", json={"code": "V1", "name": "One"})
    assert r.status_code == 201
    assert r.json()["payment_terms_days"] == 30
    assert r.json()["payable_account_id"] is None

    assert client.post(f"{API}/vendors", json={"code": "V1", "name": "Dup"}).status_code == 409

    not_liability = client.post(
        f"{API}/vendors", json={"code": "V2", "name": "Two", "payable_account_id": accounts["cash"]}
    )
    assert not_liability.status_code == 422
    assert "not a liability account" in not_liability.json()["detail"]

    vid = r.json()["id"]
    patched = client.patch(
        f"{API}/vendors/{vid}",
        json={"payable_account_id": accounts["ap"], "payment_terms_days": 45, "is_active": False},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert {k: body[k] for k in ("id", "code", "name", "is_active", "payable_account_id", "payment_terms_days")} == {
        "id": vid,
        "code": "V1",
        "name": "One",
        "is_active": False,
        "payable_account_id": accounts["ap"],
        "payment_terms_days": 45,
    }
    assert body["status"] == "inactive"
    assert client.get(f"{API}/vendors/{vid}").json()["is_active"] is False
    assert client.get(f"{API}/vendors?include_inactive=false").json() == []
    assert client.get(f"{API}/vendors/999").status_code == 404

    inactive_bill = client.post(
        f"{API}/bills",
        json={"vendor_id": vid, "invoice_number": "X", "invoice_date": "2026-01-01", "total": "1.00"},
    )
    assert inactive_bill.status_code == 422
    assert "inactive" in inactive_bill.json()["detail"]


# --- bills: lines, totals, validation -------------------------------------------------------


def test_create_bill_with_lines_computes_totals_and_defaults(client, vendor, accounts):
    r = client.post(
        f"{API}/bills",
        json={
            "vendor_id": vendor["id"],
            "invoice_number": "INV-100",
            "invoice_date": "2026-01-10",
            "total": "107.10",
            "description": "January supplies",
            "lines": [
                {
                    "description": "Paper",
                    "account_id": accounts["expense"],
                    "quantity": "3",
                    "unit_price": "12.3333",
                },
                {
                    "description": "Toner",
                    "account_id": accounts["expense2"],
                    "quantity": "0.5",
                    "unit_price": "140.20",
                    "line_total": "70.10",
                },
            ],
        },
    )
    assert r.status_code == 201, r.text
    bill = r.json()
    assert bill["status"] == "draft"
    assert bill["due_date"] == "2026-02-09"  # invoice date + vendor terms (30 days)
    assert bill["payable_account_id"] == accounts["ap"]  # vendor default
    assert bill["lines_total"] == "107.10"
    assert bill["amount_paid"] == "0.00"
    assert bill["outstanding"] == "107.10"
    assert [(line["line_no"], line["line_total"]) for line in bill["lines"]] == [
        (1, "37.00"),  # 3 x 12.3333 = 36.9999 -> 37.00
        (2, "70.10"),
    ]
    assert bill["journal_entry_id"] is None and bill["approved_at"] is None
    assert client.get(f"{API}/bills/{bill['id']}").json() == bill
    assert client.get(f"{API}/bills?vendor_id={vendor['id']}&status=draft").json() == [bill]
    assert client.get(f"{API}/bills?status=approved").json() == []


@pytest.mark.parametrize(
    ("payload_patch", "fragment"),
    [
        ({"total": "100.00"}, "Line totals (150.00) do not equal bill total (100.00)"),
        ({"total": "0"}, "greater than 0"),
        ({"total": "150.001"}, "decimal"),
        ({"due_date": "2026-01-01"}, "Due date precedes invoice date"),
        ({"lines": [{"description": "x", "account_id": 0, "unit_price": "150"}]}, "Unknown line"),
        (
            {"lines": [{"description": "x", "account_id": 1, "quantity": "0", "unit_price": "1"}]},
            "greater than 0",
        ),
        (
            {"lines": [{"description": "x", "account_id": 1, "unit_price": "1", "line_total": "2"}]},
            "does not equal quantity x unit_price",
        ),
        (
            {"lines": [{"description": "x", "account_id": 1, "quantity": "0.001", "unit_price": "1"}]},
            "at least 0.01",
        ),
        ({"payable_account_id": 0}, "does not exist"),
        ({"vendor_id": 999}, "Vendor does not exist"),
    ],
)
def test_bill_amount_and_reference_validation(client, vendor, accounts, payload_patch, fragment):
    payload = {
        "vendor_id": vendor["id"],
        "invoice_number": "INV-V",
        "invoice_date": "2026-01-10",
        "due_date": "2026-02-09",
        "total": "150.00",
        "lines": _lines(accounts, "75.00", "75.00"),
        **payload_patch,
    }
    r = client.post(f"{API}/bills", json=payload)
    assert r.status_code == 422, r.text
    assert fragment in r.text
    assert client.get(f"{API}/bills").json() == []
    assert _count(client, SupplierBillLine) == 0


def test_payable_account_must_be_active_liability(client, vendor, accounts):
    r = client.post(
        f"{API}/bills",
        json={
            "vendor_id": vendor["id"],
            "invoice_number": "INV-A",
            "invoice_date": "2026-01-10",
            "total": "1.00",
            "payable_account_id": accounts["expense"],
        },
    )
    assert r.status_code == 422
    assert "not a liability account" in r.json()["detail"]

    client.post(f"/api/v1/accounts/{accounts['ap']}/deactivate")
    r = client.post(
        f"{API}/bills",
        json={"vendor_id": vendor["id"], "invoice_number": "INV-A", "invoice_date": "2026-01-10", "total": "1.00"},
    )
    assert r.status_code == 422
    assert "inactive" in r.json()["detail"]


# --- duplicate supplier invoice protection -------------------------------------------------------


def test_duplicate_invoice_rejected_at_api_layer(client, vendor, accounts):
    first = _bill(client, vendor, accounts, number="INV-7")

    for repeat in ("INV-7", "inv-7", "  INV-7 ", "Inv-7\t"):
        r = client.post(
            f"{API}/bills",
            json={
                "vendor_id": vendor["id"],
                "invoice_number": repeat,
                "invoice_date": "2026-01-10",
                "total": "150.00",
                "lines": _lines(accounts, "150.00"),
            },
        )
        assert r.status_code == 409, (repeat, r.text)
        assert "already exists" in r.json()["detail"]

    # Nothing leaked: still one bill with its two lines.
    assert [b["id"] for b in client.get(f"{API}/bills").json()] == [first["id"]]
    assert _count(client, SupplierBillLine) == 2

    # The same number is fine for another vendor.
    other = client.post(f"{API}/vendors", json={"code": "OTHER", "name": "Other"}).json()
    r = client.post(
        f"{API}/bills",
        json={"vendor_id": other["id"], "invoice_number": "INV-7", "invoice_date": "2026-01-10", "total": "1.00"},
    )
    assert r.status_code == 201


def test_duplicate_invoice_rejected_at_database_layer_and_rolled_back(
    client, vendor, accounts, monkeypatch
):
    """Simulate a race: the API pre-check passes, the UNIQUE constraint still wins."""
    _bill(client, vendor, accounts, number="INV-8")
    monkeypatch.setattr(services.documents, "_assert_no_duplicate_invoice", lambda *a, **k: None)

    r = client.post(
        f"{API}/bills",
        json={
            "vendor_id": vendor["id"],
            "invoice_number": "inv-8",
            "invoice_date": "2026-01-10",
            "total": "20.00",
            "lines": _lines(accounts, "20.00"),
        },
    )
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]
    assert len(client.get(f"{API}/bills").json()) == 1
    assert _count(client, SupplierBillLine) == 2  # the rejected bill's line was rolled back


def test_editing_draft_cannot_collide_with_another_invoice(client, vendor, accounts):
    _bill(client, vendor, accounts, number="INV-1")
    second = _bill(client, vendor, accounts, number="INV-2")
    r = client.put(f"{API}/bills/{second['id']}", json={"invoice_number": "Inv-1"})
    assert r.status_code == 409
    # Re-saving its own number is not a duplicate.
    r = client.put(f"{API}/bills/{second['id']}", json={"invoice_number": "inv-2"})
    assert r.status_code == 200
    assert r.json()["invoice_number"] == "inv-2"


# --- draft editing -------------------------------------------------------------------------


def test_edit_and_delete_draft(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)

    r = client.put(
        f"{API}/bills/{bill['id']}",
        json={
            "total": "99.99",
            "description": "corrected",
            "lines": [
                {"description": "A", "account_id": accounts["expense"], "unit_price": "33.33", "quantity": "3"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["total"] == "99.99"
    assert edited["description"] == "corrected"
    assert [line["line_total"] for line in edited["lines"]] == ["99.99"]
    assert _count(client, SupplierBillLine) == 1

    # Partial update keeps lines, so the new total must still match them.
    r = client.put(f"{API}/bills/{bill['id']}", json={"total": "50.00"})
    assert r.status_code == 422
    assert "do not equal" in r.json()["detail"]
    assert client.get(f"{API}/bills/{bill['id']}").json()["total"] == "99.99"

    # Moving the invoice date past the due date pushes the due date out by the terms.
    r = client.put(f"{API}/bills/{bill['id']}", json={"invoice_date": "2026-03-01"})
    assert r.status_code == 200
    assert r.json()["due_date"] == "2026-03-31"

    assert client.delete(f"{API}/bills/{bill['id']}").status_code == 204
    assert client.get(f"{API}/bills/{bill['id']}").status_code == 404
    assert client.delete(f"{API}/bills/{bill['id']}").status_code == 404
    assert _count(client, SupplierBillLine) == 0


def test_approved_bill_is_frozen(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    assert client.put(f"{API}/bills/{bill['id']}", json={"total": "1.00"}).status_code == 409
    assert client.delete(f"{API}/bills/{bill['id']}").status_code == 409
    assert client.get(f"{API}/bills/{bill['id']}").json()["total"] == "150.00"


# --- approval and rejection ---------------------------------------------------------------------


def test_approval_posts_balanced_entry_with_actor_and_timestamp(client, vendor, accounts):
    bill = _bill(
        client,
        vendor,
        accounts,
        lines=[
            {"description": "Paper", "account_id": accounts["expense"], "unit_price": "100.00"},
            {"description": "Advice", "account_id": accounts["expense2"], "unit_price": "50.00"},
        ],
    )
    r = _approve(client, bill["id"], actor="alice")
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "alice"
    assert approved["approved_at"] is not None
    assert approved["journal_entry_id"] is not None

    entry = client.get(f"/api/v1/journal-entries/{approved['journal_entry_id']}").json()
    assert entry["status"] == "posted"
    assert entry["entry_date"] == "2026-01-10"
    assert entry["reference"] == f"AP-BILL-{bill['id']}"
    assert [(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]] == [
        (accounts["expense"], "100.00", "0.00"),
        (accounts["expense2"], "50.00", "0.00"),
        (accounts["ap"], "0.00", "150.00"),
    ]
    # Visible to the rest of the system through ordinary posted entries.
    tb = client.get("/api/v1/reports/trial-balance").json()
    assert tb["total_debit"] == tb["total_credit"] == "150.00"

    # Default payment schedule: one installment on the due date.
    schedule = client.get(f"{API}/bills/{bill['id']}/schedule").json()
    assert schedule["installments"] == [
        {"installment_no": 1, "due_date": "2026-02-09", "amount": "150.00", "amount_remaining": "150.00"}
    ]


def test_approve_twice_is_conflict_and_posts_once(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    again = _approve(client, bill["id"], actor="mallory")
    assert again.status_code == 409
    assert "only draft bills can be approved" in again.json()["detail"]

    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert current["approved_by"] == "alice"
    assert current["approved_at"] == bill["approved_at"]
    assert len(_journal(client)) == 1
    assert _count(client, BillPaymentSchedule) == 1


def test_approval_requires_lines_matching_total_and_payable_account(client, accounts):
    vendor = client.post(f"{API}/vendors", json={"code": "NOAP", "name": "No AP account"}).json()
    empty = client.post(
        f"{API}/bills",
        json={"vendor_id": vendor["id"], "invoice_number": "E1", "invoice_date": "2026-01-10", "total": "10.00"},
    ).json()
    r = _approve(client, empty["id"])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "bill has no lines" in detail
    assert "no payable account" in detail
    assert client.get(f"{API}/bills/{empty['id']}").json()["status"] == "draft"
    assert _journal(client) == []

    # Fix both problems through the normal draft-editing route, then approve.
    r = client.put(
        f"{API}/bills/{empty['id']}",
        json={"payable_account_id": accounts["ap"], "lines": _lines(accounts, "10.00")},
    )
    assert r.status_code == 200, r.text
    assert _approve(client, empty["id"]).status_code == 200


def test_approval_refused_when_line_account_deactivated_after_drafting(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    client.post(f"/api/v1/accounts/{accounts['expense']}/deactivate")
    r = _approve(client, bill["id"])
    assert r.status_code == 422
    assert "Inactive line accounts: 6000" in r.json()["detail"]
    assert _journal(client) == []


def test_reject_reopen_and_resubmit(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    r = client.post(
        f"{API}/bills/{bill['id']}/reject", json={"actor": "carol", "reason": "Wrong amount"}
    )
    assert r.status_code == 200, r.text
    rejected = r.json()
    assert rejected["status"] == "rejected"
    assert rejected["rejected_by"] == "carol"
    assert rejected["rejected_at"] is not None
    assert rejected["rejection_reason"] == "Wrong amount"
    assert _journal(client) == []

    # Repeat / wrong-state requests.
    assert client.post(f"{API}/bills/{bill['id']}/reject", json={"actor": "c", "reason": "x"}).status_code == 409
    assert _approve(client, bill["id"]).status_code == 409
    assert client.put(f"{API}/bills/{bill['id']}", json={"total": "1.00"}).status_code == 409
    assert client.delete(f"{API}/bills/{bill['id']}").status_code == 409
    assert client.post(f"{API}/bills/{bill['id']}/reject", json={"actor": "c"}).status_code == 422

    r = client.post(f"{API}/bills/{bill['id']}/reopen", json={"actor": "dave"})
    assert r.status_code == 200
    reopened = r.json()
    assert reopened["status"] == "draft"
    assert reopened["rejected_by"] is None
    assert reopened["rejected_at"] is None
    assert reopened["rejection_reason"] is None
    assert client.post(f"{API}/bills/{bill['id']}/reopen", json={"actor": "dave"}).status_code == 409

    r = client.put(f"{API}/bills/{bill['id']}", json={"total": "75.00", "lines": _lines(accounts, "75.00")})
    assert r.status_code == 200
    assert _approve(client, bill["id"]).status_code == 200
    assert client.post(f"{API}/bills/{bill['id']}/reopen", json={"actor": "dave"}).status_code == 409


def test_actor_is_required(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    assert client.post(f"{API}/bills/{bill['id']}/approve", json={}).status_code == 422
    assert client.post(f"{API}/bills/{bill['id']}/approve", json={"actor": ""}).status_code == 422
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "draft"


# --- payment schedules -------------------------------------------------------------------------


def test_payment_schedule_for_approved_bills(client, vendor, accounts):
    draft = _bill(client, vendor, accounts, number="D")
    r = client.put(
        f"{API}/bills/{draft['id']}/schedule",
        json={"installments": [{"due_date": "2026-02-01", "amount": "150.00"}]},
    )
    assert r.status_code == 409

    bill = _approved_bill(client, vendor, accounts, number="A")
    bad_sum = client.put(
        f"{API}/bills/{bill['id']}/schedule",
        json={"installments": [{"due_date": "2026-02-01", "amount": "100.00"}]},
    )
    assert bad_sum.status_code == 422
    assert "must sum to the bill total" in bad_sum.json()["detail"]
    early = client.put(
        f"{API}/bills/{bill['id']}/schedule",
        json={"installments": [{"due_date": "2025-12-31", "amount": "150.00"}]},
    )
    assert early.status_code == 422

    installments = [
        {"due_date": "2026-03-01", "amount": "100.00"},
        {"due_date": "2026-02-01", "amount": "50.00"},
    ]
    for _ in range(2):  # repeat request replaces, never duplicates
        r = client.put(f"{API}/bills/{bill['id']}/schedule", json={"installments": installments})
        assert r.status_code == 200, r.text
    assert r.json()["installments"] == [
        {"installment_no": 1, "due_date": "2026-02-01", "amount": "50.00", "amount_remaining": "50.00"},
        {"installment_no": 2, "due_date": "2026-03-01", "amount": "100.00", "amount_remaining": "100.00"},
    ]
    assert _count(client, BillPaymentSchedule) == 2

    # Cross-vendor schedule report with date filtering.
    report = client.get(f"{API}/payment-schedule?date_from=2026-02-15").json()
    assert [(row["invoice_number"], row["installment_no"], row["amount_remaining"]) for row in report["rows"]] == [
        ("A", 2, "100.00")
    ]
    assert report["total_remaining"] == "100.00"
    assert client.get(f"{API}/payment-schedule").json()["total_remaining"] == "150.00"
    assert client.get(f"{API}/payment-schedule?vendor_id=999").json()["rows"] == []

    # Payments are applied to the earliest installment first.
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "70.00"}]).status_code == 201
    schedule = client.get(f"{API}/bills/{bill['id']}/schedule").json()
    assert schedule["amount_paid"] == "70.00"
    assert [i["amount_remaining"] for i in schedule["installments"]] == ["0.00", "80.00"]
    assert client.get(f"{API}/payment-schedule").json()["total_remaining"] == "80.00"

    # Fully paid bills drop out and can no longer be rescheduled.
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "80.00"}]).status_code == 201
    assert client.get(f"{API}/payment-schedule").json()["rows"] == []
    assert client.put(f"{API}/bills/{bill['id']}/schedule", json={"installments": installments}).status_code == 409


# --- payments and allocations -------------------------------------------------------------------


def test_partial_payments_across_bills(client, vendor, accounts):
    one = _approved_bill(client, vendor, accounts, number="B1", total="100.00", lines=_lines(accounts, "100.00"))
    two = _approved_bill(client, vendor, accounts, number="B2", total="40.00", lines=_lines(accounts, "40.00"))

    r = _pay(
        client,
        vendor,
        accounts,
        [{"bill_id": one["id"], "amount": "60.00"}, {"bill_id": two["id"], "amount": "40.00"}],
        reference="CHQ-1",
        method="cheque",
    )
    assert r.status_code == 201, r.text
    payment = r.json()
    assert payment["amount"] == "100.00"
    assert payment["method"] == "cheque"
    assert payment["recorded_by"] == "bob"
    assert [(a["bill_id"], a["amount"]) for a in payment["allocations"]] == [(one["id"], "60.00"), (two["id"], "40.00")]

    first = client.get(f"{API}/bills/{one['id']}").json()
    assert (first["status"], first["amount_paid"], first["outstanding"]) == ("partially_paid", "60.00", "40.00")
    second = client.get(f"{API}/bills/{two['id']}").json()
    assert (second["status"], second["amount_paid"], second["outstanding"]) == ("paid", "40.00", "0.00")

    entry = client.get(f"/api/v1/journal-entries/{payment['journal_entry_id']}").json()
    assert entry["status"] == "posted"
    assert entry["entry_date"] == "2026-01-20"
    assert entry["reference"] == f"AP-PAY-{payment['id']}"
    assert [(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]] == [
        (accounts["ap"], "100.00", "0.00"),
        (accounts["cash"], "0.00", "100.00"),
    ]

    # Settle the rest; the AP account nets to zero in the trial balance.
    r = _pay(client, vendor, accounts, [{"bill_id": one["id"], "amount": "40.00"}], date_="2026-01-25")
    assert r.status_code == 201
    assert client.get(f"{API}/bills/{one['id']}").json()["status"] == "paid"
    rows = {row["code"]: row for row in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert rows["2000"]["debit"] == rows["2000"]["credit"] == "0.00"
    assert rows["1000"]["credit"] == "140.00"
    assert rows["6000"]["debit"] == "140.00"

    listed = client.get(f"{API}/payments?vendor_id={vendor['id']}").json()
    assert [p["amount"] for p in listed] == ["100.00", "40.00"]
    assert client.get(f"{API}/payments/{payment['id']}").json() == payment
    assert client.get(f"{API}/payments/999").status_code == 404


@pytest.mark.parametrize(
    ("make_payload", "status_code", "fragment"),
    [
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "200.00"}], "amount": "200.00"}, 422, "exceeds its outstanding"),
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "70.00"}], "amount": "60.00"}, 422, "exceed the payment amount"),
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "50.00"}, {"bill_id": b, "amount": "50.00"}], "amount": "100.00"}, 422, "only be allocated once"),
        (lambda b, a: {"allocations": [{"bill_id": 999, "amount": "10.00"}], "amount": "10.00"}, 422, "does not exist"),
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "10.00"}], "amount": "10.00", "cash_account_id": a["revenue"]}, 422, "not an asset account"),
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "10.00"}], "amount": "10.00", "cash_account_id": 0}, 422, "does not exist"),
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "10.00"}], "amount": "10.00", "recorded_by": ""}, 422, "recorded_by"),
        (lambda b, a: {"allocations": [{"bill_id": b, "amount": "10.005"}], "amount": "10.005"}, 422, "decimal"),
    ],
)
def test_invalid_payments_are_rejected_without_side_effects(
    client, vendor, accounts, make_payload, status_code, fragment
):
    bill = _approved_bill(client, vendor, accounts)
    payload = {
        "vendor_id": vendor["id"],
        "payment_date": "2026-01-20",
        "cash_account_id": accounts["cash"],
        "recorded_by": "bob",
        **make_payload(bill["id"], accounts),
    }
    r = client.post(f"{API}/payments", json=payload)
    assert r.status_code == status_code, r.text
    assert fragment in r.text

    assert _count(client, VendorPayment) == 0
    assert _count(client, PaymentAllocation) == 0
    assert len(_journal(client)) == 1  # only the approval entry
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["outstanding"]) == ("approved", "150.00")


def test_payment_only_for_open_bills_of_the_same_vendor(client, vendor, accounts):
    draft = _bill(client, vendor, accounts, number="DR")
    r = _pay(client, vendor, accounts, [{"bill_id": draft["id"], "amount": "10.00"}])
    assert r.status_code == 422
    assert "is draft and cannot receive payments" in r.json()["detail"]

    other = client.post(
        f"{API}/vendors", json={"code": "OTHER", "name": "Other", "payable_account_id": accounts["ap"]}
    ).json()
    theirs = _approved_bill(client, other, accounts, number="O1")
    r = _pay(client, vendor, accounts, [{"bill_id": theirs["id"], "amount": "10.00"}])
    assert r.status_code == 422
    assert "belongs to another vendor" in r.json()["detail"]

    paid = _approved_bill(client, vendor, accounts, number="P1", total="10.00", lines=_lines(accounts, "10.00"))
    assert _pay(client, vendor, accounts, [{"bill_id": paid["id"], "amount": "10.00"}]).status_code == 201
    r = _pay(client, vendor, accounts, [{"bill_id": paid["id"], "amount": "0.01"}])
    assert r.status_code == 422
    assert "is paid and cannot receive payments" in r.json()["detail"]
    assert _count(client, VendorPayment) == 1


def test_repeated_payment_request_with_idempotency_key_pays_once(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    allocations = [{"bill_id": bill["id"], "amount": "100.00"}]

    first = _pay(client, vendor, accounts, allocations, idempotency_key="pay-2026-01-20-acme")
    assert first.status_code == 201, first.text
    repeat = _pay(client, vendor, accounts, allocations, idempotency_key="pay-2026-01-20-acme")
    assert repeat.status_code == 200, repeat.text
    assert repeat.json() == first.json()

    assert _count(client, VendorPayment) == 1
    assert len(_journal(client)) == 2  # approval + one payment
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["outstanding"]) == ("partially_paid", "50.00")

    # The same key with a different payment is a conflict, not a silent no-op.
    reused = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "50.00"}], idempotency_key="pay-2026-01-20-acme")
    assert reused.status_code == 409
    assert "different payment" in reused.json()["detail"]
    assert _count(client, VendorPayment) == 1

    # Without a key, an identical second request is a real second payment, limited by what is outstanding.
    again = _pay(client, vendor, accounts, allocations)
    assert again.status_code == 422
    assert "exceeds its outstanding" in again.json()["detail"]
    assert _count(client, VendorPayment) == 1


# --- outstanding payables and aging --------------------------------------------------------------


def test_outstanding_and_aging_buckets(client, accounts):
    vendors = {}
    for code in ("ALPHA", "BETA"):
        vendors[code] = client.post(
            f"{API}/vendors", json={"code": code, "name": code.title(), "payable_account_id": accounts["ap"]}
        ).json()

    def approved(vendor, number, total, invoice_date, due_date):
        bill = _bill(
            client, vendor, accounts, number=number, total=total, lines=_lines(accounts, total),
            invoice_date=invoice_date, due_date=due_date,
        )
        assert _approve(client, bill["id"]).status_code == 200
        return bill

    a1 = approved(vendors["ALPHA"], "A1", "100.00", "2026-01-01", "2026-01-31")  # 59 days overdue on 2026-03-31
    approved(vendors["ALPHA"], "A2", "200.00", "2026-03-01", "2026-03-31")  # due today -> current
    approved(vendors["BETA"], "B1", "300.00", "2025-11-01", "2025-12-01")  # 120 days overdue
    b2 = approved(vendors["BETA"], "B2", "50.00", "2026-02-01", "2026-03-03")  # 28 days overdue
    _bill(client, vendors["BETA"], accounts, number="B-draft", total="999.00", lines=_lines(accounts, "999.00"))
    future = approved(vendors["BETA"], "B-future", "10.00", "2026-04-05", "2026-05-05")  # after as_of

    # Partial payment before as_of, full payment of B2 after as_of.
    assert _pay(client, vendors["ALPHA"], accounts, [{"bill_id": a1["id"], "amount": "30.00"}], date_="2026-03-10").status_code == 201
    assert _pay(client, vendors["BETA"], accounts, [{"bill_id": b2["id"], "amount": "50.00"}], date_="2026-04-02").status_code == 201

    outstanding = client.get(f"{API}/outstanding?as_of=2026-03-31").json()
    assert [(r["invoice_number"], r["outstanding"], r["days_overdue"]) for r in outstanding["rows"]] == [
        ("B1", "300.00", 120),
        ("A1", "70.00", 59),
        ("B2", "50.00", 28),
        ("A2", "200.00", 0),
    ]
    assert outstanding["total_outstanding"] == "620.00"
    assert [r["invoice_number"] for r in client.get(f"{API}/outstanding?as_of=2026-03-31&vendor_id={vendors['BETA']['id']}").json()["rows"]] == ["B1", "B2"]

    aging = client.get(f"{API}/aging?as_of=2026-03-31").json()
    assert aging["as_of"] == "2026-03-31"
    assert [
        (r["vendor_code"], r["current"], r["days_1_30"], r["days_31_60"], r["days_61_90"], r["over_90"], r["total"])
        for r in aging["rows"]
    ] == [
        ("ALPHA", "200.00", "0.00", "70.00", "0.00", "0.00", "270.00"),
        ("BETA", "0.00", "50.00", "0.00", "0.00", "300.00", "350.00"),
    ]
    assert aging["totals"] == {
        "current": "200.00", "days_1_30": "50.00", "days_31_60": "70.00",
        "days_61_90": "0.00", "over_90": "300.00", "total": "620.00",
    }

    # Later as_of: B2 is settled, the future bill appears, B1 is deeper in 90+.
    later = client.get(f"{API}/aging?as_of=2026-05-10").json()
    assert {r["vendor_code"]: r["total"] for r in later["rows"]} == {"ALPHA": "270.00", "BETA": "310.00"}
    assert client.get(f"{API}/outstanding?as_of=2026-05-10").json()["rows"][-1]["invoice_number"] == future["invoice_number"]
    assert client.get(f"{API}/aging").json()["as_of"] == date.today().isoformat()


# --- failure rollback ---------------------------------------------------------------------------


class RefusingLedger(JournalLedgerAdapter):
    """Drafts normally, then refuses to post (simulates the core rejecting the entry)."""

    def post(self, db, entry):
        raise LedgerPostingRefused("simulated refusal")


class CrashingLedger(JournalLedgerAdapter):
    def post(self, db, entry):
        raise RuntimeError("ledger unavailable")


class RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, db, *, actor, action, resource):
        self.events.append((actor, action, resource))


def _use(client, **integrations):
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(**integrations)


def test_approval_rolls_back_when_ledger_refuses(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    _use(client, ledger=RefusingLedger())

    r = _approve(client, bill["id"])
    assert r.status_code == 422
    assert "ledger refused" in r.json()["detail"]

    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert current["status"] == "draft"
    assert current["approved_by"] is None and current["journal_entry_id"] is None
    assert _journal(client) == []  # the drafted entry was rolled back with the approval
    assert _count(client, BillPaymentSchedule) == 0

    # Repeating with a working ledger succeeds: nothing was left half-done.
    client.app.dependency_overrides.clear()
    assert _approve(client, bill["id"]).status_code == 200
    assert len(_journal(client)) == 1


def test_payment_rolls_back_when_ledger_refuses(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    _use(client, ledger=RefusingLedger())

    r = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "150.00"}])
    assert r.status_code == 422
    assert _count(client, VendorPayment) == 0
    assert _count(client, PaymentAllocation) == 0
    assert len(_journal(client)) == 1
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["outstanding"]) == ("approved", "150.00")


def test_unexpected_error_during_posting_leaves_no_trace(client, vendor, accounts):
    bill = _bill(client, vendor, accounts)
    _use(client, ledger=CrashingLedger())

    with TestClient(client.app, raise_server_exceptions=False) as lenient:
        r = lenient.post(f"{API}/bills/{bill['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 500

    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "draft"
    assert _journal(client) == []
    assert _count(client, BillPaymentSchedule) == 0


def test_database_overpay_guard_is_the_backstop(client, vendor, accounts, monkeypatch):
    """If the application check were skipped, the trigger still refuses and nothing persists."""
    bill = _approved_bill(client, vendor, accounts)
    monkeypatch.setattr(services.payments, "_allocation_problems", lambda *a, **k: [])

    r = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "999.00"}])
    assert r.status_code == 422, r.text
    assert "exceeds the outstanding amount" in r.json()["detail"]

    assert _count(client, VendorPayment) == 0
    assert _count(client, PaymentAllocation) == 0
    assert len(_journal(client)) == 1
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "approved"


def test_audit_port_receives_events_in_order(client, vendor, accounts):
    audit = RecordingAudit()
    _use(client, audit=audit)
    bill = _bill(client, vendor, accounts)
    assert client.post(f"{API}/bills/{bill['id']}/reject", json={"actor": "carol", "reason": "no"}).status_code == 200
    assert client.post(f"{API}/bills/{bill['id']}/reopen", json={"actor": "dave"}).status_code == 200
    assert _approve(client, bill["id"]).status_code == 200
    pay = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "150.00"}])
    assert pay.status_code == 201
    assert audit.events == [
        ("carol", "payables.bill.rejected", f"supplier_bill:{bill['id']}"),
        ("dave", "payables.bill.reopened", f"supplier_bill:{bill['id']}"),
        ("alice", "payables.bill.approved", f"supplier_bill:{bill['id']}"),
        ("bob", "payables.payment.recorded", f"vendor_payment:{pay.json()['id']}"),
    ]


# --- ledger integrity ------------------------------------------------------------------------------


def test_ap_never_touches_posted_ledger_history(client, vendor, accounts):
    """Whatever happens on the AP side afterwards, the posted entries stay as posted."""
    bill = _approved_bill(client, vendor, accounts)
    pay = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "50.00"}]).json()
    before = _journal(client)

    # Attempts that are refused on the AP side ...
    assert client.put(f"{API}/bills/{bill['id']}", json={"total": "1.00"}).status_code == 409
    assert client.delete(f"{API}/bills/{bill['id']}").status_code == 409
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "500.00"}]).status_code == 422
    # ... and legitimate follow-ups add entries but never alter earlier ones.
    assert client.put(
        f"{API}/bills/{bill['id']}/schedule",
        json={"installments": [{"due_date": "2026-02-09", "amount": "150.00"}]},
    ).status_code == 200
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "100.00"}]).status_code == 201

    after = _journal(client)
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    assert all(entry["status"] == "posted" for entry in after)
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == 3
        assert session.scalar(select(func.count()).select_from(JournalLine)) == 3 + 2 + 2
        assert session.get(SupplierBill, bill["id"]).journal_entry_id == bill["journal_entry_id"]
        assert session.get(VendorPayment, pay["id"]).journal_entry_id == pay["journal_entry_id"]


def test_cash_module_sees_ap_payments_through_the_ledger(client, vendor, accounts):
    """Explicit integration point: payments credit a ledger account the cash module can link."""
    r = client.post("/api/v1/cash-accounts", json={"ledger_account_id": accounts["cash"], "label": "Main"})
    assert r.status_code == 201, r.text
    cash_id = r.json()["id"]
    bill = _approved_bill(client, vendor, accounts)
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "150.00"}]).status_code == 201
    assert client.get(f"/api/v1/cash-accounts/{cash_id}/balance").json()["balance"] == "-150.00"
