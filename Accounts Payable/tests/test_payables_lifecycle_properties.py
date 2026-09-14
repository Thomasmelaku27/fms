"""Property-style lifecycle test: random gateway behaviour and crashes, with invariants.

For each seed a sequence of payment runs is executed against a gateway whose
behaviour (accept / reject / pending / lose response / drop request) and a worker
that may crash at any injected point are chosen pseudo-randomly. After every
step recovery runs until nothing is left. Invariants (checked independently of
the production helpers with plain SQL):

* at most one accepted gateway transfer per operation, and exactly one per completed op;
* every completed operation has exactly one recorded payment with a posted entry
  whose amount equals the transfer, and every payment belongs to one operation;
* failed operations moved no money and recorded nothing;
* no operation is left non-terminal after recovery converges (or it is flagged manual);
* monetary conservation: cash credited in the ledger == sum of recorded payments;
  AP control account == replayed subledger.
"""

import os
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.models import Account, AccountType
from app.modules.payables import services
from app.modules.payables.integration import Integrations, ScriptedFaults
from app.modules.payables.schemas import BillIn, BillLineIn, PaymentRunCreateIn, VendorIn
from app.modules.payables.services.gateway import DurableSimulatedGateway, GatewayScript

D = Decimal
SEEDS = [int(s) for s in os.environ.get("FMS_LIFECYCLE_SEEDS", "3,17,101").split(",")]
ROUNDS = int(os.environ.get("FMS_LIFECYCLE_ROUNDS", "6"))
POINTS = ["submit.before_commit", "submit.after_gateway", "confirm.before_commit", "recording.before_books",
          "recording.after_flush", "recording.before_commit", "recording.after_commit", None, None, None]


class Crash(BaseException):
    pass


def _setup(session, rng):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="E", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="C", type=AccountType.ASSET)
    session.add_all([ap, expense, cash])
    session.commit()
    vendors = [services.create_vendor(session, VendorIn(code=f"V{i}", name=f"V{i}", payable_account_id=ap.id, bank_reference=f"IBAN-{i}"))
               for i in range(4)]
    return ap, expense, cash, vendors


def _invariants(session, ap_id, cash_id):
    q = lambda sql, **p: session.execute(text(sql), p).all()  # noqa: E731
    ops = q("SELECT operation_uid, state, amount, payment_id FROM payment_operations")
    transfers = {t: (s, a) for t, s, a in q("SELECT token, status, amount FROM simulated_gateway_transfers")}
    for uid, state, amount, payment_id in ops:
        accepted = [1 for t, (s, _) in transfers.items() if t == uid and s == "accepted"]
        assert len(accepted) <= 1
        if state == "completed":
            assert accepted and payment_id is not None, f"completed op {uid} without transfer/payment"
            rows = q("SELECT p.amount, e.status FROM vendor_payments p JOIN journal_entries e ON e.id = p.journal_entry_id WHERE p.id = :p", p=payment_id)
            assert rows and rows[0][1] == "posted" and D(str(rows[0][0])) == D(str(amount))
        if state == "failed":
            assert not accepted and payment_id is None
        if state in ("confirmed", "recording", "reconciliation_required"):
            assert accepted
    payments_per_op = q("SELECT payment_id, COUNT(*) FROM payment_operations WHERE payment_id IS NOT NULL GROUP BY payment_id HAVING COUNT(*) > 1")
    assert payments_per_op == []
    # Conservation: cash credited == recorded payments (reversals excluded here: none generated).
    cash_credit = D(str(q("SELECT COALESCE(SUM(l.credit - l.debit), 0) FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
                          "WHERE l.account_id = :c AND e.status = 'posted'", c=cash_id)[0][0] or 0))
    paid = D(str(q("SELECT COALESCE(SUM(amount), 0) FROM vendor_payments WHERE status = 'recorded'")[0][0] or 0))
    assert cash_credit == paid
    recon = services.reconstruction.compare(session, date(2026, 12, 31))
    assert recon.findings == [], recon.findings


@pytest.mark.parametrize("seed", SEEDS)
def test_random_gateway_behaviour_and_crashes_keep_the_books_consistent(session, seed):
    rng = random.Random(seed)
    ap, expense, cash, vendors = _setup(session, rng)
    pay_date = date(2026, 2, 1)
    log = []
    try:
        for round_no in range(ROUNDS):
            # New bills for a random subset of vendors.
            for vendor in vendors:
                if rng.random() < 0.7:
                    total = D(rng.randint(1000, 50000)) / 100
                    bill = services.create_bill(session, BillIn(vendor_id=vendor.id, invoice_number=f"{vendor.code}-{round_no}", invoice_date=pay_date - timedelta(days=10),
                                                                due_date=pay_date, total=total,
                                                                lines=[BillLineIn(description="x", account_id=expense.id, unit_price=total)]))
                    services.approve_bill(session, bill.id, actor="alice", integrations=Integrations())
            script = GatewayScript(
                reject={v.code for v in vendors if rng.random() < 0.15},
                lose_response={v.code for v in vendors if rng.random() < 0.2},
                drop={v.code for v in vendors if rng.random() < 0.15},
                pending={v.code for v in vendors if rng.random() < 0.15},
                pending_lookups=rng.randint(1, 2),
            )
            gateway = DurableSimulatedGateway(script)
            run = services.create_run(session, PaymentRunCreateIn(actor="ops", pay_date=pay_date, cash_account_id=cash.id), integrations=Integrations())
            if not [i for i in run.items if i.status.value != "excluded"]:
                pay_date += timedelta(days=7)
                continue
            services.review_run(session, run.id, actor="rev", integrations=Integrations())
            services.approve_run(session, run.id, actor="boss", integrations=Integrations())
            point = rng.choice(POINTS)
            faults = ScriptedFaults({point: Crash}) if point else None
            log.append(f"round {round_no}: reject={script.reject} lose={script.lose_response} drop={script.drop} pending={script.pending} crash={point}")
            try:
                services.execute_run(session, run.id, actor="ops", integrations=Integrations(gateway=gateway, faults=faults) if faults else Integrations(gateway=gateway))
            except Crash:
                session.rollback()
                # An operator releases the claim the dead worker left behind.
                from app.modules.payables.models import PaymentRun, PaymentRunStatus
                row = session.get(PaymentRun, run.id)
                session.refresh(row)
                if row.status == PaymentRunStatus.EXECUTING:
                    services.payment_runs._cas(session, row, PaymentRunStatus.EXECUTING, status=PaymentRunStatus.FAILED)
                    session.commit()
            _invariants(session, ap.id, cash.id)
            # Recovery converges (fresh gateway instance each time = restarted worker).
            for _ in range(8):
                results = services.recovery.process(session, integrations=Integrations(gateway=DurableSimulatedGateway(script)), worker_id=f"w{round_no}")
                if not results or all(r.action in ("ignore", "manual") for r in results):
                    break
            pending = session.execute(text("SELECT state, recovery_status FROM payment_operations WHERE state NOT IN ('completed', 'failed')")).all()
            assert all(r == "manual" for _, r in pending), f"non-terminal operations without manual flag: {pending}"
            _invariants(session, ap.id, cash.id)
            # Rejected vendors get a new operation on retry; eventually everything either pays or is manual.
            script.reject.clear()
            pay_date += timedelta(days=7)
    except AssertionError as exc:  # pragma: no cover - diagnostics
        raise AssertionError(f"seed={seed}\n" + "\n".join(log) + f"\n{exc}") from exc
    completed = session.execute(text("SELECT COUNT(*) FROM payment_operations WHERE state = 'completed'")).scalar()
    assert completed > 0
