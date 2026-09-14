"""Supplier bill lifecycle: drafting, duplicate review, approval / rejection, holds.

Bill lifecycle::

    draft ──approve──▶ approved ◀──▶ partially_paid ◀──▶ paid
      │ ▲                 (settlement states; see ``settlement``)
      ▼ └──reopen── rejected

Approval is the accounting event: it posts ``Dr line accounts / Cr payables``
through the ledger port, snapshots the vendor's early-payment discount terms
onto the bill, opens a one-installment schedule and freezes the bill. Holds and
disputes keep a bill out of payment runs and manual payments until released.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.payables.integration import Integrations
from app.modules.payables.ledger_adapter import LedgerPostingRefused
from app.modules.payables.models import (
    BillHold,
    BillPaymentSchedule,
    BillStatus,
    DuplicateFlag,
    DuplicateFlagStatus,
    HoldKind,
    SupplierBill,
    SupplierBillLine,
    sql_normalize,
)
from app.modules.payables.schemas import BillIn, BillLineIn, BillUpdate, to_cents
from app.modules.payables.services import duplicates
from app.modules.payables.services.common import (
    ZERO,
    Conflict,
    DuplicateInvoice,
    NotFound,
    ValidationFailed,
    assert_period_open,
    bill_query,
    get_bill,
    referenced_vendor,
    require_line_accounts,
    require_payable_account,
    require_vendor_documents,
    transition,
    utcnow,
)

# --- shared drafting helpers -----------------------------------------------------------------


def build_lines(db: Session, lines: Sequence[BillLineIn], line_cls):
    require_line_accounts(db, (line.account_id for line in lines))
    return [
        line_cls(
            line_no=position,
            description=line.description,
            account_id=line.account_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_total=line.line_total,
        )
        for position, line in enumerate(lines, start=1)
    ]


def lines_total(lines) -> Decimal:
    return sum((line.line_total for line in lines), ZERO)


def validate_amounts(total: Decimal, lines: Sequence, label: str = "bill") -> None:
    """A draft may have no lines yet; if it has lines they must add up to the total."""
    if lines and (computed := lines_total(lines)) != total:
        raise ValidationFailed(
            f"Line totals ({computed:.2f}) do not equal {label} total ({total:.2f})"
        )


def _validate_dates(invoice_date: date, due_date: date) -> None:
    if due_date < invoice_date:
        raise ValidationFailed("Due date precedes invoice date")


def is_duplicate_error(exc: IntegrityError, table: str, column: str) -> bool:
    message = str(exc.orig)
    return (table in message and column in message) or f"uq_{table}_vendor" in message


def _assert_no_duplicate_invoice(
    db: Session, vendor_id: int, invoice_number: str, *, exclude_bill_id: int | None = None
) -> None:
    """API-layer duplicate check, using the database's own canonical form.

    The comparison value is computed with the same SQL expression the triggers
    use, so this check and the UNIQUE constraint can never disagree. The
    database repeats the check so a concurrent request cannot slip through; see
    :func:`_flush_bill`.
    """
    stmt = select(SupplierBill.id).where(
        SupplierBill.vendor_id == vendor_id,
        SupplierBill.invoice_number_normalized == sql_normalize(invoice_number),
    )
    if exclude_bill_id is not None:
        stmt = stmt.where(SupplierBill.id != exclude_bill_id)
    if db.scalar(stmt) is not None:
        raise DuplicateInvoice(vendor_id, invoice_number)


def _flush_bill(db: Session, bill: SupplierBill) -> None:
    """Flush, translating the database's duplicate-invoice rejection into a 409.

    The session is rolled back first so no partial rows (e.g. lines) survive.
    """
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if is_duplicate_error(exc, "supplier_bills", "invoice_number"):
            raise DuplicateInvoice(bill.vendor_id, bill.invoice_number) from exc
        raise


# --- drafting -------------------------------------------------------------------------------------


def create_bill(db: Session, data: BillIn) -> SupplierBill:
    vendor = referenced_vendor(db, data.vendor_id)
    require_vendor_documents(vendor, "create bills")
    due_date = data.due_date or data.invoice_date + timedelta(days=vendor.payment_terms_days)
    _validate_dates(data.invoice_date, due_date)
    payable_account_id = (
        data.payable_account_id
        if data.payable_account_id is not None
        else vendor.payable_account_id
    )
    if payable_account_id is not None:
        require_payable_account(db, payable_account_id)
    lines = build_lines(db, data.lines, SupplierBillLine)
    validate_amounts(data.total, lines)
    _assert_no_duplicate_invoice(db, vendor.id, data.invoice_number)

    bill = SupplierBill(
        vendor_id=vendor.id,
        invoice_number=data.invoice_number,
        invoice_date=data.invoice_date,
        due_date=due_date,
        total=data.total,
        description=data.description,
        payable_account_id=payable_account_id,
        lines=lines,
    )
    db.add(bill)
    _flush_bill(db, bill)
    duplicates.refresh_flags(db, bill)
    db.commit()
    db.refresh(bill)
    return bill


def update_bill(db: Session, bill_id: int, data: BillUpdate) -> SupplierBill:
    """Edit a draft. Everything is validated against the merged state before any write."""
    bill = get_bill(db, bill_id)
    if bill.status != BillStatus.DRAFT:
        raise Conflict(f"Bill is {bill.status.value}; only draft bills can be edited")
    fields = data.model_dump(exclude_unset=True)

    vendor = bill.vendor
    if "vendor_id" in fields and fields["vendor_id"] != bill.vendor_id:
        vendor = referenced_vendor(db, fields["vendor_id"])
        require_vendor_documents(vendor, "receive bills")

    invoice_number = fields.get("invoice_number", bill.invoice_number)
    invoice_date = fields.get("invoice_date", bill.invoice_date)
    due_date = fields.get("due_date", bill.due_date)
    if "invoice_date" in fields and "due_date" not in fields and due_date < invoice_date:
        due_date = invoice_date + timedelta(days=vendor.payment_terms_days)
    _validate_dates(invoice_date, due_date)
    total = fields.get("total", bill.total)

    payable_account_id = bill.payable_account_id
    if "payable_account_id" in fields:
        payable_account_id = fields["payable_account_id"]
        if payable_account_id is None:
            payable_account_id = vendor.payable_account_id
    if payable_account_id is not None and payable_account_id != bill.payable_account_id:
        require_payable_account(db, payable_account_id)

    new_lines = build_lines(db, data.lines, SupplierBillLine) if data.lines is not None else None
    validate_amounts(total, new_lines if new_lines is not None else bill.lines)
    _assert_no_duplicate_invoice(db, vendor.id, invoice_number, exclude_bill_id=bill.id)

    bill.vendor_id = vendor.id
    bill.invoice_number = invoice_number
    bill.invoice_date, bill.due_date = invoice_date, due_date
    bill.total = total
    bill.payable_account_id = payable_account_id
    if "description" in fields:
        bill.description = fields["description"]
    if new_lines is not None:
        bill.lines.clear()
        db.flush()  # delete the old rows before new ones reuse their line numbers
        bill.lines.extend(new_lines)
    _flush_bill(db, bill)
    duplicates.refresh_flags(db, bill)
    db.commit()
    db.refresh(bill)
    return bill


def delete_bill(db: Session, bill_id: int) -> None:
    bill = get_bill(db, bill_id)
    if bill.status != BillStatus.DRAFT:
        raise Conflict(f"Bill is {bill.status.value}; only draft bills can be deleted")
    db.delete(bill)
    db.commit()


def list_bills(
    db: Session,
    *,
    vendor_id: int | None = None,
    status: BillStatus | None = None,
    on_hold: bool | None = None,
) -> list[SupplierBill]:
    stmt = bill_query().order_by(SupplierBill.id)
    if vendor_id is not None:
        stmt = stmt.where(SupplierBill.vendor_id == vendor_id)
    if status is not None:
        stmt = stmt.where(SupplierBill.status == status)
    bills = list(db.scalars(stmt))
    if on_hold is not None:
        bills = [b for b in bills if b.on_hold == on_hold]
    return bills


# --- approval workflow ---------------------------------------------------------------------------


def approval_problems(db: Session, bill: SupplierBill) -> list[str]:
    """Every reason ``bill`` cannot be approved (empty list = approvable)."""
    problems: list[str] = []
    if not bill.vendor.accepts_documents:
        problems.append(f"vendor {bill.vendor.code} is {bill.vendor.status.value}")
    if not bill.lines:
        problems.append("bill has no lines")
    elif (computed := lines_total(bill.lines)) != bill.total:
        problems.append(f"line totals ({computed:.2f}) do not equal bill total ({bill.total:.2f})")
    if bill.payable_account_id is None:
        problems.append("bill has no payable account (set one on the bill or the vendor)")
    else:
        try:
            require_payable_account(db, bill.payable_account_id)
        except ValidationFailed as exc:
            problems.append(str(exc))
    try:
        require_line_accounts(db, (line.account_id for line in bill.lines))
    except ValidationFailed as exc:
        problems.append(str(exc))
    blocking = [
        f
        for f in bill.duplicate_flags
        if f.status == DuplicateFlagStatus.CONFIRMED
        or (f.status == DuplicateFlagStatus.PENDING and f.blocks_approval)
    ]
    if blocking:
        problems.append(
            "unresolved duplicate flags: "
            + ", ".join(f"#{f.id} ({f.status.value}, bill {f.suspected_bill_id})" for f in blocking)
        )
    return problems


def _discount_snapshot(bill: SupplierBill) -> tuple[date | None, Decimal, int | None]:
    """Early-payment terms frozen onto the bill from the vendor's policy at approval."""
    policy = bill.vendor.discount_policy
    if policy is None or not policy.is_active:
        return None, ZERO, None
    amount = to_cents(bill.total * policy.discount_percent / Decimal("100"))
    if amount <= 0:
        return None, ZERO, None
    return (
        bill.invoice_date + timedelta(days=policy.discount_days),
        amount,
        policy.discount_account_id,
    )


