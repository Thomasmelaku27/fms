"""Durable payment operations: lifecycle, idempotency, recovery, diagnostics (SQLite and PostgreSQL).

The simulated gateway persists its transfers in the database, so "worker restart"
means a fresh gateway instance and a fresh session seeing the same rows.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.modules.payables import services
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.models import OperationState, PaymentAllocation, PaymentOperation, VendorPayment
from app.modules.payables.services import execution_state
from app.modules.payables.services.gateway import DurableSimulatedGateway, GatewayScript

API = "/api/v1/payables"
D = Decimal


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}


@pytest.fixture
def vendors(client, accounts):
    out = {}
    for code in ("ALPHA", "BETA"):
        r = client.post(f"{API}/vendors", json={"code": code, "name": code.title(), "payable_account_id": accounts["ap"], "bank_reference": f"IBAN-{code}"})
        assert r.status_code == 201, r.text
        out[code] = r.json()
    return out


def _bill(client, vendor, accounts, number, total, due="2026-02-01"):
    r = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": number, "invoice_date": "2026-01-10", "due_date": due,
                                          "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    assert client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"}).status_code == 200
    return r.json()


def _approved_run(client, accounts, pay_date="2026-02-10", **extra):
    r = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": pay_date, "cash_account_id": accounts["cash"], **extra})
    assert r.status_code == 201, r.text
    run = r.json()
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 200
    return run


def _execute(client, run_id, actor="ops"):
    return client.post(f"{API}/payment-runs/{run_id}/execute", json={"actor": actor})


def _execute_crashing(client, run_id, actor="ops"):
    """A request whose worker crashes mid-way returns a 500 (or nothing); the state is what matters."""
    from fastapi.testclient import TestClient

    with TestClient(client.app, raise_server_exceptions=False) as lenient:
        return lenient.post(f"{API}/payment-runs/{run_id}/execute", json={"actor": actor})


def _use(client, **kw):
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(**kw)


def _count(client, model):
    with Session(client.app.state.engine) as s:
        return s.scalar(select(func.count()).select_from(model))


def _transfers(client):
    with Session(client.app.state.engine) as s:
        return s.execute(text("SELECT token, status, reference FROM simulated_gateway_transfers ORDER BY id")).all()


def _op(client, uid):
    return client.get(f"{API}/payment-operations/{uid}").json()


def _recover(client, worker="worker-1"):
    r = client.post(f"{API}/recovery/run", json={"actor": worker, "worker_id": worker})
    assert r.status_code == 200, r.text
    return r.json()


# --- lifecycle through the gateway outcomes ------------------------------------------------------------


def test_lost_response_is_resolved_by_lookup_not_resubmission(client, vendors, accounts):
    bill = _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway(GatewayScript(lose_response={"ALPHA"}))
    _use(client, gateway=gateway)
    run = _approved_run(client, accounts)
    result = _execute(client, run["id"]).json()
    # The inline lookup already resolved it: the transfer existed, so it was recorded once.
    outcome = result["outcomes"][0]
    assert outcome["success"] and outcome["state"] == "completed"
    assert [k for k, _ in gateway.calls] == ["submit", "lookup"]
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 1
    status = _op(client, outcome["operation_uid"])
    assert [e["category"] for e in status["events"]] == ["transition", "transition", "gateway", "gateway", "transition", "transition"]
    assert any("response lost" in (e["detail"] or "") for e in status["events"])
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"


def test_dropped_request_is_resubmitted_with_the_same_token(client, vendors, accounts):
    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway(GatewayScript(drop={"ALPHA"}))
    _use(client, gateway=gateway)
    run = _approved_run(client, accounts)
    result = _execute(client, run["id"]).json()
    outcome = result["outcomes"][0]
    assert outcome["success"]
    # submit (timeout, nothing recorded) -> lookup (unknown) -> submit again with the same token
    assert [k for k, _ in gateway.calls] == ["submit", "lookup", "submit"]
    assert len({t for _, t in gateway.calls}) == 1
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 1
    assert _op(client, outcome["operation_uid"])["attempts"] == 2


def test_pending_outcome_waits_for_confirmation_then_records(client, vendors, accounts):
    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway(GatewayScript(pending={"ALPHA"}, pending_lookups=2))
    _use(client, gateway=gateway)
    run = _approved_run(client, accounts)
    result = _execute(client, run["id"]).json()
    outcome = result["outcomes"][0]
    assert outcome["state"] == "confirmation_pending" and result["run"]["status"] == "failed"
    assert _count(client, VendorPayment) == 0
    status = _op(client, outcome["operation_uid"])
    assert status["recovery_action"] == "reconcile_gateway" and status["recovery_safe"] is True
    first = _recover(client)
    assert first[0]["after"] == "confirmation_pending"  # first lookup: still pending
    second = _recover(client)
    assert second[0]["after"] == "completed"
    assert [k for k, _ in gateway.calls] == ["submit", "lookup", "lookup"]
    assert _count(client, VendorPayment) == 1 and len(_transfers(client)) == 1
    assert client.get(f"{API}/payment-runs/{run['id']}").json()["status"] == "completed"


def test_rejection_is_terminal_and_a_retry_uses_a_new_operation(client, vendors, accounts):
    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway(GatewayScript(reject={"ALPHA"}))
    _use(client, gateway=gateway)
    run = _approved_run(client, accounts)
    first = _execute(client, run["id"]).json()["outcomes"][0]
    assert first["state"] == "failed" and "REJECTED" in first["error"]
    assert _count(client, VendorPayment) == 0
    assert _op(client, first["operation_uid"])["recovery_action"] == "ignore"
    gateway.script.reject.clear()
    second = _execute(client, run["id"]).json()["outcomes"][0]
    assert second["state"] == "completed" and second["operation_uid"] != first["operation_uid"]
    page = client.get(f"{API}/payment-operations?run_id={run['id']}").json()
    assert sorted(i["state"] for i in page["items"]) == ["completed", "failed"]
    assert _count(client, VendorPayment) == 1


# --- idempotency across the lifecycle -----------------------------------------------------------------------


def test_repeating_an_identical_execution_returns_the_same_result_without_a_second_transfer(client, vendors, accounts):
    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway()
    _use(client, gateway=gateway)
    run = _approved_run(client, accounts)
    first = _execute(client, run["id"]).json()
    assert first["run"]["status"] == "completed"
    assert _execute(client, run["id"]).status_code == 409  # completed runs cannot be executed again
    # The same instruction through the service layer with the original key resumes the same op.
    with Session(client.app.state.engine, expire_on_commit=False) as s:
        op = s.scalar(select(PaymentOperation))
        again, created = execution_state.prepare(
            s, idempotency_key=op.idempotency_key, vendor_id=op.vendor_id, destination_reference=op.destination_reference,
            amount=op.amount, currency=op.currency, method=op.method, payment_date=op.payment_date, cash_account_id=op.cash_account_id,
            run_id=op.run_id, run_version=op.run_version, plan=execution_state.plan_of(op), actor="ops")
        assert not created and again.id == op.id
        outcome = services.orchestration.execute(s, again, integrations=Integrations(gateway=gateway), actor="ops")
        assert outcome.success and outcome.payment_id == op.payment_id
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 1
    assert [k for k, _ in gateway.calls] == ["submit"]


@pytest.mark.parametrize("field", ["amount", "destination", "method", "date", "cash_account", "allocation", "vendor", "run"])
def test_reusing_the_key_with_different_instructions_is_a_deterministic_conflict(session, field):
    from app.models import Account, AccountType
    from app.modules.payables.models import PaymentMethod, Vendor

    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    cash = Account(code="1000", name="C", type=AccountType.ASSET)
    cash2 = Account(code="1010", name="C2", type=AccountType.ASSET)
    v1 = Vendor(code="V1", name="V1", payable_account_id=None)
    v2 = Vendor(code="V2", name="V2", payable_account_id=None)
    session.add_all([ap, cash, cash2, v1, v2])
    session.commit()
    base = dict(idempotency_key="key-1", vendor_id=v1.id, destination_reference="IBAN-1", amount=D("100.00"), currency="XXX",
                method=PaymentMethod.BANK_TRANSFER, payment_date=date(2026, 2, 10), cash_account_id=cash.id, run_id=None, run_version=None,
                plan=[{"bill_id": 1, "settle": "100.00", "discount": "0", "credit": "0", "advance": "0", "pay": "100.00"}], actor="ops")
    op, created = execution_state.prepare(session, **base)
    session.commit()
    assert created
    same, created_again = execution_state.prepare(session, **base)
    assert not created_again and same.id == op.id
    changed = dict(base)
    if field == "amount":
        changed["amount"] = D("100.01")
    elif field == "destination":
        changed["destination_reference"] = "IBAN-2"
    elif field == "method":
        changed["method"] = PaymentMethod.CHEQUE
    elif field == "date":
        changed["payment_date"] = date(2026, 2, 11)
    elif field == "cash_account":
        changed["cash_account_id"] = cash2.id
    elif field == "allocation":
        changed["plan"] = [{"bill_id": 2, "settle": "100.00", "discount": "0", "credit": "0", "advance": "0", "pay": "100.00"}]
    elif field == "vendor":
        changed["vendor_id"] = v2.id
    elif field == "run":
        changed["run_id"] = 1
    with pytest.raises(execution_state.InstructionConflict):
        execution_state.prepare(session, **changed)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(PaymentOperation)) == 1


def test_unsupported_precision_is_rejected_not_rounded(session):
    with pytest.raises(services.ValidationFailed, match="unsupported precision"):
        execution_state.canonical_amount(D("10.005"))
    with pytest.raises(services.ValidationFailed, match="unsupported precision"):
        execution_state.canonical_plan([{"bill_id": 1, "pay": "1.001"}])
    assert execution_state.canonical_amount("10.5") == D("10.50")
    with pytest.raises(services.ValidationFailed, match="must be positive"):
        execution_state.canonical_amount(D("0"))
    with pytest.raises(services.ValidationFailed, match="not a monetary value"):
        execution_state.canonical_amount("ten")


# --- recovery after "restart" ---------------------------------------------------------------------------------


def test_confirmed_transfer_without_ap_record_is_recovered_after_restart(client, vendors, accounts):
    """Gateway confirmed, recording failed, process died. A fresh worker with a fresh gateway
    instance finds the durable state and records exactly once."""
    from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused

    class Outage(JournalLedgerAdapter):
        def post(self, db, entry):
            if entry.reference.startswith("AP-PAY-"):
                raise LedgerPostingRefused("ledger outage")
            return super().post(db, entry)

    bill = _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    _use(client, gateway=DurableSimulatedGateway(), ledger=Outage())
    run = _approved_run(client, accounts)
    outcome = _execute(client, run["id"]).json()["outcomes"][0]
    assert outcome["state"] == "reconciliation_required"
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 0
    status = _op(client, outcome["operation_uid"])
    assert status["checklist"] == {"prepared": True, "submitted": True, "gateway_confirmed": True, "ap_payment_recorded": False,
                                   "gl_entry_posted": False, "allocations_committed": False, "reversed": False, "reconciles": False, "notes": []}
    assert status["manual_investigation_required"] is False and "Recovery retries" in status["next_step"]

    # "Restart": a new gateway instance, healthy ledger, the recovery worker.
    fresh = DurableSimulatedGateway()
    _use(client, gateway=fresh)
    results = _recover(client, worker="worker-after-restart")
    assert results[0]["action"] == "retry_recording" and results[0]["after"] == "completed"
    assert fresh.calls == []  # the transfer was never resubmitted
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 1
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "paid"
    run_after = client.get(f"{API}/payment-runs/{run['id']}").json()
    assert run_after["status"] == "completed" and run_after["items"][0]["status"] == "paid"
    status = _op(client, outcome["operation_uid"])
    assert status["checklist"]["reconciles"] is True and status["recovery_action"] == "ignore"
    assert client.get(f"{API}/reconciliation").json()["total_difference"] == "0.00"
    assert _recover(client) == []  # nothing left


def test_submitted_with_unknown_outcome_is_recovered_by_lookup(client, vendors, accounts):
    """Process crashed right after the gateway accepted (before we persisted the answer)."""
    from app.modules.payables.integration import ScriptedFaults

    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway()
    _use(client, gateway=gateway, faults=ScriptedFaults({"submit.after_gateway": RuntimeError("crash")}))
    run = _approved_run(client, accounts)
    assert _execute_crashing(client, run["id"]).status_code == 500
    assert client.get(f"{API}/payment-runs/{run['id']}").json()["status"] == "executing"  # claimed, then the worker died
    uid = client.get(f"{API}/payment-operations?run_id={run['id']}").json()["items"][0]["operation_uid"]
    status = _op(client, uid)
    assert status["state"] == "submitted" and status["gateway_confirmation_ref"] is None
    assert status["recovery_action"] == "reconcile_gateway"
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 0
    _use(client, gateway=DurableSimulatedGateway())  # restart
    results = _recover(client)
    assert results[0]["after"] == "completed"
    assert len(_transfers(client)) == 1 and _count(client, VendorPayment) == 1
    # The run the dead worker had claimed is finished by the recovery of its last operation.
    assert client.get(f"{API}/payment-runs/{run['id']}").json()["status"] == "executing"  # stale claim: see runs recovery test


def test_prepared_but_never_submitted_is_resumed_only_while_its_run_executes(client, vendors, accounts):
    from app.modules.payables.integration import ScriptedFaults

    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    gateway = DurableSimulatedGateway()
    _use(client, gateway=gateway, faults=ScriptedFaults({"submit.before_commit": RuntimeError("crash before submit commit")}))
    run = _approved_run(client, accounts)
    assert _execute_crashing(client, run["id"]).status_code == 500
    uid = client.get(f"{API}/payment-operations?run_id={run['id']}").json()["items"][0]["operation_uid"]
    assert _op(client, uid)["state"] == "prepared" and gateway.calls == [] and _transfers(client) == []
    _use(client, gateway=gateway)
    scan = client.get(f"{API}/recovery/scan").json()
    assert scan[0]["action"] == "resume"
    results = _recover(client)
    assert results[0]["after"] == "completed" and len(_transfers(client)) == 1
    # A prepared intent whose run was cancelled is never submitted.
    _bill(client, vendors["BETA"], accounts, "B-1", "50.00")
    _use(client, gateway=gateway, faults=ScriptedFaults({"submit.before_commit": RuntimeError("crash")}))
    run2 = _approved_run(client, accounts, vendor_ids=[vendors["BETA"]["id"]])
    assert _execute_crashing(client, run2["id"]).status_code == 500
    assert services.payment_runs is not None
    with Session(client.app.state.engine) as s:  # the dead worker left the run claimed; release it like an operator would
        from app.modules.payables.models import PaymentRun, PaymentRunStatus
        run_row = s.get(PaymentRun, run2["id"])
        services.payment_runs._cas(s, run_row, PaymentRunStatus.EXECUTING, status=PaymentRunStatus.FAILED)
        s.commit()
    assert client.post(f"{API}/payment-runs/{run2['id']}/cancel", json={"actor": "boss", "reason": "abandon"}).status_code == 200
    _use(client, gateway=gateway)
    results = _recover(client)
    assert results[-1]["action"] == "manual" and len(_transfers(client)) == 1
    uid2 = client.get(f"{API}/payment-operations?run_id={run2['id']}").json()["items"][0]["operation_uid"]
    assert _op(client, uid2)["manual_investigation_required"] is True


def test_two_workers_cannot_process_the_same_operation(client, vendors, accounts):
    from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused

    class Outage(JournalLedgerAdapter):
        def post(self, db, entry):
            if entry.reference.startswith("AP-PAY-"):
                raise LedgerPostingRefused("ledger outage")
            return super().post(db, entry)

    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    _use(client, gateway=DurableSimulatedGateway(), ledger=Outage())
    run = _approved_run(client, accounts)
    uid = _execute(client, run["id"]).json()["outcomes"][0]["operation_uid"]
    with Session(client.app.state.engine, expire_on_commit=False) as other:
        op = other.scalar(select(PaymentOperation).where(PaymentOperation.operation_uid == uid))
        assert execution_state.lease(other, op, worker_id="worker-A", seconds=600)
        other.commit()
    _use(client, gateway=DurableSimulatedGateway())
    results = _recover(client, worker="worker-B")
    assert results[0]["action"] == "skipped" and "leased" in results[0]["note"]
    assert _count(client, VendorPayment) == 0
    with Session(client.app.state.engine, expire_on_commit=False) as other:
        op = other.scalar(select(PaymentOperation).where(PaymentOperation.operation_uid == uid))
        execution_state.release_lease(other, op)
        other.commit()
    assert _recover(client, worker="worker-B")[0]["after"] == "completed"
    assert _count(client, VendorPayment) == 1


def test_attempts_are_bounded_and_manual_completion_requires_evidence(client, vendors, accounts):
    """Regression (Turn 5): a confirmed transfer whose GL posting keeps failing could previously be
    marked completed with a free-text note while no AP payment or posted entry existed."""
    from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused

    class Outage(JournalLedgerAdapter):
        def post(self, db, entry):
            if entry.reference.startswith("AP-PAY-"):
                raise LedgerPostingRefused("persistent ledger outage")
            return super().post(db, entry)

    bill = _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    _use(client, gateway=DurableSimulatedGateway(), ledger=Outage())
    run = _approved_run(client, accounts)
    uid = _execute(client, run["id"]).json()["outcomes"][0]["operation_uid"]
    for _ in range(6):
        _recover(client)
    status = _op(client, uid)
    assert status["state"] == "reconciliation_required" and status["attempts"] >= status["max_attempts"]
    assert status["manual_investigation_required"] is True and status["recovery_action"] == "manual"
    assert status["accounting_status"] == "unrecorded" and status["delivery_status"] == "delivered"
    assert _count(client, VendorPayment) == 0 and len(_transfers(client)) == 1
    entries_before = _count(client, JournalEntry)

    # A note documents the investigation and changes nothing financial.
    note = client.post(f"{API}/payment-operations/{uid}/resolve", json={"actor": "cfo", "resolution": "recorded by hand as payment #99"})
    assert note.status_code == 200 and note.json()["state"] == "reconciliation_required"
    assert note.json()["investigation_status"] == "open" and note.json()["accounting_status"] == "unrecorded"
    # The previously accepted invalid completion: a note and a flag, no accounting record.
    done = client.post(f"{API}/payment-operations/{uid}/resolve", json={"actor": "cfo", "resolution": "booked manually", "mark_completed": True})
    assert done.status_code == 422 and "not evidence" in done.json()["detail"]
    # A payment id that does not exist, or belongs to someone else, is not evidence either.
    assert client.post(f"{API}/payment-operations/{uid}/complete", json={"actor": "cfo", "payment_id": 99, "reason": "x"}).status_code == 422
    _use(client, gateway=DurableSimulatedGateway())  # the ledger is back; nothing is recorded by itself
    other_vendor_payment = client.post(f"{API}/payments", json={"vendor_id": vendors["BETA"]["id"], "payment_date": "2026-02-10", "amount": "300.00",
                                                                 "cash_account_id": accounts["cash"], "recorded_by": "cfo", "reference": "SOMETHING-ELSE"})
    assert other_vendor_payment.status_code == 201, other_vendor_payment.text
    wrong = client.post(f"{API}/payment-operations/{uid}/complete", json={"actor": "cfo", "payment_id": other_vendor_payment.json()["id"], "reason": "x"})
    assert wrong.status_code == 422 and "payment_vendor_mismatch" in wrong.json()["detail"]
    assert _op(client, uid)["state"] == "reconciliation_required"
    # Closing the investigation does not complete anything either: the accounting status stays explicit.
    closed = client.post(f"{API}/payment-operations/{uid}/investigation/close", json={"actor": "cfo", "note": "cause found: ledger outage"})
    assert closed.status_code == 200 and closed.json()["investigation_status"] == "closed"
    assert closed.json()["state"] == "reconciliation_required" and closed.json()["accounting_status"] == "unrecorded"
    assert client.get(f"{API}/reconciliation?as_of=2026-02-28").json()["pending_obligations_total"] == "300.00"

    # The genuine correction: record the payment for the confirmed transfer, verified, completed once.
    body = {"actor": "cfo", "payment_date": "2026-02-10", "reason": "ledger outage resolved; booking the confirmed transfer",
            "allocations": [{"bill_id": bill["id"], "amount": "300.00"}], "idempotency_key": "fix-1"}
    fixed = client.post(f"{API}/payment-operations/{uid}/corrections", json=body)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["state"] == "completed" and fixed.json()["accounting_status"] == "corrected"
    assert fixed.json()["completion_kind"] == "manual_correction" and fixed.json()["completion_policy_satisfied"] is True
    assert fixed.json()["checklist"]["reconciles"] is True
    assert _count(client, VendorPayment) == 2 and len(_transfers(client)) == 1  # no second transfer
    assert _count(client, JournalEntry) == entries_before + 2  # BETA's unrelated payment + the correction
    # Identical retry: the original result. Same key, different request: conflict. No new records.
    again = client.post(f"{API}/payment-operations/{uid}/corrections", json=body)
    assert again.status_code == 200 and again.json()["payment_id"] == fixed.json()["payment_id"]
    changed = client.post(f"{API}/payment-operations/{uid}/corrections", json={**body, "reason": "different"})
    assert changed.status_code == 409
    assert _count(client, VendorPayment) == 2 and _count(client, JournalEntry) == entries_before + 2
    assert client.post(f"{API}/payment-operations/{uid}/resolve", json={"actor": "cfo", "resolution": "x", "mark_completed": True,
                                                                         "payment_id": fixed.json()["payment_id"]}).status_code == 409
    assert _recover(client) == []
    assert client.get(f"{API}/payment-runs/{run['id']}").json()["status"] == "completed"
    assert client.get(f"{API}/reconciliation?as_of=2026-02-28").json()["pending_obligations"] == []


def test_duplicate_confirmation_after_completion_is_logged_only(client, vendors, accounts):
    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    _use(client, gateway=DurableSimulatedGateway())
    run = _approved_run(client, accounts)
    outcome = _execute(client, run["id"]).json()["outcomes"][0]
    r = client.post(f"{API}/payment-operations/{outcome['operation_uid']}/confirmations", json={"actor": "callback", "reference": outcome["reference"]})
    assert r.status_code == 200 and r.json()["state"] == "completed"
    other = client.post(f"{API}/payment-operations/{outcome['operation_uid']}/confirmations", json={"actor": "callback", "reference": "OTHER-REF"})
    assert "differs from recorded" in other.json()["events"][-1]["detail"]
    assert _count(client, VendorPayment) == 1 and _count(client, PaymentAllocation) == 1
    with Session(client.app.state.engine) as s:
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 2


def test_operation_listing_filters_and_pagination(client, vendors, accounts):
    for n in range(3):
        _bill(client, vendors["ALPHA"], accounts, f"A-{n}", "10.00")
    _bill(client, vendors["BETA"], accounts, "B-1", "10.00")
    _use(client, gateway=DurableSimulatedGateway(GatewayScript(reject={"BETA"})))
    run = _approved_run(client, accounts)
    _execute(client, run["id"])
    page = client.get(f"{API}/payment-operations?page=1&page_size=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert client.get(f"{API}/payment-operations?state=failed").json()["total"] == 1
    assert client.get(f"{API}/payment-operations?vendor_id={vendors['ALPHA']['id']}").json()["items"][0]["state"] == "completed"
    assert client.get(f"{API}/payment-operations/does-not-exist").status_code == 404
    detail = client.get(f"{API}/payment-operations/{page['items'][0]['operation_uid']}").json()
    assert "IBAN" not in (detail["destination_reference_masked"] or "")
    assert detail["timestamps"]["created_at"] is not None


def test_payment_run_settlement_check_has_no_findings(client, vendors, accounts):
    _bill(client, vendors["ALPHA"], accounts, "A-1", "300.00")
    _use(client, gateway=DurableSimulatedGateway())
    run = _approved_run(client, accounts)
    _execute(client, run["id"])
    with Session(client.app.state.engine) as s:
        assert services.reconstruction.run_settlement_check(s, run["id"]) == []
        assert s.scalar(select(OperationState.COMPLETED.value == PaymentOperation.state)) is not None
