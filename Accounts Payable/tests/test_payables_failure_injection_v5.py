"""Gateway success / ledger failure, end to end, with faults at every seam (SQLite and PostgreSQL).

Expected sequence: gateway confirmation succeeds -> AP recording fails -> recovery marks
``reconciliation_required`` -> manual completion without evidence is rejected -> a
legitimate accounting correction is created and verified -> completion succeeds exactly
once, without another transfer or GL posting. Identical retries return the original
result; changed requests under the same idempotency key conflict. Faults are injected
after flush, before and after commit, during gateway status lookup and during recovery.
Recovery runs from a separately constructed application instance and from a restarted
worker *process*.
"""

import json
import subprocess
import sys
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from app.models import EntryStatus, JournalEntry
from app.modules.payables.integration import Integrations, ScriptedFaults, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import PaymentAllocation, VendorPayment
from app.modules.payables.services.gateway import DurableSimulatedGateway, GatewayScript
from tests.conftest import PROJECT_ROOT
from tests.test_payables_failure_injection import SimulatedCrash

API = "/api/v1/payables"
D = Decimal


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class Outage(JournalLedgerAdapter):
    """The GL refuses payment postings ``times`` times (bills still post)."""

    def __init__(self, times=10**6):
        self.remaining = times

    def post(self, db, entry):
        if entry.reference.startswith("AP-PAY-") and self.remaining > 0:
            self.remaining -= 1
            raise LedgerPostingRefused("GL posting failed")
        return super().post(db, entry)


@pytest.fixture
def world(client):
    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}
    vendor = client.post(f"{API}/vendors", json={"code": "ALPHA", "name": "Alpha", "payable_account_id": accounts["ap"], "bank_reference": "IBAN-1"}).json()
    r = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "A-1", "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                          "total": "300.00", "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "300.00"}]})
    assert client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"}).status_code == 200
    run = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-10", "cash_account_id": accounts["cash"]}).json()
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 200
    return {"accounts": accounts, "vendor": vendor, "bill_id": r.json()["id"], "run": run}


def _state(client):
    with Session(client.app.state.engine) as s:
        return {
            "transfers": s.execute(text("SELECT COUNT(*) FROM simulated_gateway_transfers WHERE status = 'accepted'")).scalar(),
            "payments": s.scalar(select(func.count()).select_from(VendorPayment)),
            "allocations": s.scalar(select(func.count()).select_from(PaymentAllocation)),
            "posted_payment_entries": s.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.reference.like("AP-PAY-%"), JournalEntry.status == EntryStatus.POSTED)),
            "draft_entries": s.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.status == EntryStatus.DRAFT)),
            "events": s.execute(text("SELECT COUNT(*) FROM payment_operation_events")).scalar(),
        }


def _use(client, **kw):
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(**kw)


def _op(client, uid):
    return client.get(f"{API}/payment-operations/{uid}").json()


def _recover(client, worker="w"):
    r = client.post(f"{API}/recovery/run", json={"actor": worker, "worker_id": worker})
    assert r.status_code == 200, r.text
    return r.json()


def _crash_execute(client, run_id, integrations):
    """Run the executor in its own session and let the injected crash tear it down."""
    from app.modules.payables import services

    with Session(client.app.state.engine, expire_on_commit=False) as s:
        try:
            services.execute_run(s, run_id, actor="ops", integrations=integrations)
        except SimulatedCrash:
            s.rollback()


def _stuck(client, world):
    """Gateway confirmed; the GL refused the payment posting; recovery gave up (manual)."""
    _use(client, gateway=DurableSimulatedGateway(), ledger=Outage())
    result = client.post(f"{API}/payment-runs/{world['run']['id']}/execute", json={"actor": "ops"}).json()
    uid = result["outcomes"][0]["operation_uid"]
    assert result["outcomes"][0]["state"] == "reconciliation_required" and result["run"]["status"] == "failed"
    assert result["run"]["finalization_status"] == "incomplete_accounting"
    for _ in range(6):
        _recover(client)
    status = _op(client, uid)
    assert status["state"] == "reconciliation_required" and status["recovery_action"] == "manual"
    assert status["accounting_status"] == "unrecorded" and status["delivery_status"] == "delivered"
    assert _state(client)["transfers"] == 1 and _state(client)["payments"] == 0 and _state(client)["draft_entries"] == 0
    return uid


