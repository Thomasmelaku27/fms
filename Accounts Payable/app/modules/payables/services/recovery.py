"""AP-owned recovery worker for incomplete payment operations.

The worker never initiates a second transfer because a process crashed. For each
incomplete operation it decides one of:

=======================  ====================================================================
``resume``               ``prepared`` intent of a run that is still executing / failed and
                         younger than ``abandon_after``: submit it (first and only submission)
``reconcile_gateway``    ``submitted`` with unknown outcome or ``confirmation_pending``: ask
                         the gateway by token; accepted -> record, rejected -> failed, never
                         received -> resubmit with the same token, pending -> wait
``retry_recording``      ``confirmed`` / stale ``recording`` / retryable
                         ``reconciliation_required``: write the books again (money moved)
``manual``               attempts exhausted, contradictory gateway answers, non-retryable
                         recording errors, or a prepared intent whose run is gone
``ignore``               terminal operations; duplicate confirmations are only logged
=======================  ====================================================================

Safety: operations are leased with a compare-and-swap (``worker_id``,
``lease_expires_at``) so several workers can run at once; every step is a
compare-and-swap transition; attempt budgets bound retries; errors are
categorised. Before re-recording, the verifier is consulted: an existing payment
for the transfer or missing gateway evidence escalates to ``manual`` instead of
writing the books again. Transaction boundaries are those of ``orchestration``.

Manual workflow (Turn 5)
------------------------
* :func:`note_investigation` / :func:`close_investigation` document and close an
  investigation. They never change the financial state: a closed investigation on an
  unrecorded transfer leaves ``accounting_status = 'unrecorded'`` and the operation in
  ``reconciliation_required``.
* :func:`complete_with_evidence` completes a ``reconciliation_required`` operation on
  the strength of a recorded vendor payment that :mod:`services.verification` accepts
  (gateway record, events, payment, posted entry, allocations, run). The database
  trigger re-checks the core. Idempotent by key; same key + different request = 409.
* :func:`record_correction` is the authorised correction workflow: it records the AP
  payment for the confirmed transfer (vendor, amount, cash account and reference from
  the operation; date and allocations from the operator) and completes with evidence
  in one transaction. It never contacts the gateway.
* :func:`resolve_manually` is the compatibility entry point: ``mark_completed`` without
  ``payment_id`` is refused (422) -- a resolution note is not evidence.
"""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    MONEY_MOVED_STATES,
    OperationState,
    PaymentOperation,
    PaymentRun,
    PaymentRunItem,
    PaymentRunStatus,
    RunItemStatus,
    VendorPayment,
)
from app.modules.payables.services import execution_state as state
from app.modules.payables.services import orchestration, verification
from app.modules.payables.services.common import Conflict, ValidationFailed, utcnow

ZERO = Decimal("0.00")


@dataclass
class RecoveryDecision:
    operation_uid: str
    state: str
    action: str  # resume | reconcile_gateway | retry_recording | manual | ignore
    reason: str
    attempts: int
    max_attempts: int
    run_id: int | None
    vendor_id: int


@dataclass
class RecoveryResult:
    operation_uid: str
    action: str
    before: str
    after: str
    note: str


