"""Settlement of bills: status derivation, schedules, advances, discounts, balances.

A bill's settlement state is *derived* from the rows in force against it --
payment allocations (of recorded payments, not unallocated), credit
applications (not unapplied) and discounts (not reversed). Every service that
adds or closes such a row calls :func:`sync_bill_status` so the stored status
follows. Each of those rows is dated, so the same derivation as of any earlier
date reconstructs the historical state (``SupplierBill.outstanding_as_of``).

Advances: a payment whose allocations total less than its amount carries an
unapplied remainder on its payable account. :func:`apply_advance` applies part
of that remainder to a bill later (an ``advance`` allocation, no ledger effect:
both sides already sit in payables) and :func:`unallocate_advance` closes such
an allocation with a date, actor and reason instead of deleting it.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.modules.payables.integration import Integrations
from app.modules.payables.ledger_adapter import LedgerPostingRefused
from app.modules.payables.models import (
    OPEN_BILL_STATUSES,
    SETTLEMENT_BILL_STATUSES,
    SETTLEMENT_CREDIT_STATUSES,
    AllocationKind,
    BillDiscount,
    BillPaymentSchedule,
    BillStatus,
    CreditStatus,
    PaymentAllocation,
    PaymentStatus,
    SupplierBill,
    SupplierCredit,
    Vendor,
    VendorPayment,
)
from app.modules.payables.schemas import InstallmentIn, InstallmentOut, ScheduleOut, VendorBalance
from app.modules.payables.services.common import (
    ZERO,
    Conflict,
    NotFound,
    ValidationFailed,
    assert_period_open,
    bill_query,
    get_bill,
    get_vendor,
    lock,
    transition,
    utcnow,
)

# --- settlement state ----------------------------------------------------------------------------


def settlement_status(bill: SupplierBill) -> BillStatus:
    settled = bill.amount_settled
    if settled <= 0:
        return BillStatus.APPROVED
    if settled < bill.total:
        return BillStatus.PARTIALLY_PAID
    return BillStatus.PAID


def sync_bill_status(db: Session, bill: SupplierBill) -> None:
    """Recompute a bill's settlement status from the in-memory rows against it."""
    db.flush()
    if bill.outstanding < 0:
        db.rollback()
        raise Conflict(f"Bill {bill.id} would be over-settled")
    transition(db, SupplierBill, bill, SETTLEMENT_BILL_STATUSES, status=settlement_status(bill))


def credit_settlement_status(credit: SupplierCredit) -> CreditStatus:
    applied = credit.amount_applied
    if applied <= 0:
        return CreditStatus.APPROVED
    if applied < credit.total:
        return CreditStatus.PARTIALLY_APPLIED
    return CreditStatus.APPLIED


def sync_credit_status(db: Session, credit: SupplierCredit) -> None:
    db.flush()
    if credit.remaining < 0:
        db.rollback()
        raise Conflict(f"Credit {credit.id} would be over-applied")
    transition(
        db,
        SupplierCredit,
        credit,
        SETTLEMENT_CREDIT_STATUSES,
        status=credit_settlement_status(credit),
    )


# --- payment schedules ----------------------------------------------------------------------------


def installments_with_remaining(
    installments: Sequence[BillPaymentSchedule], amount_settled: Decimal
) -> list[InstallmentOut]:
    """Apply what has been settled to the installments in order (earliest first)."""
    unapplied = amount_settled
    rows = []
    for installment in installments:
        applied = min(unapplied, installment.amount)
        unapplied -= applied
        rows.append(
            InstallmentOut(
                installment_no=installment.installment_no,
                due_date=installment.due_date,
                amount=installment.amount,
                amount_remaining=installment.amount - applied,
            )
        )
    return rows


def get_schedule(db: Session, bill_id: int) -> ScheduleOut:
    bill = get_bill(db, bill_id)
    return ScheduleOut(
        bill_id=bill.id,
        status=bill.status,
        total=bill.total,
        amount_paid=bill.amount_settled,
        installments=installments_with_remaining(bill.schedule, bill.amount_settled),
    )


