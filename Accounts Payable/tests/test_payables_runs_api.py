"""Payment runs: proposal, review / approval, execution, failure and retry
(SQLite and PostgreSQL)."""

import threading
import time

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.modules.payables import services
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.services.gateway import DurableSimulatedGateway, GatewayScript
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import CreditApplication, PaymentAllocation, VendorPayment

API = "/api/v1/payables"


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {
        "ap": _account(client, "2000", "liability"),
        "expense": _account(client, "6000", "expense"),
        "cash": _account(client, "1000", "asset"),
        "discount": _account(client, "4900", "revenue"),
    }


@pytest.fixture
def vendors(client, accounts):
    out = {}
    for code in ("ALPHA", "BETA", "GAMMA"):
        r = client.post(f"{API}/vendors", json={"code": code, "name": code.title(), "payable_account_id": accounts["ap"],
                                                 "bank_reference": f"IBAN-{code}"})
        assert r.status_code == 201, r.text
        out[code] = r.json()
    return out


def _bill(client, vendor, accounts, number, total, invoice_date, due_date, approve=True):
    r = client.post(f"{API}/bills", json={
        "vendor_id": vendor["id"], "invoice_number": number, "invoice_date": invoice_date, "due_date": due_date,
        "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _run(client, accounts, pay_date="2026-02-10", due_through=None, **extra):
    r = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": pay_date, "due_through": due_through or pay_date,
                                                 "cash_account_id": accounts["cash"], **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _advance(client, run_id, *, review="rev", approve="boss"):
    assert client.post(f"{API}/payment-runs/{run_id}/review", json={"actor": review}).status_code == 200
    r = client.post(f"{API}/payment-runs/{run_id}/approve", json={"actor": approve})
    assert r.status_code == 200, r.text
    return r.json()


def _execute(client, run_id, actor="ops"):
    return client.post(f"{API}/payment-runs/{run_id}/execute", json={"actor": actor})


def _count(client, model):
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _journal(client):
    return client.get("/api/v1/journal-entries").json()


def _items(run):
    return {(i["vendor_id"], i["bill_id"]): i for i in run["items"]}


def _use(client, **integrations):
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(**integrations)


# --- proposal ------------------------------------------------------------------------------------------


def test_proposal_selects_due_installments_and_explains_each_item(client, vendors, accounts):
    a = vendors["ALPHA"]
    due = _bill(client, a, accounts, "A-DUE", "300.00", "2026-01-10", "2026-02-09")
    later = _bill(client, a, accounts, "A-LATER", "200.00", "2026-01-10", "2026-03-09")
    held = _bill(client, a, accounts, "A-HELD", "50.00", "2026-01-10", "2026-02-01")
    assert client.post(f"{API}/bills/{held['id']}/holds", json={"kind": "hold", "reason": "waiting for credit", "actor": "alice"}).status_code == 200
    _bill(client, a, accounts, "A-DRAFT", "10.00", "2026-01-10", "2026-02-01", approve=False)
    split = _bill(client, a, accounts, "A-SPLIT", "1000.00", "2026-01-05", "2026-04-05")
    assert client.put(f"{API}/bills/{split['id']}/schedule", json={"installments": [
        {"due_date": "2026-02-05", "amount": "400.00"}, {"due_date": "2026-04-05", "amount": "600.00"}]}).status_code == 200
    b = vendors["BETA"]
    _bill(client, b, accounts, "B-1", "80.00", "2026-01-20", "2026-02-10")
    on_hold_vendor = vendors["GAMMA"]
    _bill(client, on_hold_vendor, accounts, "G-1", "70.00", "2026-01-20", "2026-02-01")
    assert client.post(f"{API}/vendors/{on_hold_vendor['id']}/status", json={"status": "on_hold", "reason": "kyc", "actor": "cfo"}).status_code == 200

    run = _run(client, accounts, pay_date="2026-02-10", description="Mid-Feb run")
    assert run["status"] == "draft" and run["reference"].startswith("RUN-2026-02-10-")
    items = _items(run)
    assert set(items) == {(a["id"], due["id"]), (a["id"], split["id"]), (b["id"], _bill_id(client, "B-1"))}
    assert items[(a["id"], due["id"])]["pay_amount"] == "300.00"
    partial = items[(a["id"], split["id"])]
    assert (partial["settle_amount"], partial["pay_amount"], partial["installment_no"]) == ("400.00", "400.00", 1)
    assert "installment(s) 1 due by 2026-02-10: 400.00 of 1000.00 outstanding" in partial["explanation"]
    assert run["total_pay_amount"] == "780.00"
    assert len(_journal(client)) == 6  # approvals only: proposing posts nothing
    # Vendor filter and a wider window.
    filtered = _run(client, accounts, pay_date="2026-03-31", vendor_ids=[a["id"]])
    assert {i["bill_id"] for i in filtered["items"]} == {due["id"], later["id"], split["id"]}


def _bill_id(client, number):
    return [b for b in client.get(f"{API}/bills").json() if b["invoice_number"] == number][0]["id"]


def test_proposal_uses_discounts_credits_and_advances(client, vendors, accounts):
    a = vendors["ALPHA"]
    policy = client.post(f"{API}/discount-policies", json={"code": "2/10", "description": "2/10", "discount_percent": "2.00",
                                                            "discount_days": 10, "discount_account_id": accounts["discount"]}).json()
    assert client.patch(f"{API}/vendors/{a['id']}", json={"discount_policy_id": policy["id"]}).status_code == 200
    bill = _bill(client, a, accounts, "A-1", "1000.00", "2026-02-01", "2026-02-05")
    credit = client.post(f"{API}/credits", json={"vendor_id": a["id"], "credit_number": "CN", "credit_date": "2026-02-02", "total": "100.00",
                                                 "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": "100.00"}]}).json()
    assert client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"}).status_code == 200
    advance = client.post(f"{API}/payments", json={"vendor_id": a["id"], "payment_date": "2026-02-03", "amount": "150.00",
                                                   "cash_account_id": accounts["cash"], "recorded_by": "bob"}).json()

    run = _run(client, accounts, pay_date="2026-02-08")
    item = run["items"][0]
    assert (item["settle_amount"], item["discount_amount"], item["credit_amount"], item["advance_amount"], item["pay_amount"]) == (
        "1000.00", "20.00", "100.00", "150.00", "730.00")
    assert f"credit #{credit['id']}: 100.00" in item["explanation"] and f"advance payment #{advance['id']}: 150.00" in item["explanation"]
    # Options switch the sources off; paying after the discount window loses the discount.
    plain = _run(client, accounts, pay_date="2026-02-12", take_discounts=False, use_credits=False, use_advances=False)
    assert plain["items"][0]["pay_amount"] == "1000.00" and plain["items"][0]["discount_amount"] == "0.00"

    _advance(client, run["id"])
    executed = _execute(client, run["id"])
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["run"]["status"] == "completed"
    assert body["outcomes"][0]["success"] and body["outcomes"][0]["amount"] == "730.00"
    current = client.get(f"{API}/bills/{bill['id']}").json()
    assert (current["status"], current["amount_paid"], current["credits_applied"], current["discounts_taken"]) == ("paid", "880.00", "100.00", "20.00")
    assert client.get(f"{API}/credits/{credit['id']}").json()["status"] == "applied"
    assert client.get(f"{API}/payments/{advance['id']}").json()["unapplied_amount"] == "0.00"
    recon = client.get(f"{API}/reconciliation?as_of=2026-02-28").json()
    assert recon["total_difference"] == "0.00" and recon["exceptions"] == []


