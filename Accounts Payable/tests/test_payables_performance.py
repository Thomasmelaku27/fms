"""Performance measurements with a realistic AP data set (SQLite and PostgreSQL).

Generates ``FMS_PERF_SCALE`` x (20 vendors x 50 bills) through the real services
-- approvals with posted entries, partial / full / advance payments, credits
with applications and unapplications, a few reversals and holds -- then times
the reports and the payment-run proposal. Results are written to
``build/perf/<backend>.json`` and printed; the assertions are deliberately loose
upper bounds (they catch pathological regressions, not tenths of a second).
Run with ``-m perf`` / skip with ``-m "not perf"``.
"""

import json
import os
import random
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Account, AccountType, JournalEntry
from app.modules.payables import services
from app.modules.payables.integration import Integrations
from app.modules.payables.models import SupplierBill, VendorPayment
from app.modules.payables.schemas import (
    AllocationIn,
    BillIn,
    BillLineIn,
    CreditIn,
    CreditLineIn,
    PaymentIn,
    PaymentRunCreateIn,
    VendorIn,
)

pytestmark = pytest.mark.perf

D = Decimal
SCALE = float(os.environ.get("FMS_PERF_SCALE", "1"))
VENDORS = max(2, int(20 * SCALE))
BILLS_PER_VENDOR = max(5, int(50 * SCALE))
INTEGRATIONS = Integrations()
RESULTS_DIR = Path(__file__).resolve().parents[1] / "build" / "perf"


def _timed(results, name, fn):
    started = time.perf_counter()
    value = fn()
    results[name] = round(time.perf_counter() - started, 3)
    return value


