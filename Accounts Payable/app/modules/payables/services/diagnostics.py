"""Payment diagnostics: what happened to a payment operation, from the records.

For each operation the checklist answers: prepared? submitted? gateway confirmed?
AP payment recorded? GL entry posted? allocations committed? reversed? reconciles?
Every answer comes from :mod:`services.verification`, which re-derives the facts from
the gateway's transfer record, the payment, the posted entry and the allocations --
never from the operation's own status flags. Money is compared exactly (no floats,
no tolerances). Sensitive payment destinations are masked.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.payables.models import OperationState, PaymentOperation
from app.modules.payables.services import execution_state as state
from app.modules.payables.services import recovery, verification

NEXT_STEPS = {
    OperationState.PREPARED: "Not submitted yet. The recovery worker resumes it while its run is executing; otherwise re-run the payment run.",
    OperationState.SUBMITTED: "Outcome unknown. Recovery asks the gateway by operation id; no second transfer is sent.",
    OperationState.CONFIRMATION_PENDING: "Gateway has the instruction; recovery polls until accepted, rejected or the attempt budget ends.",
    OperationState.CONFIRMED: "Transfer confirmed; AP recording has not run yet. Recovery records it (safe).",
    OperationState.RECORDING: "Recording in progress or interrupted; recovery re-records once the lease expires (safe).",
    OperationState.COMPLETED: "Nothing to do if the verification holds. Use 'reverse' or 'correct' on the recorded payment for corrections.",
    OperationState.FAILED: "The gateway reported no transfer (rejected or never received). Fix the cause and execute the payment run again (a new operation is created). If the gateway evidence says 'accepted', money did move: use the correction workflow.",
    OperationState.RECONCILIATION_REQUIRED: "Transfer confirmed but recording failed. Recovery retries if the error is retryable; otherwise record the payment through the correction workflow (POST .../corrections) or link an existing payment with complete-with-evidence. A note alone does not complete it.",
}


def mask(reference: str | None) -> str | None:
    if not reference:
        return reference
    return ("*" * max(len(reference) - 4, 0)) + reference[-4:]


@dataclass
class Checklist:
    prepared: bool
    submitted: bool
    gateway_confirmed: bool
    ap_payment_recorded: bool
    gl_entry_posted: bool
    allocations_committed: bool
    reversed: bool
    reconciles: bool
    notes: list[str] = field(default_factory=list)


def _checklist(op: PaymentOperation, v: verification.OperationVerification) -> Checklist:
    payment = v.evidence.get("payment")
    journal = v.evidence.get("journal")
    req = v.required_evidence
    notes = [f"{f.code}: {f.remediation}" for f in v.findings if f.severity != "info"]
    if op.allocation_status == "partial" and op.recovery_note:
        notes.append(op.recovery_note)
    return Checklist(
        prepared=True,
        submitted=op.submitted_at is not None or op.state != OperationState.PREPARED,
        gateway_confirmed=v.delivery_status == "delivered",
        ap_payment_recorded=payment is not None and payment.get("status") == "recorded",
        gl_entry_posted=journal is not None and journal.get("status") == "posted",
        allocations_committed=bool(req.get("allocations_valid"))
        and v.settlement_status == "allocated",
        reversed=payment is not None and payment.get("status") == "reversed",
        reconciles=op.state == OperationState.COMPLETED and v.completion_policy_satisfied,
        notes=notes,
    )


def operation_status(db: Session, operation_uid: str, *, gateway=None) -> dict:
    op = state.get_operation(db, operation_uid)
    decision = recovery.decide(op)
    v = verification.verify_operation(db, op, gateway=gateway)
    checklist = _checklist(op, v)
    return {
        "delivery_status": v.delivery_status,
        "accounting_status": v.accounting_status,
        "settlement_status": v.settlement_status,
        "investigation_status": op.investigation_status,
        "recorded_accounting_status": op.accounting_status,
        "completion_kind": op.completion_kind,
        "completion_policy_satisfied": v.completion_policy_satisfied,
        "verification": v.as_dict(),
        "operation_uid": op.operation_uid,
        "idempotency_key": op.idempotency_key,
        "correlation_id": op.correlation_id,
        "state": op.state.value,
        "run_id": op.run_id,
        "vendor_id": op.vendor_id,
        "amount": op.amount,
        "currency": op.currency,
        "payment_date": op.payment_date,
        "method": op.method.value,
        "destination_reference_masked": mask(op.destination_reference),
        "gateway_status": op.gateway_status,
        "gateway_confirmation_ref": op.gateway_confirmation_ref,
        "payment_id": op.payment_id,
        "checklist": checklist.__dict__,
        "attempts": op.attempts,
        "max_attempts": op.max_attempts,
        "failure_category": op.last_error_category,
        "failure_detail": op.last_error,
        "recovery_status": op.recovery_status,
        "recovery_action": decision.action,
        "recovery_reason": decision.reason,
        "recovery_safe": decision.action
        in ("resume", "reconcile_gateway", "retry_recording", "ignore")
        and op.recovery_status != "manual"
        and not any(f.severity == "critical" for f in v.findings),
        "manual_investigation_required": decision.action == "manual"
        or op.recovery_status == "manual"
        or any(f.severity in ("critical", "error") for f in v.findings),
        "next_step": NEXT_STEPS[op.state],
        "timestamps": {
            "created_at": op.created_at,
            "submitted_at": op.submitted_at,
            "confirmed_at": op.confirmed_at,
            "recorded_at": op.recorded_at,
            "completed_at": op.completed_at,
            "failed_at": op.failed_at,
            "updated_at": op.updated_at,
        },
        "events": [
            {
                "seq": e.seq,
                "at": e.at,
                "from_state": e.from_state,
                "to_state": e.to_state,
                "category": e.category,
                "detail": e.detail,
                "actor": e.actor,
            }
            for e in op.events
        ],
    }


def list_operations(
    db: Session,
    *,
    state_filter: OperationState | None = None,
    vendor_id: int | None = None,
    run_id: int | None = None,
    recovery_status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    stmt = select(PaymentOperation)
    if state_filter is not None:
        stmt = stmt.where(PaymentOperation.state == state_filter)
    if vendor_id is not None:
        stmt = stmt.where(PaymentOperation.vendor_id == vendor_id)
    if run_id is not None:
        stmt = stmt.where(PaymentOperation.run_id == run_id)
    if recovery_status is not None:
        stmt = stmt.where(PaymentOperation.recovery_status == recovery_status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt.order_by(PaymentOperation.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [
            {
                "operation_uid": op.operation_uid,
                "state": op.state.value,
                "run_id": op.run_id,
                "vendor_id": op.vendor_id,
                "amount": op.amount,
                "payment_date": op.payment_date,
                "gateway_status": op.gateway_status,
                "payment_id": op.payment_id,
                "attempts": op.attempts,
                "failure_category": op.last_error_category,
                "recovery_status": op.recovery_status,
                "recovery_action": recovery.decide(op).action,
                "created_at": op.created_at,
                "updated_at": op.updated_at,
            }
            for op in rows
        ],
    }