def set_schedule(db: Session, bill_id: int, installments: Sequence[InstallmentIn]) -> ScheduleOut:
    """Replace the installments of an approved, not fully paid bill."""
    bill = get_bill(db, bill_id)
    if bill.status not in OPEN_BILL_STATUSES:
        raise Conflict(
            f"Bill is {bill.status.value}; a payment schedule needs an approved, unpaid bill"
        )
    planned = sum((i.amount for i in installments), ZERO)
    if planned != bill.total:
        raise ValidationFailed(
            f"Installments ({planned:.2f}) must sum to the bill total ({bill.total:.2f})"
        )
    if any(i.due_date < bill.invoice_date for i in installments):
        raise ValidationFailed("Installment due dates cannot precede the invoice date")

    bill.schedule.clear()
    db.flush()
    bill.schedule.extend(
        BillPaymentSchedule(installment_no=number, due_date=i.due_date, amount=i.amount)
        for number, i in enumerate(sorted(installments, key=lambda i: i.due_date), start=1)
    )
    db.commit()
    db.refresh(bill)
    return get_schedule(db, bill.id)


# --- advances (unapplied payments) ---------------------------------------------------------------


def _apply_advance_uncommitted(
    db: Session,
    payment_id: int,
    *,
    bill_id: int,
    amount: Decimal,
    actor: str,
    allocation_date: date | None,
    integrations: Integrations,
) -> tuple[VendorPayment, PaymentAllocation]:
    """Validate and write one advance allocation (no commit; shared with payment runs)."""
    payment = lock(db, VendorPayment, [payment_id]).get(payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    bill = lock(db, SupplierBill, [bill_id]).get(bill_id)
    allocation_date = allocation_date or date.today()
    problems = []
    if payment.status != PaymentStatus.RECORDED:
        problems.append(f"payment is {payment.status.value}")
    if amount > payment.unapplied_amount:
        problems.append(
            f"amount {amount:.2f} exceeds the unapplied amount {payment.unapplied_amount:.2f} "
            "of the payment"
        )
    if bill is None:
        problems.append(f"bill {bill_id} does not exist")
    else:
        if bill.vendor_id != payment.vendor_id:
            problems.append(f"bill {bill.id} belongs to another vendor")
        if bill.status not in OPEN_BILL_STATUSES:
            problems.append(f"bill {bill.id} is {bill.status.value} and cannot receive payments")
        elif amount > bill.outstanding:
            problems.append(
                f"amount {amount:.2f} exceeds the outstanding amount {bill.outstanding:.2f} "
                f"of bill {bill.id}"
            )
        if bill.on_hold:
            problems.append(f"bill {bill.id} is on hold or disputed")
        if (
            payment.payable_account_id is None
            or bill.payable_account_id != payment.payable_account_id
        ):
            problems.append(f"bill {bill.id} posts to a different payable account than the advance")
        if allocation_date < max(payment.payment_date, bill.invoice_date):
            problems.append("allocation date cannot precede the payment or the bill")
        if any(
            a.bill_id == bill.id
            and a.kind == AllocationKind.ADVANCE
            and a.is_active
            and a.amount == amount
            and a.allocation_date == allocation_date
            for a in payment.allocations
        ):
            # Repeat-request protection (see credits): identical active allocation = duplicate.
            problems.append(
                f"advance is already applied to bill {bill.id} with amount {amount:.2f} on "
                f"{allocation_date.isoformat()}; unapply it first to change it"
            )
    if problems:
        raise ValidationFailed("Advance cannot be applied: " + "; ".join(problems))
    assert_period_open(db, allocation_date, "Advance application", integrations)

    allocation = PaymentAllocation(
        payment=payment,
        bill=bill,
        amount=amount,
        kind=AllocationKind.ADVANCE,
        allocation_date=allocation_date,
        allocated_by=actor,
    )
    db.add(allocation)
    sync_bill_status(db, bill)
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.payment.advance_applied",
        resource=f"payment_allocation:{allocation.id}",
    )
    return payment, allocation


def apply_advance(
    db: Session,
    payment_id: int,
    *,
    bill_id: int,
    amount: Decimal,
    actor: str,
    allocation_date: date | None,
    integrations: Integrations,
) -> VendorPayment:
    """Apply part of a payment's unapplied remainder to an open bill (no ledger effect)."""
    payment, _ = _apply_advance_uncommitted(
        db,
        payment_id,
        bill_id=bill_id,
        amount=amount,
        actor=actor,
        allocation_date=allocation_date,
        integrations=integrations,
    )
    db.commit()
    db.refresh(payment)
    return payment