def decide(
    op: PaymentOperation, *, now=None, abandon_after: timedelta = timedelta(days=1)
) -> RecoveryDecision:
    now = now or utcnow()
    base = dict(
        operation_uid=op.operation_uid,
        state=op.state.value,
        attempts=op.attempts,
        max_attempts=op.max_attempts,
        run_id=op.run_id,
        vendor_id=op.vendor_id,
    )
    if op.state in (OperationState.COMPLETED, OperationState.FAILED):
        return RecoveryDecision(action="ignore", reason="terminal", **base)
    if op.recovery_status == "manual":
        return RecoveryDecision(
            action="manual", reason=op.last_error or "flagged for manual investigation", **base
        )
    if op.state == OperationState.PREPARED:
        run_ok = op.run is not None and op.run.status in (
            PaymentRunStatus.EXECUTING,
            PaymentRunStatus.FAILED,
        )
        created = op.created_at.replace(tzinfo=None) if op.created_at else None
        too_old = created is not None and (now.replace(tzinfo=None) - created) > abandon_after
        if run_ok and not too_old:
            return RecoveryDecision(
                action="resume", reason="intent never submitted; run still executing", **base
            )
        return RecoveryDecision(
            action="manual",
            reason="intent never submitted and its run is no longer executing",
            **base,
        )
    if op.state == OperationState.SUBMITTED:
        return RecoveryDecision(
            action="reconcile_gateway", reason="outcome unknown; look it up by operation id", **base
        )
    if op.state == OperationState.CONFIRMATION_PENDING:
        if op.attempts >= op.max_attempts:
            return RecoveryDecision(
                action="manual", reason="gateway still pending after the attempt budget", **base
            )
        return RecoveryDecision(
            action="reconcile_gateway", reason="awaiting gateway confirmation", **base
        )
    if op.state in (OperationState.CONFIRMED, OperationState.RECORDING):
        lease_live = op.lease_expires_at is not None and op.lease_expires_at.replace(
            tzinfo=None
        ) > now.replace(tzinfo=None)
        if op.state == OperationState.RECORDING and lease_live:
            return RecoveryDecision(
                action="ignore", reason="another worker is recording it", **base
            )
        return RecoveryDecision(
            action="retry_recording", reason="transfer confirmed; AP recording incomplete", **base
        )
    if op.state == OperationState.RECONCILIATION_REQUIRED:
        if (
            op.attempts >= op.max_attempts
            or op.last_error_category not in orchestration.RETRYABLE_RECORDING_CATEGORIES
        ):
            return RecoveryDecision(
                action="manual", reason=f"{op.last_error_category}: {op.last_error}", **base
            )
        return RecoveryDecision(
            action="retry_recording",
            reason=f"retryable recording failure ({op.last_error_category})",
            **base,
        )
    return RecoveryDecision(action="manual", reason="unrecognised state", **base)


def scan(db, *, limit: int = 200) -> list[RecoveryDecision]:
    return [decide(op) for op in state.incomplete_operations(db, limit=limit)]


