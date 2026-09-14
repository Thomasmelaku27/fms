"""Deterministic failure injection across the payment lifecycle (SQLite and PostgreSQL).

Each scenario crashes the worker at one named point, then checks the invariant
"financial state is consistent and a retry cannot duplicate transfers or
accounting effects": at most one gateway transfer, at most one AP payment, one
posted payment entry, allocations only with the payment, and the operation ends
``completed`` after recovery (or ``failed`` with nothing moved).
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry
from app.modules.payables.integration import Integrations, ScriptedFaults, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import PaymentAllocation, PaymentOperation, VendorPayment
from app.modules.payables.services.gateway import DurableSimulatedGateway, GatewayScript

API = "/api/v1/payables"


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


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
        ops = s.scalars(select(PaymentOperation)).all()
        return {
            "transfers": s.execute(text("SELECT COUNT(*) FROM simulated_gateway_transfers WHERE status = 'accepted'")).scalar(),
            "payments": s.scalar(select(func.count()).select_from(VendorPayment)),
            "allocations": s.scalar(select(func.count()).select_from(PaymentAllocation)),
            "payment_entries": s.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.reference.like("AP-PAY-%"))),
            "draft_entries": s.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.status == EntryStatus.DRAFT)),
            "ops": [(o.state.value, o.attempts, o.payment_id is not None) for o in ops],
        }


class SimulatedCrash(BaseException):
    """Propagates through every ``except Exception`` like a killed process would."""


def _crash_execute(client, run_id, integrations):
    """Run the executor in its own session and let the injected crash tear it down."""
    from app.modules.payables import services

    with Session(client.app.state.engine, expire_on_commit=False) as s:
        try:
            services.execute_run(s, run_id, actor="ops", integrations=integrations)
            return "completed"
        except SimulatedCrash:
            s.rollback()  # the connection of a dead process is simply gone
            return "crashed"


def _recover(client, worker="w"):
    r = client.post(f"{API}/recovery/run", json={"actor": worker, "worker_id": worker})
    assert r.status_code == 200, r.text
    return r.json()


def _release_stale_run(client, run_id):
    """The crashed worker left the run claimed; an operator marks it failed so it can be retried."""
    from app.modules.payables.models import PaymentRun, PaymentRunStatus
    from app.modules.payables.services.payment_runs import _cas

    with Session(client.app.state.engine) as s:
        run = s.get(PaymentRun, run_id)
        if run.status == PaymentRunStatus.EXECUTING:
            _cas(s, run, PaymentRunStatus.EXECUTING, status=PaymentRunStatus.FAILED)
            s.commit()


POINTS = [
    ("submit.before_commit", "prepared", 0, False),
    ("submit.after_gateway", "submitted", 1, True),
    ("confirm.before_commit", "submitted", 1, True),
    ("recording.before_books", "recording", 1, True),
    ("recording.after_flush", "recording", 1, True),
    ("recording.before_commit", "recording", 1, True),
    ("recording.after_commit", "completed", 1, True),
]


@pytest.mark.parametrize(("point", "expected_state", "expected_transfers", "money_moved"), POINTS)
def test_crash_at_every_point_is_recoverable_without_duplication(client, world, point, expected_state, expected_transfers, money_moved):
    gateway = DurableSimulatedGateway()
    outcome = _crash_execute(client, world["run"]["id"], Integrations(gateway=gateway, faults=ScriptedFaults({point: SimulatedCrash})))
    assert outcome == "crashed"
    before = _state(client)
    assert before["transfers"] == expected_transfers
    assert before["draft_entries"] == 0, "a crash must never leave a draft payment entry behind"
    assert before["ops"][0][0] == expected_state
    if expected_state != "completed":
        assert before["payments"] == 0 and before["allocations"] == 0 and before["payment_entries"] == 0
    # Restart: fresh gateway instance (same durable store), healthy worker.
    fresh = DurableSimulatedGateway()
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=fresh)
    _release_stale_run(client, world["run"]["id"])
    for _ in range(3):
        _recover(client)
    after = _state(client)
    assert after["ops"][0][0] == "completed", after
    assert after["transfers"] == 1 and after["payments"] == 1 and after["allocations"] == 1 and after["payment_entries"] == 1
    assert after["draft_entries"] == 0
    # A second gateway submission never happened after money moved.
    submits = [t for k, t in fresh.calls if k == "submit"]
    assert (len(submits) == 0) if money_moved else (len(submits) == 1)
    assert client.get(f"{API}/bills/{world['bill_id']}").json()["status"] == "paid"
    assert client.get(f"{API}/reconciliation").json()["total_difference"] == "0.00"
    uid = after and client.get(f"{API}/payment-operations").json()["items"][0]["operation_uid"]
    status = client.get(f"{API}/payment-operations/{uid}").json()
    assert status["checklist"]["reconciles"] is True and status["recovery_action"] == "ignore"


def test_ledger_failure_after_gateway_confirmation_then_database_failure_during_retry(client, world):
    """Two different failures in a row: the transfer stays singular; the books end correct."""

    class Outage(JournalLedgerAdapter):
        def __init__(self):
            self.fail = 1

        def post(self, db, entry):
            if entry.reference.startswith("AP-PAY-") and self.fail:
                self.fail -= 1
                raise LedgerPostingRefused("ledger outage")
            return super().post(db, entry)

    gateway = DurableSimulatedGateway()
    ledger = Outage()
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway, ledger=ledger)
    result = client.post(f"{API}/payment-runs/{world['run']['id']}/execute", json={"actor": "ops"}).json()
    assert result["outcomes"][0]["state"] == "reconciliation_required"
    # The retry hits a database error while flushing (simulated by a fault raising OperationalError).
    faults = ScriptedFaults({"recording.after_flush": OperationalError("simulated", {}, Exception("db gone"))})
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway, ledger=ledger, faults=faults)
    first = _recover(client)
    assert first[0]["after"] == "reconciliation_required"
    state = _state(client)
    assert state["payments"] == 0 and state["transfers"] == 1 and state["ops"][0][1] == 2
    uid = client.get(f"{API}/payment-operations").json()["items"][0]["operation_uid"]
    assert client.get(f"{API}/payment-operations/{uid}").json()["failure_category"] == "database"
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway, ledger=ledger)
    assert _recover(client)[0]["after"] == "completed"
    final = _state(client)
    assert final["transfers"] == 1 and final["payments"] == 1 and final["payment_entries"] == 1
    assert [k for k, _ in gateway.calls] == ["submit"]


def test_crash_inside_recovery_itself_is_harmless(client, world):
    class Outage(JournalLedgerAdapter):
        def post(self, db, entry):
            if entry.reference.startswith("AP-PAY-"):
                raise LedgerPostingRefused("ledger outage")
            return super().post(db, entry)

    gateway = DurableSimulatedGateway()
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway, ledger=Outage())
    client.post(f"{API}/payment-runs/{world['run']['id']}/execute", json={"actor": "ops"})
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway, faults=ScriptedFaults({"recovery.before_action": RuntimeError}))
    with TestClient(client.app, raise_server_exceptions=False) as lenient:
        assert lenient.post(f"{API}/recovery/run", json={"actor": "w", "worker_id": "w"}).status_code == 500
    state = _state(client)
    assert state["ops"][0][0] == "reconciliation_required" and state["payments"] == 0
    # The lease the crashed worker took expires; until then another worker skips, afterwards it completes.
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway)
    results = _recover(client, worker="w2")
    assert results[0]["action"] in ("skipped", "retry_recording")
    with Session(client.app.state.engine) as s:
        s.execute(text("UPDATE payment_operations SET lease_expires_at = NULL, worker_id = NULL"))
        s.commit()
    assert _recover(client, worker="w2")[0]["after"] == "completed"
    assert _state(client)["transfers"] == 1 and _state(client)["payments"] == 1


def test_gateway_rejects_after_a_lost_response_is_handled_as_failed(client, world):
    gateway = DurableSimulatedGateway(GatewayScript(reject={"ALPHA"}, lose_response={"ALPHA"}))
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway)
    result = client.post(f"{API}/payment-runs/{world['run']['id']}/execute", json={"actor": "ops"}).json()
    assert result["outcomes"][0]["state"] == "failed"
    state = _state(client)
    assert state["transfers"] == 0 and state["payments"] == 0
    assert [k for k, _ in gateway.calls] == ["submit", "lookup"]


def test_period_close_crash_before_commit_leaves_nothing(client, world):
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(faults=ScriptedFaults({"close.before_commit": RuntimeError}))
    assert client.post(f"{API}/payment-runs/{world['run']['id']}/cancel", json={"actor": "boss", "reason": "x"}).status_code == 200
    with TestClient(client.app, raise_server_exceptions=False) as lenient:
        assert lenient.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"}).status_code == 500
    assert client.get(f"{API}/period-end/locks").json() == []
    assert client.get(f"{API}/period-end/attestations").json() == []
    client.app.dependency_overrides.clear()
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"}).status_code == 201
    assert [a["outcome"] for a in client.get(f"{API}/period-end/attestations").json()] == ["closed"]
    assert date.fromisoformat(client.get(f"{API}/period-end/locks").json()[0]["closed_through"]) == date(2026, 1, 31)