def approve_bill(
    db: Session, bill_id: int, *, actor: str, integrations: Integrations
) -> SupplierBill:
    """Approve a draft: post the liability to the ledger and open a payment schedule.

    Atomic: the status change, the discount snapshot, the default schedule and
    the journal entry are committed together at the end, or not at all.
    """
    bill = get_bill(db, bill_id)
    if bill.status != BillStatus.DRAFT:
        raise Conflict(f"Bill is {bill.status.value}; only draft bills can be approved")
    if problems := approval_problems(db, bill):
        raise ValidationFailed("Bill cannot be approved: " + "; ".join(problems))
    assert_period_open(db, bill.invoice_date, "Bill approval", integrations)
    discount_date, discount_amount, discount_account_id = _discount_snapshot(bill)

    try:
        entry = integrations.ledger.draft_bill_entry(db, bill)
        transition(
            db,
            SupplierBill,
            bill,
            BillStatus.DRAFT,
            status=BillStatus.APPROVED,
            approved_by=actor,
            approved_at=utcnow(),
            journal_entry_id=entry.id,
            discount_date=discount_date,
            discount_amount=discount_amount,
            discount_account_id=discount_account_id,
        )
        db.add(
            BillPaymentSchedule(
                bill_id=bill.id, installment_no=1, due_date=bill.due_date, amount=bill.total
            )
        )
        db.flush()
        integrations.audit.record(
            db, actor=actor, action="payables.bill.approved", resource=f"supplier_bill:{bill.id}"
        )
        integrations.ledger.post(db, entry)
    except LedgerPostingRefused as exc:
        db.rollback()
        raise ValidationFailed(f"Bill cannot be approved: ledger refused the entry: {exc}") from exc
    db.commit()
    db.refresh(bill)
    return bill


