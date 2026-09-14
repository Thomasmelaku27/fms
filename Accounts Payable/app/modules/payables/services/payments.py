"""Vendor payments: recording, idempotent retries, reversal and correction.

Recording a payment posts ``Dr payables / Cr cash`` (plus ``Dr payables / Cr
discount account`` for each early-payment discount taken with it). Allocations
to bills are written as ``original`` rows together with the payment and never
change; any unallocated remainder is an advance carried on the payment's
payable account (see ``settlement.apply_advance``).

A recorded payment is immutable (the database freezes every column, including
method, reference and actor). The only ways to change it are explicit:

* :func:`reverse_payment` -- mirror-image entry on the reversal date, status
  ``reversed`` set exactly once, discounts it earned reversed with it;
* :func:`correct_payment` -- reversal plus a replacement payment, two posted
  entries, one transaction.
"""

from collections.abc import Sequence
from datetime import date
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import EntryStatus, JournalEntry
from app.modules.payables.integration import Integrations
from app.modules.payables.ledger_adapter import LedgerPostingRefused
from app.modules.payables.models import (
    OPEN_BILL_STATUSES,
    AllocationKind,
    PaymentAllocation,
    PaymentStatus,
    SupplierBill,
    Vendor,
    VendorPayment,
)
from app.modules.payables.schemas import AllocationIn, PaymentIn
from app.modules.payables.services.common import (
    ZERO,
    Conflict,
    NotFound,
    PayablesError,
    ValidationFailed,
    assert_period_open,
    lock,
    referenced_vendor,
    require_cash_account,
    require_payable_account,
    require_vendor_payments,
    transition,
    utcnow,
)
from app.modules.payables.services.settlement import (
    reverse_discounts_of_payment,
    sync_bill_status,
    take_discount,
)

# --- idempotency --------------------------------------------------------------------------------


def _request_fingerprint(data: PaymentIn) -> tuple:
    """Every field that makes a payment request *that* payment (idempotency comparison)."""
    remainder = data.amount - sum((a.amount for a in data.allocations), ZERO)
    return (
        data.vendor_id,
        data.payment_date,
        data.amount,
        data.method,
        data.reference,
        data.cash_account_id,
        data.recorded_by,
        data.payable_account_id if remainder > 0 else None,
        frozenset((a.bill_id, a.amount, a.take_discount) for a in data.allocations),
    )


def _payment_fingerprint(payment: VendorPayment) -> tuple:
    discounted = {d.bill_id for d in payment.discounts}
    return (
        payment.vendor_id,
        payment.payment_date,
        payment.amount,
        payment.method,
        payment.reference,
        payment.cash_account_id,
        payment.recorded_by,
        payment.payable_account_id,  # None unless the payment carried a remainder
        frozenset(
            (a.bill_id, a.amount, a.bill_id in discounted)
            for a in payment.allocations
            if a.kind == AllocationKind.ORIGINAL
        ),
    )


def _existing_by_key(db: Session, data: PaymentIn) -> VendorPayment | None:
    if data.idempotency_key is None:
        return None
    existing = db.scalar(
        select(VendorPayment).where(VendorPayment.idempotency_key == data.idempotency_key)
    )
    if existing is None:
        return None
    request = _request_fingerprint(data)
    recorded = _payment_fingerprint(existing)
    # A client that omitted payable_account_id still matches a payment that used the
    # vendor default (same remainder semantics); anything else must be identical.
    if request[7] is None and recorded[7] is not None:
        recorded = (*recorded[:7], None, recorded[8])
    if request != recorded:
        raise Conflict(
            f"Idempotency key {data.idempotency_key!r} was already used for a different payment"
        )
    return existing


# --- validation -----------------------------------------------------------------------------------


def _allocation_problems(
    vendor: Vendor,
    bills: dict[int, SupplierBill],
    allocations: Sequence[AllocationIn],
    pay_date: date,
) -> list[str]:
    problems = []
    for allocation in allocations:
        bill = bills.get(allocation.bill_id)
        if bill is None:
            problems.append(f"bill {allocation.bill_id} does not exist")
            continue
        if bill.vendor_id != vendor.id:
            problems.append(f"bill {bill.id} belongs to another vendor")
        elif bill.status not in OPEN_BILL_STATUSES:
            problems.append(f"bill {bill.id} is {bill.status.value} and cannot receive payments")
        elif allocation.amount > bill.outstanding:
            problems.append(
                f"allocation {allocation.amount:.2f} to bill {bill.id} exceeds its outstanding "
                f"amount {bill.outstanding:.2f}"
            )
        elif allocation.take_discount:
            discount = bill.discount_available_on(pay_date)
            if discount <= 0:
                problems.append(
                    f"bill {bill.id} has no early-payment discount available on {pay_date}"
                )
            elif allocation.amount + discount != bill.outstanding:
                problems.append(
                    f"taking the discount of {discount:.2f} on bill {bill.id} requires an "
                    f"allocation of {bill.outstanding - discount:.2f} (the remaining balance)"
                )
        if bill.on_hold:
            holds = ", ".join(f"{h.kind.value}: {h.reason}" for h in bill.active_holds)
            problems.append(f"bill {bill.id} is on hold ({holds}); release it before paying")
    return problems


