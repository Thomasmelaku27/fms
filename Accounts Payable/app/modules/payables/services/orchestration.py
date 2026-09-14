"""Payment orchestration: drive a durable operation through the gateway and the books.

Transaction boundaries (each ``commit`` below is a durability point; a crash between
two of them is recovered by ``services.recovery`` from the persisted state):

1. ``prepared``            intent committed (done by the caller via ``execution_state.prepare``)
2. ``-> submitted``        committed *before* the gateway call
3. gateway call            external; its effect cannot be rolled back
4. ``-> confirmed`` | ``confirmation_pending`` | ``failed``   committed right after the answer
5. ``-> recording``        committed
6. recording transaction   AP payment + allocations + discounts + journal entries + ``-> completed``
                           in ONE transaction; on failure the whole transaction rolls back and a
                           separate short transaction marks ``reconciliation_required``

Fault injection: ``integrations.faults.check(point)`` is called at the named points
(``submit.before_commit``, ``submit.after_gateway``, ``lookup.after_gateway``,
``confirm.before_commit``, ``recording.before_books``, ``recording.after_flush``,
``recording.before_commit``, ``recording.after_commit``; ``recovery.before_action`` and
``completion.before_commit`` in ``recovery``; ``close.before_commit`` in ``closing``).
Tests raise (or block) there to simulate crashes and chosen interleavings; production
uses ``NullFaults``.

Completion evidence: the recording transaction stores ``accounting_status = 'recorded'``,
``completion_kind = 'automatic'`` and a ``completion_evidence_json`` snapshot (payment,
entry, gateway reference, amounts) together with the ``-> completed`` transition; the
database trigger of ``fmspayables06`` checks the payment / posted entry before accepting
the transition, so a completion can never outlive a rolled-back recording.
"""

from decimal import Decimal

from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import Account
from app.modules.payables.integration import Integrations
from app.modules.payables.ledger_adapter import LedgerPostingRefused
from app.modules.payables.models import (
    OPEN_BILL_STATUSES,
    OperationState,
    PaymentOperation,
    SupplierBill,
    SupplierCredit,
    Vendor,
    VendorPayment,
)
from app.modules.payables.schemas import AllocationIn, PaymentIn
from app.modules.payables.services import credits as credit_services
from app.modules.payables.services import execution_state as state
from app.modules.payables.services import payments as payment_services
from app.modules.payables.services import settlement
from app.modules.payables.services.common import PayablesError, ValidationFailed, lock, utcnow
from app.modules.payables.services.gateway import (
    GatewayOutcome,
    GatewayRequest,
    GatewayResponse,
    GatewayTimeout,
)

ZERO = Decimal("0.00")

# Error categories (``last_error_category``); "retryable" ones may be retried by recovery.
CATEGORY_GATEWAY_REJECTED = "gateway_rejected"
CATEGORY_GATEWAY_TIMEOUT = "gateway_timeout"
CATEGORY_GATEWAY_PENDING = "gateway_pending"
CATEGORY_VALIDATION = "validation"
CATEGORY_LEDGER = "ledger"
CATEGORY_DATABASE = "database"
CATEGORY_UNEXPECTED = "unexpected"
RETRYABLE_RECORDING_CATEGORIES = (
    CATEGORY_LEDGER,
    CATEGORY_DATABASE,
    CATEGORY_UNEXPECTED,
    CATEGORY_VALIDATION,
)


class ExecutionOutcome:
    def __init__(self, op: PaymentOperation):
        self.operation_uid = op.operation_uid
        self.state = op.state
        self.payment_id = op.payment_id
        self.reference = op.gateway_confirmation_ref
        self.error = op.last_error
        self.category = op.last_error_category

    @property
    def success(self) -> bool:
        return self.state == OperationState.COMPLETED

    @property
    def money_moved(self) -> bool:
        return self.state in (
            OperationState.CONFIRMED,
            OperationState.RECORDING,
            OperationState.COMPLETED,
            OperationState.RECONCILIATION_REQUIRED,
        )


