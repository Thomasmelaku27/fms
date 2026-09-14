"""HTTP surface of the payables module (``/api/v1/payables``).

Routes are thin: they parse input, call the services and translate
:class:`~app.modules.payables.services.PayablesError` into HTTP status codes.
Database integrity errors that slip past the service checks are handled by the
core ``IntegrityError`` handler (422), except duplicate supplier invoices /
credit numbers, which the services map to 409 themselves.
"""

from collections.abc import Callable
from datetime import date
from functools import wraps
from typing import ParamSpec, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.payables import services
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.models import (
    BillStatus,
    CreditStatus,
    OperationState,
    PaymentRunStatus,
    PaymentStatus,
)
from app.modules.payables.schemas import (
    AdvanceApplyIn,
    AttestationOut,
    CloseExceptionOut,
    CloseExceptionResolveIn,
    CloseVerificationOut,
    DuplicateConfirmationIn,
    InvestigationIn,
    OperationCompleteIn,
    OperationCorrectionIn,
    OperationPageOut,
    OperationResolveIn,
    OperationStatusOut,
    OperationVerificationOut,
    RunVerificationOut,
    ReconstructionOut,
    RecoveryDecisionOut,
    RecoveryResultOut,
    RecoveryRunIn,
    StatementImportIn,
    StatementImportOut,
    StatementLineResolveIn,
    StatementPreviewOut,
    AdvanceUnallocateIn,
    AgingReport,
    ApplyCreditIn,
    ApprovalIn,
    BillIn,
    BillOut,
    BillUpdate,
    CorrectionOut,
    CreditIn,
    CreditOut,
    CreditUpdate,
    DiscountPolicyIn,
    DiscountPolicyOut,
    DiscountPolicyUpdate,
    ExecutionOutcome,
    FlagReviewIn,
    HoldIn,
    HoldReleaseIn,
    OutstandingReport,
    PaymentCorrectionIn,
    PaymentIn,
    PaymentOut,
    PaymentRunCreateIn,
    PaymentRunOut,
    PeriodCloseIn,
    PeriodLockOut,
    PeriodValidationResult,
    ReconciliationReport,
    RejectionIn,
    ReopenIn,
    ReversalIn,
    RunActionIn,
    RunExecutionOut,
    RunItemExcludeIn,
    RunItemUpdateIn,
    ScheduleIn,
    ScheduleOut,
    ScheduleReport,
    UnapplyCreditIn,
    VendorBalance,
    VendorIn,
    VendorOut,
    VendorStatement,
    VendorStatusChangeIn,
    VendorUpdate,
)
from app.modules.payables.services import PayablesError

router = APIRouter(prefix="/payables", tags=["payables"])

P = ParamSpec("P")
R = TypeVar("R")


def http_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Map service-layer errors to HTTP responses (404 / 409 / 422)."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except PayablesError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    return wrapper


Db = Depends(get_db)
Ports = Depends(get_integrations)


# --- discount policies -------------------------------------------------------------------------


@router.post(
    "/discount-policies", response_model=DiscountPolicyOut, status_code=status.HTTP_201_CREATED
)
@http_errors
def add_discount_policy(data: DiscountPolicyIn, db: Session = Db):
    return services.create_discount_policy(db, data)


@router.get("/discount-policies", response_model=list[DiscountPolicyOut])
def discount_policies(db: Session = Db):
    return services.list_discount_policies(db)


@router.patch("/discount-policies/{policy_id}", response_model=DiscountPolicyOut)
@http_errors
def edit_discount_policy(policy_id: int, data: DiscountPolicyUpdate, db: Session = Db):
    return services.update_discount_policy(db, policy_id, data)


# --- vendors --------------------------------------------------------------------------------------


@router.post("/vendors", response_model=VendorOut, status_code=status.HTTP_201_CREATED)
@http_errors
def add_vendor(data: VendorIn, db: Session = Db):
    return services.create_vendor(db, data)


@router.get("/vendors", response_model=list[VendorOut])
def vendors(include_inactive: bool = True, db: Session = Db):
    return services.list_vendors(db, include_inactive=include_inactive)


