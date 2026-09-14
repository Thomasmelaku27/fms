"""Advance payments, holds and disputes, early-payment discounts, vendor status,
near-duplicate review (SQLite and PostgreSQL)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.payables.models import BillDiscount, DuplicateFlag, VendorPayment

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
        "discount": _account(client, "4900", "revenue"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]})
    assert r.status_code == 201, r.text
    return r.json()


def _bill(client, vendor, accounts, number, total, invoice_date="2026-01-10", due_date=None, approve=True, **extra):
    r = client.post(f"{API}/bills", json={
        "vendor_id": vendor["id"], "invoice_number": number, "invoice_date": invoice_date,
        "due_date": due_date or invoice_date, "total": total,
        "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}], **extra})
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _pay(client, vendor, accounts, allocations, amount, date_="2026-01-20", **extra):
    return client.post(f"{API}/payments", json={
        "vendor_id": vendor["id"], "payment_date": date_, "amount": amount, "cash_account_id": accounts["cash"],
        "recorded_by": "bob", "allocations": allocations, **extra})


def _journal(client):
    return client.get("/api/v1/journal-entries").json()


def _count(client, model):
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _tb(client):
    return {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}


# --- advance (unapplied) payments -------------------------------------------------------------------------


def test_advance_payment_posts_to_payables_and_is_not_a_settled_invoice(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    r = _pay(client, vendor, accounts, [], "250.00", reference="ADV-1")
    assert r.status_code == 201, r.text
    payment = r.json()
    assert (payment["allocated_total"], payment["unapplied_amount"], payment["payable_account_id"]) == ("0.00", "250.00", accounts["ap"])
    entry = client.get(f"/api/v1/journal-entries/{payment['journal_entry_id']}").json()
    assert [(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]] == [
        (accounts["ap"], "250.00", "0.00"), (accounts["cash"], "0.00", "250.00")]
    # The bill is untouched; the vendor's net position shows the advance.
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "approved"
    assert client.get(f"{API}/outstanding").json()["total_outstanding"] == "100.00"
    balance = client.get(f"{API}/vendors/{vendor['id']}/balance").json()
    assert (balance["bills_outstanding"], balance["unapplied_advances"], balance["net_owed"]) == ("100.00", "250.00", "-150.00")
    assert client.get(f"{API}/vendor-balances").json()[0]["net_owed"] == "-150.00"
    assert [p["id"] for p in client.get(f"{API}/payments?unapplied_only=true").json()] == [payment["id"]]
    tb = _tb(client)
    assert tb["2000"]["debit"] == "150.00"  # AP has a net debit (vendor credit) balance


def test_partially_allocated_payment_carries_a_remainder(client, vendor, accounts):
    one = _bill(client, vendor, accounts, "B1", "100.00")
    two = _bill(client, vendor, accounts, "B2", "80.00", payable_account_id=accounts["ap2"])
    r = _pay(client, vendor, accounts, [{"bill_id": one["id"], "amount": "100.00"}], "130.00")
    assert r.status_code == 201, r.text
    payment = r.json()
    assert payment["unapplied_amount"] == "30.00"
    entry = client.get(f"/api/v1/journal-entries/{payment['journal_entry_id']}").json()
    assert [(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]] == [
        (accounts["ap"], "130.00", "0.00"), (accounts["cash"], "0.00", "130.00")]
    # Applying the remainder to a bill on another AP account is refused (ledger would drift).
    r = client.post(f"{API}/payments/{payment['id']}/applications", json={"bill_id": two["id"], "amount": "30.00", "actor": "bob"})
    assert r.status_code == 422 and "different payable account" in r.json()["detail"]
    three = _bill(client, vendor, accounts, "B3", "50.00")
    r = client.post(f"{API}/payments/{payment['id']}/applications", json={"bill_id": three["id"], "amount": "30.00", "actor": "bob", "allocation_date": "2026-01-25"})
    assert r.status_code == 200, r.text
    assert r.json()["unapplied_amount"] == "0.00"
    assert client.get(f"{API}/bills/{three['id']}").json()["outstanding"] == "20.00"
    assert len(_journal(client)) == 4  # three approvals + one payment: applying an advance posts nothing
    recon = client.get(f"{API}/reconciliation").json()
    assert recon["total_difference"] == "0.00" and recon["exceptions"] == []


@pytest.mark.parametrize(
    ("setup", "fragment"),
    [
        ("too_much", "exceeds the unapplied amount"),
        ("over_bill", "exceeds the outstanding amount"),
        ("other_vendor", "belongs to another vendor"),
        ("held", "on hold"),
        ("reversed", "payment is reversed"),
        ("early", "cannot precede the payment or the bill"),
        ("twice", "exceeds the outstanding amount"),
    ],
)
def test_invalid_advance_applications(client, vendor, accounts, setup, fragment):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    payment = _pay(client, vendor, accounts, [], "300.00", date_="2026-01-15").json()
    payload = {"bill_id": bill["id"], "amount": "50.00", "actor": "bob", "allocation_date": "2026-01-20"}
    if setup == "too_much":
        payload["amount"] = "300.01"
    elif setup == "over_bill":
        payload["amount"] = "100.01"
    elif setup == "other_vendor":
        other = client.post(f"{API}/vendors", json={"code": "O", "name": "O", "payable_account_id": accounts["ap"]}).json()
        payload["bill_id"] = _bill(client, other, accounts, "O1", "10.00")["id"]
    elif setup == "held":
        assert client.post(f"{API}/bills/{bill['id']}/holds", json={"kind": "hold", "reason": "pending delivery", "actor": "alice"}).status_code == 200
    elif setup == "reversed":
        assert client.post(f"{API}/payments/{payment['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-01-16"}).status_code == 200
    elif setup == "early":
        payload["allocation_date"] = "2026-01-09"
    elif setup == "twice":
        assert client.post(f"{API}/payments/{payment['id']}/applications", json=payload).status_code == 200
        payload["amount"] = "60.00"  # a second application of the same advance is fine; exceeding the bill is not
    r = client.post(f"{API}/payments/{payment['id']}/applications", json=payload)
    assert r.status_code == 422, r.text
    assert fragment in r.json()["detail"]


def test_advance_without_payable_account_is_refused(client, accounts):
    vendor = client.post(f"{API}/vendors", json={"code": "NOAP", "name": "No AP"}).json()
    r = _pay(client, vendor, accounts, [], "10.00")
    assert r.status_code == 422 and "needs a payable account" in r.json()["detail"]
    assert _count(client, VendorPayment) == 0
    ok = _pay(client, vendor, accounts, [], "10.00", payable_account_id=accounts["ap"])
    assert ok.status_code == 201 and ok.json()["payable_account_id"] == accounts["ap"]


def test_idempotent_advance_payment(client, vendor, accounts):
    payload = {"vendor_id": vendor["id"], "payment_date": "2026-01-20", "amount": "40.00", "cash_account_id": accounts["cash"],
               "recorded_by": "bob", "idempotency_key": "adv-1"}
    first = client.post(f"{API}/payments", json=payload)
    assert first.status_code == 201
    repeat = client.post(f"{API}/payments", json=payload)
    assert repeat.status_code == 200 and repeat.json() == first.json()
    explicit = client.post(f"{API}/payments", json={**payload, "payable_account_id": accounts["ap"]})
    assert explicit.status_code == 200  # vendor default == explicit account: same request
    other_account = client.post(f"{API}/payments", json={**payload, "payable_account_id": accounts["ap2"]})
    assert other_account.status_code == 409
    assert _count(client, VendorPayment) == 1


# --- holds and disputes ---------------------------------------------------------------------------------------


def test_holds_block_payments_until_released_and_keep_history(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    held = client.post(f"{API}/bills/{bill['id']}/holds", json={"kind": "dispute", "reason": "Damaged goods", "actor": "alice"})
    assert held.status_code == 200, held.text
    assert held.json()["on_hold"] is True
    hold = held.json()["holds"][0]
    assert (hold["kind"], hold["reason"], hold["placed_by"], hold["is_active"]) == ("dispute", "Damaged goods", "alice", True)
    assert client.post(f"{API}/bills/{bill['id']}/holds", json={"kind": "dispute", "reason": "again", "actor": "alice"}).status_code == 409
    assert client.post(f"{API}/bills/{bill['id']}/holds", json={"kind": "hold", "reason": "also a hold", "actor": "alice"}).status_code == 200

    paid = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "100.00"}], "100.00")
    assert paid.status_code == 422 and "on hold" in paid.json()["detail"]
    assert _count(client, VendorPayment) == 0
    assert client.get(f"{API}/bills?on_hold=true").json()[0]["id"] == bill["id"]
    assert client.get(f"{API}/outstanding").json()["rows"][0]["on_hold"] is True
    # Credits may still settle a disputed bill.
    credit = client.post(f"{API}/credits", json={"vendor_id": vendor["id"], "credit_number": "CN", "credit_date": "2026-01-12", "total": "20.00",
                                                 "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": "20.00"}]}).json()
    assert client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"}).status_code == 200
    assert client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": bill["id"], "amount": "20.00", "actor": "bob", "application_date": "2026-01-15"}).status_code == 200

    release = client.post(f"{API}/bills/{bill['id']}/holds/{hold['id']}/release", json={"actor": "dave", "resolution": "Replacement received"})
    assert release.status_code == 200, release.text
    released = [h for h in release.json()["holds"] if h["id"] == hold["id"]][0]
    assert (released["released_by"], released["resolution"], released["is_active"]) == ("dave", "Replacement received", False)
    assert released["released_at"] is not None
    assert release.json()["on_hold"] is True  # the plain hold is still active
    assert client.post(f"{API}/bills/{bill['id']}/holds/{hold['id']}/release", json={"actor": "dave", "resolution": "x"}).status_code == 409
    second = [h for h in release.json()["holds"] if h["kind"] == "hold"][0]
    assert client.post(f"{API}/bills/{bill['id']}/holds/{second['id']}/release", json={"actor": "dave", "resolution": "ok"}).status_code == 200
    paid = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "80.00"}], "80.00")
    assert paid.status_code == 201, paid.text
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    assert len(client.get(f"{API}/bills/{bill['id']}").json()["holds"]) == 2  # history kept


def test_hold_requires_open_bill_and_reason(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    assert client.post(f"{API}/bills/{bill['id']}/holds", json={"kind": "hold", "actor": "alice"}).status_code == 422
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "100.00"}], "100.00").status_code == 201
    r = client.post(f"{API}/bills/{bill['id']}/holds", json={"kind": "hold", "reason": "late", "actor": "alice"})
    assert r.status_code == 409 and "only open bills can be held" in r.json()["detail"]
    assert client.post(f"{API}/bills/999/holds", json={"kind": "hold", "reason": "x", "actor": "a"}).status_code == 404


# --- early-payment discounts --------------------------------------------------------------------------------


@pytest.fixture
def policy(client, accounts):
    r = client.post(f"{API}/discount-policies", json={"code": "2/10", "description": "2% within 10 days", "discount_percent": "2.00",
                                                       "discount_days": 10, "discount_account_id": accounts["discount"]})
    assert r.status_code == 201, r.text
    return r.json()


def test_discount_policy_validation(client, accounts, policy):
    assert client.post(f"{API}/discount-policies", json={"code": "2/10", "description": "dup", "discount_percent": "2.00",
                                                          "discount_days": 10, "discount_account_id": accounts["discount"]}).status_code == 409
    bad = client.post(f"{API}/discount-policies", json={"code": "X", "description": "bad account", "discount_percent": "2.00",
                                                         "discount_days": 10, "discount_account_id": accounts["ap"]})
    assert bad.status_code == 422 and "revenue or expense" in bad.json()["detail"]
    assert client.post(f"{API}/discount-policies", json={"code": "Y", "description": "pct", "discount_percent": "100",
                                                          "discount_days": 10, "discount_account_id": accounts["discount"]}).status_code == 422
    assert client.get(f"{API}/discount-policies").json()[0]["code"] == "2/10"
    assert client.patch(f"{API}/discount-policies/{policy['id']}", json={"discount_days": 15}).json()["discount_days"] == 15


def test_discount_snapshot_taken_at_approval_and_settled_with_payment(client, vendor, accounts, policy):
    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"discount_policy_id": policy["id"]}).status_code == 200
    bill = _bill(client, vendor, accounts, "B1", "1000.00", invoice_date="2026-01-10", due_date="2026-02-09")
    assert (bill["discount_date"], bill["discount_amount"], bill["discount_account_id"]) == ("2026-01-20", "20.00", accounts["discount"])
    # Changing the policy afterwards does not alter the approved bill (snapshot).
    assert client.patch(f"{API}/discount-policies/{policy['id']}", json={"discount_percent": "5.00"}).status_code == 200
    assert client.get(f"{API}/bills/{bill['id']}").json()["discount_amount"] == "20.00"

    # Paying the net amount inside the window with take_discount settles the bill in full.
    r = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "980.00", "take_discount": True}], "980.00", date_="2026-01-19")
    assert r.status_code == 201, r.text
    payment = r.json()
    assert len(payment["discounts"]) == 1
    discount = payment["discounts"][0]
    assert (discount["amount"], discount["taken_on"], discount["discount_account_id"], discount["is_active"]) == ("20.00", "2026-01-19", accounts["discount"], True)
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["amount_paid"], current["discounts_taken"], current["outstanding"]) == ("paid", "980.00", "20.00", "0.00")
    entry = client.get(f"/api/v1/journal-entries/{discount['journal_entry_id']}").json()
    assert entry["reference"] == f"AP-DISC-{discount['id']}"
    assert [(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]] == [
        (accounts["ap"], "20.00", "0.00"), (accounts["discount"], "0.00", "20.00")]
    tb = _tb(client)
    assert tb["2000"]["credit"] == "0.00" and tb["2000"]["debit"] == "0.00" and tb["4900"]["credit"] == "20.00"
    statement = client.get(f"{API}/vendors/{vendor['id']}/statement?date_from=2026-01-01&date_to=2026-01-31").json()
    assert [(l["type"], l["debit"]) for l in statement["lines"][1:]] == [("discount", "20.00"), ("payment", "980.00")]
    assert statement["totals"]["discounts"] == "20.00" and statement["closing_balance"] == "0.00" and statement["difference"] == "0.00"
    recon = client.get(f"{API}/reconciliation").json()
    assert recon["total_difference"] == "0.00" and recon["exceptions"] == []


def test_discount_rules(client, vendor, accounts, policy):
    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"discount_policy_id": policy["id"]}).status_code == 200
    bill = _bill(client, vendor, accounts, "B1", "1000.00", invoice_date="2026-01-10", due_date="2026-02-09")
    late = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "980.00", "take_discount": True}], "980.00", date_="2026-01-21")
    assert late.status_code == 422 and "no early-payment discount available" in late.json()["detail"]
    partial = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "500.00", "take_discount": True}], "500.00", date_="2026-01-15")
    assert partial.status_code == 422 and "requires an allocation of 980.00" in partial.json()["detail"]
    # Paying in full without taking the discount is allowed; the entitlement simply lapses.
    full = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "1000.00"}], "1000.00", date_="2026-01-15")
    assert full.status_code == 201
    assert _count(client, BillDiscount) == 0
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"


def test_reversing_a_discounted_payment_reverses_the_discount(client, vendor, accounts, policy):
    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"discount_policy_id": policy["id"]}).status_code == 200
    bill = _bill(client, vendor, accounts, "B1", "1000.00", invoice_date="2026-01-10", due_date="2026-02-09")
    payment = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "980.00", "take_discount": True}], "980.00", date_="2026-01-19").json()
    r = client.post(f"{API}/payments/{payment['id']}/reverse", json={"actor": "c", "reason": "bounced", "reversal_date": "2026-01-25"})
    assert r.status_code == 200, r.text
    discount = r.json()["discounts"][0]
    assert discount["is_active"] is False and discount["reversal_date"] == "2026-01-25" and discount["reversed_by"] == "c"
    reversal = client.get(f"/api/v1/journal-entries/{discount['reversal_journal_entry_id']}").json()
    assert reversal["reference"] == f"AP-DISC-REV-{discount['id']}"
    assert [(l["account_id"], l["debit"], l["credit"]) for l in reversal["lines"]] == [
        (accounts["ap"], "0.00", "20.00"), (accounts["discount"], "20.00", "0.00")]
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["outstanding"], current["discounts_taken"]) == ("approved", "1000.00", "0.00")
    assert client.get(f"{API}/outstanding?as_of=2026-01-24").json()["total_outstanding"] == "0.00"
    assert client.get(f"{API}/outstanding?as_of=2026-01-25").json()["total_outstanding"] == "1000.00"
    statement = client.get(f"{API}/vendors/{vendor['id']}/statement?date_from=2026-01-01&date_to=2026-01-31").json()
    assert statement["totals"]["discount_reversals"] == "20.00" and statement["closing_balance"] == "1000.00"
    assert statement["difference"] == "0.00"
    tb = _tb(client)
    assert tb["4900"]["credit"] == "0.00" and tb["4900"]["debit"] == "0.00"
    # The discount is still claimable by a new payment inside the window.
    again = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "980.00", "take_discount": True}], "980.00", date_="2026-01-20")
    assert again.status_code == 201
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    assert client.get(f"{API}/reconciliation").json()["total_difference"] == "0.00"


# --- vendor status ------------------------------------------------------------------------------------------------


def test_vendor_status_controls(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00")
    r = client.post(f"{API}/vendors/{vendor['id']}/status", json={"status": "on_hold", "reason": "Contract under review", "actor": "cfo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["status_reason"], body["status_changed_by"], body["is_active"]) == ("on_hold", "Contract under review", "cfo", True)
    assert body["status_changed_at"] is not None
    assert client.post(f"{API}/vendors/{vendor['id']}/status", json={"status": "on_hold", "reason": "again", "actor": "cfo"}).status_code == 409
    # On hold: documents and approvals fine, payments refused.
    assert _bill(client, vendor, accounts, "B2", "10.00")["status"] == "approved"
    refused = _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "10.00"}], "10.00")
    assert refused.status_code == 422 and "on_hold" in refused.json()["detail"]
    # Suspended: no new documents either; corrections still work.
    assert client.post(f"{API}/vendors/{vendor['id']}/status", json={"status": "suspended", "reason": "Fraud check", "actor": "cfo"}).status_code == 200
    r = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "B3", "invoice_date": "2026-01-10", "total": "1.00"})
    assert r.status_code == 422 and "suspended" in r.json()["detail"]
    assert client.get(f"{API}/vendors/{vendor['id']}").json()["is_active"] is False
    assert client.get(f"{API}/vendors?include_inactive=false").json() == []
    # Legacy flag round-trips onto the status model.
    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"is_active": True}).json()["status"] == "active"
    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"is_active": False}).json()["status"] == "inactive"
    assert client.post(f"{API}/vendors/{vendor['id']}/status", json={"status": "active", "reason": "Cleared", "actor": "cfo"}).status_code == 200
    assert _pay(client, vendor, accounts, [{"bill_id": bill["id"], "amount": "10.00"}], "10.00").status_code == 201
    # Master data: preferred method and bank reference.
    r = client.patch(f"{API}/vendors/{vendor['id']}", json={"preferred_method": "cheque", "bank_reference": "GB00-MASKED-1234"})
    assert (r.json()["preferred_method"], r.json()["bank_reference"]) == ("cheque", "GB00-MASKED-1234")


# --- near-duplicate review ----------------------------------------------------------------------------------------


def test_blocking_duplicate_flag_requires_review_before_approval(client, vendor, accounts):
    original = _bill(client, vendor, accounts, "INV-001", "500.00")
    suspect = _bill(client, vendor, accounts, "INV 001", "500.00", approve=False)  # canonical differs, compact matches
    flags = suspect["duplicate_flags"]
    assert len(flags) == 1
    flag = flags[0]
    assert (flag["suspected_bill_id"], flag["status"], flag["blocks_approval"]) == (original["id"], "pending", True)
    assert flag["score"] >= 60 and "compact_number" in flag["reasons"] and "same_total_date" in flag["reasons"]

    blocked = client.post(f"{API}/bills/{suspect['id']}/approve", json={"actor": "alice"})
    assert blocked.status_code == 422 and "unresolved duplicate flags" in blocked.json()["detail"]
    # Confirming keeps it blocked (the bill should be rejected); clearing lets it through.
    bad_decision = client.post(f"{API}/bills/{suspect['id']}/duplicate-flags/{flag['id']}/review", json={"decision": "pending", "actor": "rev"})
    assert bad_decision.status_code == 422
    confirmed = client.post(f"{API}/bills/{suspect['id']}/duplicate-flags/{flag['id']}/review",
                            json={"decision": "confirmed", "actor": "rev", "note": "same PDF"})
    assert confirmed.status_code == 200
    reviewed = confirmed.json()["duplicate_flags"][0]
    assert (reviewed["status"], reviewed["reviewed_by"], reviewed["review_note"]) == ("confirmed", "rev", "same PDF")
    assert client.post(f"{API}/bills/{suspect['id']}/approve", json={"actor": "alice"}).status_code == 422
    assert client.post(f"{API}/bills/{suspect['id']}/duplicate-flags/{flag['id']}/review", json={"decision": "cleared", "actor": "rev"}).status_code == 409
    assert client.post(f"{API}/bills/{suspect['id']}/reject", json={"actor": "alice", "reason": "duplicate"}).status_code == 200

    # A different suspect, cleared by the reviewer, can be approved.
    other = _bill(client, vendor, accounts, "inv001", "500.00", approve=False)
    flag = other["duplicate_flags"][0]
    assert client.post(f"{API}/bills/{other['id']}/duplicate-flags/{flag['id']}/review", json={"decision": "cleared", "actor": "rev", "note": "separate delivery"}).status_code == 200
    assert client.post(f"{API}/bills/{other['id']}/approve", json={"actor": "alice"}).status_code == 200
    assert _count(client, DuplicateFlag) == 2


def test_advisory_flags_inform_but_do_not_block(client, vendor, accounts):
    _bill(client, vendor, accounts, "INV-1001", "250.00", invoice_date="2026-01-10")
    near = _bill(client, vendor, accounts, "INV-1007", "250.00", invoice_date="2026-01-12", approve=False)
    assert len(near["duplicate_flags"]) == 1
    flag = near["duplicate_flags"][0]
    assert flag["blocks_approval"] is False and "near_number" in flag["reasons"] and "same_total_window" in flag["reasons"]
    assert client.post(f"{API}/bills/{near['id']}/approve", json={"actor": "alice"}).status_code == 200
    # Same amount and date alone is not even advisory (recurring charges are normal).
    plain = _bill(client, vendor, accounts, "SERVICE-A", "250.00", invoice_date="2026-01-10", approve=False)
    assert plain["duplicate_flags"] == []


def test_cross_vendor_duplicate_and_flag_refresh_on_edit(client, vendor, accounts):
    other = client.post(f"{API}/vendors", json={"code": "BETA", "name": "Beta", "payable_account_id": accounts["ap"]}).json()
    _bill(client, other, accounts, "X-77", "300.00")
    mine = _bill(client, vendor, accounts, "X77", "300.00", approve=False)
    assert [f["blocks_approval"] for f in mine["duplicate_flags"]] == [True]
    assert "cross_vendor" in mine["duplicate_flags"][0]["reasons"]
    # Editing the draft re-evaluates pending flags: a different number clears the suspicion.
    edited = client.put(f"{API}/bills/{mine['id']}", json={"invoice_number": "Z-1"})
    assert edited.status_code == 200 and edited.json()["duplicate_flags"] == []
    assert client.post(f"{API}/bills/{mine['id']}/approve", json={"actor": "alice"}).status_code == 200
    # Exact duplicates are still rejected outright, never flagged.
    assert client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": " z-1 ", "invoice_date": "2026-01-10", "total": "1.00"}).status_code == 409