def _gateway_request(db, op: PaymentOperation) -> GatewayRequest:
    vendor = db.get(Vendor, op.vendor_id)
    cash = db.get(Account, op.cash_account_id)
    plan = state.plan_of(op)
    bills = (
        {
            b.id: b
            for b in db.query(SupplierBill)
            .filter(SupplierBill.id.in_([i["bill_id"] for i in plan]))
            .all()
        }
        if plan
        else {}
    )
    return GatewayRequest(
        token=op.operation_uid,
        fingerprint=op.fingerprint,
        vendor_code=vendor.code,
        destination_reference=op.destination_reference,
        amount=op.amount,
        currency=op.currency,
        pay_date=op.payment_date,
        method=op.method.value,
        cash_account_code=cash.code,
        bill_references=tuple(
            bills[i["bill_id"]].invoice_number for i in plan if i["bill_id"] in bills
        ),
        correlation_id=op.correlation_id,
    )


# --- step 2-4: submission --------------------------------------------------------------------------------


def submit(db, op: PaymentOperation, *, integrations: Integrations, actor: str) -> PaymentOperation:
    """prepared -> submitted (committed) -> gateway -> confirmed | pending | failed (committed)."""
    faults = integrations.faults
    if op.state == OperationState.PREPARED:
        state.transition(
            db,
            op,
            OperationState.PREPARED,
            OperationState.SUBMITTED,
            actor=actor,
            detail="handing the instruction to the gateway",
            submitted_at=utcnow(),
            attempts=op.attempts + 1,
            gateway_status=None,
        )
        faults.check("submit.before_commit")
        db.commit()
    request = _gateway_request(db, op)
    try:
        response = integrations.gateway.submit(request, db=db)
    except GatewayTimeout as exc:
        # Unknown outcome: the transfer may have happened. Stay 'submitted', resolve by lookup.
        state.record_event(db, op, category="gateway", detail=f"response lost: {exc}", actor=actor)
        db.execute(
            state.update(PaymentOperation)
            .where(PaymentOperation.id == op.id)
            .values(
                gateway_status=GatewayOutcome.UNKNOWN.value,
                last_error_category=CATEGORY_GATEWAY_TIMEOUT,
                last_error=str(exc)[:1000],
            )
            .execution_options(synchronize_session=False)
        )
        op.gateway_status, op.last_error_category, op.last_error = (
            GatewayOutcome.UNKNOWN.value,
            CATEGORY_GATEWAY_TIMEOUT,
            str(exc),
        )
        db.commit()
        return resolve_unknown(db, op, integrations=integrations, actor=actor)
    faults.check("submit.after_gateway")
    return apply_gateway_response(db, op, response, integrations=integrations, actor=actor)