@router.get("/vendors/{vendor_id}", response_model=VendorOut)
@http_errors
def vendor(vendor_id: int, db: Session = Db):
    return services.get_vendor(db, vendor_id)


@router.patch("/vendors/{vendor_id}", response_model=VendorOut)
@http_errors
def edit_vendor(vendor_id: int, data: VendorUpdate, db: Session = Db):
    return services.update_vendor(db, vendor_id, data)


@router.post("/vendors/{vendor_id}/status", response_model=VendorOut)
@http_errors
def change_vendor_status(
    vendor_id: int, data: VendorStatusChangeIn, db: Session = Db, integrations: Integrations = Ports
):
    """Explicit supplier status change (active / on_hold / suspended / inactive) with a reason."""
    return services.change_vendor_status(
        db,
        vendor_id,
        status=data.status,
        reason=data.reason,
        actor=data.actor,
        integrations=integrations,
    )


@router.get("/vendors/{vendor_id}/balance", response_model=VendorBalance)
@http_errors
def vendor_balance(vendor_id: int, as_of: date | None = None, db: Session = Db):
    """Open bill balances versus the net amount owed (less unapplied credits and advances)."""
    return services.vendor_balance(db, vendor_id, as_of=as_of)


@router.get("/vendor-balances", response_model=list[VendorBalance])
def vendor_balances(as_of: date | None = None, db: Session = Db):
    return services.vendor_balances(db, as_of=as_of)


# --- bills ----------------------------------------------------------------------------------------


@router.post("/bills", response_model=BillOut, status_code=status.HTTP_201_CREATED)
@http_errors
def add_bill(data: BillIn, db: Session = Db):
    """Create a draft bill. Lines are optional on a draft but must sum to ``total``."""
    return services.create_bill(db, data)


@router.get("/bills", response_model=list[BillOut])
def bills(
    vendor_id: int | None = None,
    status: BillStatus | None = None,
    on_hold: bool | None = None,
    db: Session = Db,
):
    return services.list_bills(db, vendor_id=vendor_id, status=status, on_hold=on_hold)


@router.get("/bills/{bill_id}", response_model=BillOut)
@http_errors
def bill(bill_id: int, db: Session = Db):
    return services.get_bill(db, bill_id)


@router.put("/bills/{bill_id}", response_model=BillOut)
@http_errors
def edit_bill(bill_id: int, data: BillUpdate, db: Session = Db):
    """Edit a draft (409 otherwise). ``lines``, when present, replaces all lines."""
    return services.update_bill(db, bill_id, data)


@router.delete("/bills/{bill_id}", status_code=status.HTTP_204_NO_CONTENT)
@http_errors
def remove_bill(bill_id: int, db: Session = Db) -> None:
    services.delete_bill(db, bill_id)


@router.post("/bills/{bill_id}/approve", response_model=BillOut)
@http_errors
def approve_bill(
    bill_id: int, data: ApprovalIn, db: Session = Db, integrations: Integrations = Ports
):
    """Approve a draft: posts Dr expense / Cr payables through the core posting service."""
    return services.approve_bill(db, bill_id, actor=data.actor, integrations=integrations)


@router.post("/bills/{bill_id}/reject", response_model=BillOut)
@http_errors
def reject_bill(
    bill_id: int, data: RejectionIn, db: Session = Db, integrations: Integrations = Ports
):
    return services.reject_bill(
        db, bill_id, actor=data.actor, reason=data.reason, integrations=integrations
    )


@router.post("/bills/{bill_id}/reopen", response_model=BillOut)
@http_errors
def reopen_bill(bill_id: int, data: ReopenIn, db: Session = Db, integrations: Integrations = Ports):
    """Return a rejected bill to draft."""
    return services.reopen_bill(db, bill_id, actor=data.actor, integrations=integrations)


@router.post("/bills/{bill_id}/holds", response_model=BillOut)
@http_errors
def place_hold(bill_id: int, data: HoldIn, db: Session = Db, integrations: Integrations = Ports):
    """Place a payment hold or record a vendor dispute; held bills are not paid."""
    return services.place_hold(
        db, bill_id, kind=data.kind, reason=data.reason, actor=data.actor, integrations=integrations
    )