def unallocate_advance(
    db: Session,
    payment_id: int,
    allocation_id: int,
    *,
    actor: str,
    reason: str | None = None,
    unallocation_date: date | None = None,
    integrations: Integrations,
) -> VendorPayment:
    """Close an advance allocation (dated); the bill regains its outstanding amount."""
    payment = lock(db, VendorPayment, [payment_id]).get(payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    allocation = db.get(PaymentAllocation, allocation_id)
    if allocation is None or allocation.payment_id != payment.id:
        raise NotFound("Payment allocation not found")
    if allocation.kind != AllocationKind.ADVANCE:
        raise Conflict(
            "Original allocations cannot be unallocated; reverse or correct the payment instead"
        )
    if allocation.unallocation_date is not None:
        raise Conflict(f"Allocation {allocation.id} was already unallocated")
    unallocation_date = unallocation_date or date.today()
    if unallocation_date < allocation.allocation_date:
        raise ValidationFailed("Unallocation date cannot precede the allocation date")
    assert_period_open(db, unallocation_date, "Advance unallocation", integrations)
    bill = lock(db, SupplierBill, [allocation.bill_id])[allocation.bill_id]

    allocation.unallocation_date = unallocation_date
    allocation.unallocated_by = actor
    allocation.unallocated_at = utcnow()
    allocation.unallocation_reason = reason
    sync_bill_status(db, bill)
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.payment.advance_unallocated",
        resource=f"payment_allocation:{allocation.id}",
    )
    db.commit()
    db.refresh(payment)
    return payment


# --- discounts ----------------------------------------------------------------------------------


def take_discount(
    db: Session,
    bill: SupplierBill,
    payment: VendorPayment,
    *,
    amount: Decimal,
    actor: str,
    integrations: Integrations,
) -> BillDiscount:
    """Record an early-payment discount earned by ``payment`` and post it (no commit).

    Ledger: ``Dr payables / Cr discount account``. The discount is a settlement
    row on the bill: it reduces the outstanding balance like a payment would.
    """
    if amount <= 0:
        raise ValidationFailed("Discount amount must be positive")
    available = bill.discount_available_on(payment.payment_date)
    if amount > available:
        raise ValidationFailed(
            f"Discount {amount:.2f} exceeds the discount available on bill {bill.id} "
            f"({available:.2f} when paid on {payment.payment_date})"
        )
    if bill.discount_account_id is None:
        raise ValidationFailed(f"Bill {bill.id} has no discount account")
    discount = BillDiscount(
        bill=bill,
        payment=payment,
        amount=amount,
        taken_on=payment.payment_date,
        taken_by=actor,
        discount_account_id=bill.discount_account_id,
    )
    db.add(discount)
    db.flush()
    entry = integrations.ledger.draft_discount_entry(db, discount)
    discount.journal_entry_id = entry.id
    db.flush()
    integrations.audit.record(
        db, actor=actor, action="payables.discount.taken", resource=f"bill_discount:{discount.id}"
    )
    integrations.ledger.post(db, entry)
    return discount


def reverse_discounts_of_payment(
    db: Session,
    payment: VendorPayment,
    *,
    actor: str,
    reversal_date: date,
    integrations: Integrations,
) -> list[BillDiscount]:
    """Reverse every discount a payment earned (called while the payment is reversed)."""
    reversed_ = []
    for discount in payment.discounts:
        if discount.reversal_date is not None:
            continue
        if discount.journal_entry is None:
            raise LedgerPostingRefused(f"discount {discount.id} has no journal entry")
        entry = integrations.ledger.draft_reversal_entry(
            db,
            discount.journal_entry,
            entry_date=reversal_date,
            reference=f"AP-DISC-REV-{discount.id}",
            description=f"Reversal of early-payment discount on bill {discount.bill.invoice_number}",
        )
        discount.reversal_date = reversal_date
        discount.reversed_by = actor
        discount.reversal_journal_entry_id = entry.id
        db.flush()
        integrations.ledger.post(db, entry)
        reversed_.append(discount)
    return reversed_


# --- vendor balances ----------------------------------------------------------------------------


def open_bills_as_of(db: Session, as_of: date, vendor_id: int | None = None):
    stmt = (
        bill_query()
        .options(selectinload(SupplierBill.vendor))
        .where(
            SupplierBill.status.in_(SETTLEMENT_BILL_STATUSES), SupplierBill.invoice_date <= as_of
        )
    )
    if vendor_id is not None:
        stmt = stmt.where(SupplierBill.vendor_id == vendor_id)
    return db.scalars(stmt)


