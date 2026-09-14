"""Supplier credit notes and adjustments: drafting, approval, application, reversal.

Credit lifecycle::

    draft ──approve──▶ approved ◀──▶ partially_applied ◀──▶ applied
      │ ▲                 │
      ▼ └──reopen──       └──reverse (only while nothing is applied)──▶ reversed
    rejected

Applying a credit to a bill settles the bill with no ledger effect (both sides
already sit in payables). Unapplying does **not** delete the application: it
stamps ``unapplication_date`` / ``unapplied_by`` / ``unapplication_reason`` so
that reports as of any date between application and unapplication still show
the credit as applied, and reports after it show the bill open again.
Backdating either date is allowed only back to the later of the credit date and
the bill date, never into a closed AP period, and never before the application
itself.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import EntryStatus
from app.modules.payables.integration import Integrations
from app.modules.payables.ledger_adapter import LedgerPostingRefused
from app.modules.payables.models import (
    OPEN_BILL_STATUSES,
    OPEN_CREDIT_STATUSES,
    CreditApplication,
    CreditStatus,
    SupplierBill,
    SupplierCredit,
    SupplierCreditLine,
    sql_normalize,
)
from app.modules.payables.schemas import CreditIn, CreditUpdate
from app.modules.payables.services.common import (
    Conflict,
    DuplicateCredit,
    NotFound,
    ValidationFailed,
    assert_period_open,
    get_credit,
    lock,
    referenced_vendor,
    require_line_accounts,
    require_payable_account,
    require_vendor_documents,
    transition,
    utcnow,
)
from app.modules.payables.services.documents import (
    build_lines,
    is_duplicate_error,
    lines_total,
    validate_amounts,
)
from app.modules.payables.services.settlement import sync_bill_status, sync_credit_status

# --- drafting -------------------------------------------------------------------------------------


def _assert_no_duplicate_credit(
    db: Session, vendor_id: int, credit_number: str, *, exclude_credit_id: int | None = None
) -> None:
    stmt = select(SupplierCredit.id).where(
        SupplierCredit.vendor_id == vendor_id,
        SupplierCredit.credit_number_normalized == sql_normalize(credit_number),
    )
    if exclude_credit_id is not None:
        stmt = stmt.where(SupplierCredit.id != exclude_credit_id)
    if db.scalar(stmt) is not None:
        raise DuplicateCredit(vendor_id, credit_number)


def _flush_credit(db: Session, credit: SupplierCredit) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if is_duplicate_error(exc, "supplier_credits", "credit_number"):
            raise DuplicateCredit(credit.vendor_id, credit.credit_number) from exc
        raise


def _resolve_related_bill(db: Session, vendor_id: int, bill_id: int | None) -> int | None:
    if bill_id is None:
        return None
    bill = db.get(SupplierBill, bill_id)
    if bill is None:
        raise ValidationFailed(f"Related bill {bill_id} does not exist")
    if bill.vendor_id != vendor_id:
        raise ValidationFailed(f"Related bill {bill_id} belongs to another vendor")
    return bill.id


def create_credit(db: Session, data: CreditIn) -> SupplierCredit:
    vendor = referenced_vendor(db, data.vendor_id)
    require_vendor_documents(vendor, "create credits")
    payable_account_id = (
        data.payable_account_id
        if data.payable_account_id is not None
        else vendor.payable_account_id
    )
    if payable_account_id is not None:
        require_payable_account(db, payable_account_id)
    related_bill_id = _resolve_related_bill(db, vendor.id, data.related_bill_id)
    lines = build_lines(db, data.lines, SupplierCreditLine)
    validate_amounts(data.total, lines, "credit")
    _assert_no_duplicate_credit(db, vendor.id, data.credit_number)

    credit = SupplierCredit(
        vendor_id=vendor.id,
        kind=data.kind,
        credit_number=data.credit_number,
        credit_date=data.credit_date,
        total=data.total,
        description=data.description,
        related_bill_id=related_bill_id,
        payable_account_id=payable_account_id,
        lines=lines,
    )
    db.add(credit)
    _flush_credit(db, credit)
    db.commit()
    db.refresh(credit)
    return credit


def update_credit(db: Session, credit_id: int, data: CreditUpdate) -> SupplierCredit:
    credit = get_credit(db, credit_id)
    if credit.status != CreditStatus.DRAFT:
        raise Conflict(f"Credit is {credit.status.value}; only draft credits can be edited")
    fields = data.model_dump(exclude_unset=True)
    vendor = credit.vendor

    credit_number = fields.get("credit_number", credit.credit_number)
    total = fields.get("total", credit.total)
    payable_account_id = credit.payable_account_id
    if "payable_account_id" in fields:
        payable_account_id = fields["payable_account_id"]
        if payable_account_id is None:
            payable_account_id = vendor.payable_account_id
    if payable_account_id is not None and payable_account_id != credit.payable_account_id:
        require_payable_account(db, payable_account_id)
    related_bill_id = credit.related_bill_id
    if "related_bill_id" in fields:
        related_bill_id = _resolve_related_bill(db, vendor.id, fields["related_bill_id"])
    new_lines = build_lines(db, data.lines, SupplierCreditLine) if data.lines is not None else None
    validate_amounts(total, new_lines if new_lines is not None else credit.lines, "credit")
    _assert_no_duplicate_credit(db, vendor.id, credit_number, exclude_credit_id=credit.id)

    credit.credit_number = credit_number
    credit.total = total
    credit.payable_account_id = payable_account_id
    credit.related_bill_id = related_bill_id
    for name in ("kind", "credit_date", "description"):
        if name in fields:
            setattr(credit, name, fields[name])
    if new_lines is not None:
        credit.lines.clear()
        db.flush()
        credit.lines.extend(new_lines)
    _flush_credit(db, credit)
    db.commit()
    db.refresh(credit)
    return credit


def delete_credit(db: Session, credit_id: int) -> None:
    credit = get_credit(db, credit_id)
    if credit.status != CreditStatus.DRAFT:
        raise Conflict(f"Credit is {credit.status.value}; only draft credits can be deleted")
    db.delete(credit)
    db.commit()


def list_credits(
    db: Session, *, vendor_id: int | None = None, status: CreditStatus | None = None
) -> list[SupplierCredit]:
    stmt = (
        select(SupplierCredit)
        .options(selectinload(SupplierCredit.lines), selectinload(SupplierCredit.applications))
        .order_by(SupplierCredit.id)
    )
    if vendor_id is not None:
        stmt = stmt.where(SupplierCredit.vendor_id == vendor_id)
    if status is not None:
        stmt = stmt.where(SupplierCredit.status == status)
    return list(db.scalars(stmt))


# --- approval workflow ----------------------------------------------------------------------------


def credit_approval_problems(db: Session, credit: SupplierCredit) -> list[str]:
    problems: list[str] = []
    if not credit.vendor.accepts_documents:
        problems.append(f"vendor {credit.vendor.code} is {credit.vendor.status.value}")
    if not credit.lines:
        problems.append("credit has no lines")
    elif (computed := lines_total(credit.lines)) != credit.total:
        problems.append(
            f"line totals ({computed:.2f}) do not equal credit total ({credit.total:.2f})"
        )
    if credit.payable_account_id is None:
        problems.append("credit has no payable account (set one on the credit or the vendor)")
    else:
        try:
            require_payable_account(db, credit.payable_account_id)
        except ValidationFailed as exc:
            problems.append(str(exc))
    try:
        require_line_accounts(db, (line.account_id for line in credit.lines))
    except ValidationFailed as exc:
        problems.append(str(exc))
    return problems


def approve_credit(
    db: Session, credit_id: int, *, actor: str, integrations: Integrations
) -> SupplierCredit:
    """Approve a draft credit: posts Dr payables / Cr line accounts."""
    credit = get_credit(db, credit_id)
    if credit.status != CreditStatus.DRAFT:
        raise Conflict(f"Credit is {credit.status.value}; only draft credits can be approved")
    if problems := credit_approval_problems(db, credit):
        raise ValidationFailed("Credit cannot be approved: " + "; ".join(problems))
    assert_period_open(db, credit.credit_date, "Credit approval", integrations)
    try:
        entry = integrations.ledger.draft_credit_entry(db, credit)
        transition(
            db,
            SupplierCredit,
            credit,
            CreditStatus.DRAFT,
            status=CreditStatus.APPROVED,
            approved_by=actor,
            approved_at=utcnow(),
            journal_entry_id=entry.id,
        )
        db.flush()
        integrations.audit.record(
            db,
            actor=actor,
            action="payables.credit.approved",
            resource=f"supplier_credit:{credit.id}",
        )
        integrations.ledger.post(db, entry)
    except LedgerPostingRefused as exc:
        db.rollback()
        raise ValidationFailed(
            f"Credit cannot be approved: ledger refused the entry: {exc}"
        ) from exc
    db.commit()
    db.refresh(credit)
    return credit


def reject_credit(
    db: Session, credit_id: int, *, actor: str, reason: str, integrations: Integrations
) -> SupplierCredit:
    credit = get_credit(db, credit_id)
    if credit.status != CreditStatus.DRAFT:
        raise Conflict(f"Credit is {credit.status.value}; only draft credits can be rejected")
    transition(
        db,
        SupplierCredit,
        credit,
        CreditStatus.DRAFT,
        status=CreditStatus.REJECTED,
        rejected_by=actor,
        rejected_at=utcnow(),
        rejection_reason=reason,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.credit.rejected", resource=f"supplier_credit:{credit.id}"
    )
    db.commit()
    db.refresh(credit)
    return credit


def reopen_credit(
    db: Session, credit_id: int, *, actor: str, integrations: Integrations
) -> SupplierCredit:
    credit = get_credit(db, credit_id)
    if credit.status != CreditStatus.REJECTED:
        raise Conflict(f"Credit is {credit.status.value}; only rejected credits can be reopened")
    transition(
        db,
        SupplierCredit,
        credit,
        CreditStatus.REJECTED,
        status=CreditStatus.DRAFT,
        rejected_by=None,
        rejected_at=None,
        rejection_reason=None,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.credit.reopened", resource=f"supplier_credit:{credit.id}"
    )
    db.commit()
    db.refresh(credit)
    return credit


# --- application and unapplication ----------------------------------------------------------------


def _apply_credit_uncommitted(
    db: Session,
    credit_id: int,
    *,
    bill_id: int,
    amount: Decimal,
    actor: str,
    application_date: date | None,
    integrations: Integrations,
) -> tuple[SupplierCredit, CreditApplication]:
    """Validate and write one credit application (no commit; shared with payment runs)."""
    credit = lock(db, SupplierCredit, [credit_id]).get(credit_id)
    if credit is None:
        raise NotFound("Credit not found")
    bill = lock(db, SupplierBill, [bill_id]).get(bill_id)
    problems = []
    if credit.status == CreditStatus.APPLIED:
        problems.append("credit is fully applied")
    elif credit.status not in OPEN_CREDIT_STATUSES:
        problems.append(f"credit is {credit.status.value} and cannot be applied")
    if bill is None:
        problems.append(f"bill {bill_id} does not exist")
    else:
        if bill.vendor_id != credit.vendor_id:
            problems.append(f"bill {bill.id} belongs to another vendor")
        if bill.status not in OPEN_BILL_STATUSES:
            problems.append(f"bill {bill.id} is {bill.status.value} and cannot receive credits")
        elif amount > bill.outstanding:
            problems.append(
                f"amount {amount:.2f} exceeds the outstanding amount {bill.outstanding:.2f} "
                f"of bill {bill.id}"
            )
    if amount > credit.remaining:
        problems.append(
            f"amount {amount:.2f} exceeds the remaining amount {credit.remaining:.2f} of the credit"
        )
    application_date = application_date or date.today()
    if bill is not None and application_date < max(credit.credit_date, bill.invoice_date):
        problems.append("application date cannot precede the credit or the bill")
    if bill is not None and any(
        a.bill_id == bill.id
        and a.is_active
        and a.amount == amount
        and a.application_date == application_date
        for a in credit.applications
    ):
        # Repeat-request protection: several dated applications per pair are allowed, but an
        # identical one (same bill, amount, date) is treated as a duplicate request.
        problems.append(
            f"credit is already applied to bill {bill.id} with amount {amount:.2f} on "
            f"{application_date.isoformat()}; unapply it first to change it"
        )
    if problems:
        raise ValidationFailed("Credit cannot be applied: " + "; ".join(problems))
    assert_period_open(db, application_date, "Credit application", integrations)

    application = CreditApplication(
        credit=credit,
        bill=bill,
        amount=amount,
        application_date=application_date,
        applied_by=actor,
    )
    db.add(application)
    sync_bill_status(db, bill)
    sync_credit_status(db, credit)
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.credit.applied",
        resource=f"credit_application:{application.id}",
    )
    return credit, application


def apply_credit(
    db: Session,
    credit_id: int,
    *,
    bill_id: int,
    amount: Decimal,
    actor: str,
    application_date: date | None,
    integrations: Integrations,
) -> SupplierCredit:
    """Settle part of a bill with part of a credit, effective on ``application_date``."""
    credit, _ = _apply_credit_uncommitted(
        db,
        credit_id,
        bill_id=bill_id,
        amount=amount,
        actor=actor,
        application_date=application_date,
        integrations=integrations,
    )
    db.commit()
    db.refresh(credit)
    return credit


def unapply_credit(
    db: Session,
    credit_id: int,
    application_id: int,
    *,
    actor: str,
    reason: str | None = None,
    unapplication_date: date | None = None,
    integrations: Integrations,
) -> SupplierCredit:
    """Close a credit application effective on ``unapplication_date`` (never deletes it)."""
    credit = lock(db, SupplierCredit, [credit_id]).get(credit_id)
    if credit is None:
        raise NotFound("Credit not found")
    application = db.get(CreditApplication, application_id)
    if application is None or application.credit_id != credit.id:
        raise NotFound("Credit application not found")
    if application.unapplication_date is not None:
        raise Conflict(
            f"Credit application {application.id} was already unapplied on "
            f"{application.unapplication_date.isoformat()}"
        )
    unapplication_date = unapplication_date or date.today()
    if unapplication_date < application.application_date:
        raise ValidationFailed("Unapplication date cannot precede the application date")
    assert_period_open(db, unapplication_date, "Credit unapplication", integrations)
    bill = lock(db, SupplierBill, [application.bill_id])[application.bill_id]

    application.unapplication_date = unapplication_date
    application.unapplied_by = actor
    application.unapplied_at = utcnow()
    application.unapplication_reason = reason
    sync_bill_status(db, bill)
    sync_credit_status(db, credit)
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.credit.unapplied",
        resource=f"credit_application:{application.id}",
    )
    db.commit()
    db.refresh(credit)
    return credit


def reverse_credit(
    db: Session,
    credit_id: int,
    *,
    actor: str,
    reason: str,
    reversal_date: date | None = None,
    integrations: Integrations,
) -> SupplierCredit:
    """Undo an approved, unapplied credit with a mirror-image journal entry."""
    credit = lock(db, SupplierCredit, [credit_id]).get(credit_id)
    if credit is None:
        raise NotFound("Credit not found")
    if credit.status == CreditStatus.REVERSED:
        raise Conflict(f"Credit {credit.id} is already reversed")
    if credit.status != CreditStatus.APPROVED or credit.amount_applied > 0:
        raise Conflict(
            f"Credit is {credit.status.value}; only approved credits without applications "
            "can be reversed (unapply first)"
        )
    if credit.journal_entry is None or credit.journal_entry.status != EntryStatus.POSTED:
        raise ValidationFailed(f"Credit {credit.id} has no posted journal entry to reverse")
    reversal_date = reversal_date or date.today()
    if reversal_date < credit.credit_date:
        raise ValidationFailed("Reversal date cannot precede the credit date")
    latest_unapplication = max(
        (a.unapplication_date for a in credit.applications if a.unapplication_date), default=None
    )
    if latest_unapplication is not None and reversal_date < latest_unapplication:
        raise ValidationFailed("Reversal date cannot precede the latest unapplication")
    assert_period_open(db, reversal_date, "Credit reversal", integrations)
    try:
        entry = integrations.ledger.draft_reversal_entry(
            db,
            credit.journal_entry,
            entry_date=reversal_date,
            reference=f"AP-CREDIT-REV-{credit.id}",
            description=(
                f"Reversal of supplier credit {credit.credit_number} - "
                f"{credit.vendor.name}: {reason}"
            ),
        )
        transition(
            db,
            SupplierCredit,
            credit,
            CreditStatus.APPROVED,
            status=CreditStatus.REVERSED,
            reversed_by=actor,
            reversed_at=utcnow(),
            reversal_date=reversal_date,
            reversal_reason=reason,
            reversal_journal_entry_id=entry.id,
        )
        db.flush()
        integrations.audit.record(
            db,
            actor=actor,
            action="payables.credit.reversed",
            resource=f"supplier_credit:{credit.id}",
        )
        integrations.ledger.post(db, entry)
    except LedgerPostingRefused as exc:
        db.rollback()
        raise ValidationFailed(
            f"Credit cannot be reversed: ledger refused the entry: {exc}"
        ) from exc
    db.commit()
    db.refresh(credit)
    return credit
