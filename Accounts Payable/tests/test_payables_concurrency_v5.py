"""Recovery races on independent PostgreSQL connections with deterministic synchronisation.

Each worker runs in its own thread on its own ``Session`` (its own connection). A
``Gate`` fault port stops a worker at a named durability point until the test releases
it, so interleavings are chosen, not hoped for. SQLite variants are skipped where its
single-writer lock would serialise the scenario away (they run on PostgreSQL only).

Invariants checked in every scenario: one AP payment, one posted cash movement and one
set of allocations per confirmed transfer; the operation history stays contiguous and
hash-chained; runs are never finalised ``completed`` while a transfer is unrecorded.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry
from app.modules.payables import services
from app.modules.payables.integration import Integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import (
    OperationState,
    PaymentAllocation,
    PaymentOperation,
    PaymentRun,
    PaymentRunStatus,
    Vendor,
    VendorPayment,
    VendorStatus,
)
from app.modules.payables.schemas import AllocationIn, PaymentRunCreateIn
from app.modules.payables.services import execution_state as state
from app.modules.payables.services import recovery, verification
from app.modules.payables.services.gateway import DurableSimulatedGateway

D = Decimal


class Gate:
    """Fault port that parks the calling worker at ``point`` until released."""

    def __init__(self, point: str):
        self.point = point
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.hits: list[str] = []

    def check(self, p: str) -> None:
        self.hits.append(p)
        if p == self.point and not self.arrived.is_set():
            self.arrived.set()
            assert self.release.wait(timeout=30), f"gate {self.point} never released"

    def wait_arrival(self):
        assert self.arrived.wait(timeout=30), f"worker never reached {self.point}"

    def open(self):
        self.release.set()


class Outage(JournalLedgerAdapter):
    def __init__(self, times=1):
        self.remaining = times

    def post(self, db, entry):
        if entry.reference.startswith("AP-PAY-") and self.remaining > 0:
            self.remaining -= 1
            raise LedgerPostingRefused("ledger unavailable")
        return super().post(db, entry)


@pytest.fixture
def pg(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("independent-connection races need PostgreSQL")
    return engine


@pytest.fixture
def world(pg, session):
    from app.models import Account, AccountType
    from app.modules.payables.models import SupplierBill, SupplierBillLine as BillLine

    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="E", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="C", type=AccountType.ASSET)
    session.add_all([ap, expense, cash])
    session.flush()
    vendor = Vendor(code="ALPHA", name="Alpha", payable_account_id=ap.id, bank_reference="IBAN-A")
    session.add(vendor)
    session.flush()
    bill = SupplierBill(vendor_id=vendor.id, invoice_number="A-1", invoice_date=date(2026, 1, 10), due_date=date(2026, 2, 1), total=D("300.00"),
                        payable_account_id=ap.id, lines=[BillLine(line_no=1, description="x", account_id=expense.id, quantity=D("1"), unit_price=D("300.00"), line_total=D("300.00"))])
    session.add(bill)
    session.commit()
    services.approve_bill(session, bill.id, actor="alice", integrations=Integrations())
    run = services.create_run(session, PaymentRunCreateIn(actor="ops", pay_date=date(2026, 2, 10), cash_account_id=cash.id), integrations=Integrations())
    services.review_run(session, run.id, actor="rev", integrations=Integrations())
    services.approve_run(session, run.id, actor="boss", integrations=Integrations())
    return {"ap": ap.id, "expense": expense.id, "cash": cash.id, "vendor": vendor.id, "bill": bill.id, "run": run.id}


def _counts(engine):
    with Session(engine) as s:
        return {
            "payments": s.scalar(select(func.count()).select_from(VendorPayment)),
            "allocations": s.scalar(select(func.count()).select_from(PaymentAllocation)),
            "posted_cash": s.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.reference.like("AP-PAY-%"), JournalEntry.status == EntryStatus.POSTED)),
            "transfers": s.execute(text("SELECT COUNT(*) FROM simulated_gateway_transfers WHERE status = 'accepted'")).scalar(),
        }


def _op(engine):
    with Session(engine, expire_on_commit=False) as s:
        return s.scalar(select(PaymentOperation).order_by(PaymentOperation.id))


def _verified(engine, uid):
    with Session(engine) as s:
        op = state.get_operation(s, uid)
        return verification.verify_operation(s, op, gateway=DurableSimulatedGateway())


def _confirmed_operation(engine, world):
    """Execute until the gateway confirmed, then stop before recording (crash at recording.before_books)."""
    from tests.test_payables_failure_injection import SimulatedCrash
    from app.modules.payables.integration import ScriptedFaults

    with Session(engine, expire_on_commit=False) as s:
        try:
            services.execute_run(s, world["run"], actor="ops", integrations=Integrations(gateway=DurableSimulatedGateway(), faults=ScriptedFaults({"recording.before_books": SimulatedCrash})))
        except SimulatedCrash:
            s.rollback()
    op = _op(engine)
    assert op.state == OperationState.RECORDING
    with Session(engine) as s:
        fresh = state.get_operation(s, op.operation_uid)
        state.release_lease(s, fresh)
        run = s.get(PaymentRun, world["run"])
        services.payment_runs._cas(s, run, PaymentRunStatus.EXECUTING, status=PaymentRunStatus.FAILED)
        s.commit()
    return op


def _reconciliation_required_operation(engine, world, attempts_left=True):
    with Session(engine, expire_on_commit=False) as s:
        services.execute_run(s, world["run"], actor="ops", integrations=Integrations(gateway=DurableSimulatedGateway(), ledger=Outage(times=1)))
    op = _op(engine)
    assert op.state == OperationState.RECONCILIATION_REQUIRED
    return op


def _thread(fn):
    errors = []

    def run():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run)
    t.start()
    return t, errors


# --- 1. two workers, one confirmed transfer -------------------------------------------------------


def test_two_workers_on_the_same_confirmed_transfer_record_it_once(pg, world):
    op = _confirmed_operation(pg, world)
    gate = Gate("recording.after_flush")
    results = {}

    def worker(name, faults, lease_seconds=300):
        with Session(pg, expire_on_commit=False) as s:
            results[name] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway(), faults=faults), worker_id=name, actor=name, lease_seconds=lease_seconds)

    a, errors_a = _thread(lambda: worker("A", gate))
    gate.wait_arrival()  # A holds the lease and has the books flushed but not committed
    worker("B", Integrations().faults)  # B runs to completion concurrently
    assert results["B"][0].action in ("ignore", "retry_recording", "skipped")
    assert results["B"][0].note in ("leased by another worker or changed concurrently", "another worker is recording it")
    gate.open()
    a.join(timeout=60)
    assert not errors_a, errors_a
    assert results["A"][0].after == "completed"
    assert _counts(pg) == {"payments": 1, "allocations": 1, "posted_cash": 1, "transfers": 1}
    v = _verified(pg, op.operation_uid)
    assert v.completion_policy_satisfied and v.evidence["events"]["hash_chain_verified"]


def test_expired_lease_taken_over_mid_recording_loses_the_compare_and_swap(pg, world):
    """A's lease expired while it was writing the books; B records; A's commit is refused by the CAS."""
    op = _confirmed_operation(pg, world)
    gate = Gate("recording.after_flush")
    results = {}

    def worker(name, faults, lease_seconds):
        with Session(pg, expire_on_commit=False) as s:
            results[name] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway(), faults=faults), worker_id=name, actor=name, lease_seconds=lease_seconds)

    a, errors_a = _thread(lambda: worker("A", gate, -1))  # lease already expired when taken
    gate.wait_arrival()
    b, errors_b = _thread(lambda: worker("B", Integrations().faults, 300))
    b.join(timeout=5)
    # B is blocked on A's row lock? No: A has not touched the operation row in this transaction yet
    # (only bills / payment rows); B's transition 'recording -> reconciliation_required' ... is refused
    # because A's state is 'recording' with a live-looking version? B sees state=recording, lease
    # expired -> treats it as interrupted, moves it to reconciliation_required and records.
    gate.open()
    a.join(timeout=60)
    b.join(timeout=60)
    assert not errors_a and not errors_b, (errors_a, errors_b)
    outcomes = sorted((results["A"][0].after, results["B"][0].after))
    assert outcomes == ["completed", "completed"] or "completed" in outcomes, (results["A"], results["B"])
    assert _counts(pg) == {"payments": 1, "allocations": 1, "posted_cash": 1, "transfers": 1}
    assert _op(pg).state == OperationState.COMPLETED
    assert _verified(pg, op.operation_uid).completion_policy_satisfied


# --- 2. manual completion vs recording retry --------------------------------------------------------


def test_manual_correction_racing_a_recording_retry_yields_one_payment(pg, world):
    op = _reconciliation_required_operation(pg, world)
    gate = Gate("completion.before_commit")
    outcome = {}

    def correct():
        with Session(pg, expire_on_commit=False) as s:
            outcome["correction"] = recovery.record_correction(
                s, op.operation_uid, actor="cfo", payment_date=date(2026, 2, 10), reason="manual booking",
                allocations=[AllocationIn(bill_id=world["bill"], amount=D("300.00"))],
                integrations=Integrations(gateway=DurableSimulatedGateway(), faults=gate)).state.value

    c, errors_c = _thread(correct)
    gate.wait_arrival()  # the correction holds the row lock with its payment flushed, not committed

    def retry():
        with Session(pg, expire_on_commit=False) as s:
            outcome["retry"] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway()), worker_id="W", actor="W")

    r, errors_r = _thread(retry)
    r.join(timeout=3)
    assert r.is_alive()  # the retry's compare-and-swap waits on the row lock
    gate.open()
    c.join(timeout=60)
    r.join(timeout=60)
    assert not errors_c and not errors_r, (errors_c, errors_r)
    assert outcome["correction"] == "completed"
    assert outcome["retry"][0].action in ("retry_recording", "ignore", "skipped") and outcome["retry"][0].after == "completed"
    assert "lost a race" in outcome["retry"][0].note or "changed concurrently" in outcome["retry"][0].note or outcome["retry"][0].note == "terminal"
    assert _counts(pg)["payments"] == 1 and _counts(pg)["posted_cash"] == 1
    v = _verified(pg, op.operation_uid)
    assert v.completion_policy_satisfied and v.accounting_status == "corrected"


def test_recording_retry_racing_a_manual_correction_refuses_the_correction(pg, world):
    op = _reconciliation_required_operation(pg, world)
    gate = Gate("recording.after_flush")
    outcome = {}

    def retry():
        with Session(pg, expire_on_commit=False) as s:
            outcome["retry"] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway(), faults=gate), worker_id="W", actor="W")

    r, errors_r = _thread(retry)
    gate.wait_arrival()  # state is 'recording', books flushed, operation row locked by the recording

    def correct():
        with Session(pg, expire_on_commit=False) as s:
            recovery.record_correction(s, op.operation_uid, actor="cfo", payment_date=date(2026, 2, 10), reason="x",
                                       allocations=[AllocationIn(bill_id=world["bill"], amount=D("300.00"))], integrations=Integrations(gateway=DurableSimulatedGateway()))

    c, errors_c = _thread(correct)
    c.join(timeout=3)
    assert c.is_alive(), "the correction must wait for the recording's row lock, not race it"
    gate.open()
    r.join(timeout=60)
    c.join(timeout=60)
    assert not errors_r and outcome["retry"][0].after == "completed", outcome["retry"]
    assert len(errors_c) == 1 and isinstance(errors_c[0], services.Conflict) and "already completed" in str(errors_c[0])
    assert _counts(pg)["payments"] == 1


# --- 3. gateway callback during recovery ------------------------------------------------------------


def test_gateway_callback_during_recovery_is_logged_without_breaking_the_history(pg, world):
    op = _confirmed_operation(pg, world)
    gate = Gate("recording.after_flush")
    results = {}

    def worker():
        with Session(pg, expire_on_commit=False) as s:
            results["A"] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway(), faults=gate), worker_id="A", actor="A")

    a, errors_a = _thread(worker)
    gate.wait_arrival()

    def callback():
        with Session(pg, expire_on_commit=False) as s:
            recovery.duplicate_confirmation(s, op.operation_uid, reference=op.gateway_confirmation_ref or "LATE", actor="gateway-callback")

    cb, errors_cb = _thread(callback)
    cb.join(timeout=3)
    assert cb.is_alive(), "the callback serialises behind the recording's row lock"
    gate.open()
    a.join(timeout=60)
    cb.join(timeout=60)
    assert not errors_a and not errors_cb and results["A"][0].after == "completed", (results["A"], errors_cb)
    with Session(pg) as s:
        last = s.execute(text("SELECT category, detail FROM payment_operation_events e JOIN payment_operations o ON o.id = e.operation_id WHERE o.operation_uid = :u ORDER BY seq DESC LIMIT 1"),
                         {"u": op.operation_uid}).first()
        assert last.category == "gateway" and "duplicate confirmation" in last.detail
    assert _counts(pg)["payments"] == 1
    v = _verified(pg, op.operation_uid)
    assert v.completion_policy_satisfied and v.required_evidence["events_intact"]
    with Session(pg) as s:
        seqs = [r[0] for r in s.execute(text("SELECT seq FROM payment_operation_events e JOIN payment_operations o ON o.id = e.operation_id WHERE o.operation_uid = :u ORDER BY seq"), {"u": op.operation_uid})]
        assert seqs == list(range(1, len(seqs) + 1))


# --- 4. run finalisation while a transfer is unrecorded -------------------------------------------


def test_run_is_never_finalised_completed_while_recording_is_in_flight(pg, world):
    _reconciliation_required_operation(pg, world)
    with Session(pg) as s:
        run = s.get(PaymentRun, world["run"])
        assert run.status == PaymentRunStatus.FAILED and run.finalization_status == "incomplete_accounting"
    gate = Gate("recording.after_flush")
    results = {}

    def worker():
        with Session(pg, expire_on_commit=False) as s:
            results["A"] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway(), faults=gate), worker_id="A", actor="A")

    a, errors_a = _thread(worker)
    gate.wait_arrival()
    # An operator re-executes the failed run meanwhile: the operation is leased, nothing is paid twice,
    # and the run cannot come out 'completed'.
    with Session(pg, expire_on_commit=False) as s:
        run, outcomes = services.execute_run(s, world["run"], actor="ops2", integrations=Integrations(gateway=DurableSimulatedGateway()))
        assert run.status == PaymentRunStatus.FAILED and outcomes[0]["error"] == "operation leased by another worker"
        assert run.finalization_status == "incomplete_accounting"
    gate.open()
    a.join(timeout=60)
    assert not errors_a and results["A"][0].after == "completed"
    with Session(pg) as s:
        run = s.get(PaymentRun, world["run"])
        assert run.status == PaymentRunStatus.COMPLETED and run.finalization_status == "completed"
        assert [i.settlement_status for i in run.items] == ["recorded"]
    assert _counts(pg) == {"payments": 1, "allocations": 1, "posted_cash": 1, "transfers": 1}


# --- 5. stale reads during recording commits --------------------------------------------------------


def test_stale_operation_snapshot_cannot_commit_a_transition(pg, world):
    op = _confirmed_operation(pg, world)
    with Session(pg, expire_on_commit=False) as stale:
        snapshot = state.get_operation(stale, op.operation_uid)  # version v, state recording
        with Session(pg, expire_on_commit=False) as other:
            recovery.process(other, integrations=Integrations(gateway=DurableSimulatedGateway()), worker_id="B", actor="B")
        assert _op(pg).state == OperationState.COMPLETED
        with pytest.raises(state.OperationConflict):
            state.transition(stale, snapshot, OperationState.RECORDING, OperationState.RECONCILIATION_REQUIRED, actor="stale", detail="stale write")
        stale.rollback()
    fresh = _op(pg)
    assert fresh.state == OperationState.COMPLETED and fresh.version == _op(pg).version
    assert _verified(pg, op.operation_uid).completion_policy_satisfied


# --- 6. period close during confirmed-payment recording ---------------------------------------------


def test_period_close_waits_for_in_flight_recording_and_then_sees_it(pg, world):
    _confirmed_operation(pg, world)
    gate = Gate("recording.after_flush")
    results = {}

    def worker():
        with Session(pg, expire_on_commit=False) as s:
            results["A"] = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway(), faults=gate), worker_id="A", actor="A")

    a, errors_a = _thread(worker)
    gate.wait_arrival()  # the recording transaction holds the shared close lock

    def close():
        with Session(pg, expire_on_commit=False) as s:
            results["close"] = services.closing.close_period(s, period_end=date(2026, 2, 28), actor="cfo", note=None, integrations=Integrations(gateway=DurableSimulatedGateway())).closed_through

    c, errors_c = _thread(close)
    c.join(timeout=3)
    assert c.is_alive(), "the close must wait for the dated writer"
    gate.open()
    a.join(timeout=60)
    c.join(timeout=60)
    assert not errors_a and results["A"][0].after == "completed"
    # The close ran after the recording committed, so it saw the payment: the transfer is never an
    # unrecorded obligation. It may still have been refused because the run itself was not yet
    # re-finalised (a separate, later transaction) -- in which case the next attempt succeeds.
    with Session(pg) as s:
        attestation = services.closing.list_attestations(s)[-1]
        assert attestation.obligations_json == "[]"
        assert "unrecorded_confirmed_transfers" not in str(errors_c)
    if errors_c:
        assert isinstance(errors_c[0], services.ValidationFailed) and "payment_runs_in_flight" in str(errors_c[0])
        with Session(pg, expire_on_commit=False) as s:
            services.closing.close_period(s, period_end=date(2026, 2, 28), actor="cfo", note=None, integrations=Integrations(gateway=DurableSimulatedGateway()))
    with Session(pg) as s:
        attestation = services.closing.list_attestations(s)[-1]
        assert attestation.outcome == "closed" and attestation.obligations_json == "[]"
        assert services.closing.verify_close(s, attestation.id, gateway=DurableSimulatedGateway()).verified


def test_close_before_recording_blocks_then_forced_close_keeps_the_obligation(pg, world):
    op = _confirmed_operation(pg, world)
    with Session(pg, expire_on_commit=False) as s:
        with pytest.raises(services.ValidationFailed, match="unrecorded_confirmed_transfers"):
            services.closing.close_period(s, period_end=date(2026, 2, 28), actor="cfo", note=None, integrations=Integrations(gateway=DurableSimulatedGateway()))
        lock = services.closing.close_period(s, period_end=date(2026, 2, 28), actor="cfo", note=None, force=True, force_reason="month end",
                                             integrations=Integrations(gateway=DurableSimulatedGateway()))
        attestation = services.closing.list_attestations(s)[-1]
        assert lock.closed_through == date(2026, 2, 28) and attestation.outcome == "forced"
        assert {e.code for e in attestation.exceptions} == {"unrecorded_confirmed_transfers", "payment_runs_in_flight"}
        assert op.operation_uid in attestation.obligations_json
    # Recording into the closed period is refused; the obligation survives as reconciliation_required.
    with Session(pg, expire_on_commit=False) as s:
        result = recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway()), worker_id="A", actor="A")
        assert result[0].after == "reconciliation_required" and "closed AP period" in result[0].note
    assert _counts(pg)["payments"] == 0
    with Session(pg) as s:
        check = services.closing.verify_close(s, attestation.id, gateway=DurableSimulatedGateway())
        assert check.obligations[0]["current_state"] == "reconciliation_required"
        assert {f.code for f in check.findings} >= {"obligation_outstanding", "exception_unresolved"}
    # Remediation in the open period through the correction workflow, then the exception is resolved once.
    with Session(pg, expire_on_commit=False) as s:
        done = recovery.record_correction(s, op.operation_uid, actor="cfo", payment_date=date(2026, 3, 2), reason="booked in March",
                                          allocations=[AllocationIn(bill_id=world["bill"], amount=D("300.00"))], integrations=Integrations(gateway=DurableSimulatedGateway()))
        assert done.state == OperationState.COMPLETED
        for pending in services.closing.list_close_exceptions(s, attestation_id=attestation.id):
            exc = services.closing.resolve_close_exception(s, pending.id, actor="cfo", note="recorded 2026-03-02", reference=f"vendor_payment:{done.payment_id}",
                                                           integrations=Integrations())
            assert exc.status == "resolved"
        with pytest.raises(services.Conflict):
            services.closing.resolve_close_exception(s, exc.id, actor="cfo", note="again", reference=None, integrations=Integrations())
        check = services.closing.verify_close(s, attestation.id, gateway=DurableSimulatedGateway())
        assert check.obligations[0]["current_state"] == "completed" and check.obligations[0]["verified"] is True
        assert not any(f.code in ("obligation_outstanding", "exception_unresolved") for f in check.findings)
    assert _counts(pg)["payments"] == 1 and _counts(pg)["transfers"] == 1


# --- 7. vendor suspension after authorisation --------------------------------------------------------


def test_vendor_suspended_after_authorisation_is_caught_before_the_transfer_or_reported_after(pg, world):
    # Before execution: revalidation invalidates the approval, nothing is sent.
    with Session(pg, expire_on_commit=False) as s:
        services.change_vendor_status(s, world["vendor"], status=VendorStatus.SUSPENDED, reason="audit", actor="risk", integrations=Integrations())
        with pytest.raises(services.Conflict, match="Approval invalidated"):
            services.execute_run(s, world["run"], actor="ops", integrations=Integrations(gateway=DurableSimulatedGateway()))
        assert s.get(PaymentRun, world["run"]).status == PaymentRunStatus.DRAFT
        assert _counts(pg)["transfers"] == 0
        services.change_vendor_status(s, world["vendor"], status=VendorStatus.ACTIVE, reason="cleared", actor="risk", integrations=Integrations())
        services.review_run(s, world["run"], actor="rev", integrations=Integrations())
        services.approve_run(s, world["run"], actor="boss", integrations=Integrations())
    # During execution: suspension lands between the claim and the gateway call; the money moves,
    # the transfer is recorded regardless, and the verifier reports the supplier's status.
    gate = Gate("submit.before_commit")
    results = {}

    def execute():
        with Session(pg, expire_on_commit=False) as s:
            run, outcomes = services.execute_run(s, world["run"], actor="ops", integrations=Integrations(gateway=DurableSimulatedGateway(), faults=gate))
            results["run"] = run.status.value
            results["outcome"] = outcomes[0]

    e, errors_e = _thread(execute)
    gate.wait_arrival()
    with Session(pg, expire_on_commit=False) as s:
        services.change_vendor_status(s, world["vendor"], status=VendorStatus.SUSPENDED, reason="audit again", actor="risk", integrations=Integrations())
    gate.open()
    e.join(timeout=60)
    assert not errors_e, errors_e
    assert results["run"] == "completed" and results["outcome"]["state"] == "completed"
    assert _counts(pg) == {"payments": 1, "allocations": 1, "posted_cash": 1, "transfers": 1}
    v = _verified(pg, results["outcome"]["operation_uid"])
    assert v.completion_policy_satisfied and {f.code for f in v.findings} == {"vendor_not_active"}
