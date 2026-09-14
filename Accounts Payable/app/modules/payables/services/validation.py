"""AP period-end validation and close.

:func:`validate_period` inspects everything that would make an AP period
unsafe to close as of ``period_end`` and returns a structured result with one
check per concern (blocker / warning / info, each with the affected items).
:func:`close_period` records an :class:`APPeriodLock` when no blocker remains;
from then on the application and the database refuse accounting effects dated
on or before the lock, and the GL period port is notified (the GL module itself
is not modified here -- see ``integration.PeriodPort``).
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    OPEN_BILL_STATUSES,
    OPEN_CREDIT_STATUSES,
    APPeriodLock,
    BillHold,
    BillStatus,
    CreditStatus,
    DuplicateFlag,
    DuplicateFlagStatus,
    APCloseException,
    PaymentOperation,
    PaymentRun,
    PaymentRunStatus,
    PaymentStatus,
    SupplierBill,
    SupplierCredit,
    VendorPayment,
)
from app.modules.payables.schemas import PeriodCheck, PeriodValidationResult
from app.modules.payables.services.common import latest_ap_lock
from app.modules.payables.services.reconciliation import reconciliation_report

BLOCKER, WARNING, INFO = "blocker", "warning", "info"


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def validate_period(db: Session, period_end: date, *, gateway=None) -> PeriodValidationResult:
    """Everything that makes the period unsafe to close; ``gateway`` supplies transfer evidence
    for the payment-operation checks (the production gateway port by default)."""
    from app.modules.payables.services import verification

    if gateway is None:
        gateway = Integrations().gateway
    checks: list[PeriodCheck] = []

    def add(code, severity, title, items):
        checks.append(
            PeriodCheck(code=code, severity=severity, title=title, count=len(items), items=items)
        )

    latest = latest_ap_lock(db)
    already_closed = latest is not None and period_end <= latest

    # 1. Unresolved documents dated in or before the period.
    drafts = [
        {
            "type": "bill",
            "id": b.id,
            "vendor_id": b.vendor_id,
            "number": b.invoice_number,
            "date": b.invoice_date.isoformat(),
            "status": b.status.value,
            "total": _money(b.total),
        }
        for b in db.scalars(
            select(SupplierBill).where(
                SupplierBill.status == BillStatus.DRAFT, SupplierBill.invoice_date <= period_end
            )
        )
    ] + [
        {
            "type": "credit",
            "id": c.id,
            "vendor_id": c.vendor_id,
            "number": c.credit_number,
            "date": c.credit_date.isoformat(),
            "status": c.status.value,
            "total": _money(c.total),
        }
        for c in db.scalars(
            select(SupplierCredit).where(
                SupplierCredit.status == CreditStatus.DRAFT,
                SupplierCredit.credit_date <= period_end,
            )
        )
    ]
    add(
        "unapproved_documents",
        BLOCKER,
        "Draft bills / credits dated in the period (approve, re-date or reject them)",
        drafts,
    )
    rejected = [
        {
            "type": "bill",
            "id": b.id,
            "vendor_id": b.vendor_id,
            "number": b.invoice_number,
            "date": b.invoice_date.isoformat(),
            "reason": b.rejection_reason,
        }
        for b in db.scalars(
            select(SupplierBill).where(
                SupplierBill.status == BillStatus.REJECTED, SupplierBill.invoice_date <= period_end
            )
        )
    ]
    add(
        "rejected_documents",
        INFO,
        "Rejected bills dated in the period (no accounting effect)",
        rejected,
    )

    # 2. Duplicate suspicions still open (blocking ones stop the close, advisory ones warn).
    pending_flags = [
        f
        for f in db.scalars(
            select(DuplicateFlag)
            .options(selectinload(DuplicateFlag.bill))
            .where(DuplicateFlag.status == DuplicateFlagStatus.PENDING)
        )
        if f.bill.invoice_date <= period_end
    ]

    def flag_item(f):
        return {
            "flag_id": f.id,
            "bill_id": f.bill_id,
            "suspected_bill_id": f.suspected_bill_id,
            "score": f.score,
            "reasons": f.reasons,
        }

    add(
        "pending_duplicate_flags",
        BLOCKER,
        "Bills with unreviewed blocking duplicate flags",
        [flag_item(f) for f in pending_flags if f.blocks_approval],
    )
    add(
        "advisory_duplicate_flags",
        WARNING,
        "Bills with unreviewed advisory duplicate flags",
        [flag_item(f) for f in pending_flags if not f.blocks_approval],
    )

    # 3. Holds and disputes on open bills.
    holds = [
        {
            "hold_id": h.id,
            "bill_id": h.bill_id,
            "vendor_id": h.bill.vendor_id,
            "kind": h.kind.value,
            "reason": h.reason,
            "placed_by": h.placed_by,
            "outstanding": _money(h.bill.outstanding_as_of(period_end)),
        }
        for h in db.scalars(
            select(BillHold)
            .options(selectinload(BillHold.bill))
            .where(BillHold.released_at.is_(None))
        )
        if h.bill.invoice_date <= period_end and h.bill.status in OPEN_BILL_STATUSES
    ]
    add("held_or_disputed_bills", WARNING, "Open bills on hold or in dispute", holds)

    # 4. Unapplied credit balances.
    credits = []
    for c in db.scalars(
        select(SupplierCredit)
        .options(selectinload(SupplierCredit.applications))
        .where(
            SupplierCredit.status.in_(OPEN_CREDIT_STATUSES),
            SupplierCredit.credit_date <= period_end,
        )
    ):
        remaining = c.remaining_as_of(period_end)
        if remaining > 0:
            credits.append(
                {
                    "credit_id": c.id,
                    "vendor_id": c.vendor_id,
                    "number": c.credit_number,
                    "date": c.credit_date.isoformat(),
                    "remaining": _money(remaining),
                }
            )
    add("unapplied_credits", WARNING, "Approved credits not (fully) applied to bills", credits)

    # 5. Unapplied advance payments.
    advances = []
    for p in db.scalars(
        select(VendorPayment)
        .options(selectinload(VendorPayment.allocations))
        .where(
            VendorPayment.status == PaymentStatus.RECORDED, VendorPayment.payment_date <= period_end
        )
    ):
        unapplied = p.unapplied_as_of(period_end)
        if unapplied > 0:
            advances.append(
                {
                    "payment_id": p.id,
                    "vendor_id": p.vendor_id,
                    "date": p.payment_date.isoformat(),
                    "reference": p.reference,
                    "unapplied": _money(unapplied),
                }
            )
    add("unapplied_advances", WARNING, "Payments with an unapplied (advance) balance", advances)

    # 6. Ledger reconciliation. Document-level defects and unexplained differences block;
    #    manual postings on the control account are explained findings for the GL side.
    recon = reconciliation_report(db, as_of=period_end)
    foreign = [e for e in recon.exceptions if e.cause.startswith("foreign_entry")]
    defects = [
        e.model_dump(mode="json")
        for e in recon.exceptions
        if not e.cause.startswith("foreign_entry")
    ]
    explained = sum((e.difference for e in recon.exceptions), Decimal("0"))
    if recon.total_difference != explained:
        defects.append(
            {
                "cause": "unexplained difference between ledger and subledger",
                "difference": _money(recon.total_difference - explained),
            }
        )
    add(
        "reconciliation_exceptions",
        BLOCKER,
        f"AP subledger vs ledger as of {period_end} (difference {_money(recon.total_difference)})",
        defects,
    )
    add(
        "foreign_ledger_entries",
        WARNING,
        "Posted journal entries on AP control accounts not originated by payables",
        [e.model_dump(mode="json") for e in foreign],
    )

    # 7. Payment runs still in flight for the period.
    runs = [
        {
            "run_id": r.id,
            "reference": r.reference,
            "status": r.status.value,
            "pay_date": r.pay_date.isoformat(),
        }
        for r in db.scalars(
            select(PaymentRun).where(
                PaymentRun.pay_date <= period_end,
                PaymentRun.status.in_(
                    (PaymentRunStatus.APPROVED, PaymentRunStatus.EXECUTING, PaymentRunStatus.FAILED)
                ),
            )
        )
    ]
    add(
        "payment_runs_in_flight",
        BLOCKER,
        "Approved, executing or failed payment runs dated in the period",
        runs,
    )

    # 7b. Payment operations: money that left the bank but is not (verifiably) in the books.
    unrecorded = [
        {
            "operation_uid": op.operation_uid,
            "vendor_id": op.vendor_id,
            "amount": _money(op.amount),
            "payment_date": op.payment_date.isoformat(),
            "state": op.state.value,
            "gateway_confirmation_ref": op.gateway_confirmation_ref,
            "accounting_status": op.accounting_status,
            "run_id": op.run_id,
        }
        for op in verification.unrecorded_transfers(db, as_of=period_end)
    ]
    add(
        "unrecorded_confirmed_transfers",
        BLOCKER,
        "Gateway-confirmed transfers dated in the period that are not recorded in AP / GL",
        unrecorded,
    )
    unverified = [
        {
            "operation_uid": v.operation_uid,
            "vendor_id": v.vendor_id,
            "amount": _money(v.amount),
            "accounting_status": v.accounting_status,
            "missing_evidence": [k for k, ok in v.required_evidence.items() if not ok],
            "findings": [f.code for f in v.blocking_findings],
        }
        for v in verification.unverified_completions(db, gateway=gateway, as_of=period_end)
    ]
    add(
        "unverified_completed_operations",
        BLOCKER,
        "Completed payment operations dated in the period whose completion evidence does not verify",
        unverified,
    )
    investigations = [
        {
            "operation_uid": op.operation_uid,
            "vendor_id": op.vendor_id,
            "amount": _money(op.amount),
            "state": op.state.value,
            "note": op.investigation_note,
        }
        for op in db.scalars(
            select(PaymentOperation).where(
                PaymentOperation.investigation_status == "open",
                PaymentOperation.payment_date <= period_end,
            )
        )
    ]
    add(
        "open_payment_investigations",
        WARNING,
        "Payment operations under open investigation",
        investigations,
    )
    unresolved = [
        {
            "exception_id": e.id,
            "attestation_id": e.attestation_id,
            "period_end": e.period_end.isoformat(),
            "code": e.code,
            "accepted_by": e.accepted_by,
            "reason": e.accept_reason,
        }
        for e in db.scalars(
            select(APCloseException).where(
                APCloseException.status == "accepted", APCloseException.period_end <= period_end
            )
        )
    ]
    add(
        "unresolved_close_exceptions",
        WARNING,
        "Exceptions accepted by earlier forced closes that are still unresolved",
        unresolved,
    )

    # 8. Transactions dated after the cutoff (informational: they belong to the next period).
    later = (
        [
            {
                "type": "bill",
                "id": b.id,
                "date": b.invoice_date.isoformat(),
                "status": b.status.value,
            }
            for b in db.scalars(select(SupplierBill).where(SupplierBill.invoice_date > period_end))
        ]
        + [
            {
                "type": "credit",
                "id": c.id,
                "date": c.credit_date.isoformat(),
                "status": c.status.value,
            }
            for c in db.scalars(
                select(SupplierCredit).where(SupplierCredit.credit_date > period_end)
            )
        ]
        + [
            {
                "type": "payment",
                "id": p.id,
                "date": p.payment_date.isoformat(),
                "status": p.status.value,
            }
            for p in db.scalars(
                select(VendorPayment).where(VendorPayment.payment_date > period_end)
            )
        ]
    )
    add("post_cutoff_transactions", INFO, "Transactions dated after the cutoff", later)

    blockers = [c for c in checks if c.severity == BLOCKER and c.count]
    warnings = [c for c in checks if c.severity == WARNING and c.count]
    status = "blocked" if blockers else ("warnings" if warnings else "ok")
    return PeriodValidationResult(
        period_end=period_end,
        status=status,
        can_close=not blockers and not already_closed,
        checks=checks,
        already_closed=already_closed,
    )


def close_period(
    db: Session,
    *,
    period_end: date,
    actor: str,
    note: str | None,
    integrations: Integrations,
    **kwargs,
):
    """Kept for compatibility: the close itself lives in ``services.closing``."""
    from app.modules.payables.services.closing import close_period as _close

    return _close(
        db, period_end=period_end, actor=actor, note=note, integrations=integrations, **kwargs
    )


def list_period_locks(db: Session) -> list[APPeriodLock]:
    return list(db.scalars(select(APPeriodLock).order_by(APPeriodLock.closed_through)))
