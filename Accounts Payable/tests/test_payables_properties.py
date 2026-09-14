"""Reproducible randomized (property-style) tests of the AP subledger.

A seeded generator drives a random sequence of AP operations -- bills, partial
and advance payments, credit applications and unapplications, advance
allocations and unallocations, payment reversals -- through the *service layer*
on a migrated database, while an independent in-test model tracks what must be
true. After every step the invariants are checked:

* monetary conservation: for every open bill, total = paid + credits + discounts
  + outstanding; the AP control account in the ledger equals the subledger;
* outstanding balances are never negative, unapplied balances never negative;
* no double counting: every allocation / application in force is counted exactly
  once, on exactly one bill;
* historical replay: once a date is in the past of the simulation, reports as of
  that date never change again, whatever happens later (reversals and
  unapplications included).

Failures print the seed and the operation log so they can be replayed. Set
``FMS_PROPERTY_SEEDS`` (comma-separated) or ``FMS_PROPERTY_STEPS`` to widen the search.
"""

import os
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Account, AccountType, EntryStatus, JournalEntry, JournalLine
from app.modules.payables import services
from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    BillStatus,
    CreditStatus,
    PaymentStatus,
    SupplierBill,
    SupplierCredit,
    VendorPayment,
)
from app.modules.payables.schemas import (
    AllocationIn,
    BillIn,
    BillLineIn,
    CreditIn,
    CreditLineIn,
    PaymentIn,
    VendorIn,
)

D = Decimal
SEEDS = [int(s) for s in os.environ.get("FMS_PROPERTY_SEEDS", "1,7,42").split(",")]
STEPS = int(os.environ.get("FMS_PROPERTY_STEPS", "60"))
INTEGRATIONS = Integrations()


def money(rng, lo, hi):
    return D(rng.randint(int(lo * 100), int(hi * 100))) / 100