def process(
    db,
    *,
    integrations: Integrations,
    worker_id: str,
    limit: int = 50,
    lease_seconds: int = 300,
    actor: str | None = None,
) -> list[RecoveryResult]:
    """Lease and act on incomplete operations. Safe to run from several workers."""
    actor = actor or worker_id
    results: list[RecoveryResult] = []
    for op in state.incomplete_operations(db, limit=limit):
        decision = decide(op)
        if decision.action in ("ignore", "manual"):
            if decision.action == "manual" and op.recovery_status != "manual":
                db.execute(
                    state.update(PaymentOperation)
                    .where(PaymentOperation.id == op.id)
                    .values(recovery_status="manual", recovery_note=decision.reason[:1000])
                    .execution_options(synchronize_session=False)
                )
                state.record_event(
                    db,
                    op,
                    category="recovery",
                    detail=f"needs manual investigation: {decision.reason}",
                    actor=actor,
                )
                db.commit()
            results.append(
                RecoveryResult(
                    op.operation_uid,
                    decision.action,
                    op.state.value,
                    op.state.value,
                    decision.reason,
                )
            )
            continue
        before_state = op.state.value
        if not state.lease(db, op, worker_id=worker_id, seconds=lease_seconds):
            db.rollback()
            results.append(
                RecoveryResult(
                    op.operation_uid,
                    "skipped",
                    before_state,
                    state.get_operation(db, op.operation_uid).state.value,
                    "leased by another worker or changed concurrently",
                )
            )
            continue
        db.commit()
        before = op.state.value
        integrations.faults.check("recovery.before_action")
        try:
            state.record_event(
                db,
                op,
                category="recovery",
                detail=f"{decision.action}: {decision.reason}",
                actor=actor,
            )
            db.commit()
            if decision.action == "resume":
                outcome = orchestration.execute(db, op, integrations=integrations, actor=actor)
            elif decision.action == "reconcile_gateway":
                if op.state == OperationState.SUBMITTED:
                    op = orchestration.resolve_unknown(
                        db, op, integrations=integrations, actor=actor
                    )
                else:
                    op = orchestration.lookup_pending(
                        db, op, integrations=integrations, actor=actor
                    )
                if op.state == OperationState.CONFIRMED:
                    op = orchestration.record(db, op, integrations=integrations, actor=actor)
                outcome = orchestration.ExecutionOutcome(op)
            else:  # retry_recording
                # Never write the books twice: if a payment for this transfer already exists
                # (a previous recording committed but the transition did not, or a manual
                # correction is under way), recording again would duplicate it.
                duplicate = [
                    f
                    for f in verification.verify_operation(
                        db, op, gateway=integrations.gateway
                    ).findings
                    if f.code
                    in (
                        "duplicate_payment_for_operation",
                        "duplicate_payment_for_transfer",
                        "gateway_not_accepted",
                        "gateway_evidence_missing",
                    )
                ]
                if duplicate:
                    raise _Escalate("; ".join(f"{f.code}: {f.remediation}" for f in duplicate))
                if op.state == OperationState.RECORDING:
                    # Stale lease from a crashed worker: its transaction rolled back with it.
                    state.transition(
                        db,
                        op,
                        OperationState.RECORDING,
                        OperationState.RECONCILIATION_REQUIRED,
                        actor=actor,
                        category="recovery",
                        detail="recording interrupted by a crash; retrying",
                        last_error_category=orchestration.CATEGORY_UNEXPECTED,
                        last_error="worker crashed while recording",
                    )
                    db.commit()
                op = orchestration.record(db, op, integrations=integrations, actor=actor)
                outcome = orchestration.ExecutionOutcome(op)
            _sync_run_items(db, op, gateway=integrations.gateway)
            note = outcome.error or "completed"
        except state.OperationConflict as exc:
            db.rollback()
            note = f"lost a race: {exc}"
        except _Escalate as exc:
            db.rollback()
            fresh = state.get_operation(db, op.operation_uid)
            db.execute(
                state.update(PaymentOperation)
                .where(PaymentOperation.id == fresh.id)
                .values(recovery_status="manual", recovery_note=str(exc)[:1000])
                .execution_options(synchronize_session=False)
            )
            state.record_event(
                db,
                fresh,
                category="recovery",
                detail=f"recording refused by verification: {exc}",
                actor=actor,
            )
            db.commit()
            note = f"escalated to manual: {exc}"
        finally:
            fresh = state.get_operation(db, op.operation_uid)
            state.release_lease(db, fresh)
            db.commit()
        results.append(
            RecoveryResult(fresh.operation_uid, decision.action, before, fresh.state.value, note)
        )
    results.extend(resync_stale_runs(db, actor=actor, gateway=integrations.gateway))
    return results


