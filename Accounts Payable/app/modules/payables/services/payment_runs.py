"""Payment runs: proposal, review, approval, execution, retry.

Lifecycle::

    draft ──review──▶ reviewed ──approve──▶ approved ──execute──▶ executing ──▶ completed
      ▲                  │                                            │
      └───send back──────┘                                            └──▶ failed ──retry──▶ executing
    (draft / reviewed / approved / failed ──cancel──▶ cancelled)

* **Proposal** (:func:`create_run`) selects open, unheld bills of active vendors
  whose installments fall due on or before ``due_through``, and plans how each
  bill is settled on ``pay_date``: early-payment discount (if still available and
  the bill is settled in full), available credits, available advances, and the
  cash remainder. Every item carries an explanation of the rules that applied.
* **Review / approval** freeze the plan. Approval is *not* a ledger event; the
  approver must differ from the reviewer (four eyes).
* **Versioning.** Every run transition is a compare-and-swap on ``(status,
  version)``. The material content of the proposal is fingerprinted at review and
  approval; before execution the fingerprint and every material business fact are
  re-validated (:func:`revalidate_run`). A material change after approval
  invalidates the approval (``approved -> draft`` with the reason) and the run must
  be reviewed and approved again. Maker-checker: the approver must differ from the
  creator and the reviewer; the executor must not be the approver.
* **Execution** (:func:`execute_run`) is the only step that moves money and it goes
  through durable *payment operations* (``services.execution_state`` /
  ``services.orchestration``): one operation per (run, vendor) with a stable
  idempotency key and an instruction fingerprint. The run is claimed
  (``executing``) and committed; each operation then advances through its own
  durability points (submitted, confirmed, recorded). A gateway rejection marks
  the items ``failed`` with nothing recorded; a lost response is resolved by
  lookup; a recording failure after the gateway confirmed leaves the operation
  ``reconciliation_required`` for the recovery worker -- the transfer is real and
  is never repeated. Retrying the run resumes incomplete operations and skips paid
  items; a *definitively failed* operation is superseded by a new one (new key).
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.models import Account

import hashlib
import json

from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    OPEN_BILL_STATUSES,
    OPEN_CREDIT_STATUSES,
    AllocationKind,
    BillDiscount,
    CreditApplication,
    OperationState,
    PaymentAllocation,
    PaymentOperation,
    PaymentRun,
    PaymentRunItem,
    PaymentRunStatus,
    PaymentStatus,
    RunItemStatus,
    SupplierBill,
    SupplierCredit,
    Vendor,
    VendorPayment,
    VendorStatus,
)
from app.modules.payables.schemas import PaymentRunCreateIn
from app.modules.payables.services import execution_state
from app.modules.payables.services import orchestration
from app.modules.payables.services import settlement
from app.modules.payables.services.common import (
    ZERO,
    Conflict,
    NotFound,
    ValidationFailed,
    assert_period_open,
    bill_query,
    latest_ap_lock,
    lock,
    require_cash_account,
    utcnow,
)

FINAL_STATUSES = (PaymentRunStatus.COMPLETED, PaymentRunStatus.CANCELLED)
EXECUTABLE_STATUSES = (PaymentRunStatus.APPROVED, PaymentRunStatus.FAILED)


def get_run(db: Session, run_id: int) -> PaymentRun:
    run = db.get(PaymentRun, run_id)
    if run is None:
        raise NotFound("Payment run not found")
    return run


def list_runs(db: Session, *, status: PaymentRunStatus | None = None) -> list[PaymentRun]:
    stmt = select(PaymentRun).options(selectinload(PaymentRun.items)).order_by(PaymentRun.id)
    if status is not None:
        stmt = stmt.where(PaymentRun.status == status)
    return list(db.scalars(stmt))


# --- proposal -------------------------------------------------------------------------------------


def _credit_pools(db: Session, pay_date: date, vendor_ids=None) -> dict[int, dict[int, Decimal]]:
    """Remaining value per open credit, grouped by vendor (one query for any number of vendors)."""
    stmt = (
        select(SupplierCredit)
        .options(selectinload(SupplierCredit.applications))
        .where(
            SupplierCredit.status.in_(OPEN_CREDIT_STATUSES), SupplierCredit.credit_date <= pay_date
        )
        .order_by(SupplierCredit.credit_date, SupplierCredit.id)
    )
    if vendor_ids is not None:
        stmt = stmt.where(SupplierCredit.vendor_id.in_(list(vendor_ids)))
    pools: dict[int, dict[int, Decimal]] = defaultdict(dict)
    for credit in db.scalars(stmt):
        if credit.remaining > 0:
            pools[credit.vendor_id][credit.id] = credit.remaining
    return pools


def _advance_pools(db: Session, pay_date: date, vendor_ids=None) -> dict[int, dict[int, Decimal]]:
    """Unapplied remainder per recorded payment, grouped by vendor (one query)."""
    stmt = (
        select(VendorPayment)
        .options(selectinload(VendorPayment.allocations))
        .where(
            VendorPayment.status == PaymentStatus.RECORDED,
            VendorPayment.payment_date <= pay_date,
            VendorPayment.payable_account_id.is_not(None),
        )
        .order_by(VendorPayment.payment_date, VendorPayment.id)
    )
    if vendor_ids is not None:
        stmt = stmt.where(VendorPayment.vendor_id.in_(list(vendor_ids)))
    pools: dict[int, dict[int, Decimal]] = defaultdict(dict)
    for payment in db.scalars(stmt):
        if payment.unapplied_amount > 0:
            pools[payment.vendor_id][payment.id] = payment.unapplied_amount
    return pools


def _available_credits(db: Session, vendor_id: int, pay_date: date) -> dict[int, Decimal]:
    return _credit_pools(db, pay_date, [vendor_id]).get(vendor_id, {})


def _available_advances(db: Session, vendor_id: int, pay_date: date) -> dict[int, Decimal]:
    return _advance_pools(db, pay_date, [vendor_id]).get(vendor_id, {})


def _account_of(db: Session, credit_or_payment_ids, model) -> dict[int, int | None]:
    if not credit_or_payment_ids:
        return {}
    return dict(
        db.execute(
            select(model.id, model.payable_account_id).where(model.id.in_(credit_or_payment_ids))
        ).all()
    )


def _plan_items(db: Session, run: PaymentRun, vendor_ids: list[int] | None) -> list[PaymentRunItem]:
    """Build the proposal items for ``run`` from the current state of the subledger."""
    stmt = (
        bill_query()
        .options(selectinload(SupplierBill.schedule), selectinload(SupplierBill.vendor))
        .join(Vendor, Vendor.id == SupplierBill.vendor_id)
        .where(
            SupplierBill.status.in_(OPEN_BILL_STATUSES),
            SupplierBill.invoice_date <= run.pay_date,
            Vendor.status == VendorStatus.ACTIVE,
        )
        .order_by(SupplierBill.vendor_id, SupplierBill.due_date, SupplierBill.id)
    )
    if vendor_ids:
        stmt = stmt.where(SupplierBill.vendor_id.in_(vendor_ids))

    items: list[PaymentRunItem] = []
    candidates = list(db.scalars(stmt))
    vendor_scope = {b.vendor_id for b in candidates}
    credit_pool = _credit_pools(db, run.pay_date, vendor_scope) if run.use_credits else {}
    advance_pool = _advance_pools(db, run.pay_date, vendor_scope) if run.use_advances else {}
    credit_accounts = _account_of(
        db, [c for pool in credit_pool.values() for c in pool], SupplierCredit
    )
    advance_accounts = _account_of(
        db, [p for pool in advance_pool.values() for p in pool], VendorPayment
    )
    for bill in candidates:
        if bill.on_hold or bill.outstanding <= 0:
            continue
        reasons = []
        due_installments = [
            i
            for i in settlement.installments_with_remaining(bill.schedule, bill.amount_settled)
            if i.amount_remaining > 0 and i.due_date <= run.due_through
        ]
        if not due_installments:
            continue
        settle = sum((i.amount_remaining for i in due_installments), ZERO)
        numbers = ", ".join(str(i.installment_no) for i in due_installments)
        reasons.append(
            f"installment(s) {numbers} due by {run.due_through}: {settle:.2f} of "
            f"{bill.outstanding:.2f} outstanding"
        )
        full_settlement = settle == bill.outstanding
        discount = ZERO
        if run.take_discounts and full_settlement:
            discount = bill.discount_available_on(run.pay_date)
            if discount > 0:
                reasons.append(
                    f"early-payment discount {discount:.2f} available until {bill.discount_date}"
                )
        remaining = settle - discount

        credit_amount = ZERO
        if run.use_credits and remaining > 0:
            if bill.vendor_id not in credit_pool:  # one lookup per vendor, not per bill
                credit_pool[bill.vendor_id] = _available_credits(db, bill.vendor_id, run.pay_date)
                credit_accounts.update(
                    _account_of(db, list(credit_pool[bill.vendor_id]), SupplierCredit)
                )
            pool = credit_pool[bill.vendor_id]
            for credit_id in list(pool):
                if remaining <= 0:
                    break
                if credit_accounts.get(credit_id) != bill.payable_account_id:
                    continue
                take = min(pool[credit_id], remaining)
                if take <= 0:
                    continue
                pool[credit_id] -= take
                remaining -= take
                credit_amount += take
                reasons.append(f"credit #{credit_id}: {take:.2f}")
        advance_amount = ZERO
        if run.use_advances and remaining > 0:
            if bill.vendor_id not in advance_pool:
                advance_pool[bill.vendor_id] = _available_advances(db, bill.vendor_id, run.pay_date)
                advance_accounts.update(
                    _account_of(db, list(advance_pool[bill.vendor_id]), VendorPayment)
                )
            pool = advance_pool[bill.vendor_id]
            for payment_id in list(pool):
                if remaining <= 0:
                    break
                if advance_accounts.get(payment_id) != bill.payable_account_id:
                    continue
                take = min(pool[payment_id], remaining)
                if take <= 0:
                    continue
                pool[payment_id] -= take
                remaining -= take
                advance_amount += take
                reasons.append(f"advance payment #{payment_id}: {take:.2f}")
        if remaining > 0:
            reasons.append(f"cash {remaining:.2f} via {run.method.value}")
        items.append(
            PaymentRunItem(
                vendor_id=bill.vendor_id,
                bill_id=bill.id,
                installment_no=due_installments[-1].installment_no,
                due_date=due_installments[0].due_date,
                outstanding_snapshot=bill.outstanding,
                settle_amount=settle,
                discount_amount=discount,
                credit_amount=credit_amount,
                advance_amount=advance_amount,
                pay_amount=remaining,
                status=RunItemStatus.PROPOSED,
                explanation="; ".join(reasons)[:1000],
                destination_reference=bill.vendor.bank_reference,
            )
        )
    return items


def _next_reference(db: Session, pay_date: date) -> str:
    count = db.scalar(select(PaymentRun.id).order_by(PaymentRun.id.desc())) or 0
    return f"RUN-{pay_date.isoformat()}-{count + 1:04d}"


def create_run(db: Session, data: PaymentRunCreateIn, *, integrations: Integrations) -> PaymentRun:
    """Generate a payment proposal (status ``draft``) from the current AP state."""
    require_cash_account(db, data.cash_account_id)
    due_through = data.due_through or data.pay_date
    assert_period_open(db, data.pay_date, "Payment run", integrations)
    reference = data.reference or _next_reference(db, data.pay_date)
    if db.scalar(select(PaymentRun.id).where(PaymentRun.reference == reference)):
        raise Conflict(f"Payment run reference {reference!r} exists")
    run = PaymentRun(
        reference=reference,
        description=data.description,
        status=PaymentRunStatus.DRAFT,
        pay_date=data.pay_date,
        due_through=due_through,
        vendor_filter=",".join(str(v) for v in data.vendor_ids) if data.vendor_ids else None,
        take_discounts=data.take_discounts,
        use_credits=data.use_credits,
        use_advances=data.use_advances,
        cash_account_id=data.cash_account_id,
        method=data.method,
        created_by=data.actor,
    )
    db.add(run)
    db.flush()
    run.items.extend(_plan_items(db, run, data.vendor_ids))
    db.flush()
    integrations.audit.record(
        db, actor=data.actor, action="payables.run.created", resource=f"payment_run:{run.id}"
    )
    db.commit()
    db.refresh(run)
    return run


# --- versioning and revalidation --------------------------------------------------------------------


def _cas(db: Session, run: PaymentRun, from_statuses, **values) -> None:
    """Compare-and-swap on (status, version); a lost race is a 409."""
    if not isinstance(from_statuses, (tuple, list)):
        from_statuses = (from_statuses,)
    expected = run.version
    values = {**values, "version": expected + 1}
    result = db.execute(
        update(PaymentRun)
        .where(
            PaymentRun.id == run.id,
            PaymentRun.status.in_(from_statuses),
            PaymentRun.version == expected,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        raise Conflict(
            f"Payment run {run.id} changed concurrently (expected {'/'.join(s.value for s in from_statuses)} v{expected})"
        )
    for name, value in values.items():
        setattr(run, name, value)


def proposal_fingerprint(run: PaymentRun) -> str:
    """Material content of the proposal: what is paid, to whom, how, from where, when."""
    items = [
        {
            "bill_id": i.bill_id,
            "vendor_id": i.vendor_id,
            "settle": f"{i.settle_amount:.2f}",
            "discount": f"{i.discount_amount:.2f}",
            "credit": f"{i.credit_amount:.2f}",
            "advance": f"{i.advance_amount:.2f}",
            "pay": f"{i.pay_amount:.2f}",
            "destination": i.destination_reference or "",
        }
        for i in sorted(run.items, key=lambda i: i.bill_id)
        if i.status != RunItemStatus.EXCLUDED
    ]
    payload = {
        "pay_date": run.pay_date.isoformat(),
        "cash_account_id": run.cash_account_id,
        "method": run.method.value,
        "items": items,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def revalidate_run(db: Session, run: PaymentRun) -> list[str]:
    """Every material business fact the approval relied on, re-checked against the books now."""
    problems: list[str] = []
    if run.approved_fingerprint and proposal_fingerprint(run) != run.approved_fingerprint:
        problems.append("proposal content changed since approval")
    lock_date = latest_ap_lock(db)
    if lock_date is not None and run.pay_date <= lock_date:
        problems.append(f"pay date {run.pay_date} falls in a closed AP period")
    cash = db.get(Account, run.cash_account_id)
    if cash is None or not cash.is_active:
        problems.append("cash account is inactive")
    pending_items = [
        i for i in run.items if i.status in (RunItemStatus.PROPOSED, RunItemStatus.FAILED)
    ]
    bills = (
        {
            b.id: b
            for b in db.scalars(
                bill_query()
                .options(selectinload(SupplierBill.vendor))
                .where(SupplierBill.id.in_([i.bill_id for i in pending_items]))
            )
        }
        if pending_items
        else {}
    )
    vendor_scope = {i.vendor_id for i in pending_items}
    credit_totals = (
        {
            v: sum(pool.values(), ZERO)
            for v, pool in _credit_pools(db, run.pay_date, vendor_scope).items()
        }
        if pending_items
        else {}
    )
    advance_totals = (
        {
            v: sum(pool.values(), ZERO)
            for v, pool in _advance_pools(db, run.pay_date, vendor_scope).items()
        }
        if pending_items
        else {}
    )
    credit_pool: dict[int, Decimal] = {}
    advance_pool: dict[int, Decimal] = {}
    for item in pending_items:
        bill = bills.get(item.bill_id)
        if bill is None:
            problems.append(f"bill {item.bill_id} no longer exists")
            continue
        vendor = bill.vendor
        if not vendor.accepts_payments:
            problems.append(f"vendor {vendor.code} is {vendor.status.value}")
        if bill.status not in OPEN_BILL_STATUSES:
            problems.append(f"bill {bill.invoice_number} is {bill.status.value}")
        if bill.on_hold:
            holds = ", ".join(f"{h.kind.value}: {h.reason}" for h in bill.active_holds)
            problems.append(f"bill {bill.invoice_number} is on hold ({holds})")
        if item.settle_amount > bill.outstanding:
            problems.append(
                f"bill {bill.invoice_number}: planned {item.settle_amount:.2f} exceeds outstanding {bill.outstanding:.2f}"
            )
        if item.discount_amount > bill.discount_available_on(run.pay_date):
            problems.append(
                f"bill {bill.invoice_number}: discount {item.discount_amount:.2f} no longer available"
            )
        if (vendor.bank_reference or "") != (item.destination_reference or ""):
            problems.append(
                f"vendor {vendor.code}: payment instructions changed since the proposal"
            )
        if item.credit_amount > 0:
            if vendor.id not in credit_pool:
                credit_pool[vendor.id] = credit_totals.get(vendor.id, ZERO)
            credit_pool[vendor.id] -= item.credit_amount
            if credit_pool[vendor.id] < 0:
                problems.append(f"vendor {vendor.code}: planned credits are no longer available")
        if item.advance_amount > 0:
            if vendor.id not in advance_pool:
                advance_pool[vendor.id] = advance_totals.get(vendor.id, ZERO)
            advance_pool[vendor.id] -= item.advance_amount
            if advance_pool[vendor.id] < 0:
                problems.append(f"vendor {vendor.code}: planned advances are no longer available")
    return problems


# --- review and approval ----------------------------------------------------------------------------


def _active_items(run: PaymentRun) -> list[PaymentRunItem]:
    return [i for i in run.items if i.status != RunItemStatus.EXCLUDED]


def update_item(
    db: Session, run_id: int, item_id: int, *, settle_amount: Decimal, actor: str
) -> PaymentRun:
    """Reviewer changes how much of the bill this run settles (controlled partial settlement)."""
    run = get_run(db, run_id)
    if run.status != PaymentRunStatus.DRAFT:
        raise Conflict(f"Run is {run.status.value}; items can only change while draft")
    item = db.get(PaymentRunItem, item_id)
    if item is None or item.run_id != run.id:
        raise NotFound("Payment run item not found")
    if item.status == RunItemStatus.EXCLUDED:
        raise Conflict("Item is excluded")
    if settle_amount > item.outstanding_snapshot:
        raise ValidationFailed(
            f"settle_amount {settle_amount:.2f} exceeds the outstanding {item.outstanding_snapshot:.2f}"
        )
    bill = item.bill
    full = settle_amount == item.outstanding_snapshot
    discount = bill.discount_available_on(run.pay_date) if (run.take_discounts and full) else ZERO
    remaining = settle_amount - discount
    credit = min(item.credit_amount, remaining)
    remaining -= credit
    advance = min(item.advance_amount, remaining)
    remaining -= advance
    item.settle_amount = settle_amount
    item.discount_amount = discount
    item.credit_amount = credit
    item.advance_amount = advance
    item.pay_amount = remaining
    item.explanation = (
        f"adjusted by {actor}: settle {settle_amount:.2f}; discount {discount:.2f}; "
        f"credits {credit:.2f}; advances {advance:.2f}; cash {remaining:.2f}"
    )
    _cas(db, run, PaymentRunStatus.DRAFT)  # bumps the version: concurrent reviewers notice
    db.commit()
    db.refresh(run)
    return run


def exclude_item(
    db: Session, run_id: int, item_id: int, *, reason: str, actor: str, integrations: Integrations
) -> PaymentRun:
    run = get_run(db, run_id)
    if run.status != PaymentRunStatus.DRAFT:
        raise Conflict(f"Run is {run.status.value}; items can only change while draft")
    item = db.get(PaymentRunItem, item_id)
    if item is None or item.run_id != run.id:
        raise NotFound("Payment run item not found")
    if item.status == RunItemStatus.EXCLUDED:
        raise Conflict("Item is already excluded")
    item.status = RunItemStatus.EXCLUDED
    item.excluded_by = actor
    item.exclusion_reason = reason
    _cas(db, run, PaymentRunStatus.DRAFT)
    integrations.audit.record(
        db, actor=actor, action="payables.run.item_excluded", resource=f"payment_run_item:{item.id}"
    )
    db.commit()
    db.refresh(run)
    return run


def review_run(db: Session, run_id: int, *, actor: str, integrations: Integrations) -> PaymentRun:
    run = get_run(db, run_id)
    if run.status != PaymentRunStatus.DRAFT:
        raise Conflict(f"Run is {run.status.value}; only draft runs can be reviewed")
    if not _active_items(run):
        raise ValidationFailed("Run has no items to pay")
    if problems := revalidate_run(db, run):
        raise ValidationFailed("Run cannot be reviewed: " + "; ".join(problems))
    _cas(
        db,
        run,
        PaymentRunStatus.DRAFT,
        status=PaymentRunStatus.REVIEWED,
        reviewed_by=actor,
        reviewed_at=utcnow(),
        proposal_fingerprint=proposal_fingerprint(run),
        approval_invalidated_reason=None,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.run.reviewed", resource=f"payment_run:{run.id}"
    )
    db.commit()
    db.refresh(run)
    return run


def send_back_run(
    db: Session, run_id: int, *, actor: str, integrations: Integrations
) -> PaymentRun:
    run = get_run(db, run_id)
    if run.status != PaymentRunStatus.REVIEWED:
        raise Conflict(f"Run is {run.status.value}; only reviewed runs can be sent back")
    _cas(
        db,
        run,
        PaymentRunStatus.REVIEWED,
        status=PaymentRunStatus.DRAFT,
        reviewed_by=None,
        reviewed_at=None,
        proposal_fingerprint=None,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.run.sent_back", resource=f"payment_run:{run.id}"
    )
    db.commit()
    db.refresh(run)
    return run


def approve_run(db: Session, run_id: int, *, actor: str, integrations: Integrations) -> PaymentRun:
    """Authorise execution. Does not touch the ledger.

    Maker-checker: the approver must be neither the creator nor the reviewer. The
    proposal must still match what the reviewer saw and must still be valid.
    """
    run = get_run(db, run_id)
    if run.status != PaymentRunStatus.REVIEWED:
        raise Conflict(f"Run is {run.status.value}; only reviewed runs can be approved")
    if actor in (run.reviewed_by, run.created_by):
        raise ValidationFailed(
            "The approver must be a different person than the creator and the reviewer"
        )
    if not integrations.authorization.authorize(
        actor, "payables.run.approve", f"payment_run:{run.id}"
    ):
        raise ValidationFailed(f"Actor {actor!r} is not authorised to approve payment runs")
    current = proposal_fingerprint(run)
    if run.proposal_fingerprint and current != run.proposal_fingerprint:
        raise Conflict("The proposal changed since it was reviewed; review it again")
    if problems := revalidate_run(db, run):
        raise ValidationFailed("Run cannot be approved: " + "; ".join(problems))
    _cas(
        db,
        run,
        PaymentRunStatus.REVIEWED,
        status=PaymentRunStatus.APPROVED,
        approved_by=actor,
        approved_at=utcnow(),
        approved_fingerprint=current,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.run.approved", resource=f"payment_run:{run.id}"
    )
    db.commit()
    db.refresh(run)
    return run


def cancel_run(
    db: Session, run_id: int, *, actor: str, reason: str, integrations: Integrations
) -> PaymentRun:
    run = get_run(db, run_id)
    cancellable = (
        PaymentRunStatus.DRAFT,
        PaymentRunStatus.REVIEWED,
        PaymentRunStatus.APPROVED,
        PaymentRunStatus.FAILED,
    )
    if run.status not in cancellable:
        raise Conflict(f"Run is {run.status.value} and cannot be cancelled")
    _cas(
        db,
        run,
        cancellable,
        status=PaymentRunStatus.CANCELLED,
        cancelled_by=actor,
        cancelled_at=utcnow(),
        cancel_reason=reason,
    )
    integrations.audit.record(
        db, actor=actor, action="payables.run.cancelled", resource=f"payment_run:{run.id}"
    )
    db.commit()
    db.refresh(run)
    return run


def invalidate_approval(
    db: Session, run: PaymentRun, *, reason: str, actor: str, integrations: Integrations
) -> PaymentRun:
    """A material change after approval: back to draft, reason recorded, approval void."""
    _cas(
        db,
        run,
        PaymentRunStatus.APPROVED,
        status=PaymentRunStatus.DRAFT,
        approved_by=None,
        approved_at=None,
        approved_fingerprint=None,
        reviewed_by=None,
        reviewed_at=None,
        proposal_fingerprint=None,
        approval_invalidated_reason=reason[:1000],
    )
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.run.approval_invalidated",
        resource=f"payment_run:{run.id}",
    )
    db.commit()
    db.refresh(run)
    return run


# --- execution ---------------------------------------------------------------------------------------


def _operation_for_group(
    db: Session, run: PaymentRun, vendor: Vendor, items: list[PaymentRunItem], *, actor: str
):
    """Find the live operation for (run, vendor) or prepare a new one (committed)."""
    plan = [
        {
            "bill_id": i.bill_id,
            "settle": i.settle_amount,
            "discount": i.discount_amount,
            "credit": i.credit_amount,
            "advance": i.advance_amount,
            "pay": i.pay_amount,
        }
        for i in items
    ]
    total_cash = sum((i.pay_amount for i in items), ZERO)
    existing = list(
        db.scalars(
            select(PaymentOperation)
            .where(PaymentOperation.run_id == run.id, PaymentOperation.vendor_id == vendor.id)
            .order_by(PaymentOperation.id)
        )
    )
    live = [o for o in existing if o.state != OperationState.FAILED]
    if live:
        op = live[-1]
        if op.state == OperationState.COMPLETED:
            return op, False
        # Resume: same key, same instructions (the fingerprint check happens in prepare()).
        execution_state.prepare(
            db,
            idempotency_key=op.idempotency_key,
            vendor_id=vendor.id,
            destination_reference=op.destination_reference,
            amount=op.amount,
            currency=op.currency,
            method=op.method,
            payment_date=op.payment_date,
            cash_account_id=op.cash_account_id,
            run_id=run.id,
            run_version=op.run_version,
            plan=plan,
            actor=actor,
        )
        return op, False
    attempt_no = len(existing) + 1
    key = f"run:{run.id}:vendor:{vendor.id}:approval:{run.approved_fingerprint[:12] if run.approved_fingerprint else 'na'}:attempt:{attempt_no}"
    op, created = execution_state.prepare(
        db,
        idempotency_key=key,
        vendor_id=vendor.id,
        destination_reference=vendor.bank_reference,
        amount=total_cash,
        currency="XXX",
        method=run.method,
        payment_date=run.pay_date,
        cash_account_id=run.cash_account_id,
        run_id=run.id,
        run_version=run.version,
        plan=plan,
        actor=actor,
        correlation_id=f"run-{run.id}-{run.reference}",
    )
    db.commit()
    return op, created


def _mark_items(
    db: Session,
    items: list[PaymentRunItem],
    *,
    status: RunItemStatus,
    payment_id=None,
    reference=None,
    reason: str | None = None,
) -> None:
    for item in items:
        row = db.get(PaymentRunItem, item.id)
        row.status = status
        row.payment_id = payment_id
        row.execution_reference = reference
        row.failure_reason = reason[:500] if reason else None
    db.commit()


def execute_run(
    db: Session, run_id: int, *, actor: str, integrations: Integrations
) -> tuple[PaymentRun, list[dict]]:
    """Execute an approved run (or retry a failed one) through durable payment operations."""
    run = lock(db, PaymentRun, [run_id]).get(run_id)
    if run is None:
        raise NotFound("Payment run not found")
    if run.status not in EXECUTABLE_STATUSES:
        raise Conflict(f"Run is {run.status.value}; only approved or failed runs can be executed")
    if actor == run.approved_by:
        raise ValidationFailed("The executor must be a different person than the approver")
    if not integrations.authorization.authorize(
        actor, "payables.run.execute", f"payment_run:{run.id}"
    ):
        raise ValidationFailed(f"Actor {actor!r} is not authorised to execute payment runs")
    assert_period_open(db, run.pay_date, "Payment run execution", integrations)
    cash_account = require_cash_account(db, run.cash_account_id)
    if run.status == PaymentRunStatus.APPROVED and (problems := revalidate_run(db, run)):
        # Material change since approval: approval is void; nothing is paid.
        invalidate_approval(
            db, run, reason="; ".join(problems), actor=actor, integrations=integrations
        )
        raise Conflict(
            "Approval invalidated by material changes since approval: " + "; ".join(problems)
        )
    _cas(
        db,
        run,
        EXECUTABLE_STATUSES,
        status=PaymentRunStatus.EXECUTING,
        executed_by=actor,
        execution_started_at=utcnow(),
        attempts=run.attempts + 1,
    )
    db.commit()  # the claim is visible to everyone before any money moves

    pending: dict[int, list[PaymentRunItem]] = defaultdict(list)
    outcomes: list[dict] = []
    for item in run.items:
        if item.status in (RunItemStatus.PROPOSED, RunItemStatus.FAILED):
            pending[item.vendor_id].append(item)
        elif item.status == RunItemStatus.PAID:
            outcomes.append(
                {
                    "vendor_id": item.vendor_id,
                    "vendor_code": item.vendor.code,
                    "amount": item.pay_amount,
                    "success": True,
                    "payment_id": item.payment_id,
                    "reference": item.execution_reference,
                    "error": None,
                    "skipped": True,
                    "operation_uid": None,
                    "state": "completed",
                }
            )
    for vendor_id, items in pending.items():
        vendor = db.get(Vendor, vendor_id)
        outcomes.append(
            _execute_group(
                db,
                run,
                vendor,
                items,
                actor=actor,
                integrations=integrations,
                cash_account=cash_account,
            )
        )

    db.refresh(run)
    finalize_run(db, run, gateway=integrations.gateway, actor=actor)
    integrations.audit.record(
        db, actor=actor, action="payables.run.executed", resource=f"payment_run:{run.id}"
    )
    db.commit()
    db.refresh(run)
    return run, outcomes


# --- finalisation ------------------------------------------------------------------------------------

FINAL_COMPLETED = "completed"
FINAL_INCOMPLETE_ACCOUNTING = (
    "incomplete_accounting"  # confirmed transfers not (verifiably) recorded
)
FINAL_AWAITING_GATEWAY = "awaiting_gateway"  # unknown outcomes / pending confirmations
FINAL_PARTIALLY_REJECTED = "partially_rejected"  # at least one definitive rejection, rest fine
FINAL_MANUAL_REVIEW = "manual_review"
FINAL_NOT_STARTED = "not_started"


def finalize_run(db: Session, run: PaymentRun, *, gateway, actor: str) -> PaymentRun:
    """Decide ``completed`` / ``failed`` for an executing run from verified evidence (no commit).

    Completion policy: every non-excluded item is either settled without cash or covered
    by a *completed* operation whose completion evidence verifies
    (:func:`verification.verify_run`). Anything else leaves the run ``failed`` with a
    ``finalization_status`` saying why (unrecorded confirmed transfers, unknown gateway
    outcomes, rejections, manual review, not started). Items get their actual
    settlement amounts next to the approved plan; differences carry a note.
    """
    from app.modules.payables.services import verification

    result = verification.verify_run(db, run, gateway=gateway)
    _record_actual_settlement(db, run, result)
    categories = result.categories
    if result.can_complete:
        final = FINAL_COMPLETED
    elif categories.get(verification.CATEGORY_MANUAL):
        final = FINAL_MANUAL_REVIEW
    elif categories.get(verification.CATEGORY_CONFIRMED_UNRECORDED):
        final = FINAL_INCOMPLETE_ACCOUNTING
    elif categories.get(verification.CATEGORY_UNKNOWN):
        final = FINAL_AWAITING_GATEWAY
    elif categories.get(verification.CATEGORY_REJECTED):
        final = FINAL_PARTIALLY_REJECTED
    else:
        final = FINAL_NOT_STARTED
    summary = {
        "categories": {k: len(v) for k, v in categories.items()},
        "approved_total_cash": f"{result.approved_total_cash:.2f}",
        "actual_total_cash": f"{result.actual_total_cash:.2f}",
        "blocking_findings": [
            {"code": f.code, "operation_uid": f.operation_uid, "severity": f.severity}
            for f in result.findings
            if f.severity in ("critical", "error")
        ][:50],
        "decided_by": actor,
    }
    _cas(
        db,
        run,
        PaymentRunStatus.EXECUTING,
        status=PaymentRunStatus.COMPLETED if result.can_complete else PaymentRunStatus.FAILED,
        completed_at=utcnow() if result.can_complete else None,
        finalization_status=final,
        settlement_summary_json=json.dumps(summary, sort_keys=True)[:4000],
        finalized_at=utcnow(),
    )
    return run


def _record_actual_settlement(db: Session, run: PaymentRun, result) -> None:
    """Per item: what was actually settled (allocation, discount, credit, advance) vs the plan."""
    from app.modules.payables.services import verification

    ops: dict[int, PaymentOperation] = {}
    for op in db.scalars(
        select(PaymentOperation)
        .where(PaymentOperation.run_id == run.id)
        .order_by(PaymentOperation.id)
    ):
        current = ops.get(op.vendor_id)
        if (
            current is None
            or op.state != OperationState.FAILED
            or current.state == OperationState.FAILED
        ):
            ops[op.vendor_id] = op
    verified = {v.operation_uid: v for v in result.operations}
    # Batch loads: what was actually settled for the run's paid items (4 queries, not 4 per item).
    paid_items = [i for i in run.items if i.status == RunItemStatus.PAID]
    payment_ids = {i.payment_id for i in paid_items if i.payment_id is not None}
    bill_ids = {i.bill_id for i in paid_items}
    original_by_key: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    discount_by_key: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    credit_by_bill: dict[int, Decimal] = defaultdict(lambda: ZERO)
    advance_by_bill: dict[int, Decimal] = defaultdict(lambda: ZERO)
    if payment_ids:
        for a in db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.payment_id.in_(payment_ids),
                PaymentAllocation.kind == AllocationKind.ORIGINAL,
                PaymentAllocation.unallocation_date.is_(None),
            )
        ):
            original_by_key[(a.payment_id, a.bill_id)] += a.amount
        for d in db.scalars(
            select(BillDiscount).where(
                BillDiscount.payment_id.in_(payment_ids), BillDiscount.reversal_date.is_(None)
            )
        ):
            discount_by_key[(d.payment_id, d.bill_id)] += d.amount
    if bill_ids:
        for a in db.scalars(
            select(CreditApplication).where(
                CreditApplication.bill_id.in_(bill_ids),
                CreditApplication.application_date == run.pay_date,
                CreditApplication.unapplication_date.is_(None),
            )
        ):
            credit_by_bill[a.bill_id] += a.amount
        for a in db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.bill_id.in_(bill_ids),
                PaymentAllocation.kind == AllocationKind.ADVANCE,
                PaymentAllocation.allocation_date == run.pay_date,
                PaymentAllocation.unallocation_date.is_(None),
            )
        ):
            advance_by_bill[a.bill_id] += a.amount
    for item in run.items:
        if item.status == RunItemStatus.EXCLUDED:
            continue
        op = ops.get(item.vendor_id)
        v = verified.get(op.operation_uid) if op else None
        if item.pay_amount <= 0:
            # settled without cash: credits / advances applied on the pay date
            item.settlement_status = (
                verification.CATEGORY_RECORDED_NO_CASH
                if item.status == RunItemStatus.PAID
                else verification.CATEGORY_NOT_STARTED
            )
        elif op is None:
            item.settlement_status = verification.CATEGORY_NOT_STARTED
        else:
            item.settlement_status = verification.classify(v, op)
        if item.status != RunItemStatus.PAID:
            continue
        actual_pay = original_by_key[(item.payment_id, item.bill_id)] if item.payment_id else ZERO
        actual_discount = discount_by_key[(item.payment_id, item.bill_id)] if item.payment_id else ZERO
        actual_credit = credit_by_bill[item.bill_id]
        actual_advance = advance_by_bill[item.bill_id]
        item.actual_pay_amount = actual_pay
        item.actual_discount_amount = actual_discount
        item.actual_credit_amount = actual_credit
        item.actual_advance_amount = actual_advance
        differences = [
            f"{name} planned {planned:.2f} actual {actual:.2f}"
            for name, planned, actual in (
                ("cash", item.pay_amount, actual_pay),
                ("discount", item.discount_amount, actual_discount),
                ("credit", item.credit_amount, actual_credit),
                ("advance", item.advance_amount, actual_advance),
            )
            if planned != actual
        ]
        if differences:
            reason = (
                op.recovery_note
                if op and op.recovery_note
                else "settled differently at recording time"
            )
            item.settlement_note = ("; ".join(differences) + f" -- {reason}")[:500]
            if item.settlement_status == verification.CATEGORY_RECORDED:
                item.settlement_status = verification.CATEGORY_PARTIAL
        else:
            item.settlement_note = None
    db.flush()


def _execute_group(
    db: Session,
    run: PaymentRun,
    vendor: Vendor,
    items: list[PaymentRunItem],
    *,
    actor: str,
    integrations: Integrations,
    cash_account,
) -> dict:
    total_cash = sum((i.pay_amount for i in items), ZERO)
    outcome = {
        "vendor_id": vendor.id,
        "vendor_code": vendor.code,
        "amount": total_cash,
        "success": False,
        "payment_id": None,
        "reference": None,
        "error": None,
        "skipped": False,
        "operation_uid": None,
        "state": None,
    }
    try:
        if total_cash <= 0:
            # Settled entirely by credits / advances: no transfer, no operation; book it directly.
            _settle_without_cash(db, run, items, actor=actor, integrations=integrations)
            _mark_items(db, items, status=RunItemStatus.PAID)
            outcome.update(success=True, state="completed")
            return outcome
        op, _ = _operation_for_group(db, run, vendor, items, actor=actor)
    except execution_state.InstructionConflict as exc:
        db.rollback()
        _mark_items(db, items, status=RunItemStatus.FAILED, reason=f"instruction conflict: {exc}")
        outcome["error"] = str(exc)
        return outcome
    except Exception as exc:  # noqa: BLE001 - a group failure must not abort other vendors
        db.rollback()
        _mark_items(db, items, status=RunItemStatus.FAILED, reason=str(exc).splitlines()[0][:500])
        outcome["error"] = str(exc).splitlines()[0][:500]
        return outcome

    worker = f"exec:{run.id}:{actor}"
    if not execution_state.lease(db, op, worker_id=worker, seconds=600):
        db.rollback()
        _mark_items(
            db,
            items,
            status=RunItemStatus.PROPOSED,
            reason="operation is being processed by another worker",
        )
        outcome.update(
            operation_uid=op.operation_uid,
            state=op.state.value,
            error="operation leased by another worker",
        )
        return outcome
    db.commit()
    try:
        result = orchestration.execute(db, op, integrations=integrations, actor=actor)
    except BaseException:
        db.rollback()  # never let the lease release commit a half-done step
        raise
    finally:
        fresh = execution_state.get_operation(db, op.operation_uid)
        execution_state.release_lease(db, fresh)
        db.commit()
    outcome.update(
        operation_uid=result.operation_uid,
        state=result.state.value,
        reference=result.reference,
        payment_id=result.payment_id,
    )
    if result.success:
        _mark_items(
            db,
            items,
            status=RunItemStatus.PAID,
            payment_id=result.payment_id,
            reference=result.reference,
        )
        outcome["success"] = True
    elif result.money_moved:
        # Transfer confirmed, recording incomplete: keep the items proposed (not failed: money is out),
        # the recovery worker completes them.
        _mark_items(
            db,
            items,
            status=RunItemStatus.PROPOSED,
            reason=f"transfer confirmed; recording pending recovery ({result.category}: {result.error})",
        )
        outcome["error"] = f"transfer confirmed; recording pending recovery ({result.category})"
    elif result.state == OperationState.FAILED:
        _mark_items(db, items, status=RunItemStatus.FAILED, reason=result.error or result.category)
        outcome["error"] = result.error or result.category
    else:  # submitted with unknown outcome / pending confirmation
        _mark_items(
            db,
            items,
            status=RunItemStatus.PROPOSED,
            reason=f"awaiting gateway outcome ({result.state.value})",
        )
        outcome["error"] = f"awaiting gateway outcome ({result.state.value})"
    return outcome


def _settle_without_cash(
    db: Session,
    run: PaymentRun,
    items: list[PaymentRunItem],
    *,
    actor: str,
    integrations: Integrations,
) -> None:
    from app.modules.payables.services import credits as credit_services

    for item in items:
        bill = lock(db, SupplierBill, [item.bill_id])[item.bill_id]
        for kind, wanted in (("credit", item.credit_amount), ("advance", item.advance_amount)):
            if wanted <= 0:
                continue
            pool = (
                _available_credits(db, bill.vendor_id, run.pay_date)
                if kind == "credit"
                else _available_advances(db, bill.vendor_id, run.pay_date)
            )
            accounts = _account_of(
                db, list(pool), SupplierCredit if kind == "credit" else VendorPayment
            )
            for source_id, available in pool.items():
                if wanted <= 0:
                    break
                if accounts.get(source_id) != bill.payable_account_id or available <= 0:
                    continue
                take = min(available, wanted)
                if kind == "credit":
                    credit_services._apply_credit_uncommitted(
                        db,
                        source_id,
                        bill_id=bill.id,
                        amount=take,
                        actor=actor,
                        application_date=run.pay_date,
                        integrations=integrations,
                    )
                else:
                    settlement._apply_advance_uncommitted(
                        db,
                        source_id,
                        bill_id=bill.id,
                        amount=take,
                        actor=actor,
                        allocation_date=run.pay_date,
                        integrations=integrations,
                    )
                wanted -= take
            if wanted > 0:
                raise ValidationFailed(
                    f"bill {bill.id}: planned {kind} of {wanted:.2f} is no longer available"
                )
    db.commit()