class Simulation:
    """Random AP activity with a monotonically advancing clock."""

    def __init__(self, session, rng):
        self.db = session
        self.rng = rng
        self.today = date(2026, 1, 2)
        self.log: list[str] = []
        self.snapshots: dict[date, dict] = {}
        ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
        expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
        cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
        session.add_all([ap, expense, cash])
        session.commit()
        self.ap, self.expense, self.cash = ap, expense, cash
        self.vendors = [
            services.create_vendor(session, VendorIn(code=f"V{i}", name=f"Vendor {i}", payable_account_id=ap.id))
            for i in range(3)
        ]
        self.counter = 0

    # -- helpers ---------------------------------------------------------------------------------

    def tick(self):
        self.today += timedelta(days=self.rng.randint(0, 3))

    def note(self, message):
        self.log.append(f"{self.today}: {message}")

    def open_bills(self, vendor_id=None):
        bills = self.db.scalars(select(SupplierBill).where(SupplierBill.status.in_((BillStatus.APPROVED, BillStatus.PARTIALLY_PAID)))).all()
        return [b for b in bills if (vendor_id is None or b.vendor_id == vendor_id) and not b.on_hold]

    def expect_refused(self, fn):
        try:
            fn()
        except services.PayablesError:
            self.db.rollback()
            return True
        return False

    # -- operations --------------------------------------------------------------------------------

    def op_bill(self):
        vendor = self.rng.choice(self.vendors)
        self.counter += 1
        total = money(self.rng, 10, 500)
        bill = services.create_bill(self.db, BillIn(
            vendor_id=vendor.id, invoice_number=f"INV-{vendor.code}-{self.counter:04d}", invoice_date=self.today,
            due_date=self.today + timedelta(days=30), total=total,
            lines=[BillLineIn(description="x", account_id=self.expense.id, unit_price=total)]))
        services.approve_bill(self.db, bill.id, actor="sim", integrations=INTEGRATIONS)
        self.note(f"bill {bill.id} {total} for {vendor.code}")

    def op_payment(self):
        vendor = self.rng.choice(self.vendors)
        bills = self.open_bills(vendor.id)
        allocations = []
        for bill in self.rng.sample(bills, k=min(len(bills), self.rng.randint(0, 2))):
            amount = money(self.rng, 0.01, float(bill.outstanding))
            allocations.append(AllocationIn(bill_id=bill.id, amount=amount))
        allocated = sum((a.amount for a in allocations), D("0"))
        advance = money(self.rng, 0, 60) if self.rng.random() < 0.4 else D("0")
        amount = allocated + advance
        if amount <= 0:
            return
        payment, _ = services.record_payment(self.db, PaymentIn(
            vendor_id=vendor.id, payment_date=self.today, amount=amount, cash_account_id=self.cash.id, recorded_by="sim",
            allocations=allocations), integrations=INTEGRATIONS)
        self.note(f"payment {payment.id} {amount} ({len(allocations)} allocations, advance {advance})")

    def op_over_allocation_is_refused(self):
        bills = self.open_bills()
        if not bills:
            return
        bill = self.rng.choice(bills)
        too_much = bill.outstanding + D("0.01")
        refused = self.expect_refused(lambda: services.record_payment(self.db, PaymentIn(
            vendor_id=bill.vendor_id, payment_date=self.today, amount=too_much, cash_account_id=self.cash.id, recorded_by="sim",
            allocations=[AllocationIn(bill_id=bill.id, amount=too_much)]), integrations=INTEGRATIONS))
        assert refused, f"over-allocation of bill {bill.id} was accepted"
        self.note(f"over-allocation of bill {bill.id} refused")

    def op_credit(self):
        vendor = self.rng.choice(self.vendors)
        self.counter += 1
        total = money(self.rng, 5, 120)
        credit = services.create_credit(self.db, CreditIn(
            vendor_id=vendor.id, credit_number=f"CN-{self.counter:04d}", credit_date=self.today, total=total,
            lines=[CreditLineIn(description="c", account_id=self.expense.id, unit_price=total)]))
        services.approve_credit(self.db, credit.id, actor="sim", integrations=INTEGRATIONS)
        self.note(f"credit {credit.id} {total} for {vendor.code}")

    def op_apply_credit(self):
        credits = self.db.scalars(select(SupplierCredit).where(SupplierCredit.status.in_((CreditStatus.APPROVED, CreditStatus.PARTIALLY_APPLIED)))).all()
        candidates = [(c, b) for c in credits for b in self.open_bills(c.vendor_id)
                      if not any(a.bill_id == b.id and a.is_active for a in c.applications)]
        if not candidates:
            return
        credit, bill = self.rng.choice(candidates)
        amount = money(self.rng, 0.01, float(min(credit.remaining, bill.outstanding)))
        services.apply_credit(self.db, credit.id, bill_id=bill.id, amount=amount, actor="sim", application_date=self.today,
                              integrations=INTEGRATIONS)
        self.note(f"apply credit {credit.id} -> bill {bill.id} {amount}")

    def op_unapply_credit(self):
        credits = self.db.scalars(select(SupplierCredit)).all()
        active = [(c, a) for c in credits for a in c.applications if a.is_active]
        if not active:
            return
        credit, application = self.rng.choice(active)
        services.unapply_credit(self.db, credit.id, application.id, actor="sim", reason="sim", unapplication_date=self.today,
                                integrations=INTEGRATIONS)
        self.note(f"unapply application {application.id} of credit {credit.id}")

    def op_apply_advance(self):
        payments = [p for p in self.db.scalars(select(VendorPayment)).all() if p.unapplied_amount > 0]
        candidates = [(p, b) for p in payments for b in self.open_bills(p.vendor_id)
                      if not any(a.bill_id == b.id and a.is_active and a.kind.value == "advance" for a in p.allocations)]
        if not candidates:
            return
        payment, bill = self.rng.choice(candidates)
        amount = money(self.rng, 0.01, float(min(payment.unapplied_amount, bill.outstanding)))
        services.apply_advance(self.db, payment.id, bill_id=bill.id, amount=amount, actor="sim", allocation_date=self.today,
                               integrations=INTEGRATIONS)
        self.note(f"apply advance {payment.id} -> bill {bill.id} {amount}")

    def op_unallocate_advance(self):
        payments = self.db.scalars(select(VendorPayment)).all()
        active = [(p, a) for p in payments for a in p.allocations if a.is_active and a.kind.value == "advance"]
        if not active:
            return
        payment, allocation = self.rng.choice(active)
        services.unallocate_advance(self.db, payment.id, allocation.id, actor="sim", reason="sim", unallocation_date=self.today,
                                    integrations=INTEGRATIONS)
        self.note(f"unallocate allocation {allocation.id} of payment {payment.id}")

    def op_reverse_payment(self):
        payments = [p for p in self.db.scalars(select(VendorPayment)).all() if p.status == PaymentStatus.RECORDED]
        if not payments:
            return
        payment = self.rng.choice(payments)
        services.reverse_payment(self.db, payment.id, actor="sim", reason="sim", reversal_date=self.today, integrations=INTEGRATIONS)
        self.note(f"reverse payment {payment.id}")

    OPERATIONS = ("bill", "bill", "payment", "payment", "credit", "apply_credit", "unapply_credit",
                  "apply_advance", "unallocate_advance", "reverse_payment", "over_allocation_is_refused")

    def step(self):
        self.tick()
        getattr(self, f"op_{self.rng.choice(self.OPERATIONS)}")()
        self.db.expire_all()

    # -- invariants --------------------------------------------------------------------------------------

    def ledger_ap_balance(self, as_of):
        rows = self.db.execute(
            select(JournalLine.debit, JournalLine.credit).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(JournalLine.account_id == self.ap.id, JournalEntry.status == EntryStatus.POSTED, JournalEntry.entry_date <= as_of)
        ).all()
        return sum((credit - debit for debit, credit in rows), D("0"))

    def check_invariants(self):
        bills = self.db.scalars(select(SupplierBill)).all()
        for bill in bills:
            paid, credited, discounted = bill.settled_as_of(self.today)
            assert bill.outstanding >= 0, f"bill {bill.id} outstanding {bill.outstanding}"
            assert bill.total == paid + credited + discounted + bill.outstanding_as_of(self.today), f"bill {bill.id} does not conserve"
            assert bill.outstanding == bill.outstanding_as_of(self.today), f"bill {bill.id} current vs as-of mismatch"
            expected = (BillStatus.PAID if bill.outstanding == 0 else BillStatus.PARTIALLY_PAID if bill.amount_settled > 0 else BillStatus.APPROVED)
            assert bill.status == expected, f"bill {bill.id} status {bill.status} expected {expected}"
        for payment in self.db.scalars(select(VendorPayment)).all():
            assert payment.unapplied_amount >= 0, f"payment {payment.id} over-allocated"
            active = [a for a in payment.allocations if a.is_active]
            assert sum((a.amount for a in active), D("0")) <= payment.amount
        for credit in self.db.scalars(select(SupplierCredit)).all():
            assert credit.remaining >= 0, f"credit {credit.id} over-applied"
        # Every allocation / application in force is counted exactly once across all bills.
        counted = sum((b.amount_paid + b.credits_applied for b in bills), D("0"))
        direct = sum((a.amount for p in self.db.scalars(select(VendorPayment)).all() for a in p.allocations if a.is_active), D("0"))
        direct += sum((a.amount for c in self.db.scalars(select(SupplierCredit)).all() for a in c.applications if a.is_active), D("0"))
        assert counted == direct, "settlement rows double counted"
        # Ledger control account equals the subledger replay, and the statements reconcile.
        recon = services.reconciliation_report(self.db, as_of=self.today)
        assert recon.exceptions == [] and recon.total_difference == 0, f"reconciliation: {recon.exceptions}"
        assert recon.total_ledger == self.ledger_ap_balance(self.today)
        for vendor in self.vendors:
            statement = services.vendor_statement(self.db, vendor.id, date_from=date(2026, 1, 1), date_to=self.today)
            assert statement.difference == 0, f"vendor {vendor.code} statement does not reconcile"

    def snapshot(self):
        """Freeze every report as of a day strictly in the past."""
        as_of = self.today - timedelta(days=1)
        self.snapshots.setdefault(as_of, {
            "outstanding": services.outstanding_report(self.db, as_of=as_of).model_dump(),
            "aging": services.aging_report(self.db, as_of=as_of).model_dump(),
            "reconciliation": services.reconciliation_report(self.db, as_of=as_of).model_dump(),
            "balances": [b.model_dump() for b in services.vendor_balances(self.db, as_of=as_of)],
            "statements": [services.vendor_statement(self.db, v.id, date_from=date(2026, 1, 1), date_to=as_of).model_dump() for v in self.vendors],
        })

    def check_replay(self):
        for as_of, frozen in self.snapshots.items():
            current = {
                "outstanding": services.outstanding_report(self.db, as_of=as_of).model_dump(),
                "aging": services.aging_report(self.db, as_of=as_of).model_dump(),
                "reconciliation": services.reconciliation_report(self.db, as_of=as_of).model_dump(),
                "balances": [b.model_dump() for b in services.vendor_balances(self.db, as_of=as_of)],
                "statements": [services.vendor_statement(self.db, v.id, date_from=date(2026, 1, 1), date_to=as_of).model_dump() for v in self.vendors],
            }
            for key in frozen:
                assert _stable(current[key]) == _stable(frozen[key]), f"report {key} as of {as_of} changed after later operations"


def _stable(report):
    """Drop fields that legitimately change later (a bill's *current* status / hold flag)."""
    if isinstance(report, dict):
        return {k: _stable(v) for k, v in report.items() if k not in ("status", "on_hold")}
    if isinstance(report, list):
        return [_stable(item) for item in report]
    return report


@pytest.mark.parametrize("seed", SEEDS)
def test_random_ap_activity_keeps_the_books_consistent(session, seed):
    rng = random.Random(seed)
    sim = Simulation(session, rng)
    try:
        for step in range(STEPS):
            sim.step()
            sim.check_invariants()
            if step % 7 == 0:
                sim.snapshot()
            if step % 11 == 0:
                sim.check_replay()
        sim.check_replay()
    except AssertionError as exc:  # pragma: no cover - diagnostic path
        log = "\n".join(sim.log[-25:])
        raise AssertionError(f"seed={seed} step log (last 25):\n{log}\n{exc}") from exc
    # Sanity: the simulation did exercise the interesting paths.
    kinds = {line.split(": ", 1)[1].split(" ")[0] for line in sim.log}
    assert {"bill", "payment"} <= kinds
    assert session.scalar(select(func.count()).select_from(JournalEntry)) > 10