def unapplied_credits_as_of(db: Session, as_of: date, *, vendor_id: int | None = None) -> Decimal:
    """Credit value in force on ``as_of`` that had not been applied to bills by then."""
    stmt = (
        select(SupplierCredit)
        .options(selectinload(SupplierCredit.applications))
        .where(
            SupplierCredit.status.in_((*SETTLEMENT_CREDIT_STATUSES, CreditStatus.REVERSED)),
            SupplierCredit.credit_date <= as_of,
        )
    )
    if vendor_id is not None:
        stmt = stmt.where(SupplierCredit.vendor_id == vendor_id)
    return sum((credit.remaining_as_of(as_of) for credit in db.scalars(stmt)), ZERO)


def unapplied_advances_as_of(db: Session, as_of: date, *, vendor_id: int | None = None) -> Decimal:
    """Payment value in force on ``as_of`` that was not applied to any bill by then."""
    stmt = (
        select(VendorPayment)
        .options(selectinload(VendorPayment.allocations))
        .where(VendorPayment.payment_date <= as_of)
    )
    if vendor_id is not None:
        stmt = stmt.where(VendorPayment.vendor_id == vendor_id)
    return sum((payment.unapplied_as_of(as_of) for payment in db.scalars(stmt)), ZERO)


def _balance_rows(db: Session, as_of: date, vendor_ids: list[int] | None = None) -> dict[int, dict]:
    """Per-vendor balance components as of ``as_of`` computed in one pass over each table."""
    rows: dict[int, dict] = defaultdict(
        lambda: {"outstanding": ZERO, "count": 0, "credits": ZERO, "advances": ZERO}
    )
    bills_stmt = bill_query().where(
        SupplierBill.status.in_(SETTLEMENT_BILL_STATUSES), SupplierBill.invoice_date <= as_of
    )
    credits_stmt = (
        select(SupplierCredit)
        .options(selectinload(SupplierCredit.applications))
        .where(
            SupplierCredit.status.in_((*SETTLEMENT_CREDIT_STATUSES, CreditStatus.REVERSED)),
            SupplierCredit.credit_date <= as_of,
        )
    )
    payments_stmt = (
        select(VendorPayment)
        .options(selectinload(VendorPayment.allocations))
        .where(VendorPayment.payment_date <= as_of)
    )
    if vendor_ids is not None:
        bills_stmt = bills_stmt.where(SupplierBill.vendor_id.in_(vendor_ids))
        credits_stmt = credits_stmt.where(SupplierCredit.vendor_id.in_(vendor_ids))
        payments_stmt = payments_stmt.where(VendorPayment.vendor_id.in_(vendor_ids))
    for bill in db.scalars(bills_stmt):
        amount = bill.outstanding_as_of(as_of)
        if amount > 0:
            rows[bill.vendor_id]["outstanding"] += amount
            rows[bill.vendor_id]["count"] += 1
    for credit in db.scalars(credits_stmt):
        rows[credit.vendor_id]["credits"] += credit.remaining_as_of(as_of)
    for payment in db.scalars(payments_stmt):
        rows[payment.vendor_id]["advances"] += payment.unapplied_as_of(as_of)
    return rows


def _to_balance(vendor: Vendor, as_of: date, row: dict) -> VendorBalance:
    return VendorBalance(
        vendor_id=vendor.id,
        vendor_code=vendor.code,
        vendor_name=vendor.name,
        as_of=as_of,
        bills_outstanding=row["outstanding"],
        unapplied_credits=row["credits"],
        unapplied_advances=row["advances"],
        net_owed=row["outstanding"] - row["credits"] - row["advances"],
        open_bill_count=row["count"],
    )


def vendor_balance(db: Session, vendor_id: int, *, as_of: date | None = None) -> VendorBalance:
    """Bill balances versus the net amount owed (bills less unapplied credits and advances)."""
    as_of = as_of or date.today()
    vendor = get_vendor(db, vendor_id)
    return _to_balance(vendor, as_of, _balance_rows(db, as_of, [vendor.id])[vendor.id])


def vendor_balances(db: Session, *, as_of: date | None = None) -> list[VendorBalance]:
    """All vendors in one pass (a handful of queries regardless of the number of vendors)."""
    as_of = as_of or date.today()
    rows = _balance_rows(db, as_of)
    vendors = db.scalars(select(Vendor).order_by(Vendor.code))
    return [_to_balance(vendor, as_of, rows[vendor.id]) for vendor in vendors]