def apply_gateway_response(
    db, op: PaymentOperation, response: GatewayResponse, *, integrations: Integrations, actor: str
) -> PaymentOperation:
    """Persist what the gateway said (one short transaction)."""
    faults = integrations.faults
    from_states = (OperationState.SUBMITTED, OperationState.CONFIRMATION_PENDING)
    if response.outcome == GatewayOutcome.ACCEPTED:
        if op.gateway_confirmation_ref and op.gateway_confirmation_ref != response.reference:
            state.transition(
                db,
                op,
                from_states,
                OperationState.RECONCILIATION_REQUIRED,
                actor=actor,
                category="gateway",
                detail=f"contradictory confirmation references {op.gateway_confirmation_ref} / {response.reference}",
                last_error_category=CATEGORY_UNEXPECTED,
                last_error="contradictory gateway confirmations",
                recovery_status="manual",
                accounting_status="unrecorded",
            )
        else:
            state.transition(
                db,
                op,
                from_states,
                OperationState.CONFIRMED,
                actor=actor,
                category="gateway",
                detail=f"gateway accepted, reference {response.reference}",
                confirmed_at=utcnow(),
                gateway_status=GatewayOutcome.ACCEPTED.value,
                gateway_submission_ref=op.gateway_submission_ref or response.reference,
                gateway_confirmation_ref=response.reference,
                last_error=None,
                last_error_category=None,
                accounting_status="unrecorded",  # cash is out; nothing is in the books yet
            )
        faults.check("confirm.before_commit")
    elif response.outcome == GatewayOutcome.REJECTED:
        state.transition(
            db,
            op,
            from_states,
            OperationState.FAILED,
            actor=actor,
            category="gateway",
            detail=f"gateway rejected: {response.error_code} {response.message or ''}".strip(),
            failed_at=utcnow(),
            gateway_status=GatewayOutcome.REJECTED.value,
            last_error_category=CATEGORY_GATEWAY_REJECTED,
            last_error=f"{response.error_code}: {response.message or ''}"[:1000],
        )
    elif response.outcome == GatewayOutcome.PENDING:
        if op.state != OperationState.CONFIRMATION_PENDING:
            state.transition(
                db,
                op,
                from_states,
                OperationState.CONFIRMATION_PENDING,
                actor=actor,
                category="gateway",
                detail="gateway reports pending",
                gateway_status=GatewayOutcome.PENDING.value,
                last_error_category=CATEGORY_GATEWAY_PENDING,
                last_error="awaiting gateway confirmation",
            )
        else:
            state.record_event(db, op, category="gateway", detail="still pending", actor=actor)
    else:  # UNKNOWN: the gateway never received it -> safe to submit again with the same token
        state.record_event(
            db, op, category="gateway", detail="gateway has no record of the token", actor=actor
        )
        db.execute(
            state.update(PaymentOperation)
            .where(PaymentOperation.id == op.id)
            .values(gateway_status=GatewayOutcome.UNKNOWN.value)
            .execution_options(synchronize_session=False)
        )
        op.gateway_status = GatewayOutcome.UNKNOWN.value
    db.commit()
    return op