def _generate(session, rng):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add_all([ap, expense, cash])
    session.commit()
    vendors = [
        services.create_vendor(session, VendorIn(code=f"V{i:03d}", name=f"Vendor {i}", payable_account_id=ap.id,
                                                 payment_terms_days=rng.choice((14, 30, 45, 60))))
        for i in range(VENDORS)
    ]
    start = date(2025, 7, 1)
    counts = {"bills": 0, "payments": 0, "credits": 0, "applications": 0, "unapplications": 0, "reversals": 0, "holds": 0}
    for vendor in vendors:
        bills = []
        for n in range(BILLS_PER_VENDOR):
            invoice_date = start + timedelta(days=rng.randint(0, 240))
            total = D(rng.randint(5000, 250000)) / 100
            bill = services.create_bill(session, BillIn(
                vendor_id=vendor.id, invoice_number=f"{vendor.code}-{n:05d}", invoice_date=invoice_date, total=total,
                lines=[BillLineIn(description="goods", account_id=expense.id, quantity=D("2"), unit_price=total / 2)]))
            services.approve_bill(session, bill.id, actor="gen", integrations=INTEGRATIONS)
            bills.append(bill)
            counts["bills"] += 1
        rng.shuffle(bills)
        # Pay ~60 % of the bills: a third partially, a third fully, a third via an advance.
        for bill in bills[: int(len(bills) * 0.6)]:
            pay_date = bill.invoice_date + timedelta(days=rng.randint(1, 40))
            choice = rng.random()
            if choice < 0.33:
                amount = (bill.total / 2).quantize(D("0.01"))
                services.record_payment(session, PaymentIn(vendor_id=vendor.id, payment_date=pay_date, amount=amount, cash_account_id=cash.id,
                                                           recorded_by="gen", allocations=[AllocationIn(bill_id=bill.id, amount=amount)]),
                                        integrations=INTEGRATIONS)
            elif choice < 0.66:
                services.record_payment(session, PaymentIn(vendor_id=vendor.id, payment_date=pay_date, amount=bill.total, cash_account_id=cash.id,
                                                           recorded_by="gen", allocations=[AllocationIn(bill_id=bill.id, amount=bill.total)]),
                                        integrations=INTEGRATIONS)
            else:
                payment, _ = services.record_payment(session, PaymentIn(vendor_id=vendor.id, payment_date=pay_date, amount=bill.total,
                                                                        cash_account_id=cash.id, recorded_by="gen"), integrations=INTEGRATIONS)
                services.apply_advance(session, payment.id, bill_id=bill.id, amount=bill.total, actor="gen",
                                       allocation_date=pay_date + timedelta(days=2), integrations=INTEGRATIONS)
            counts["payments"] += 1
        # A few credits, some applied then unapplied; a few reversals and holds.
        for k in range(max(1, len(bills) // 10)):
            target = bills[-(k + 1)]
            credit = services.create_credit(session, CreditIn(vendor_id=vendor.id, credit_number=f"CN-{vendor.code}-{k}", credit_date=target.invoice_date + timedelta(days=3),
                                                              total=(target.total / 4).quantize(D("0.01")),
                                                              lines=[CreditLineIn(description="c", account_id=expense.id, unit_price=(target.total / 4).quantize(D("0.01")))]))
            services.approve_credit(session, credit.id, actor="gen", integrations=INTEGRATIONS)
            counts["credits"] += 1
            if target.outstanding >= credit.total:
                services.apply_credit(session, credit.id, bill_id=target.id, amount=credit.total, actor="gen",
                                      application_date=credit.credit_date + timedelta(days=1), integrations=INTEGRATIONS)
                counts["applications"] += 1
                if k % 2 == 0:
                    services.unapply_credit(session, credit.id, credit.applications[0].id, actor="gen", reason="perf",
                                            unapplication_date=credit.credit_date + timedelta(days=20), integrations=INTEGRATIONS)
                    counts["unapplications"] += 1
        payments = session.scalars(select(VendorPayment).where(VendorPayment.vendor_id == vendor.id)).all()
        for payment in payments[:2]:
            latest = max((a.allocation_date for a in payment.allocations), default=payment.payment_date)
            services.reverse_payment(session, payment.id, actor="gen", reason="perf", reversal_date=latest + timedelta(days=5),
                                     integrations=INTEGRATIONS)
            counts["reversals"] += 1
        for bill in bills[-3:]:
            if bill.status.value in ("approved", "partially_paid"):
                services.place_hold(session, bill.id, kind=services.documents.HoldKind.HOLD, reason="perf", actor="gen", integrations=INTEGRATIONS)
                counts["holds"] += 1
        session.expire_all()
    return counts, vendors, cash


def test_reports_and_proposals_at_scale(session, engine):
    rng = random.Random(2026)
    results = {"backend": engine.dialect.name, "scale": SCALE, "vendors": VENDORS, "bills_per_vendor": BILLS_PER_VENDOR}
    counts, vendors, cash = _timed(results, "generate_seconds", lambda: _generate(session, rng))
    results.update(counts)
    results["journal_entries"] = session.scalar(select(func.count()).select_from(JournalEntry))
    session.expire_all()
    as_of = date(2026, 4, 30)

    aging = _timed(results, "aging_report", lambda: services.aging_report(session, as_of=as_of))
    outstanding = _timed(results, "outstanding_report", lambda: services.outstanding_report(session, as_of=as_of))
    recon = _timed(results, "reconciliation_report", lambda: services.reconciliation_report(session, as_of=as_of))
    balances = _timed(results, "vendor_balances", lambda: services.vendor_balances(session, as_of=as_of))
    busiest = max(vendors, key=lambda v: sum(1 for b in session.scalars(select(SupplierBill.id).where(SupplierBill.vendor_id == v.id))))
    statement = _timed(results, "vendor_statement", lambda: services.vendor_statement(
        session, busiest.id, date_from=date(2025, 7, 1), date_to=as_of))
    run = _timed(results, "payment_run_proposal", lambda: services.create_run(
        session, PaymentRunCreateIn(actor="ops", pay_date=as_of, due_through=as_of, cash_account_id=cash.id), integrations=INTEGRATIONS))
    validation = _timed(results, "period_validation", lambda: services.validate_period(session, date(2026, 3, 31)))
    results["open_bills"] = len(outstanding.rows)
    results["proposal_items"] = len(run.items)
    results["statement_lines"] = len(statement.lines)

    # Correctness at scale: the books reconcile and the statement agrees with the open items.
    assert recon.exceptions == [] and recon.total_difference == 0
    assert statement.difference == 0
    assert aging.totals.total == outstanding.total_outstanding
    assert sum((b.bills_outstanding for b in balances), D("0")) == outstanding.total_outstanding
    assert validation.status in ("ok", "warnings", "blocked")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{engine.dialect.name}.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nPERF", json.dumps(results, default=str))
    # Loose ceilings: every report well under a minute at scale 1.
    for key in ("aging_report", "outstanding_report", "reconciliation_report", "vendor_balances", "vendor_statement",
                "payment_run_proposal", "period_validation"):
        assert results[key] < 60 * max(SCALE, 1), f"{key} took {results[key]}s"