def resync_stale_runs(db, *, actor: str, gateway=None) -> list[RecoveryResult]:
    """Completed operations whose run items still say proposed / failed (executor died after the
    recording commit): bring the items and the run's finalisation in line. No money involved."""
    results: list[RecoveryResult] = []
    stale = (
        db.execute(
            select(PaymentOperation)
            .join(
                PaymentRunItem,
                (PaymentRunItem.run_id == PaymentOperation.run_id)
                & (PaymentRunItem.vendor_id == PaymentOperation.vendor_id),
            )
            .join(PaymentRun, PaymentRun.id == PaymentOperation.run_id)
            .where(
                PaymentOperation.state == OperationState.COMPLETED,
                PaymentRunItem.status.in_((RunItemStatus.PROPOSED, RunItemStatus.FAILED)),
                PaymentRun.status == PaymentRunStatus.FAILED,
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    for op in stale:
        try:
            _sync_run_items(db, op, gateway=gateway)
            results.append(
                RecoveryResult(
                    op.operation_uid, "resync_run", "completed", "completed", "run items re-synced"
                )
            )
        except state.OperationConflict as exc:
            db.rollback()
            results.append(
                RecoveryResult(
                    op.operation_uid, "resync_run", "completed", "completed", f"lost a race: {exc}"
                )
            )
        except Exception as exc:  # noqa: BLE001 - one stale run must not stop the others
            db.rollback()
            results.append(
                RecoveryResult(
                    op.operation_uid, "resync_run", "completed", "completed", f"failed: {exc}"
                )
            )
    return results


class _Escalate(Exception):
    """Recovery must stop and hand the operation to a person."""


def _sync_run_items(db, op: PaymentOperation, *, gateway=None) -> None:
    """Reflect a recovered operation on its run items, then re-finalise the run.

    Items may only change while the run is draft or executing (database rule), so a
    ``failed`` run is taken back to ``executing`` for the update; the finalisation
    policy (``payment_runs.finalize_run``) then decides completed / failed again.
    """
    if op.run_id is None:
        return
    from app.modules.payables.services.payment_runs import _cas, finalize_run

    run = db.get(PaymentRun, op.run_id)
    if run is None or run.status not in (PaymentRunStatus.FAILED, PaymentRunStatus.EXECUTING):
        return
    if (
        run.status == PaymentRunStatus.EXECUTING
        and op.worker_id
        and op.worker_id.startswith("exec:")
    ):
        return  # the executor that owns this run will mark the items itself
    reopened = False
    if run.status == PaymentRunStatus.FAILED:
        _cas(db, run, PaymentRunStatus.FAILED, status=PaymentRunStatus.EXECUTING)
        reopened = True
    items = list(
        db.scalars(
            select(PaymentRunItem).where(
                PaymentRunItem.run_id == op.run_id,
                PaymentRunItem.vendor_id == op.vendor_id,
                PaymentRunItem.status != RunItemStatus.EXCLUDED,
            )
        )
    )
    if op.state == OperationState.COMPLETED:
        for item in items:
            item.status = RunItemStatus.PAID
            item.payment_id = op.payment_id
            item.execution_reference = op.gateway_confirmation_ref
            item.failure_reason = None
    elif op.state == OperationState.FAILED:
        for item in items:
            if item.status != RunItemStatus.PAID:
                item.status = RunItemStatus.FAILED
                item.failure_reason = (op.last_error or op.last_error_category or "failed")[:500]
    db.flush()
    if reopened:
        db.refresh(run)
        finalize_run(db, run, gateway=gateway, actor=op.worker_id or "recovery")
    db.commit()


# --- manual workflow: investigation (no financial effect) vs completion with evidence ---------------


def _authorise(integrations: Integrations, actor: str, action: str, op: PaymentOperation) -> None:
    if not integrations.authorization.authorize(
        actor, action, f"payment_operation:{op.operation_uid}"
    ):
        raise ValidationFailed(
            f"Actor {actor!r} is not authorised to {action.split('.')[-1]} payment operations"
        )


def note_investigation(
    db, operation_uid: str, *, actor: str, note: str, integrations: Integrations
) -> PaymentOperation:
    """Record what an investigator found. Opens the investigation; changes nothing financial."""
    op = state.get_operation(db, operation_uid, for_update=True)
    _authorise(integrations, actor, "payables.operation.resolve", op)
    db.execute(
        state.update(PaymentOperation)
        .where(PaymentOperation.id == op.id)
        .values(
            recovery_note=note[:1000],
            investigation_status="open" if op.investigation_status != "closed" else "closed",
            investigation_note=note[:1000],
        )
        .execution_options(synchronize_session=False)
    )
    state.record_event(db, op, category="investigation", detail=f"note: {note}", actor=actor)
    db.commit()
    db.refresh(op)
    return op


def close_investigation(
    db, operation_uid: str, *, actor: str, note: str, integrations: Integrations
) -> PaymentOperation:
    """Close the investigation. The accounting status is untouched: a note is not evidence."""
    op = state.get_operation(db, operation_uid, for_update=True)
    _authorise(integrations, actor, "payables.operation.resolve", op)
    if op.investigation_status == "closed":
        raise Conflict("The investigation is already closed")
    db.execute(
        state.update(PaymentOperation)
        .where(PaymentOperation.id == op.id)
        .values(
            investigation_status="closed",
            investigation_note=note[:1000],
            investigation_closed_by=actor,
            investigation_closed_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    detail = f"investigation closed: {note}"
    if op.state != OperationState.COMPLETED and op.state in MONEY_MOVED_STATES:
        detail += f" (accounting status remains {op.accounting_status}: the transfer is still not in the books)"
    state.record_event(db, op, category="investigation", detail=detail, actor=actor)
    db.commit()
    db.refresh(op)
    return op


def resolve_manually(
    db,
    operation_uid: str,
    *,
    actor: str,
    resolution: str,
    integrations: Integrations,
    mark_completed: bool = False,
    payment_id: int | None = None,
    idempotency_key: str | None = None,
) -> PaymentOperation:
    """Compatibility entry point (``POST .../resolve``).

    Without ``mark_completed`` this is :func:`note_investigation`. With it, completion
    requires ``payment_id`` -- the accounting record -- and goes through
    :func:`complete_with_evidence`; a resolution text alone is refused.
    """
    if not mark_completed:
        return note_investigation(
            db, operation_uid, actor=actor, note=resolution, integrations=integrations
        )
    if payment_id is None:
        raise ValidationFailed(
            "A resolution note is not evidence: completing an operation requires the id of the "
            "recorded vendor payment (payment_id) whose posted entry moved the cash, or the "
            "correction workflow (POST .../corrections)"
        )
    return complete_with_evidence(
        db,
        operation_uid,
        actor=actor,
        payment_id=payment_id,
        reason=resolution,
        idempotency_key=idempotency_key,
        integrations=integrations,
    )


def _completion_request_fingerprint(payment_id: int, reason: str) -> str:
    return f"complete:payment={payment_id}:reason={reason.strip()}"


def complete_with_evidence(
    db,
    operation_uid: str,
    *,
    actor: str,
    payment_id: int,
    reason: str,
    integrations: Integrations,
    idempotency_key: str | None = None,
    kind: str = verification.KIND_MANUAL_CORRECTION,
    commit: bool = True,
) -> PaymentOperation:
    """Complete a ``reconciliation_required`` operation on the strength of an accounting record.

    The candidate payment is verified by :mod:`services.verification` against the
    gateway's transfer record, the operation, the posted ledger entry, the allocations
    and the run *before* the transition; the database trigger re-checks the core of it.
    Repeating the request with the same ``idempotency_key`` and content returns the
    completed operation; the same key with different content is a conflict.
    """
    op = state.get_operation(db, operation_uid, for_update=True)
    _authorise(integrations, actor, "payables.operation.resolve", op)
    fingerprint = _completion_request_fingerprint(payment_id, reason)
    if op.state == OperationState.COMPLETED:
        if idempotency_key and op.completion_idempotency_key == idempotency_key:
            if op.payment_id != payment_id:
                raise Conflict(
                    f"completion key {idempotency_key!r} was already used with payment {op.payment_id}"
                )
            return op  # identical retry: the original result
        raise Conflict(
            f"operation {op.operation_uid} is already completed (payment {op.payment_id})"
        )
    if op.state != OperationState.RECONCILIATION_REQUIRED:
        raise ValidationFailed(
            f"only operations in reconciliation_required can be completed manually (state: {op.state.value})"
        )
    if (
        idempotency_key
        and op.completion_idempotency_key
        and op.completion_idempotency_key != idempotency_key
    ):
        raise Conflict(
            "another completion request with a different key is recorded for this operation"
        )
    if (
        idempotency_key
        and op.completion_idempotency_key == idempotency_key
        and (op.completion_evidence_json or "").find(fingerprint) < 0
    ):
        raise Conflict(
            f"completion key {idempotency_key!r} was already used for a different request"
        )
    result = verification.verify_operation(
        db, op, gateway=integrations.gateway, candidate_payment_id=payment_id, completion_kind=kind
    )
    if not result.completion_policy_satisfied:
        missing = [k for k, v in result.required_evidence.items() if not v]
        problems = [f"{f.code}: {f.remediation}" for f in result.blocking_findings]
        raise ValidationFailed(
            "Completion refused: the records do not support it. Missing evidence: "
            + (", ".join(missing) or "none")
            + ". Findings: "
            + ("; ".join(problems) or "none")
        )
    payment = db.get(VendorPayment, payment_id)
    allocated = sum((a.amount for a in payment.allocations if a.is_active), ZERO)
    planned = sum((Decimal(i["pay"]) for i in state.plan_of(op)), ZERO)
    evidence = orchestration.completion_evidence(
        payment_id=payment.id,
        journal_entry_id=payment.journal_entry_id,
        gateway_confirmation_ref=op.gateway_confirmation_ref,
        amount=op.amount,
        allocated=allocated,
        kind=kind,
        actor=actor,
        reason=fingerprint,
        verified=result.required_evidence,
    )
    # Link first (the trigger checks the payment belongs to the vendor with the amount), then
    # the evidence-checked transition; both in the one transaction.
    db.execute(
        state.update(PaymentOperation)
        .where(PaymentOperation.id == op.id, PaymentOperation.version == op.version)
        .values(
            payment_id=payment.id,
            recording_status="recorded",
            ledger_status="posted",
            allocation_status="committed"
            if state.cents(allocated) >= state.cents(planned)
            else "partial",
            accounting_status="corrected" if kind != verification.KIND_AUTOMATIC else "recorded",
            completion_kind=kind,
            completion_evidence_json=evidence,
            completion_idempotency_key=idempotency_key,
        )
        .execution_options(synchronize_session=False)
    )
    op.payment_id = payment.id
    op.recording_status, op.ledger_status = "recorded", "posted"
    op.allocation_status = (
        "committed" if state.cents(allocated) >= state.cents(planned) else "partial"
    )
    op.accounting_status = "corrected" if kind != verification.KIND_AUTOMATIC else "recorded"
    op.completion_kind, op.completion_evidence_json = kind, evidence
    op.completion_idempotency_key = idempotency_key
    state.transition(
        db,
        op,
        OperationState.RECONCILIATION_REQUIRED,
        OperationState.COMPLETED,
        actor=actor,
        category="recovery",
        detail=(
            f"completed with evidence: payment {payment.id}, journal entry {payment.journal_entry_id}, "
            f"gateway {op.gateway_confirmation_ref}; {reason}"
        ),
        completed_at=utcnow(),
        recorded_at=utcnow(),
        recovery_status="resolved",
        recovery_note=reason[:1000],
        last_error=None,
        last_error_category=None,
    )
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.operation.completed_with_evidence",
        resource=f"payment_operation:{op.operation_uid}",
    )
    db.flush()
    _sync_run_items_uncommitted(db, op, gateway=integrations.gateway)
    integrations.faults.check("completion.before_commit")
    if commit:
        db.commit()
        db.refresh(op)
    return op


def _sync_run_items_uncommitted(db, op: PaymentOperation, *, gateway=None) -> None:
    """Like :func:`_sync_run_items` but inside the caller's transaction (manual completion)."""
    if op.run_id is None:
        return
    from app.modules.payables.services.payment_runs import _cas, finalize_run

    run = db.get(PaymentRun, op.run_id)
    if run is None or run.status not in (PaymentRunStatus.FAILED, PaymentRunStatus.EXECUTING):
        return
    if run.status == PaymentRunStatus.FAILED:
        _cas(db, run, PaymentRunStatus.FAILED, status=PaymentRunStatus.EXECUTING)
    for item in db.scalars(
        select(PaymentRunItem).where(
            PaymentRunItem.run_id == op.run_id,
            PaymentRunItem.vendor_id == op.vendor_id,
            PaymentRunItem.status != RunItemStatus.EXCLUDED,
        )
    ):
        item.status = RunItemStatus.PAID
        item.payment_id = op.payment_id
        item.execution_reference = op.gateway_confirmation_ref
        item.failure_reason = None
    db.flush()
    db.refresh(run)
    finalize_run(db, run, gateway=gateway, actor="manual-completion")


def correction_payment_key(operation_uid: str, idempotency_key: str | None) -> str | None:
    """Payment idempotency key of a correction (fits ``vendor_payments.idempotency_key``)."""
    if not idempotency_key:
        return None
    import hashlib

    digest = hashlib.sha256(f"{operation_uid}|{idempotency_key}".encode()).hexdigest()[:40]
    return f"opc:{digest}"


def record_correction(
    db,
    operation_uid: str,
    *,
    actor: str,
    payment_date: date,
    allocations: list,
    reason: str,
    integrations: Integrations,
    idempotency_key: str | None = None,
) -> PaymentOperation:
    """Authorised correction workflow: record the AP payment for a confirmed transfer, then complete.

    Vendor, amount, cash account, method and reference come from the operation (they are
    what the gateway moved); the operator supplies the accounting date (an open period)
    and the allocations. Recording and completion are one transaction: if verification
    fails nothing is recorded. Nothing is sent to the gateway.
    """
    from app.modules.payables.schemas import PaymentIn
    from app.modules.payables.services import payments as payment_services

    op = state.get_operation(db, operation_uid, for_update=True)
    _authorise(integrations, actor, "payables.operation.correct", op)
    key = correction_payment_key(op.operation_uid, idempotency_key)
    if op.state == OperationState.COMPLETED:
        if key and op.payment is not None and op.payment.idempotency_key == key:
            # Same key: the payment content must match (raises Conflict otherwise) and so must the reason.
            payment_services._existing_by_key(
                db,
                PaymentIn(
                    vendor_id=op.vendor_id,
                    payment_date=payment_date,
                    amount=op.amount,
                    method=op.method,
                    reference=op.gateway_confirmation_ref,
                    cash_account_id=op.cash_account_id,
                    recorded_by=actor,
                    idempotency_key=key,
                    allocations=allocations,
                ),
            )
            if _completion_request_fingerprint(op.payment_id, reason) not in (
                op.completion_evidence_json or ""
            ):
                raise Conflict(
                    f"correction key {idempotency_key!r} was already used for a different request"
                )
            return op  # identical retry: the original result
        raise Conflict(
            f"operation {op.operation_uid} is already completed (payment {op.payment_id})"
        )
    if op.state != OperationState.RECONCILIATION_REQUIRED:
        raise ValidationFailed(
            f"corrections apply to reconciliation_required operations only (state: {op.state.value})"
        )
    if not op.gateway_confirmation_ref:
        raise ValidationFailed(
            "the operation has no gateway confirmation reference; confirm the transfer first"
        )
    existing = [
        f
        for f in verification.verify_operation(db, op, gateway=integrations.gateway).findings
        if f.code in ("duplicate_payment_for_operation", "duplicate_payment_for_transfer")
    ]
    if existing:
        raise Conflict(
            "a payment for this transfer already exists; link it with complete-with-evidence instead: "
            + "; ".join(f.actual or f.code for f in existing)
        )
    if key:
        # Same key + different content raises Conflict here; same key + same content means a
        # payment already exists without a completed operation -> link it instead.
        previous = payment_services._existing_by_key(
            db,
            PaymentIn(
                vendor_id=op.vendor_id,
                payment_date=payment_date,
                amount=op.amount,
                method=op.method,
                reference=op.gateway_confirmation_ref,
                cash_account_id=op.cash_account_id,
                recorded_by=actor,
                idempotency_key=key,
                allocations=allocations,
            ),
        )
        if previous is not None:
            raise Conflict(
                f"a payment ({previous.id}) was already recorded with correction key {idempotency_key!r}; "
                "complete the operation with that payment as evidence"
            )
    payment, _ = payment_services._record_payment_uncommitted(
        db,
        PaymentIn(
            vendor_id=op.vendor_id,
            payment_date=payment_date,
            amount=op.amount,
            method=op.method,
            reference=op.gateway_confirmation_ref,
            cash_account_id=op.cash_account_id,
            recorded_by=actor,
            idempotency_key=key,
            allocations=allocations,
        ),
        integrations=integrations,
        allow_inactive_vendor=True,  # the cash has moved regardless of the vendor's status now
    )
    state.record_event(
        db,
        op,
        category="correction",
        detail=f"payment {payment.id} recorded through the correction workflow by {actor}: {reason}",
        actor=actor,
    )
    return complete_with_evidence(
        db,
        operation_uid,
        actor=actor,
        payment_id=payment.id,
        reason=reason,
        integrations=integrations,
        idempotency_key=idempotency_key,
        kind=verification.KIND_MANUAL_CORRECTION,
    )


def duplicate_confirmation(
    db, operation_uid: str, *, reference: str, actor: str
) -> PaymentOperation:
    """A confirmation (callback) for an operation that is already complete: log, never re-record.

    The row is locked so the event sequence cannot collide with a worker's transition.
    """
    op = state.get_operation(db, operation_uid, for_update=True)
    detail = f"duplicate confirmation received ({reference})"
    if op.gateway_confirmation_ref and reference != op.gateway_confirmation_ref:
        detail += f"; differs from recorded {op.gateway_confirmation_ref}"
    state.record_event(db, op, category="gateway", detail=detail, actor=actor)
    db.commit()
    return op


def as_dicts(items) -> list[dict]:
    return [asdict(i) for i in items]
