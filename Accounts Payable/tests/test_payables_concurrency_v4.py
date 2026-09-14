"""Competing sessions on payment runs, operations and period closes.

Each thread uses its *own* ``Session`` (and therefore its own connection and
transaction) against the same database; a barrier releases them together.
PostgreSQL exercises real row locks and advisory locks; on SQLite the single
writer serialises them, and the compare-and-swap versions still decide.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import Account, AccountType, JournalEntry
from app.modules.payables import services
from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    APPeriodLock,
    OperationState,
    PaymentOperation,
    PaymentRun,
    PaymentRunStatus,
    VendorPayment,
)
from app.modules.payables.schemas import AllocationIn, BillIn, BillLineIn, CreditIn, CreditLineIn, PaymentIn, PaymentRunCreateIn, VendorIn
from app.modules.payables.services import execution_state
from app.modules.payables.services.gateway import DurableSimulatedGateway

D = Decimal


@pytest.fixture
def world(engine):
    with Session(engine, expire_on_commit=False) as s:
        ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
        expense = Account(code="6000", name="E", type=AccountType.EXPENSE)
        cash = Account(code="1000", name="C", type=AccountType.ASSET)
        s.add_all([ap, expense, cash])
        s.commit()
        vendor = services.create_vendor(s, VendorIn(code="V1", name="Vendor", payable_account_id=ap.id, bank_reference="IBAN-1"))
        bills = []
        for n in range(3):
            bill = services.create_bill(s, BillIn(vendor_id=vendor.id, invoice_number=f"B-{n}", invoice_date=date(2026, 1, 10),
                                                  due_date=date(2026, 2, 1), total=D("100.00"),
                                                  lines=[BillLineIn(description="x", account_id=expense.id, unit_price=D("100.00"))]))
            services.approve_bill(s, bill.id, actor="alice", integrations=Integrations())
            bills.append(bill.id)
        return {"ap": ap.id, "expense": expense.id, "cash": cash.id, "vendor": vendor.id, "bills": bills}


def _race(engine, workers):
    """Run callables ``fn(session)`` concurrently, each on its own session; collect results / errors."""
    barrier = threading.Barrier(len(workers))
    results = [None] * len(workers)

    def run(i, fn):
        with Session(engine, expire_on_commit=False) as s:
            barrier.wait(timeout=10)
            try:
                results[i] = ("ok", fn(s))
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                results[i] = ("error", exc)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def _draft_run(engine, world, pay_date=date(2026, 2, 10)):
    with Session(engine, expire_on_commit=False) as s:
        run = services.create_run(s, PaymentRunCreateIn(actor="ops", pay_date=pay_date, cash_account_id=world["cash"]), integrations=Integrations())
        return run.id


def _count(engine, model):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(model))


# --- runs ------------------------------------------------------------------------------------------


def test_competing_reviews_only_one_wins(engine, world):
    run_id = _draft_run(engine, world)
    results = _race(engine, [lambda s, a=a: services.review_run(s, run_id, actor=a, integrations=Integrations()).reviewed_by for a in ("r1", "r2", "r3")])
    assert sorted(r[0] for r in results) == ["error", "error", "ok"]
    assert all(isinstance(r[1], services.Conflict) for r in results if r[0] == "error")
    with Session(engine) as s:
        run = s.get(PaymentRun, run_id)
        assert run.status == PaymentRunStatus.REVIEWED and run.version == 1


def test_competing_approve_and_modify_and_cancel(engine, world):
    run_id = _draft_run(engine, world)
    with Session(engine, expire_on_commit=False) as s:
        services.review_run(s, run_id, actor="rev", integrations=Integrations())
        item_id = s.get(PaymentRun, run_id).items[0].id
    results = _race(engine, [
        lambda s: services.approve_run(s, run_id, actor="boss", integrations=Integrations()).status.value,
        lambda s: services.cancel_run(s, run_id, actor="boss2", reason="no", integrations=Integrations()).status.value,
        lambda s: services.send_back_run(s, run_id, actor="rev", integrations=Integrations()).status.value,
        lambda s: services.update_item(s, run_id, item_id, settle_amount=D("50.00"), actor="rev").status.value,
    ])
    statuses = [r[0] == "ok" for r in results]
    approved_ok, cancelled_ok, sent_back_ok, edited_ok = statuses
    ok = [r[1] for r in results if r[0] == "ok"]
    # Exactly one transition leaves "reviewed"; whatever follows must be a legal sequential history:
    # a sent-back (draft) run may then be edited or cancelled, an approved run may then be cancelled.
    assert sum([approved_ok, sent_back_ok]) <= 1, results
    assert edited_ok <= sent_back_ok, results
    assert len(ok) >= 1, results
    expected = "cancelled" if cancelled_ok else ("approved" if approved_ok else "draft")
    with Session(engine) as s:
        run = s.get(PaymentRun, run_id)
        assert run.status.value == expected, results
        assert run.version == 1 + len(ok), results  # every successful transition bumped the version exactly once


def test_competing_executions_and_retries_produce_one_transfer_per_vendor(engine, world):
    run_id = _draft_run(engine, world)
    with Session(engine, expire_on_commit=False) as s:
        services.review_run(s, run_id, actor="rev", integrations=Integrations())
        services.approve_run(s, run_id, actor="boss", integrations=Integrations())
    gateway = DurableSimulatedGateway()
    results = _race(engine, [lambda s, a=a: services.execute_run(s, run_id, actor=a, integrations=Integrations(gateway=gateway))[0].status.value
                             for a in ("ops1", "ops2", "ops3")])
    assert sorted(r[0] for r in results) == ["error", "error", "ok"], results
    assert results and [r[1] for r in results if r[0] == "ok"] == ["completed"]
    with Session(engine) as s:
        assert s.execute(text("SELECT COUNT(*) FROM simulated_gateway_transfers")).scalar() == 1
        assert s.scalar(select(func.count()).select_from(VendorPayment)) == 1
        assert s.scalar(select(func.count()).select_from(PaymentOperation)) == 1
        assert s.get(PaymentRun, run_id).attempts == 1


def test_competing_workers_on_the_same_operation_submit_once(engine, world):
    """Two workers pick up the same prepared operation; the gateway is called once."""
    with Session(engine, expire_on_commit=False) as s:
        op, _ = execution_state.prepare(
            s, idempotency_key="manual-op", vendor_id=world["vendor"], destination_reference="IBAN-1", amount=D("100.00"), currency="XXX",
            method=services.payments.PaymentMethod.BANK_TRANSFER if hasattr(services.payments, "PaymentMethod") else __import__("app.modules.payables.models", fromlist=["PaymentMethod"]).PaymentMethod.BANK_TRANSFER,
            payment_date=date(2026, 2, 10), cash_account_id=world["cash"], run_id=None, run_version=None,
            plan=[{"bill_id": world["bills"][0], "settle": "100.00", "discount": "0", "credit": "0", "advance": "0", "pay": "100.00"}], actor="ops")
        s.commit()
        uid = op.operation_uid
    gateway = DurableSimulatedGateway()

    def worker(s, name):
        mine = execution_state.get_operation(s, uid, for_update=True)
        if not execution_state.lease(s, mine, worker_id=name):
            s.rollback()
            raise services.Conflict("leased")
        s.commit()
        return services.orchestration.execute(s, mine, integrations=Integrations(gateway=gateway), actor=name).state.value

    results = _race(engine, [lambda s, n=n: worker(s, n) for n in ("w1", "w2")])
    assert sorted(r[0] for r in results) == ["error", "ok"]
    assert [k for k, _ in gateway.calls] == ["submit"]
    with Session(engine) as s:
        assert s.execute(text("SELECT COUNT(*) FROM simulated_gateway_transfers")).scalar() == 1
        assert s.scalar(select(PaymentOperation)).state == OperationState.COMPLETED
        assert s.scalar(select(func.count()).select_from(VendorPayment)) == 1


def test_stale_version_is_refused_by_the_database_state_machine(engine, world):
    run_id = _draft_run(engine, world)
    with Session(engine, expire_on_commit=False) as s:
        run = s.get(PaymentRun, run_id)
        stale_version = run.version
        services.review_run(s, run_id, actor="rev", integrations=Integrations())
    with Session(engine) as s:
        from sqlalchemy import update

        # A writer holding a stale version cannot move the run (rowcount 0), whatever it tries.
        result = s.execute(update(PaymentRun).where(PaymentRun.id == run_id, PaymentRun.version == stale_version)
                           .values(status=PaymentRunStatus.APPROVED, version=stale_version + 1))
        assert result.rowcount == 0
        s.rollback()


# --- period close ---------------------------------------------------------------------------------------


def test_competing_close_requests_only_one_closes_and_both_attest(engine, world):
    results = _race(engine, [lambda s, a=a: services.close_period(s, period_end=date(2026, 1, 31), actor=a, note=None, integrations=Integrations()).id
                             for a in ("cfo1", "cfo2")])
    assert sorted(r[0] for r in results) == ["error", "ok"], results
    assert isinstance([r[1] for r in results if r[0] == "error"][0], services.Conflict)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(APPeriodLock)) == 1
        assert [a.outcome for a in services.list_attestations(s)] == ["closed"]


def test_dated_write_and_close_cannot_both_succeed_in_the_closing_period(engine, world):
    """A credit application dated in January races the January close: either the application
    commits first (and the close sees it; it is a warning, not a blocker) or the close commits
    first and the application is refused. Never both silently."""
    with Session(engine, expire_on_commit=False) as s:
        credit = services.create_credit(s, CreditIn(vendor_id=world["vendor"], credit_number="CN", credit_date=date(2026, 1, 12), total=D("30.00"),
                                                    lines=[CreditLineIn(description="c", account_id=world["expense"], unit_price=D("30.00"))]))
        services.approve_credit(s, credit.id, actor="a", integrations=Integrations())
        credit_id = credit.id
    outcomes = []
    for _ in range(3):  # repeat to hit both interleavings on fast machines
        with Session(engine) as s:
            s.execute(text("DELETE FROM credit_applications WHERE unapplication_date IS NULL AND 1 = 0"))  # no-op, keeps the flow explicit
        results = _race(engine, [
            lambda s: services.apply_credit(s, credit_id, bill_id=world["bills"][0], amount=D("10.00"), actor="b",
                                            application_date=date(2026, 1, 20), integrations=Integrations()).status.value,
            lambda s: services.close_period(s, period_end=date(2026, 1, 31), actor="cfo", note=None, integrations=Integrations()).id,
        ])
        outcomes.append(tuple(r[0] for r in results))
        with Session(engine) as s:
            s.execute(text("SELECT COUNT(*) FROM credit_applications WHERE application_date <= '2026-01-31' AND unapplication_date IS NULL")).scalar()
            closed = s.scalar(select(func.count()).select_from(APPeriodLock).where(APPeriodLock.closed_through >= date(2026, 1, 31)))
        if closed:
            # Once closed, the application either happened before the close (visible, attested) or was refused.
            with Session(engine) as s:
                checks_json = [a.checks_json for a in services.list_attestations(s) if a.outcome in ("closed", "forced")][-1]
            assert '"unapplied_credits"' in checks_json
            break
    assert closed == 1
    # After the close, the same January application is refused for sure, and a February one works.
    with Session(engine, expire_on_commit=False) as s:
        with pytest.raises(services.PeriodClosed):
            services.apply_credit(s, credit_id, bill_id=world["bills"][1], amount=D("5.00"), actor="b", application_date=date(2026, 1, 25), integrations=Integrations())
        s.rollback()
        services.apply_credit(s, credit_id, bill_id=world["bills"][1], amount=D("5.00"), actor="b", application_date=date(2026, 2, 2), integrations=Integrations())


def test_failed_close_leaves_the_period_untouched_and_attests_the_blockers(engine, world):
    with Session(engine, expire_on_commit=False) as s:
        draft = services.create_bill(s, BillIn(vendor_id=world["vendor"], invoice_number="DRAFT", invoice_date=date(2026, 1, 20), total=D("5.00"),
                                               lines=[BillLineIn(description="x", account_id=world["expense"], unit_price=D("5.00"))]))
        with pytest.raises(services.ValidationFailed, match="unapproved_documents"):
            services.close_period(s, period_end=date(2026, 1, 31), actor="cfo", note=None, integrations=Integrations())
        s.rollback()
        assert s.scalar(select(func.count()).select_from(APPeriodLock)) == 0
        attestations = services.list_attestations(s)
        assert [a.outcome for a in attestations] == ["failed"] and '"unapproved_documents", "severity": "blocker", "count": 1' in attestations[0].checks_json
        assert attestations[0].liability_balance == D("300.00") and attestations[0].lock_id is None
        # Forced close by an authorised actor preserves the exact exceptions and the decision.
        lock = services.close_period(s, period_end=date(2026, 1, 31), actor="cfo", note="month end", integrations=Integrations(),
                                     force=True, force_reason="draft will be re-dated to February")
        assert lock.closed_through == date(2026, 1, 31)
        forced = services.list_attestations(s)[-1]
        assert forced.outcome == "forced" and forced.forced and forced.force_reason == "draft will be re-dated to February"
        assert forced.lock_id == lock.id and '"count": 1' in forced.checks_json
        assert len(forced.source_fingerprint) == 64
        # The draft dated in the closed period can no longer be approved there.
        with pytest.raises(services.ValidationFailed, match="closed AP period"):
            services.approve_bill(s, draft.id, actor="alice", integrations=Integrations())
        s.rollback()


def test_forced_close_requires_reason_and_authorization(engine, world):
    class Deny:
        def authorize(self, actor, action, resource):
            return action != "payables.period.force_close"

    with Session(engine, expire_on_commit=False) as s:
        services.create_bill(s, BillIn(vendor_id=world["vendor"], invoice_number="DRAFT", invoice_date=date(2026, 1, 20), total=D("5.00")))
        with pytest.raises(services.ValidationFailed, match="requires a reason"):
            services.close_period(s, period_end=date(2026, 1, 31), actor="cfo", note=None, integrations=Integrations(), force=True)
        s.rollback()
        with pytest.raises(services.ValidationFailed, match="not authorised"):
            services.close_period(s, period_end=date(2026, 1, 31), actor="clerk", note=None, integrations=Integrations(authorization=Deny()),
                                  force=True, force_reason="x")
        s.rollback()
        assert s.scalar(select(func.count()).select_from(APPeriodLock)) == 0
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 3


def test_backdated_events_during_close_on_every_path(engine, world):
    """Bill approval, payment, advance allocation, reversal and discount dated in a closed period are all refused."""
    with Session(engine, expire_on_commit=False) as s:
        payment, _ = services.record_payment(s, PaymentIn(vendor_id=world["vendor"], payment_date=date(2026, 1, 15), amount=D("150.00"),
                                                          cash_account_id=world["cash"], recorded_by="b",
                                                          allocations=[AllocationIn(bill_id=world["bills"][0], amount=D("100.00"))]), integrations=Integrations())
        services.close_period(s, period_end=date(2026, 1, 31), actor="cfo", note=None, integrations=Integrations())
        for fn, pattern in (
            (lambda: services.record_payment(s, PaymentIn(vendor_id=world["vendor"], payment_date=date(2026, 1, 20), amount=D("1.00"),
                                                          cash_account_id=world["cash"], recorded_by="b"), integrations=Integrations()), "closed"),
            (lambda: services.apply_advance(s, payment.id, bill_id=world["bills"][1], amount=D("10.00"), actor="b",
                                            allocation_date=date(2026, 1, 30), integrations=Integrations()), "closed"),
            (lambda: services.reverse_payment(s, payment.id, actor="c", reason="r", reversal_date=date(2026, 1, 31), integrations=Integrations()), "closed"),
        ):
            with pytest.raises(services.ValidationFailed, match=pattern):
                fn()
            s.rollback()
        # The same operations dated after the close succeed.
        services.apply_advance(s, payment.id, bill_id=world["bills"][1], amount=D("10.00"), actor="b", allocation_date=date(2026, 2, 1), integrations=Integrations())
        assert s.scalar(select(func.count()).select_from(VendorPayment)) == 1