# --- recording ------------------------------------------------------------------------------------


def _record_payment_uncommitted(
    db: Session,
    data: PaymentIn,
    *,
    integrations: Integrations,
    corrects: VendorPayment | None = None,
    allow_inactive_vendor: bool = False,
) -> tuple[VendorPayment, bool]:
    """Everything :func:`record_payment` does except committing (shared with corrections).

    ``allow_inactive_vendor`` is used when recording a transfer the gateway has already
    confirmed: the cash is gone whatever the vendor's status is now.
    """
    vendor = referenced_vendor(db, data.vendor_id)
    if not allow_inactive_vendor:
        require_vendor_payments(vendor, "record payments")
    require_cash_account(db, data.cash_account_id)
    bills = lock(db, SupplierBill, (a.bill_id for a in data.allocations))
    if problems := _allocation_problems(vendor, bills, data.allocations, data.payment_date):
        raise ValidationFailed("Payment cannot be recorded: " + "; ".join(problems))
    allocated = sum((a.amount for a in data.allocations), ZERO)
    remainder = data.amount - allocated
    payable_account_id = (
        data.payable_account_id
        if data.payable_account_id is not None
        else vendor.payable_account_id
    )
    if remainder > 0:
        if payable_account_id is None:
            raise ValidationFailed(
                "Payment cannot be recorded: an unapplied remainder needs a payable account "
                "(set one on the payment or the vendor)"
            )
        require_payable_account(db, payable_account_id)
    assert_period_open(db, data.payment_date, "Payment", integrations)

    payment = VendorPayment(
        vendor_id=vendor.id,
        payment_date=data.payment_date,
        amount=data.amount,
        method=data.method,
        reference=data.reference,
        cash_account_id=data.cash_account_id,
        payable_account_id=payable_account_id if remainder > 0 else None,
        idempotency_key=data.idempotency_key,
        recorded_by=data.recorded_by,
        status=PaymentStatus.RECORDED,
        corrects_payment_id=corrects.id if corrects is not None else None,
        allocations=[
            PaymentAllocation(
                bill=bills[a.bill_id],
                amount=a.amount,
                kind=AllocationKind.ORIGINAL,
                allocation_date=data.payment_date,
                allocated_by=data.recorded_by,
            )
            for a in data.allocations
        ],
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if "idempotency_key" in str(exc.orig) and (existing := _existing_by_key(db, data)):
            return existing, False  # lost the race against an identical request
        raise

    for allocation in data.allocations:
        if allocation.take_discount:
            bill = bills[allocation.bill_id]
            take_discount(
                db,
                bill,
                payment,
                amount=bill.discount_available_on(data.payment_date),
                actor=data.recorded_by,
                integrations=integrations,
            )
    entry = integrations.ledger.draft_payment_entry(db, payment)
    payment.journal_entry_id = entry.id
    for bill_id in {a.bill_id for a in data.allocations}:
        sync_bill_status(db, bills[bill_id])
    db.flush()
    integrations.audit.record(
        db,
        actor=data.recorded_by,
        action="payables.payment.recorded",
        resource=f"vendor_payment:{payment.id}",
    )
    integrations.ledger.post(db, entry)
    return payment, True


def record_payment(
    db: Session, data: PaymentIn, *, integrations: Integrations
) -> tuple[VendorPayment, bool]:
    """Record a vendor payment, allocate it to bills and post it to the ledger.

    Returns ``(payment, created)``; ``created`` is ``False`` when the request
    repeats an earlier one with the same ``idempotency_key`` *and* the same
    content (any difference in vendor, date, amount, method, reference, cash
    account, actor, payable account or allocations is a 409). Atomic: payment,
    allocations, discounts, bill status changes and journal entries commit
    together or not at all.
    """
    if (existing := _existing_by_key(db, data)) is not None:
        return existing, False
    try:
        payment, created = _record_payment_uncommitted(db, data, integrations=integrations)
    except LedgerPostingRefused as exc:
        db.rollback()
        raise ValidationFailed(
            f"Payment cannot be recorded: ledger refused the entry: {exc}"
        ) from exc
    if not created:
        return payment, False
    db.commit()
    db.refresh(payment)
    return payment, True


# --- reversal and correction ----------------------------------------------------------------------


def _reverse_payment_uncommitted(
    db: Session,
    payment: VendorPayment,
    *,
    actor: str,
    reason: str,
    reversal_date: date | None,
    integrations: Integrations,
) -> JournalEntry:
    if payment.status != PaymentStatus.RECORDED:
        raise Conflict(f"Payment {payment.id} is already reversed")
    if payment.journal_entry is None or payment.journal_entry.status != EntryStatus.POSTED:
        raise ValidationFailed(f"Payment {payment.id} has no posted journal entry to reverse")
    reversal_date = reversal_date or date.today()
    if reversal_date < payment.payment_date:
        raise ValidationFailed("Reversal date cannot precede the payment date")
    latest_allocation = max(
        (a.allocation_date for a in payment.allocations), default=payment.payment_date
    )
    if reversal_date < latest_allocation:
        raise ValidationFailed(
            "Reversal date cannot precede the latest advance allocation of the payment"
        )
    assert_period_open(db, reversal_date, "Payment reversal", integrations)
    bills = lock(db, SupplierBill, (a.bill_id for a in payment.allocations))

    label = payment.reference or f"#{payment.id}"
    entry = integrations.ledger.draft_reversal_entry(
        db,
        payment.journal_entry,
        entry_date=reversal_date,
        reference=f"AP-PAY-REV-{payment.id}",
        description=f"Reversal of vendor payment {label} - {payment.vendor.name}: {reason}",
    )
    transition(
        db,
        VendorPayment,
        payment,
        PaymentStatus.RECORDED,
        status=PaymentStatus.REVERSED,
        reversed_by=actor,
        reversed_at=utcnow(),
        reversal_date=reversal_date,
        reversal_reason=reason,
        reversal_journal_entry_id=entry.id,
    )
    reverse_discounts_of_payment(
        db, payment, actor=actor, reversal_date=reversal_date, integrations=integrations
    )
    for bill in bills.values():
        sync_bill_status(db, bill)
    db.flush()
    integrations.audit.record(
        db, actor=actor, action="payables.payment.reversed", resource=f"vendor_payment:{payment.id}"
    )
    integrations.ledger.post(db, entry)
    return entry


def reverse_payment(
    db: Session,
    payment_id: int,
    *,
    actor: str,
    reason: str,
    reversal_date: date | None = None,
    integrations: Integrations,
) -> VendorPayment:
    """Undo a recorded payment with a mirror-image journal entry.

    The original payment, its allocations and its journal entry are preserved;
    the payment is marked ``reversed`` (once), the discounts it earned are
    reversed with it and the bills it settled regain their outstanding amounts.
    """
    payment = lock(db, VendorPayment, [payment_id]).get(payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    try:
        _reverse_payment_uncommitted(
            db,
            payment,
            actor=actor,
            reason=reason,
            reversal_date=reversal_date,
            integrations=integrations,
        )
    except LedgerPostingRefused as exc:
        db.rollback()
        raise ValidationFailed(
            f"Payment cannot be reversed: ledger refused the entry: {exc}"
        ) from exc
    db.commit()
    db.refresh(payment)
    return payment


def correct_payment(
    db: Session,
    payment_id: int,
    *,
    actor: str,
    reason: str,
    reversal_date: date | None,
    replacement: PaymentIn,
    integrations: Integrations,
) -> tuple[VendorPayment, VendorPayment]:
    """Reverse a payment and record its replacement as one unit of work.

    Two (or more) journal entries are posted; both AP rows and all entries
    commit together or not at all. The replacement may not be dated before the
    reversal, otherwise as-of balances would count both.
    """
    payment = lock(db, VendorPayment, [payment_id]).get(payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    if replacement.idempotency_key is not None and _existing_by_key(db, replacement) is not None:
        raise Conflict(
            f"Idempotency key {replacement.idempotency_key!r} already belongs to a recorded payment"
        )
    effective_reversal_date = reversal_date or date.today()
    if replacement.payment_date < effective_reversal_date:
        raise ValidationFailed("The replacement payment cannot be dated before the reversal")
    try:
        _reverse_payment_uncommitted(
            db,
            payment,
            actor=actor,
            reason=reason,
            reversal_date=effective_reversal_date,
            integrations=integrations,
        )
        new_payment, created = _record_payment_uncommitted(
            db, replacement, integrations=integrations, corrects=payment
        )
        if not created:  # pragma: no cover - key checked above; defensive
            db.rollback()
            raise Conflict("Replacement payment already exists")
    except LedgerPostingRefused as exc:
        db.rollback()
        raise ValidationFailed(
            f"Payment cannot be corrected: ledger refused the entry: {exc}"
        ) from exc
    except PayablesError:
        db.rollback()  # e.g. the replacement fails validation after the reversal was drafted
        raise
    db.commit()
    db.refresh(payment)
    db.refresh(new_payment)
    return payment, new_payment


def list_payments(
    db: Session,
    *,
    vendor_id: int | None = None,
    status: PaymentStatus | None = None,
    unapplied_only: bool = False,
) -> list[VendorPayment]:
    stmt = (
        select(VendorPayment)
        .options(selectinload(VendorPayment.allocations), selectinload(VendorPayment.discounts))
        .order_by(VendorPayment.payment_date, VendorPayment.id)
    )
    if vendor_id is not None:
        stmt = stmt.where(VendorPayment.vendor_id == vendor_id)
    if status is not None:
        stmt = stmt.where(VendorPayment.status == status)
    payments = list(db.scalars(stmt))
    if unapplied_only:
        payments = [p for p in payments if p.unapplied_amount > 0]
    return payments