@router.post("/bills/{bill_id}/holds/{hold_id}/release", response_model=BillOut)
@http_errors
def release_hold(
    bill_id: int,
    hold_id: int,
    data: HoldReleaseIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    return services.release_hold(
        db,
        bill_id,
        hold_id,
        resolution=data.resolution,
        actor=data.actor,
        integrations=integrations,
    )


@router.post("/bills/{bill_id}/duplicate-flags/{flag_id}/review", response_model=BillOut)
@http_errors
def review_duplicate_flag(
    bill_id: int,
    flag_id: int,
    data: FlagReviewIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Clear or confirm a near-duplicate suspicion (bills with pending flags cannot be approved)."""
    return services.review_duplicate_flag(
        db,
        bill_id,
        flag_id,
        decision=data.decision,
        actor=data.actor,
        note=data.note,
        integrations=integrations,
    )


# --- payment schedules ----------------------------------------------------------------------------


@router.get("/bills/{bill_id}/schedule", response_model=ScheduleOut)
@http_errors
def bill_schedule(bill_id: int, db: Session = Db):
    return services.get_schedule(db, bill_id)


@router.put("/bills/{bill_id}/schedule", response_model=ScheduleOut)
@http_errors
def set_bill_schedule(bill_id: int, data: ScheduleIn, db: Session = Db):
    """Replace the installments of an approved bill; they must sum to the bill total."""
    return services.set_schedule(db, bill_id, data.installments)


@router.get("/payment-schedule", response_model=ScheduleReport)
def payment_schedule(
    vendor_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Db,
):
    """Installments still to be paid across all approved bills."""
    return services.schedule_report(db, vendor_id=vendor_id, date_from=date_from, date_to=date_to)


# --- payments -------------------------------------------------------------------------------------


@router.post("/payments", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
@http_errors
def add_payment(
    data: PaymentIn, response: Response, db: Session = Db, integrations: Integrations = Ports
):
    """Record and post a vendor payment; allocations may leave an unapplied (advance) remainder.

    Repeating a request with the same ``idempotency_key`` returns the original
    payment with ``200`` instead of paying again.
    """
    payment, created = services.record_payment(db, data, integrations=integrations)
    if not created:
        response.status_code = status.HTTP_200_OK
    return payment


@router.get("/payments", response_model=list[PaymentOut])
def payments(
    vendor_id: int | None = None,
    status: PaymentStatus | None = None,
    unapplied_only: bool = False,
    db: Session = Db,
):
    return services.list_payments(
        db, vendor_id=vendor_id, status=status, unapplied_only=unapplied_only
    )


@router.get("/payments/{payment_id}", response_model=PaymentOut)
@http_errors
def payment(payment_id: int, db: Session = Db):
    return services.get_payment(db, payment_id)


@router.post("/payments/{payment_id}/reverse", response_model=PaymentOut)
@http_errors
def reverse_payment(
    payment_id: int, data: ReversalIn, db: Session = Db, integrations: Integrations = Ports
):
    """Reverse a recorded payment with a mirror-image journal entry (once; 409 if repeated)."""
    return services.reverse_payment(
        db,
        payment_id,
        actor=data.actor,
        reason=data.reason,
        reversal_date=data.reversal_date,
        integrations=integrations,
    )


@router.post("/payments/{payment_id}/correct", response_model=CorrectionOut)
@http_errors
def correct_payment(
    payment_id: int, data: PaymentCorrectionIn, db: Session = Db, integrations: Integrations = Ports
):
    """Reverse a payment and record its replacement atomically (two posted entries)."""
    reversed_payment, replacement = services.correct_payment(
        db,
        payment_id,
        actor=data.actor,
        reason=data.reason,
        reversal_date=data.reversal_date,
        replacement=data.replacement,
        integrations=integrations,
    )
    return CorrectionOut(
        reversed=PaymentOut.model_validate(reversed_payment),
        replacement=PaymentOut.model_validate(replacement),
    )


@router.post("/payments/{payment_id}/applications", response_model=PaymentOut)
@http_errors
def apply_advance(
    payment_id: int, data: AdvanceApplyIn, db: Session = Db, integrations: Integrations = Ports
):
    """Apply part of the payment's unapplied remainder to an open bill (no ledger entry)."""
    return services.apply_advance(
        db,
        payment_id,
        bill_id=data.bill_id,
        amount=data.amount,
        actor=data.actor,
        allocation_date=data.allocation_date,
        integrations=integrations,
    )


@router.post(
    "/payments/{payment_id}/applications/{allocation_id}/unallocate", response_model=PaymentOut
)
@http_errors
def unallocate_advance(
    payment_id: int,
    allocation_id: int,
    data: AdvanceUnallocateIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Close an advance allocation on a date; the allocation row is kept for history."""
    return services.unallocate_advance(
        db,
        payment_id,
        allocation_id,
        actor=data.actor,
        reason=data.reason,
        unallocation_date=data.unallocation_date,
        integrations=integrations,
    )


# --- supplier credits and adjustments ------------------------------------------------------------


@router.post("/credits", response_model=CreditOut, status_code=status.HTTP_201_CREATED)
@http_errors
def add_credit(data: CreditIn, db: Session = Db):
    """Create a draft supplier credit note (``kind=credit_note``) or adjustment."""
    return services.create_credit(db, data)


@router.get("/credits", response_model=list[CreditOut])
def credits(vendor_id: int | None = None, status: CreditStatus | None = None, db: Session = Db):
    return services.list_credits(db, vendor_id=vendor_id, status=status)


@router.get("/credits/{credit_id}", response_model=CreditOut)
@http_errors
def credit(credit_id: int, db: Session = Db):
    return services.get_credit(db, credit_id)


@router.put("/credits/{credit_id}", response_model=CreditOut)
@http_errors
def edit_credit(credit_id: int, data: CreditUpdate, db: Session = Db):
    return services.update_credit(db, credit_id, data)


@router.delete("/credits/{credit_id}", status_code=status.HTTP_204_NO_CONTENT)
@http_errors
def remove_credit(credit_id: int, db: Session = Db) -> None:
    services.delete_credit(db, credit_id)


@router.post("/credits/{credit_id}/approve", response_model=CreditOut)
@http_errors
def approve_credit(
    credit_id: int, data: ApprovalIn, db: Session = Db, integrations: Integrations = Ports
):
    """Approve a draft credit: posts Dr payables / Cr line accounts."""
    return services.approve_credit(db, credit_id, actor=data.actor, integrations=integrations)


@router.post("/credits/{credit_id}/reject", response_model=CreditOut)
@http_errors
def reject_credit(
    credit_id: int, data: RejectionIn, db: Session = Db, integrations: Integrations = Ports
):
    return services.reject_credit(
        db, credit_id, actor=data.actor, reason=data.reason, integrations=integrations
    )


@router.post("/credits/{credit_id}/reopen", response_model=CreditOut)
@http_errors
def reopen_credit(
    credit_id: int, data: ReopenIn, db: Session = Db, integrations: Integrations = Ports
):
    return services.reopen_credit(db, credit_id, actor=data.actor, integrations=integrations)


@router.post("/credits/{credit_id}/applications", response_model=CreditOut)
@http_errors
def apply_credit(
    credit_id: int, data: ApplyCreditIn, db: Session = Db, integrations: Integrations = Ports
):
    """Apply (part of) an approved credit to an open bill of the same vendor. No ledger entry."""
    return services.apply_credit(
        db,
        credit_id,
        bill_id=data.bill_id,
        amount=data.amount,
        actor=data.actor,
        application_date=data.application_date,
        integrations=integrations,
    )


@router.post("/credits/{credit_id}/applications/{application_id}/unapply", response_model=CreditOut)
@http_errors
def unapply_credit(
    credit_id: int,
    application_id: int,
    data: UnapplyCreditIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Close a credit application on a date (the row is kept; history stays reconstructible)."""
    return services.unapply_credit(
        db,
        credit_id,
        application_id,
        actor=data.actor,
        reason=data.reason,
        unapplication_date=data.unapplication_date,
        integrations=integrations,
    )


@router.post("/credits/{credit_id}/reverse", response_model=CreditOut)
@http_errors
def reverse_credit(
    credit_id: int, data: ReversalIn, db: Session = Db, integrations: Integrations = Ports
):
    """Reverse an approved, unapplied credit with a mirror-image journal entry."""
    return services.reverse_credit(
        db,
        credit_id,
        actor=data.actor,
        reason=data.reason,
        reversal_date=data.reversal_date,
        integrations=integrations,
    )


# --- payment runs ---------------------------------------------------------------------------------


@router.post("/payment-runs", response_model=PaymentRunOut, status_code=status.HTTP_201_CREATED)
@http_errors
def create_payment_run(
    data: PaymentRunCreateIn, db: Session = Db, integrations: Integrations = Ports
):
    """Generate a payment proposal (draft) from approved, unheld bills due by ``due_through``."""
    return services.create_run(db, data, integrations=integrations)


@router.get("/payment-runs", response_model=list[PaymentRunOut])
def payment_runs(status: PaymentRunStatus | None = None, db: Session = Db):
    return services.list_runs(db, status=status)


@router.get("/payment-runs/{run_id}", response_model=PaymentRunOut)
@http_errors
def payment_run(run_id: int, db: Session = Db):
    return services.get_run(db, run_id)


@router.patch("/payment-runs/{run_id}/items/{item_id}", response_model=PaymentRunOut)
@http_errors
def update_run_item(run_id: int, item_id: int, data: RunItemUpdateIn, db: Session = Db):
    """Change the amount this run settles on a bill (draft runs only)."""
    return services.update_item(
        db, run_id, item_id, settle_amount=data.settle_amount, actor=data.actor
    )


@router.post("/payment-runs/{run_id}/items/{item_id}/exclude", response_model=PaymentRunOut)
@http_errors
def exclude_run_item(
    run_id: int,
    item_id: int,
    data: RunItemExcludeIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    return services.exclude_item(
        db, run_id, item_id, reason=data.reason, actor=data.actor, integrations=integrations
    )


@router.post("/payment-runs/{run_id}/review", response_model=PaymentRunOut)
@http_errors
def review_run(
    run_id: int, data: RunActionIn, db: Session = Db, integrations: Integrations = Ports
):
    return services.review_run(db, run_id, actor=data.actor, integrations=integrations)


@router.post("/payment-runs/{run_id}/send-back", response_model=PaymentRunOut)
@http_errors
def send_back_run(
    run_id: int, data: RunActionIn, db: Session = Db, integrations: Integrations = Ports
):
    return services.send_back_run(db, run_id, actor=data.actor, integrations=integrations)


@router.post("/payment-runs/{run_id}/approve", response_model=PaymentRunOut)
@http_errors
def approve_run(
    run_id: int, data: RunActionIn, db: Session = Db, integrations: Integrations = Ports
):
    """Authorise execution (no ledger effect). The approver must differ from the reviewer."""
    return services.approve_run(db, run_id, actor=data.actor, integrations=integrations)


@router.post("/payment-runs/{run_id}/cancel", response_model=PaymentRunOut)
@http_errors
def cancel_run(
    run_id: int, data: RunItemExcludeIn, db: Session = Db, integrations: Integrations = Ports
):
    return services.cancel_run(
        db, run_id, actor=data.actor, reason=data.reason, integrations=integrations
    )


@router.post("/payment-runs/{run_id}/execute", response_model=RunExecutionOut)
@http_errors
def execute_run(
    run_id: int, data: RunActionIn, db: Session = Db, integrations: Integrations = Ports
):
    """Execute an approved run (or retry a failed one): pays vendor by vendor through the
    execution port, records and posts the payments. Items already paid are skipped."""
    run, outcomes = services.execute_run(db, run_id, actor=data.actor, integrations=integrations)
    return RunExecutionOut(
        run=PaymentRunOut.model_validate(run),
        outcomes=[ExecutionOutcome(**o) for o in outcomes],
    )


# --- outstanding, aging, statements, reconciliation ----------------------------------------------


@router.get("/outstanding", response_model=OutstandingReport)
def outstanding(as_of: date | None = None, vendor_id: int | None = None, db: Session = Db):
    return services.outstanding_report(db, as_of=as_of, vendor_id=vendor_id)


@router.get("/aging", response_model=AgingReport)
def aging(as_of: date | None = None, db: Session = Db):
    return services.aging_report(db, as_of=as_of)


@router.get("/vendors/{vendor_id}/statement", response_model=VendorStatement)
@http_errors
def vendor_statement(vendor_id: int, date_from: date, date_to: date, db: Session = Db):
    """Opening balance, bills, credits, payments, discounts, reversals and closing balance."""
    return services.vendor_statement(db, vendor_id, date_from=date_from, date_to=date_to)


@router.get("/reconciliation", response_model=ReconciliationReport)
def reconciliation(as_of: date | None = None, db: Session = Db):
    """AP control accounts: posted ledger balance vs. the payables subledger, with exceptions."""
    return services.reconciliation_report(db, as_of=as_of)


# --- period end -----------------------------------------------------------------------------------


@router.get("/period-end/validate", response_model=PeriodValidationResult)
def validate_period(period_end: date, db: Session = Db, integrations: Integrations = Ports):
    """Structured readiness checks for closing AP as of ``period_end``."""
    return services.validate_period(db, period_end, gateway=integrations.gateway)


@router.get(
    "/period-end/attestations/{attestation_id}/verification", response_model=CloseVerificationOut
)
@http_errors
def verify_close(attestation_id: int, db: Session = Db, integrations: Integrations = Ports):
    """Re-derive a close package from AP rows and posted GL history (hash, fingerprint, liability,
    obligations, accepted exceptions)."""
    return services.closing.verify_close(db, attestation_id, gateway=integrations.gateway).as_dict()


@router.get("/period-end/exceptions", response_model=list[CloseExceptionOut])
def close_exceptions(attestation_id: int | None = None, db: Session = Db):
    return services.closing.list_close_exceptions(db, attestation_id=attestation_id)


@router.post("/period-end/exceptions/{exception_id}/resolve", response_model=CloseExceptionOut)
@http_errors
def resolve_close_exception(
    exception_id: int,
    data: CloseExceptionResolveIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Record (once) how an exception accepted by a forced close was resolved."""
    return services.closing.resolve_close_exception(
        db,
        exception_id,
        actor=data.actor,
        note=data.note,
        reference=data.reference,
        integrations=integrations,
    )


@router.post("/period-end/close", response_model=PeriodLockOut, status_code=status.HTTP_201_CREATED)
@http_errors
def close_period(data: PeriodCloseIn, db: Session = Db, integrations: Integrations = Ports):
    """Close AP through ``period_end`` under the close lock (422 with blockers unless forced by an
    authorised actor). Every attempt leaves an immutable attestation."""
    return services.close_period(
        db,
        period_end=data.period_end,
        actor=data.actor,
        note=data.note,
        integrations=integrations,
        force=data.force,
        force_reason=data.force_reason,
        gl_period_reference=data.gl_period_reference,
    )


@router.get("/period-end/attestations", response_model=list[AttestationOut])
def attestations(period_end: date | None = None, db: Session = Db):
    return services.list_attestations(db, period_end=period_end)


# --- payment operations, recovery, diagnostics -------------------------------------------------------


@router.get("/payment-operations", response_model=OperationPageOut)
def payment_operations(
    state: OperationState | None = None,
    vendor_id: int | None = None,
    run_id: int | None = None,
    recovery_status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Db,
):
    return services.diagnostics.list_operations(
        db,
        state_filter=state,
        vendor_id=vendor_id,
        run_id=run_id,
        recovery_status=recovery_status,
        page=page,
        page_size=page_size,
    )


@router.get("/payment-operations/{operation_uid}", response_model=OperationStatusOut)
@http_errors
def payment_operation(operation_uid: str, db: Session = Db, integrations: Integrations = Ports):
    """What happened to this payment: prepared / submitted / confirmed / recorded / posted / allocated /
    reversed / reconciles -- derived from the records by the verifier -- with failure category,
    recovery safety and next step."""
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.get(
    "/payment-operations/{operation_uid}/verification", response_model=OperationVerificationOut
)
@http_errors
def verify_operation(operation_uid: str, db: Session = Db, integrations: Integrations = Ports):
    """Independent verification: gateway record, events, payment, posted entry, allocations, run."""
    op = services.execution_state.get_operation(db, operation_uid)
    return services.verification.verify_operation(db, op, gateway=integrations.gateway).as_dict()


@router.get("/verification/operations", response_model=list[OperationVerificationOut])
def verify_operations(
    as_of: date | None = None,
    only_failing: bool = True,
    limit: int = 500,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Verify every operation whose money moved (completed included); by default only those with
    blocking findings are returned."""
    results = services.verification.verify_operations(
        db, gateway=integrations.gateway, as_of=as_of, limit=limit
    )
    if only_failing:
        results = [
            r
            for r in results
            if r.blocking_findings or not r.completion_policy_satisfied and r.state == "completed"
        ]
    return [r.as_dict() for r in results]


@router.get("/payment-runs/{run_id}/verification", response_model=RunVerificationOut)
@http_errors
def verify_run(run_id: int, db: Session = Db, integrations: Integrations = Ports):
    """Per-operation verification and the run-level completion decision."""
    run = services.get_run(db, run_id)
    return services.verification.verify_run(db, run, gateway=integrations.gateway).as_dict()


@router.post("/payment-operations/{operation_uid}/resolve", response_model=OperationStatusOut)
@http_errors
def resolve_operation(
    operation_uid: str,
    data: OperationResolveIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Document a manual investigation. ``mark_completed`` additionally requires ``payment_id``
    (the accounting record) and is verified; a resolution note alone never completes anything."""
    services.recovery.resolve_manually(
        db,
        operation_uid,
        actor=data.actor,
        resolution=data.resolution,
        integrations=integrations,
        mark_completed=data.mark_completed,
        payment_id=data.payment_id,
        idempotency_key=data.idempotency_key,
    )
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.post("/payment-operations/{operation_uid}/investigation", response_model=OperationStatusOut)
@http_errors
def investigation_note(
    operation_uid: str, data: InvestigationIn, db: Session = Db, integrations: Integrations = Ports
):
    """Record an investigation note (opens the investigation). No financial effect."""
    services.recovery.note_investigation(
        db, operation_uid, actor=data.actor, note=data.note, integrations=integrations
    )
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.post(
    "/payment-operations/{operation_uid}/investigation/close", response_model=OperationStatusOut
)
@http_errors
def investigation_close(
    operation_uid: str, data: InvestigationIn, db: Session = Db, integrations: Integrations = Ports
):
    """Close the investigation. The accounting status stays what the records say."""
    services.recovery.close_investigation(
        db, operation_uid, actor=data.actor, note=data.note, integrations=integrations
    )
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.post("/payment-operations/{operation_uid}/complete", response_model=OperationStatusOut)
@http_errors
def complete_operation(
    operation_uid: str,
    data: OperationCompleteIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Complete a reconciliation_required operation with an existing, verified vendor payment."""
    services.recovery.complete_with_evidence(
        db,
        operation_uid,
        actor=data.actor,
        payment_id=data.payment_id,
        reason=data.reason,
        idempotency_key=data.idempotency_key,
        integrations=integrations,
    )
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.post("/payment-operations/{operation_uid}/corrections", response_model=OperationStatusOut)
@http_errors
def correct_operation(
    operation_uid: str,
    data: OperationCorrectionIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """Authorised correction: record the AP payment for the confirmed transfer (no new transfer),
    verify it and complete the operation in one transaction."""
    services.recovery.record_correction(
        db,
        operation_uid,
        actor=data.actor,
        payment_date=data.payment_date,
        allocations=data.allocations,
        reason=data.reason,
        idempotency_key=data.idempotency_key,
        integrations=integrations,
    )
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.post("/payment-operations/{operation_uid}/confirmations", response_model=OperationStatusOut)
@http_errors
def gateway_confirmation(
    operation_uid: str,
    data: DuplicateConfirmationIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    """A (possibly duplicate) gateway confirmation callback: logged, never re-recorded."""
    services.recovery.duplicate_confirmation(
        db, operation_uid, reference=data.reference, actor=data.actor
    )
    return services.diagnostics.operation_status(db, operation_uid, gateway=integrations.gateway)


@router.get("/recovery/scan", response_model=list[RecoveryDecisionOut])
def recovery_scan(limit: int = 200, db: Session = Db):
    """Incomplete operations and what the recovery worker would do with each."""
    return services.recovery.scan(db, limit=limit)


@router.post("/recovery/run", response_model=list[RecoveryResultOut])
@http_errors
def recovery_run(data: RecoveryRunIn, db: Session = Db, integrations: Integrations = Ports):
    """Run the recovery worker once (safe with several workers; never re-submits a confirmed transfer)."""
    return services.recovery.process(
        db, integrations=integrations, worker_id=data.worker_id, limit=data.limit, actor=data.actor
    )


# --- historical reconstruction ------------------------------------------------------------------------


@router.get("/reconstruction", response_model=ReconstructionOut)
def reconstruction(as_of: date, view: str = "as_of", db: Session = Db):
    """Independent replay of balances from durable rows, compared with the production reports."""
    if view not in ("as_of", "restated"):
        raise HTTPException(422, "view must be 'as_of' or 'restated'")
    result = services.reconstruction.compare(db, as_of, view=view)
    return ReconstructionOut(
        as_of=result.as_of,
        view=result.view,
        bill_outstanding=result.bill_outstanding,
        vendor_bills=result.vendor_bills,
        vendor_credits=result.vendor_credits,
        vendor_advances=result.vendor_advances,
        control_balance=result.control_balance,
        ledger_balance=result.ledger_balance,
        limitations=result.limitations,
        findings=[{**f.__dict__, "difference": f.difference} for f in result.findings],
        operation_positions=result.operation_positions,
        obligations_total=result.obligations_total,
        statement_imports=result.statement_imports,
    )


# --- supplier statement imports ------------------------------------------------------------------------


def _content(data: StatementImportIn) -> bytes:
    if data.content is not None:
        return data.content.encode("utf-8")
    import base64

    return base64.b64decode(data.content_base64)


@router.post("/vendors/{vendor_id}/statement-imports/preview", response_model=StatementPreviewOut)
@http_errors
def preview_statement(vendor_id: int, data: StatementImportIn, db: Session = Db):
    """Validate and match a supplier statement without persisting anything."""
    return services.supplier_statements.preview(
        db, vendor_id, _content(data), file_name=data.file_name, statement_date=data.statement_date
    )


@router.post(
    "/vendors/{vendor_id}/statement-imports",
    response_model=StatementImportOut,
    status_code=status.HTTP_201_CREATED,
)
@http_errors
def import_statement(
    vendor_id: int, data: StatementImportIn, db: Session = Db, integrations: Integrations = Ports
):
    """Import a supplier statement as review evidence (no ledger entries are posted)."""
    return services.supplier_statements.import_statement(
        db,
        vendor_id,
        _content(data),
        file_name=data.file_name,
        statement_date=data.statement_date,
        actor=data.actor,
        integrations=integrations,
    )


@router.get("/statement-imports", response_model=list[StatementImportOut])
def statement_imports(vendor_id: int | None = None, db: Session = Db):
    return services.supplier_statements.list_imports(db, vendor_id=vendor_id)


@router.get("/statement-imports/{import_id}", response_model=StatementImportOut)
@http_errors
def statement_import(import_id: int, db: Session = Db):
    return services.supplier_statements.get_import(db, import_id)


@router.post(
    "/statement-imports/{import_id}/lines/{line_id}/resolve", response_model=StatementImportOut
)
@http_errors
def resolve_statement_line(
    import_id: int,
    line_id: int,
    data: StatementLineResolveIn,
    db: Session = Db,
    integrations: Integrations = Ports,
):
    return services.supplier_statements.resolve_line(
        db, import_id, line_id, actor=data.actor, note=data.note, integrations=integrations
    )


@router.get("/period-end/locks", response_model=list[PeriodLockOut])
def period_locks(db: Session = Db):
    return services.list_period_locks(db)