def resolve_unknown(
    db, op: PaymentOperation, *, integrations: Integrations, actor: str
) -> PaymentOperation:
    """Ask the gateway what happened to a submitted operation whose answer we never saw."""
    response = integrations.gateway.lookup(op.operation_uid, db=db)
    integrations.faults.check("lookup.after_gateway")
    if response.outcome == GatewayOutcome.UNKNOWN:
        # Never received: resubmitting with the same token cannot cause a second transfer.
        if op.attempts >= op.max_attempts:
            state.transition(
                db,
                op,
                (OperationState.SUBMITTED, OperationState.CONFIRMATION_PENDING),
                OperationState.FAILED,
                actor=actor,
                category="gateway",
                detail="gateway never received the instruction; attempts exhausted",
                failed_at=utcnow(),
                last_error_category=CATEGORY_GATEWAY_TIMEOUT,
                last_error="instruction never reached the gateway",
            )
            db.commit()
            return op
        db.execute(
            state.update(PaymentOperation)
            .where(PaymentOperation.id == op.id)
            .values(attempts=op.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        op.attempts += 1
        state.record_event(
            db,
            op,
            category="gateway",
            detail=f"resubmitting with the same token (attempt {op.attempts})",
            actor=actor,
        )
        db.commit()
        try:
            response = integrations.gateway.submit(_gateway_request(db, op), db=db)
        except GatewayTimeout as exc:
            state.record_event(
                db, op, category="gateway", detail=f"response lost again: {exc}", actor=actor
            )
            db.commit()
            return op  # still 'submitted'; recovery will look it up later
    return apply_gateway_response(db, op, response, integrations=integrations, actor=actor)


# --- step 5-6: recording ---------------------------------------------------------------------------------


def _categorise(exc: Exception) -> str:
    if isinstance(exc, LedgerPostingRefused):
        return CATEGORY_LEDGER
    if isinstance(exc, (IntegrityError, OperationalError)):
        return CATEGORY_DATABASE
    if isinstance(exc, PayablesError):
        return CATEGORY_VALIDATION
    return CATEGORY_UNEXPECTED


def record(db, op: PaymentOperation, *, integrations: Integrations, actor: str) -> PaymentOperation:
    """confirmed | reconciliation_required -> recording (committed) -> books + completed (one transaction)."""
    faults = integrations.faults
    retrying = op.state == OperationState.RECONCILIATION_REQUIRED
    state.transition(
        db,
        op,
        (OperationState.CONFIRMED, OperationState.RECONCILIATION_REQUIRED),
        OperationState.RECORDING,
        actor=actor,
        detail="recording the confirmed transfer" + (" (retry)" if retrying else ""),
        attempts=op.attempts + (1 if retrying else 0),
        recovery_status="none",
    )
    db.commit()
    try:
        faults.check("recording.before_books")
        notes = _write_books(db, op, integrations=integrations, actor=actor)
        faults.check("recording.after_flush")
        state.transition(
            db,
            op,
            OperationState.RECORDING,
            OperationState.COMPLETED,
            actor=actor,
            detail="; ".join(notes) or "AP payment, allocations and journal entries recorded",
            completed_at=utcnow(),
            recorded_at=utcnow(),
            last_error=None,
            last_error_category=None,
        )
        faults.check("recording.before_commit")
        db.commit()
        faults.check("recording.after_commit")
    except Exception as exc:  # noqa: BLE001 - every failure here must be categorised and persisted
        db.rollback()
        category = _categorise(exc)
        fresh = state.get_operation(db, op.operation_uid)
        if (
            fresh.state == OperationState.COMPLETED
        ):  # the fault hit after the commit: nothing to undo
            return fresh
        manual = (
            fresh.attempts >= fresh.max_attempts or category not in RETRYABLE_RECORDING_CATEGORIES
        )
        state.transition(
            db,
            fresh,
            OperationState.RECORDING,
            OperationState.RECONCILIATION_REQUIRED,
            actor=actor,
            category="error",
            detail=f"recording failed ({category}): {str(exc).splitlines()[0][:300]}",
            last_error_category=category,
            last_error=str(exc)[:1000],
            recovery_status="manual" if manual else "scheduled",
        )
        db.commit()
        return fresh
    return op


def _write_books(db, op: PaymentOperation, *, integrations: Integrations, actor: str) -> list[str]:
    """AP payment, applications and ledger entries for a confirmed operation (no commit).

    The cash has left the bank, so the payment is always recorded. Plan items that
    can no longer be applied (bill closed, held or settled meanwhile) leave their cash
    as an unapplied remainder on the vendor's payable account and are reported.
    """
    plan = state.plan_of(op)
    vendor = db.get(Vendor, op.vendor_id)
    bills = lock(db, SupplierBill, [i["bill_id"] for i in plan])
    notes: list[str] = []
    allocations: list[AllocationIn] = []
    for item in plan:
        bill = bills.get(item["bill_id"])
        pay = Decimal(item["pay"])
        credit = Decimal(item["credit"])
        advance = Decimal(item["advance"])
        discount = Decimal(item["discount"])
        if bill is None or bill.status not in OPEN_BILL_STATUSES or bill.on_hold:
            notes.append(f"bill {item['bill_id']}: no longer payable; cash kept as advance")
            continue
        if credit > 0:
            applied = _apply_pool(
                db,
                vendor.id,
                bill,
                credit,
                kind="credit",
                op=op,
                actor=actor,
                integrations=integrations,
            )
            if applied < credit:
                notes.append(f"bill {bill.id}: planned credit {credit:.2f}, applied {applied:.2f}")
        if advance > 0:
            applied = _apply_pool(
                db,
                vendor.id,
                bill,
                advance,
                kind="advance",
                op=op,
                actor=actor,
                integrations=integrations,
            )
            if applied < advance:
                notes.append(
                    f"bill {bill.id}: planned advance {advance:.2f}, applied {applied:.2f}"
                )
        if pay > 0:
            take_discount = discount > 0 and bill.discount_available_on(op.payment_date) >= discount
            if pay + (discount if take_discount else ZERO) > bill.outstanding:
                fits = bill.outstanding if not take_discount else bill.outstanding - discount
                if fits <= 0:
                    notes.append(f"bill {bill.id}: settled meanwhile; {pay:.2f} kept as advance")
                    continue
                notes.append(
                    f"bill {bill.id}: only {fits:.2f} of {pay:.2f} still allocatable; rest kept as advance"
                )
                pay = fits
            if discount > 0 and not take_discount:
                notes.append(f"bill {bill.id}: discount {discount:.2f} no longer available")
            allocations.append(
                AllocationIn(bill_id=bill.id, amount=pay, take_discount=take_discount)
            )
    payment, _ = payment_services._record_payment_uncommitted(
        db,
        PaymentIn(
            vendor_id=vendor.id,
            payment_date=op.payment_date,
            amount=op.amount,
            method=op.method,
            reference=op.gateway_confirmation_ref,
            cash_account_id=op.cash_account_id,
            recorded_by=actor,
            idempotency_key=f"op:{op.operation_uid}",
            allocations=allocations,
        ),
        integrations=integrations,
        allow_inactive_vendor=True,  # the cash has moved regardless of the vendor's status now
    )
    allocated = sum((a.amount for a in allocations), ZERO)
    evidence = completion_evidence(
        payment_id=payment.id,
        journal_entry_id=payment.journal_entry_id,
        gateway_confirmation_ref=op.gateway_confirmation_ref,
        amount=op.amount,
        allocated=allocated,
        kind="automatic",
        actor=actor,
    )
    db.execute(
        state.update(PaymentOperation)
        .where(PaymentOperation.id == op.id)
        .values(
            payment_id=payment.id,
            recording_status="recorded",
            ledger_status="posted",
            allocation_status="committed" if not notes else "partial",
            recovery_note="; ".join(notes)[:1000] or None,
            accounting_status="recorded",
            completion_kind="automatic",
            completion_evidence_json=evidence,
        )
        .execution_options(synchronize_session=False)
    )
    op.payment_id, op.recording_status, op.ledger_status = payment.id, "recorded", "posted"
    op.allocation_status = "committed" if not notes else "partial"
    op.accounting_status, op.completion_kind, op.completion_evidence_json = (
        "recorded",
        "automatic",
        evidence,
    )
    if allocated < op.amount:
        notes.append(f"unapplied remainder {op.amount - allocated:.2f} recorded as advance")
    return notes


def completion_evidence(
    *,
    payment_id: int,
    journal_entry_id: int | None,
    gateway_confirmation_ref: str | None,
    amount: Decimal,
    allocated: Decimal,
    kind: str,
    actor: str,
    reason: str | None = None,
    verified: dict | None = None,
) -> str:
    """Durable snapshot of what the completion rests on (stored with the operation)."""
    import json

    return json.dumps(
        {
            "payment_id": payment_id,
            "journal_entry_id": journal_entry_id,
            "gateway_confirmation_ref": gateway_confirmation_ref,
            "amount": f"{amount:.2f}",
            "allocated": f"{allocated:.2f}",
            "kind": kind,
            "actor": actor,
            "reason": reason,
            "verified_at": utcnow().isoformat(),
            "required_evidence": verified or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )[:4000]


def _apply_pool(
    db,
    vendor_id: int,
    bill: SupplierBill,
    wanted: Decimal,
    *,
    kind: str,
    op: PaymentOperation,
    actor: str,
    integrations: Integrations,
) -> Decimal:
    """Apply up to ``wanted`` from the vendor's open credits (kind='credit') or advances ('advance')."""
    from app.modules.payables.services.payment_runs import (
        _account_of,
        _available_advances,
        _available_credits,
    )

    pool = (
        _available_credits(db, vendor_id, op.payment_date)
        if kind == "credit"
        else _available_advances(db, vendor_id, op.payment_date)
    )
    accounts = _account_of(db, list(pool), SupplierCredit if kind == "credit" else VendorPayment)
    applied = ZERO
    for source_id, available in pool.items():
        if wanted <= 0:
            break
        if accounts.get(source_id) != bill.payable_account_id or available <= 0:
            continue
        take = min(available, wanted, bill.outstanding)
        if take <= 0:
            break
        try:
            if kind == "credit":
                credit_services._apply_credit_uncommitted(
                    db,
                    source_id,
                    bill_id=bill.id,
                    amount=take,
                    actor=actor,
                    application_date=op.payment_date,
                    integrations=integrations,
                )
            else:
                settlement._apply_advance_uncommitted(
                    db,
                    source_id,
                    bill_id=bill.id,
                    amount=take,
                    actor=actor,
                    allocation_date=op.payment_date,
                    integrations=integrations,
                )
        except ValidationFailed:
            continue
        wanted -= take
        applied += take
    return applied


# --- the whole thing --------------------------------------------------------------------------------------


def execute(
    db, op: PaymentOperation, *, integrations: Integrations, actor: str
) -> ExecutionOutcome:
    """Advance an operation as far as it can go right now; never submits after money moved."""
    if op.state in (OperationState.PREPARED,):
        op = submit(db, op, integrations=integrations, actor=actor)
    elif op.state == OperationState.SUBMITTED and op.gateway_status in (
        None,
        GatewayOutcome.UNKNOWN.value,
    ):
        op = resolve_unknown(db, op, integrations=integrations, actor=actor)
    elif op.state == OperationState.CONFIRMATION_PENDING:
        op = lookup_pending(db, op, integrations=integrations, actor=actor)
    elif op.state == OperationState.RECORDING:
        # A previous worker crashed while recording: its transaction rolled back with it.
        state.transition(
            db,
            op,
            OperationState.RECORDING,
            OperationState.RECONCILIATION_REQUIRED,
            actor=actor,
            category="recovery",
            detail="recording interrupted; retrying",
            last_error_category=CATEGORY_UNEXPECTED,
            last_error="interrupted while recording",
        )
        db.commit()
    if op.state == OperationState.CONFIRMED or (
        op.state == OperationState.RECONCILIATION_REQUIRED
        and op.recovery_status != "manual"
        and op.attempts < op.max_attempts
    ):
        op = record(db, op, integrations=integrations, actor=actor)
    return ExecutionOutcome(op)


def lookup_pending(
    db, op: PaymentOperation, *, integrations: Integrations, actor: str
) -> PaymentOperation:
    response = integrations.gateway.lookup(op.operation_uid, db=db)
    integrations.faults.check("lookup.after_gateway")
    if response.outcome == GatewayOutcome.PENDING and op.attempts >= op.max_attempts:
        state.transition(
            db,
            op,
            OperationState.CONFIRMATION_PENDING,
            OperationState.RECONCILIATION_REQUIRED,
            actor=actor,
            category="gateway",
            detail="still pending after the attempt budget; investigate with the gateway",
            last_error_category=CATEGORY_GATEWAY_PENDING,
            recovery_status="manual",
        )
        db.commit()
        return op
    if response.outcome == GatewayOutcome.PENDING:
        db.execute(
            state.update(PaymentOperation)
            .where(PaymentOperation.id == op.id)
            .values(attempts=op.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        op.attempts += 1
    return apply_gateway_response(db, op, response, integrations=integrations, actor=actor)