def reject_bill(
    db: Session, bill_id: int, *, actor: str, reason: str, integrations: Integrations
) -> SupplierBill:
    bill = get_bill(db, bill_id)
    if bill.status != BillStatus.DRAFT:
        raise Conflict(f"Bill is {bill.status.value}; only draft bills can be rejected")
    transition(
        db,
        SupplierBill,
        bill,
        BillStatus.DRAFT,
        status=BillStatus.REJECTED,
        rejected_by=actor,
        rejected_at=utcnow(),
        rejection_reason=reason,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.bill.rejected", resource=f"supplier_bill:{bill.id}"
    )
    db.commit()
    db.refresh(bill)
    return bill


def reopen_bill(
    db: Session, bill_id: int, *, actor: str, integrations: Integrations
) -> SupplierBill:
    """Return a rejected bill to draft so it can be corrected and resubmitted."""
    bill = get_bill(db, bill_id)
    if bill.status != BillStatus.REJECTED:
        raise Conflict(f"Bill is {bill.status.value}; only rejected bills can be reopened")
    transition(
        db,
        SupplierBill,
        bill,
        BillStatus.REJECTED,
        status=BillStatus.DRAFT,
        rejected_by=None,
        rejected_at=None,
        rejection_reason=None,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.bill.reopened", resource=f"supplier_bill:{bill.id}"
    )
    db.commit()
    db.refresh(bill)
    return bill