# --- state machine -----------------------------------------------------------------------------------------


def test_run_transitions_and_safeguards(client, vendors, accounts):
    a = vendors["ALPHA"]
    bill = _bill(client, a, accounts, "A-1", "300.00", "2026-01-10", "2026-02-09")
    run = _run(client, accounts)
    rid = run["id"]
    item = run["items"][0]

    # Draft: items can be adjusted (partial settlement) or excluded; an empty run cannot be reviewed.
    r = client.patch(f"{API}/payment-runs/{rid}/items/{item['id']}", json={"actor": "rev", "settle_amount": "100.00"})
    assert r.status_code == 200 and r.json()["items"][0]["pay_amount"] == "100.00"
    assert client.patch(f"{API}/payment-runs/{rid}/items/{item['id']}", json={"actor": "rev", "settle_amount": "300.01"}).status_code == 422
    excluded = client.post(f"{API}/payment-runs/{rid}/items/{item['id']}/exclude", json={"actor": "rev", "reason": "pay next week"})
    assert excluded.status_code == 200 and excluded.json()["items"][0]["status"] == "excluded"
    assert client.post(f"{API}/payment-runs/{rid}/review", json={"actor": "rev"}).status_code == 422
    assert client.post(f"{API}/payment-runs/{rid}/items/{item['id']}/exclude", json={"actor": "rev", "reason": "x"}).status_code == 409
    assert client.post(f"{API}/payment-runs/{rid}/approve", json={"actor": "boss"}).status_code == 409  # not reviewed
    assert _execute(client, rid).status_code == 409

    run = _run(client, accounts)
    rid, item = run["id"], run["items"][0]
    assert client.post(f"{API}/payment-runs/{rid}/review", json={"actor": "rev"}).status_code == 200
    # Reviewed: frozen (API and database); approver must differ from reviewer; send back reopens.
    assert client.patch(f"{API}/payment-runs/{rid}/items/{item['id']}", json={"actor": "rev", "settle_amount": "10.00"}).status_code == 409
    with Session(client.app.state.engine) as session:
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError, match="payment run items can only change"):
            session.execute(text("UPDATE payment_run_items SET pay_amount = 1 WHERE id = :id"), {"id": item["id"]})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError, match="selection parameters"):
            session.execute(text("UPDATE payment_runs SET pay_date = '2027-01-01' WHERE id = :id"), {"id": rid})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError, match="invalid payment run status transition"):
            session.execute(text("UPDATE payment_runs SET status = 'completed' WHERE id = :id"), {"id": rid})
            session.commit()
        session.rollback()
    same_person = client.post(f"{API}/payment-runs/{rid}/approve", json={"actor": "rev"})
    assert same_person.status_code == 422 and "different person" in same_person.json()["detail"]
    assert client.post(f"{API}/payment-runs/{rid}/send-back", json={"actor": "boss"}).json()["status"] == "draft"
    assert client.post(f"{API}/payment-runs/{rid}/review", json={"actor": "rev"}).status_code == 200
    approved = client.post(f"{API}/payment-runs/{rid}/approve", json={"actor": "boss"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "boss" and approved.json()["approved_at"] is not None
    assert len(_journal(client)) == 1  # approval of a run posts nothing
    assert client.post(f"{API}/payment-runs/{rid}/review", json={"actor": "rev"}).status_code == 409  # repeat
    cancelled = client.post(f"{API}/payment-runs/{rid}/cancel", json={"actor": "boss", "reason": "cash flow"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled" and cancelled.json()["cancel_reason"] == "cash flow"
    assert _execute(client, rid).status_code == 409
    assert client.post(f"{API}/payment-runs/{rid}/cancel", json={"actor": "boss", "reason": "again"}).status_code == 409
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "approved"
    assert _count(client, VendorPayment) == 0
    assert [r["status"] for r in client.get(f"{API}/payment-runs?status=cancelled").json()] == ["cancelled"]
    assert client.get(f"{API}/payment-runs/999").status_code == 404


# --- execution, failure and retry -----------------------------------------------------------------------------


def test_execution_records_one_payment_per_vendor_and_posts_once(client, vendors, accounts):
    a, b = vendors["ALPHA"], vendors["BETA"]
    a1 = _bill(client, a, accounts, "A-1", "300.00", "2026-01-10", "2026-02-01")
    a2 = _bill(client, a, accounts, "A-2", "200.00", "2026-01-12", "2026-02-05")
    b1 = _bill(client, b, accounts, "B-1", "80.00", "2026-01-20", "2026-02-10")
    gateway = DurableSimulatedGateway()
    _use(client, gateway=gateway)
    run = _run(client, accounts)
    _advance(client, run["id"])
    entries_before = len(_journal(client))

    r = _execute(client, run["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["status"] == "completed" and body["run"]["attempts"] == 1
    assert body["run"]["completed_at"] is not None and body["run"]["executed_by"] == "ops"
    outcomes = {o["vendor_code"]: o for o in body["outcomes"]}
    assert outcomes["ALPHA"]["amount"] == "500.00" and outcomes["BETA"]["amount"] == "80.00"
    assert all(o["success"] and o["payment_id"] and o["operation_uid"] and o["state"] == "completed" for o in outcomes.values())
    # One gateway submission per vendor, keyed by the durable operation id.
    assert [kind for kind, _ in gateway.calls] == ["submit", "submit"]
    assert {token for _, token in gateway.calls} == {o["operation_uid"] for o in outcomes.values()}

    payments = client.get(f"{API}/payments").json()
    assert [(p["vendor_id"], p["amount"]) for p in payments] == [(a["id"], "500.00"), (b["id"], "80.00")]
    assert payments[0]["reference"].startswith("SIM-") and payments[0]["reference"].endswith("-ALPHA")
    assert payments[0]["idempotency_key"] == f"op:{outcomes['ALPHA']['operation_uid']}"
    status = client.get(f"{API}/payment-operations/{outcomes['ALPHA']['operation_uid']}").json()
    assert status["state"] == "completed" and status["checklist"]["reconciles"] is True
    assert status["destination_reference_masked"] == "******LPHA"
    for bill in (a1, a2, b1):
        assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    assert len(_journal(client)) == entries_before + 2
    items = client.get(f"{API}/payment-runs/{run['id']}").json()["items"]
    assert all(i["status"] == "paid" and i["payment_id"] and i["execution_reference"] for i in items)
    # A completed run cannot be executed again.
    assert _execute(client, run["id"]).status_code == 409
    assert _count(client, VendorPayment) == 2


def test_failed_vendor_leaves_no_trace_and_retry_pays_only_the_failed_vendor(client, vendors, accounts):
    a, b = vendors["ALPHA"], vendors["BETA"]
    a1 = _bill(client, a, accounts, "A-1", "300.00", "2026-01-10", "2026-02-01")
    b1 = _bill(client, b, accounts, "B-1", "80.00", "2026-01-20", "2026-02-10")
    gateway = DurableSimulatedGateway(GatewayScript(reject={"BETA"}))
    _use(client, gateway=gateway)
    run = _run(client, accounts)
    _advance(client, run["id"])

    first = _execute(client, run["id"]).json()
    assert first["run"]["status"] == "failed"
    outcomes = {o["vendor_code"]: o for o in first["outcomes"]}
    assert outcomes["ALPHA"]["success"] is True
    assert outcomes["BETA"]["success"] is False and "simulated rejection" in outcomes["BETA"]["error"]
    assert client.get(f"{API}/bills/{a1['id']}").json()["status"] == "paid"
    assert client.get(f"{API}/bills/{b1['id']}").json()["status"] == "approved"
    assert _count(client, VendorPayment) == 1 and _count(client, PaymentAllocation) == 1
    assert len(_journal(client)) == 3  # two approvals + ALPHA's payment
    items = _items(client.get(f"{API}/payment-runs/{run['id']}").json())
    assert items[(b["id"], b1["id"])]["status"] == "failed" and "simulated rejection" in items[(b["id"], b1["id"])]["failure_reason"]

    # Retry while the bank still rejects: still failed, still nothing for BETA, ALPHA untouched.
    second = _execute(client, run["id"]).json()
    assert second["run"]["status"] == "failed" and second["run"]["attempts"] == 2
    assert {o["vendor_code"]: o["skipped"] for o in second["outcomes"]} == {"ALPHA": True, "BETA": False}
    assert _count(client, VendorPayment) == 1

    # Bank accepts now: the retry pays BETA exactly once and skips ALPHA (a new operation, the
    # rejected one is terminal).
    gateway.script.reject.clear()
    third = _execute(client, run["id"]).json()
    assert third["run"]["status"] == "completed" and third["run"]["attempts"] == 3
    outcomes = {o["vendor_code"]: o for o in third["outcomes"]}
    assert outcomes["ALPHA"]["skipped"] is True and outcomes["BETA"]["success"] is True
    assert _count(client, VendorPayment) == 2
    assert client.get(f"{API}/bills/{b1['id']}").json()["status"] == "paid"
    assert len(_journal(client)) == 4
    assert [kind for kind, _ in gateway.calls] == ["submit"] * 4  # ALPHA, BETA (rejected), BETA (rejected), BETA (accepted)
    ops = client.get(f"{API}/payment-operations?vendor_id={b['id']}").json()
    assert sorted(o["state"] for o in ops["items"]) == ["completed", "failed", "failed"]
    assert client.get(f"{API}/reconciliation").json()["total_difference"] == "0.00"


def test_ledger_refusal_after_bank_success_is_retried_without_paying_twice(client, vendors, accounts):
    """The bank accepted but our booking failed: the transfer is durable state, nothing is
    recorded in the books, and the retry records it once without asking the bank again."""
    a = vendors["ALPHA"]
    a1 = _bill(client, a, accounts, "A-1", "300.00", "2026-01-10", "2026-02-01")
    gateway = DurableSimulatedGateway()

    class FlakyLedger(JournalLedgerAdapter):
        def __init__(self):
            self.fail_next = True

        def post(self, db, entry):
            if self.fail_next and entry.reference.startswith("AP-PAY-"):
                self.fail_next = False
                raise LedgerPostingRefused("simulated ledger outage")
            return super().post(db, entry)

    ledger = FlakyLedger()
    _use(client, gateway=gateway, ledger=ledger)
    run = _run(client, accounts)
    _advance(client, run["id"])

    first = _execute(client, run["id"]).json()
    assert first["run"]["status"] == "failed"
    outcome = first["outcomes"][0]
    assert outcome["state"] == "reconciliation_required" and "recording pending recovery" in outcome["error"]
    assert _count(client, VendorPayment) == 0 and _count(client, PaymentAllocation) == 0
    assert len(_journal(client)) == 1  # no orphan payment entry, not even a draft
    assert client.get(f"{API}/bills/{a1['id']}").json()["status"] == "approved"
    status = client.get(f"{API}/payment-operations/{outcome['operation_uid']}").json()
    assert status["checklist"]["gateway_confirmed"] is True and status["checklist"]["ap_payment_recorded"] is False
    assert status["failure_category"] == "ledger" and status["recovery_safe"] is True and status["recovery_action"] == "retry_recording"

    second = _execute(client, run["id"]).json()
    assert second["run"]["status"] == "completed"
    assert _count(client, VendorPayment) == 1 and len(_journal(client)) == 2
    # The bank was asked exactly once; the retry only re-recorded the confirmed transfer.
    assert [kind for kind, _ in gateway.calls] == ["submit"]
    payment = client.get(f"{API}/payments").json()[0]
    assert payment["reference"] == status["gateway_confirmation_ref"]


def test_material_change_after_approval_invalidates_the_approval(client, vendors, accounts):
    a = vendors["ALPHA"]
    a1 = _bill(client, a, accounts, "A-1", "300.00", "2026-01-10", "2026-02-01")
    a2 = _bill(client, a, accounts, "A-2", "200.00", "2026-01-12", "2026-02-05")
    run = _run(client, accounts)
    _advance(client, run["id"])
    # Between approval and execution someone pays A-1 manually and disputes A-2.
    assert client.post(f"{API}/payments", json={"vendor_id": a["id"], "payment_date": "2026-02-09", "amount": "300.00",
                                                "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                "allocations": [{"bill_id": a1["id"], "amount": "300.00"}]}).status_code == 201
    assert client.post(f"{API}/bills/{a2['id']}/holds", json={"kind": "dispute", "reason": "price", "actor": "alice"}).status_code == 200
    result = _execute(client, run["id"])
    assert result.status_code == 409, result.text
    assert "Approval invalidated" in result.json()["detail"] and "is paid" in result.json()["detail"] and "on hold" in result.json()["detail"]
    current = client.get(f"{API}/payment-runs/{run['id']}").json()
    assert current["status"] == "draft" and current["approved_by"] is None and "on hold" in current["approval_invalidated_reason"]
    assert _count(client, VendorPayment) == 1  # only the manual payment; nothing was sent to the gateway
    assert client.get(f"{API}/payment-operations").json()["total"] == 0
    # The reviewer fixes the proposal (drop both items) -> the run has nothing left to pay.
    items = current["items"]
    for item in items:
        assert client.post(f"{API}/payment-runs/{run['id']}/items/{item['id']}/exclude", json={"actor": "rev", "reason": "stale"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 422
    assert client.post(f"{API}/payment-runs/{run['id']}/cancel", json={"actor": "boss", "reason": "stale"}).status_code == 200
    assert client.get(f"{API}/bills/{a2['id']}").json()["outstanding"] == "200.00"


def test_credit_only_settlement_needs_no_bank_instruction(client, vendors, accounts):
    a = vendors["ALPHA"]
    bill = _bill(client, a, accounts, "A-1", "100.00", "2026-01-10", "2026-02-01")
    credit = client.post(f"{API}/credits", json={"vendor_id": a["id"], "credit_number": "CN", "credit_date": "2026-01-15", "total": "100.00",
                                                 "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": "100.00"}]}).json()
    assert client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"}).status_code == 200
    gateway = DurableSimulatedGateway()
    _use(client, gateway=gateway)
    run = _run(client, accounts)
    assert run["items"][0]["pay_amount"] == "0.00" and run["items"][0]["credit_amount"] == "100.00"
    _advance(client, run["id"])
    result = _execute(client, run["id"]).json()
    assert result["run"]["status"] == "completed"
    assert gateway.calls == []  # nothing to send to the bank
    assert result["outcomes"][0]["payment_id"] is None
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    assert _count(client, CreditApplication) == 1 and _count(client, VendorPayment) == 0


def test_execution_is_refused_in_a_closed_period(client, vendors, accounts):
    a = vendors["ALPHA"]
    _bill(client, a, accounts, "A-1", "100.00", "2026-01-10", "2026-02-01")
    run = _run(client, accounts, pay_date="2026-02-10")
    _advance(client, run["id"])
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"}).status_code == 422  # run in flight blocks
    assert client.post(f"{API}/payment-runs/{run['id']}/cancel", json={"actor": "boss", "reason": "closing"}).status_code == 200
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"}).status_code == 201
    late = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-20", "cash_account_id": accounts["cash"]})
    assert late.status_code == 422 and "closed AP period" in late.json()["detail"]


# --- concurrency ---------------------------------------------------------------------------------------------


def test_concurrent_execution_attempts_only_one_wins(client, vendors, accounts):
    a = vendors["ALPHA"]
    _bill(client, a, accounts, "A-1", "300.00", "2026-01-10", "2026-02-01")
    run = _run(client, accounts)
    _advance(client, run["id"])
    original = services.payment_runs._execute_group
    gate = threading.Event()

    def slow_group(*args, **kwargs):
        gate.wait(timeout=5)
        return original(*args, **kwargs)

    results = {}

    def attempt(name):
        results[name] = _execute(client, run["id"], actor=name)

    services.payment_runs._execute_group = slow_group
    try:
        t1 = threading.Thread(target=attempt, args=("first",))
        t1.start()
        time.sleep(0.5)  # let the first claim the run (committed "executing")
        second = _execute(client, run["id"], actor="second")
        gate.set()
        t1.join(timeout=30)
    finally:
        services.payment_runs._execute_group = original
    assert second.status_code == 409, second.text
    assert results["first"].status_code == 200
    assert results["first"].json()["run"]["status"] == "completed"
    assert _count(client, VendorPayment) == 1
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == 2