def test_the_expected_sequence(client, world):
    uid = _stuck(client, world)
    before = _state(client)
    # 1. manual completion without evidence is rejected (three ways)
    assert client.post(f"{API}/payment-operations/{uid}/resolve", json={"actor": "cfo", "resolution": "done by hand", "mark_completed": True}).status_code == 422
    assert client.post(f"{API}/payment-operations/{uid}/complete", json={"actor": "cfo", "payment_id": 424242, "reason": "x"}).status_code == 422
    assert client.post(f"{API}/payment-operations/{uid}/investigation/close", json={"actor": "cfo", "note": "looked at it"}).status_code == 200
    assert _op(client, uid)["state"] == "reconciliation_required" and _state(client) == {**before, "events": before["events"] + 1}
    # 2. the GL is back; the legitimate correction records, verifies and completes -- exactly once
    _use(client, gateway=DurableSimulatedGateway())
    body = {"actor": "cfo", "payment_date": "2026-02-10", "reason": "GL outage over", "idempotency_key": "corr-1",
            "allocations": [{"bill_id": world["bill_id"], "amount": "300.00"}]}
    first = client.post(f"{API}/payment-operations/{uid}/corrections", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "completed" and first.json()["accounting_status"] == "corrected" and first.json()["completion_policy_satisfied"]
    after = _state(client)
    assert after["transfers"] == 1 and after["payments"] == 1 and after["allocations"] == 1 and after["posted_payment_entries"] == 1
    # 3. identical retries: the original result; changed request, same key: conflict; nothing else written
    for _ in range(3):
        again = client.post(f"{API}/payment-operations/{uid}/corrections", json=body)
        assert again.status_code == 200 and again.json()["payment_id"] == first.json()["payment_id"]
    assert client.post(f"{API}/payment-operations/{uid}/corrections", json={**body, "payment_date": "2026-02-11"}).status_code == 409
    assert client.post(f"{API}/payment-operations/{uid}/corrections", json={**body, "reason": "other words"}).status_code == 409
    assert client.post(f"{API}/payment-operations/{uid}/corrections", json={**body, "allocations": []}).status_code == 409
    assert client.post(f"{API}/payment-operations/{uid}/complete", json={"actor": "cfo", "payment_id": first.json()["payment_id"], "reason": "x"}).status_code == 409
    assert _state(client) == {**after, "events": _state(client)["events"]}
    assert _recover(client) == []
    run = client.get(f"{API}/payment-runs/{world['run']['id']}").json()
    assert run["status"] == "completed" and run["finalization_status"] == "completed"
    assert run["items"][0]["settlement_status"] == "recorded" and run["items"][0]["actual_pay_amount"] == "300.00"
    assert client.get(f"{API}/reconciliation").json()["pending_obligations"] == []


@pytest.mark.parametrize("point", ["recording.after_flush", "recording.before_commit", "completion.before_commit"])
def test_faults_during_the_correction_leave_nothing_behind_and_retry_cleanly(client, world, point):
    uid = _stuck(client, world)
    faults = ScriptedFaults({point: RuntimeError})
    _use(client, gateway=DurableSimulatedGateway(), faults=faults)
    body = {"actor": "cfo", "payment_date": "2026-02-10", "reason": "fix", "idempotency_key": "corr-2",
            "allocations": [{"bill_id": world["bill_id"], "amount": "300.00"}]}
    if point.startswith("recording."):
        # Those points belong to the automatic recording path: exercise them through recovery.
        with Session(client.app.state.engine) as s:
            s.execute(text("UPDATE payment_operations SET recovery_status = 'scheduled', attempts = 0"))
            s.commit()
        result = _recover(client)
        assert result[0]["after"] == "reconciliation_required"
    else:
        from fastapi.testclient import TestClient

        with TestClient(client.app, raise_server_exceptions=False) as lenient:
            assert lenient.post(f"{API}/payment-operations/{uid}/corrections", json=body).status_code == 500
    state = _state(client)
    assert state["payments"] == 0 and state["allocations"] == 0 and state["posted_payment_entries"] == 0 and state["draft_entries"] == 0
    assert _op(client, uid)["state"] == "reconciliation_required"
    _use(client, gateway=DurableSimulatedGateway())
    fixed = client.post(f"{API}/payment-operations/{uid}/corrections", json=body)
    assert fixed.status_code == 200 and fixed.json()["state"] == "completed"
    assert _state(client)["payments"] == 1 and _state(client)["transfers"] == 1


def test_fault_after_commit_is_idempotent(client, world):
    uid = _stuck(client, world)
    _use(client, gateway=DurableSimulatedGateway(), faults=ScriptedFaults({"recording.after_commit": RuntimeError}))
    with Session(client.app.state.engine) as s:
        s.execute(text("UPDATE payment_operations SET recovery_status = 'scheduled', attempts = 0"))
        s.commit()
    result = _recover(client)
    assert result[0]["after"] == "completed"  # the fault hit after the commit: nothing to undo
    assert _state(client)["payments"] == 1 and _op(client, uid)["completion_policy_satisfied"]
    assert _recover(client) == [] and _state(client)["payments"] == 1


@pytest.mark.parametrize("lost", ["lookup.after_gateway", "recovery.before_action"])
def test_faults_during_gateway_lookup_and_recovery_never_duplicate(client, world, lost):
    gateway = DurableSimulatedGateway(GatewayScript(lose_response={"ALPHA"}))
    _crash_execute(client, world["run"]["id"], Integrations(gateway=gateway, faults=ScriptedFaults({lost: SimulatedCrash})))
    with Session(client.app.state.engine) as s:
        uid, st = s.execute(text("SELECT operation_uid, state FROM payment_operations")).first()
    assert st in ("submitted", "confirmed", "recording", "completed"), st
    assert _state(client)["transfers"] == 1
    # Recovery from a *separately constructed* application instance on the same database.
    other = create_app(Settings(database_url=str(client.app.state.engine.url)))
    with TestClient(other) as fresh:
        with Session(other.state.engine) as s:
            s.execute(text("UPDATE payment_runs SET status = 'failed' WHERE status = 'executing'"))
            s.execute(text("UPDATE payment_operations SET worker_id = NULL, lease_expires_at = NULL"))
            s.commit()
        for _ in range(3):
            fresh.post(f"{API}/recovery/run", json={"actor": "w2", "worker_id": "w2"})
        assert fresh.get(f"{API}/payment-operations/{uid}").json()["state"] == "completed"
    other.state.engine.dispose()
    assert _state(client) == {**_state(client), "transfers": 1, "payments": 1, "allocations": 1, "posted_payment_entries": 1, "draft_entries": 0}
    assert [t for k, t in gateway.calls if k == "submit"] == [uid]  # one submission ever


def test_restarted_worker_process_recovers_from_durable_state_only(client, world):
    """The recovery worker is a separate OS process with no memory of the first attempt."""
    _crash_execute(client, world["run"]["id"], Integrations(gateway=DurableSimulatedGateway(), faults=ScriptedFaults({"recording.before_books": SimulatedCrash})))
    with Session(client.app.state.engine) as s:
        uid, st = s.execute(text("SELECT operation_uid, state FROM payment_operations")).first()
        s.execute(text("UPDATE payment_runs SET status = 'failed' WHERE status = 'executing'"))
        s.execute(text("UPDATE payment_operations SET worker_id = NULL, lease_expires_at = NULL"))
        s.commit()
    assert st == "recording" and _state(client)["payments"] == 0
    url = str(client.app.state.engine.url.render_as_string(hide_password=False))
    proc = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "ap_recovery_worker.py"), "--url", url, "--worker-id", "restarted-1", "--json"],
                          capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=180)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert [r["after"] for r in results if r["operation_uid"] == uid] == ["completed"], results
    assert _state(client)["payments"] == 1 and _state(client)["transfers"] == 1 and _state(client)["draft_entries"] == 0
    status = _op(client, uid)
    assert status["completion_policy_satisfied"] and any(e["actor"] == "restarted-1" for e in status["events"])
    assert client.get(f"{API}/payment-runs/{world['run']['id']}").json()["status"] == "completed"
