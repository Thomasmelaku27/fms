"""End-to-end AP scenario across the whole milestone (SQLite and PostgreSQL).

Invoice approval -> partial payment -> credit application and later
unapplication -> advance payment -> a held invoice -> payment proposal, review,
approval and execution -> a failed execution and its retry -> payment
correction -> final reconciliation and period close. Transaction history and
balances are verified at every stage, including as-of reconstruction.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JournalEntry, JournalLine
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.services.gateway import DurableSimulatedGateway, GatewayScript
from app.modules.payables.models import CreditApplication, PaymentAllocation, VendorPayment

API = "/api/v1/payables"
D = Decimal


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def world(client):
    accounts = {
        "ap": _account(client, "2000", "liability"),
        "expense": _account(client, "6000", "expense"),
        "cash": _account(client, "1000", "asset"),
        "discount": _account(client, "4900", "revenue"),
    }
    policy = client.post(f"{API}/discount-policies", json={"code": "2/10", "description": "2% within 10 days", "discount_percent": "2.00",
                                                            "discount_days": 10, "discount_account_id": accounts["discount"]}).json()
    vendors = {}
    for code, name, terms in (("NORTH", "North Timber", 30), ("SOUTH", "South Steel", 45)):
        r = client.post(f"{API}/vendors", json={"code": code, "name": name, "payable_account_id": accounts["ap"], "payment_terms_days": terms,
                                                 "discount_policy_id": policy["id"] if code == "NORTH" else None, "bank_reference": f"IBAN-{code}"})
        assert r.status_code == 201, r.text
        vendors[code] = r.json()
    return {"accounts": accounts, "vendors": vendors}


def _bill(client, world, vendor, number, lines, invoice_date, due_date=None):
    total = sum((D(q) * D(p) for _, q, p in lines), D("0"))
    r = client.post(f"{API}/bills", json={
        "vendor_id": world["vendors"][vendor]["id"], "invoice_number": number, "invoice_date": invoice_date, "due_date": due_date,
        "total": str(total), "lines": [{"description": d, "account_id": world["accounts"]["expense"], "quantity": q, "unit_price": p} for d, q, p in lines]})
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _bill_state(client, bill_id):
    b = client.get(f"{API}/bills/{bill_id}").json()
    return (b["status"], b["amount_paid"], b["credits_applied"], b["discounts_taken"], b["outstanding"])


def _ap_balance(client):
    rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    row = rows.get("2000", {"credit": "0", "debit": "0"})
    return D(row["credit"]) - D(row["debit"])


def _recon(client, as_of):
    r = client.get(f"{API}/reconciliation?as_of={as_of}").json()
    assert r["exceptions"] == [], r["exceptions"]
    return D(r["total_difference"])


def _count(client, model):
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_full_accounts_payable_cycle(client, world):
    accounts, vendors = world["accounts"], world["vendors"]
    north, south = vendors["NORTH"], vendors["SOUTH"]
    gateway = DurableSimulatedGateway(GatewayScript(reject={"SOUTH"}))
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway)

    # 1. Invoices are approved (liabilities posted). NORTH's bill snapshots the 2/10 discount.
    n1 = _bill(client, world, "NORTH", "N-1001", [("oak planks", "40", "25.00"), ("delivery", "1", "200.00")], "2026-03-02", "2026-04-01")
    n2 = _bill(client, world, "NORTH", "N-1002", [("pine boards", "10", "60.00")], "2026-03-05")  # due by terms: 2026-04-04
    s1 = _bill(client, world, "SOUTH", "S-77", [("steel beams", "3", "400.00")], "2026-03-03")  # due 2026-04-17
    s2 = _bill(client, world, "SOUTH", "S-78", [("bolts", "500", "0.90")], "2026-03-10", "2026-04-10")
    assert (n1["total"], n1["discount_amount"], n1["discount_date"]) == ("1200.00", "24.00", "2026-03-12")
    assert (n2["due_date"], s1["due_date"]) == ("2026-04-04", "2026-04-17")
    assert _ap_balance(client) == D("1200.00") + D("600.00") + D("1200.00") + D("450.00")
    assert _recon(client, "2026-03-10") == 0

    # 2. A partial payment on N-1001, inside the discount window but without taking the discount (partial).
    partial = client.post(f"{API}/payments", json={"vendor_id": north["id"], "payment_date": "2026-03-10", "amount": "700.00",
                                                   "cash_account_id": accounts["cash"], "recorded_by": "bob", "reference": "TRF-1",
                                                   "allocations": [{"bill_id": n1["id"], "amount": "700.00"}]})
    assert partial.status_code == 201, partial.text
    assert _bill_state(client, n1["id"]) == ("partially_paid", "700.00", "0.00", "0.00", "500.00")

    # 3. A credit note from NORTH is applied to N-1001 ... and unapplied a month later (it was meant for N-1002).
    credit = client.post(f"{API}/credits", json={"vendor_id": north["id"], "credit_number": "CN-55", "credit_date": "2026-03-12", "total": "100.00",
                                                 "description": "Warped planks", "lines": [{"description": "planks", "account_id": accounts["expense"], "unit_price": "100.00"}]}).json()
    assert client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"}).status_code == 200
    applied = client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": n1["id"], "amount": "100.00", "actor": "bob", "application_date": "2026-03-13"})
    assert applied.status_code == 200
    assert _bill_state(client, n1["id"]) == ("partially_paid", "700.00", "100.00", "0.00", "400.00")
    march_balance_before_unapply = client.get(f"{API}/vendors/{north['id']}/balance?as_of=2026-03-31").json()
    unapplied = client.post(f"{API}/credits/{credit['id']}/applications/{applied.json()['applications'][0]['id']}/unapply",
                            json={"actor": "carol", "reason": "belongs to N-1002", "unapplication_date": "2026-04-14"})
    assert unapplied.status_code == 200
    assert _bill_state(client, n1["id"]) == ("partially_paid", "700.00", "0.00", "0.00", "500.00")
    assert client.get(f"{API}/vendors/{north['id']}/balance?as_of=2026-03-31").json() == march_balance_before_unapply  # March unchanged
    reapplied = client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": n2["id"], "amount": "100.00", "actor": "carol", "application_date": "2026-04-14"})
    assert reapplied.status_code == 200 and reapplied.json()["status"] == "applied"
    assert _bill_state(client, n2["id"]) == ("partially_paid", "0.00", "100.00", "0.00", "500.00")
    assert _count(client, CreditApplication) == 2  # history kept: the closed and the new application

    # 4. SOUTH receives an advance of 1,000 (no invoice yet assigned) - it is owed money, not a settled bill.
    advance = client.post(f"{API}/payments", json={"vendor_id": south["id"], "payment_date": "2026-04-01", "amount": "1000.00",
                                                   "cash_account_id": accounts["cash"], "recorded_by": "bob", "reference": "ADV-S"})
    assert advance.status_code == 201 and advance.json()["unapplied_amount"] == "1000.00"
    south_balance = client.get(f"{API}/vendors/{south['id']}/balance?as_of=2026-04-01").json()
    assert (south_balance["bills_outstanding"], south_balance["unapplied_advances"], south_balance["net_owed"]) == ("1650.00", "1000.00", "650.00")
    assert _bill_state(client, s1["id"])[0] == "approved"  # not settled by the advance

    # 5. S-78 is disputed and must stay out of the payment run.
    hold = client.post(f"{API}/bills/{s2['id']}/holds", json={"kind": "dispute", "reason": "short delivery", "actor": "alice"})
    assert hold.status_code == 200 and hold.json()["on_hold"] is True

    # 6. Payment proposal for everything due by 2026-04-20, paid on 2026-04-15.
    run = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-04-15", "due_through": "2026-04-20",
                                                   "cash_account_id": accounts["cash"], "description": "April run"}).json()
    assert run["status"] == "draft"
    items = {i["bill_id"]: i for i in run["items"]}
    assert set(items) == {n1["id"], n2["id"], s1["id"]}  # S-78 is held
    assert (items[n1["id"]]["pay_amount"], items[n1["id"]]["discount_amount"]) == ("500.00", "0.00")  # window passed
    assert items[n2["id"]]["pay_amount"] == "500.00"
    assert (items[s1["id"]]["advance_amount"], items[s1["id"]]["pay_amount"]) == ("1000.00", "200.00")
    assert f"advance payment #{advance.json()['id']}: 1000.00" in items[s1["id"]]["explanation"]
    assert run["total_pay_amount"] == "1200.00"
    entries_before_run = _count(client, JournalEntry)
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "rev"}).status_code == 422  # four eyes
    approved = client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert _count(client, JournalEntry) == entries_before_run  # approval posts nothing

    # 7. Execution: NORTH is paid, SOUTH is rejected by the (simulated) bank.
    first = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
    assert first["run"]["status"] == "failed"
    outcomes = {o["vendor_code"]: o for o in first["outcomes"]}
    assert outcomes["NORTH"]["success"] and outcomes["NORTH"]["amount"] == "1000.00"
    assert not outcomes["SOUTH"]["success"]
    assert _bill_state(client, n1["id"]) == ("paid", "1200.00", "0.00", "0.00", "0.00")
    assert _bill_state(client, n2["id"]) == ("paid", "500.00", "100.00", "0.00", "0.00")
    assert _bill_state(client, s1["id"])[0] == "approved"  # nothing happened for SOUTH, not even the advance application
    assert client.get(f"{API}/payments/{advance.json()['id']}").json()["unapplied_amount"] == "1000.00"
    assert _count(client, VendorPayment) == 3  # partial, advance, NORTH's run payment

    # 8. Retry after the bank accepts: only SOUTH is executed; NORTH is skipped.
    gateway.script.reject.clear()
    second = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
    assert second["run"]["status"] == "completed" and second["run"]["attempts"] == 2
    outcomes = {o["vendor_code"]: o for o in second["outcomes"]}
    assert outcomes["NORTH"]["skipped"] is True and outcomes["SOUTH"]["success"] is True and outcomes["SOUTH"]["amount"] == "200.00"
    assert _bill_state(client, s1["id"]) == ("paid", "1200.00", "0.00", "0.00", "0.00")
    assert client.get(f"{API}/payments/{advance.json()['id']}").json()["unapplied_amount"] == "0.00"
    assert _count(client, VendorPayment) == 4
    north_run_payment = [p for p in client.get(f"{API}/payments?vendor_id={north['id']}").json() if p["reference"].startswith("SIM-")][0]

    # 9. NORTH's run payment went from the wrong bank account: correct it (reverse + replace) on 2026-04-20.
    other_cash = _account(client, "1010", "asset")
    correction = client.post(f"{API}/payments/{north_run_payment['id']}/correct", json={
        "actor": "carol", "reason": "paid from the wrong account", "reversal_date": "2026-04-20",
        "replacement": {"vendor_id": north["id"], "payment_date": "2026-04-20", "amount": "1000.00", "cash_account_id": other_cash,
                        "recorded_by": "carol", "reference": "TRF-FIX",
                        "allocations": [{"bill_id": n1["id"], "amount": "500.00"}, {"bill_id": n2["id"], "amount": "500.00"}]}})
    assert correction.status_code == 200, correction.text
    assert correction.json()["reversed"]["status"] == "reversed"
    assert correction.json()["replacement"]["corrects_payment_id"] == north_run_payment["id"]
    assert _bill_state(client, n1["id"]) == ("paid", "1200.00", "0.00", "0.00", "0.00")
    assert client.get(f"{API}/outstanding?as_of=2026-04-19").json()["total_outstanding"] == "450.00"  # only S-78
    # The original run payment is still there, immutable, with its allocations and entries.
    original = client.get(f"{API}/payments/{north_run_payment['id']}").json()
    assert original["journal_entry_id"] and original["reversal_journal_entry_id"] and original["allocations"]

    # 10. Final settlement of the disputed bill after the dispute is resolved.
    hold_id = hold.json()["holds"][0]["id"]
    assert client.post(f"{API}/bills/{s2['id']}/holds/{hold_id}/release", json={"actor": "alice", "resolution": "credit agreed separately"}).status_code == 200
    final = client.post(f"{API}/payments", json={"vendor_id": south["id"], "payment_date": "2026-04-25", "amount": "450.00",
                                                 "cash_account_id": accounts["cash"], "recorded_by": "bob", "reference": "TRF-9",
                                                 "allocations": [{"bill_id": s2["id"], "amount": "450.00"}]})
    assert final.status_code == 201
    assert _bill_state(client, s2["id"]) == ("paid", "450.00", "0.00", "0.00", "0.00")

    # 11. Everything reconciles, at every date; history is reconstructible month by month.
    for as_of in ("2026-03-10", "2026-03-31", "2026-04-14", "2026-04-16", "2026-04-20", "2026-04-30"):
        assert _recon(client, as_of) == 0, as_of
    assert _ap_balance(client) == 0
    expectations = {
        "2026-03-31": {"NORTH": ("1000.00", "0.00", "0.00"), "SOUTH": ("1650.00", "0.00", "0.00")},   # 1800 - 700 - 100
        "2026-04-13": {"NORTH": ("1000.00", "0.00", "0.00"), "SOUTH": ("1650.00", "0.00", "1000.00")},
        "2026-04-14": {"NORTH": ("1000.00", "0.00", "0.00"), "SOUTH": ("1650.00", "0.00", "1000.00")},  # credit moved N-1001 -> N-1002
        "2026-04-16": {"NORTH": ("0.00", "0.00", "0.00"), "SOUTH": ("450.00", "0.00", "0.00")},
        "2026-04-30": {"NORTH": ("0.00", "0.00", "0.00"), "SOUTH": ("0.00", "0.00", "0.00")},
    }
    for as_of, per_vendor in expectations.items():
        for code, expected in per_vendor.items():
            b = client.get(f"{API}/vendors/{vendors[code]['id']}/balance?as_of={as_of}").json()
            assert (b["bills_outstanding"], b["unapplied_credits"], b["unapplied_advances"]) == expected, (as_of, code)
    statement = client.get(f"{API}/vendors/{north['id']}/statement?date_from=2026-03-01&date_to=2026-04-30").json()
    assert [l["type"] for l in statement["lines"]] == ["bill", "bill", "payment", "credit", "payment", "payment_reversal", "payment"]
    assert statement["closing_balance"] == "0.00" and statement["difference"] == "0.00"
    assert statement["totals"] == {"bills": "1800.00", "credits": "100.00", "payments": "2700.00", "discounts": "0.00",
                                   "payment_reversals": "1000.00", "credit_reversals": "0.00", "discount_reversals": "0.00"}
    # Ledger: all entries posted and balanced; nothing was ever altered.
    with Session(client.app.state.engine) as session:
        entries = session.scalars(select(JournalEntry)).all()
        assert all(e.status.value == "posted" for e in entries)
        for entry in entries:
            lines = session.scalars(select(JournalLine).where(JournalLine.entry_id == entry.id)).all()
            assert sum(l.debit for l in lines) == sum(l.credit for l in lines)
        assert len(entries) == 4 + 1 + 1 + 1 + 1 + 1 + 2 + 1  # bills, partial, credit, advance, NORTH run, SOUTH run, correction (2), final
    assert _count(client, PaymentAllocation) == 1 + 1 + 2 + 1 + 2 + 1  # partial, advance application, NORTH run, SOUTH run, replacement, final

    # 12. Period-end: April closes once the run is done; March would also be closable.
    validation = client.get(f"{API}/period-end/validate?period_end=2026-04-30").json()
    assert validation["status"] in ("ok", "warnings") and validation["can_close"] is True
    closed = client.post(f"{API}/period-end/close", json={"period_end": "2026-04-30", "actor": "cfo"})
    assert closed.status_code == 201
    # April is now immutable: a backdated unapplication is refused, but nothing in the reports moved.
    assert client.post(f"{API}/credits/{credit['id']}/applications/{reapplied.json()['applications'][-1]['id']}/unapply",
                       json={"actor": "x", "unapplication_date": "2026-04-29"}).status_code == 422
    assert client.get(f"{API}/outstanding?as_of=2026-04-30").json()["total_outstanding"] == "0.00"