# --- duplicate review ----------------------------------------------------------------------------


def review_duplicate_flag(
    db: Session,
    bill_id: int,
    flag_id: int,
    *,
    decision: DuplicateFlagStatus,
    actor: str,
    note: str | None,
    integrations: Integrations,
) -> SupplierBill:
    bill = get_bill(db, bill_id)
    flag = db.get(DuplicateFlag, flag_id)
    if flag is None or flag.bill_id != bill.id:
        raise NotFound("Duplicate flag not found")
    if flag.status != DuplicateFlagStatus.PENDING:
        raise Conflict(f"Duplicate flag {flag.id} was already reviewed ({flag.status.value})")
    flag.status = decision
    flag.reviewed_by = actor
    flag.reviewed_at = utcnow()
    flag.review_note = note
    integrations.audit.record(
        db, actor=actor, action="payables.bill.flag_reviewed", resource=f"duplicate_flag:{flag.id}"
    )
    db.commit()
    db.refresh(bill)
    return bill


# --- holds and disputes --------------------------------------------------------------------------


def place_hold(
    db: Session,
    bill_id: int,
    *,
    kind: HoldKind,
    reason: str,
    actor: str,
    integrations: Integrations,
) -> SupplierBill:
    """Keep a bill out of payments until the hold / dispute is released."""
    bill = get_bill(db, bill_id)
    if bill.status not in (BillStatus.APPROVED, BillStatus.PARTIALLY_PAID, BillStatus.DRAFT):
        raise Conflict(f"Bill is {bill.status.value}; only open bills can be held")
    if any(h.kind == kind for h in bill.active_holds):
        raise Conflict(f"Bill {bill.id} already has an active {kind.value}")
    hold = BillHold(bill=bill, kind=kind, reason=reason, placed_by=actor)
    db.add(hold)
    db.flush()
    integrations.audit.record(
        db, actor=actor, action="payables.bill.held", resource=f"bill_hold:{hold.id}"
    )
    db.commit()
    db.refresh(bill)
    return bill


def release_hold(
    db: Session,
    bill_id: int,
    hold_id: int,
    *,
    resolution: str,
    actor: str,
    integrations: Integrations,
) -> SupplierBill:
    bill = get_bill(db, bill_id)
    hold = db.get(BillHold, hold_id)
    if hold is None or hold.bill_id != bill.id:
        raise NotFound("Hold not found")
    if hold.released_at is not None:
        raise Conflict(f"Hold {hold.id} was already released")
    hold.released_by = actor
    hold.released_at = utcnow()
    hold.resolution = resolution
    integrations.audit.record(
        db, actor=actor, action="payables.bill.released", resource=f"bill_hold:{hold.id}"
    )
    db.commit()
    db.refresh(bill)
    return bill
