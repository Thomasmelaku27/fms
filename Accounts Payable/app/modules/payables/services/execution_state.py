"""Durable payment execution state: intents, compare-and-swap transitions, events, leases.

State machine (``PaymentOperation.state``)::

    prepared ──▶ submitted ──▶ confirmed ──▶ recording ──▶ completed
       │            │  │           │             │
       │            │  └─▶ confirmation_pending ─┤ (gateway has it, outcome unknown yet)
       │            │           │    │           │
       ▼            ▼           ▼    │           ▼
     failed ◀──── failed ◀──── failed│   reconciliation_required ◀──┘ (money moved, recording failed)
                                     └─▶ reconciliation_required       │
                                                                       └─▶ recording / completed

Evidence required for every transition (service rule -> database rule of migration
``fmspayables06``):

======================================  ==============================================================
``prepared -> submitted``               the intent row is committed *before* the gateway is called;
                                        the idempotency key and instruction fingerprint are frozen
``submitted -> confirmed``              a gateway response ``accepted`` with a confirmation reference
                                        -> DB: ``gateway_status = 'accepted'`` and a reference (unique)
``submitted -> confirmation_pending``   a gateway response ``pending``
``-> failed``                           only a gateway answer that **no money moved**: ``rejected``, or
                                        ``unknown`` after the attempt budget -> DB: refused once the
                                        gateway accepted / a confirmation reference exists. ``failed``
                                        never means "money did not move" unless the gateway said so.
``confirmed -> recording``              a worker lease; the recording transaction then writes the
                                        AP payment, allocations and the posted journal entry
``recording -> completed``              in the *same* transaction as the books: a recorded vendor
                                        payment of the operation's vendor, amount and cash account,
                                        a posted entry crediting cash by the amount, allocations
                                        <= amount -> DB trigger re-checks all of it;
                                        ``accounting_status`` recorded / corrected; evidence snapshot
``recording -> reconciliation_required`` the recording transaction rolled back (category, error
                                        text, ``accounting_status = 'unrecorded'``)
``reconciliation_required -> recording`` recovery retry (attempt budget, retryable category, no
                                        duplicate payment found by the verifier)
``reconciliation_required -> completed`` manual completion **with evidence only**: a payment id whose
                                        records pass ``services.verification`` (gateway record,
                                        events, payment, posted entry, allocations, run) -> the
                                        same DB trigger; a note is never evidence
``completed``                           terminal and frozen: payment link, gateway reference, status
                                        flags, evidence, completed_at cannot change; investigation
                                        columns, lease and notes can

Compare-and-swap, leases, lock ordering
---------------------------------------
Every transition is ``UPDATE ... WHERE id = ? AND state IN (...) AND version = ?`` and
appends a hash-chained event; rowcount 0 means another worker moved the row first
(:class:`OperationConflict`, HTTP 409) and the caller rolls back. Workers claim an
operation with :func:`lease` (``worker_id``, ``lease_expires_at``; only free or expired
leases can be taken; the CAS on ``version`` means a lease taken after a concurrent
transition fails). A lease is advisory for scheduling; correctness rests on the CAS:
an expired lease taken over mid-recording makes the slower worker's final transition
fail and its books roll back.

Lock order inside one recording transaction (PostgreSQL row locks): supplier bills
(``FOR UPDATE``, ascending id) -> vendor payment insert -> journal rows -> operation row
(status columns, then the CAS). Callers that need the operation row first (manual
completion, corrections, gateway callbacks) take ``SELECT ... FOR UPDATE`` on it and
therefore wait for an in-flight recording to commit instead of racing it. The AP close
takes the exclusive close advisory lock before reading; every dated write takes the
shared side (``common.assert_period_open``), so a close serialises with recordings.

Lost database connections: nothing here commits except at the documented durability
points; a connection lost in the middle of a transaction loses that transaction only.
The recovery worker re-derives what to do from the committed state (and the gateway's
durable record), never from process memory; a lease left behind expires. Nothing in
this module commits.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.payables.models import (
    TERMINAL_OPERATION_STATES,
    OperationState,
    PaymentMethod,
    PaymentOperation,
    PaymentOperationEvent,
)
from app.modules.payables.services.common import Conflict, ValidationFailed, utcnow

CENT = Decimal("0.01")
# NUMERIC(18, 2): sixteen integer digits is the largest magnitude the database can store.
MAX_MAGNITUDE = Decimal("9999999999999999.99")


class OperationConflict(Conflict):
    """The operation was moved by someone else (lost compare-and-swap)."""


class InstructionConflict(Conflict):
    """The same idempotency key / operation id was reused with different instructions."""


class MoneyError(ValidationFailed):
    """A monetary input that cannot be represented exactly as NUMERIC(18, 2)."""


# --- canonical money and fingerprints --------------------------------------------------------------


def canonical_amount(value, field: str = "amount", *, allow_zero: bool = False) -> Decimal:
    """Exact ``Decimal`` with two decimals, representable as NUMERIC(18, 2); never rounded.

    Accepted inputs: ``Decimal``, ``int`` (not ``bool``) and ``str``. Floats are refused
    (binary fractions are not money), as are NaN / infinities, negative amounts,
    negative zero, more than two decimals, and magnitudes the database cannot store.
    Every refusal is a :class:`MoneyError` (HTTP 422), never ``decimal.InvalidOperation``.
    """
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise MoneyError(
            f"{field}: unsupported numeric type {type(value).__name__}; use a decimal string"
        )
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate or candidate.lower() in (
            "nan",
            "snan",
            "inf",
            "infinity",
            "-inf",
            "-infinity",
        ):
            raise MoneyError(f"{field}: not a finite monetary value ({value!r})")
        try:
            amount = Decimal(candidate)
        except (InvalidOperation, ValueError) as exc:
            raise MoneyError(f"{field}: not a monetary value ({value!r})") from exc
    else:
        raise MoneyError(
            f"{field}: unsupported numeric type {type(value).__name__}; use a decimal string"
        )
    if not amount.is_finite():
        raise MoneyError(f"{field}: not a finite monetary value ({value!r})")
    if amount.is_signed() and amount == 0:
        raise MoneyError(f"{field}: negative zero is not a monetary value")
    if amount < 0:
        raise MoneyError(f"{field}: must not be negative ({amount})")
    # Scale and magnitude are checked before quantize(): quantize() itself raises
    # InvalidOperation on values such as 1E+40, which must surface as a domain error.
    if _scale(amount) > 2:
        raise MoneyError(f"{field}: unsupported precision ({amount}); whole cents only")
    if abs(amount) > MAX_MAGNITUDE:
        raise MoneyError(
            f"{field}: {amount} exceeds the representable range (NUMERIC(18,2), at most {MAX_MAGNITUDE})"
        )
    amount = amount.quantize(CENT)
    if amount == 0 and not allow_zero:
        raise MoneyError(f"{field}: must be positive ({amount})")
    return amount


def _scale(amount: Decimal) -> int:
    """Number of significant decimal places (``1.50`` -> 1, ``1E+3`` -> 0, ``0.001`` -> 3)."""
    normalized = amount.normalize() if amount != 0 else Decimal(0)
    exponent = normalized.as_tuple().exponent
    return max(-exponent, 0) if isinstance(exponent, int) else 0


def canonical_plan(items: Sequence[dict]) -> list[dict]:
    """Sorted, string-money representation of the settlement plan (each part validated)."""
    plan = []
    for item in sorted(items, key=lambda i: int(i["bill_id"])):
        entry = {"bill_id": int(item["bill_id"])}
        for key in ("settle", "discount", "credit", "advance", "pay"):
            raw = item.get(key, "0")
            amount = canonical_amount(
                "0" if raw is None else raw, f"{key} for bill {item['bill_id']}", allow_zero=True
            )
            entry[key] = f"{amount:.2f}"
        plan.append(entry)
    return plan


def cents(amount: Decimal) -> int:
    """Exact integer cents (for comparisons that must never involve floats)."""
    return int((amount * 100).to_integral_value())


def instruction_fingerprint(
    *,
    vendor_id: int,
    destination_reference: str | None,
    amount: Decimal,
    currency: str,
    method: PaymentMethod | str,
    payment_date: date,
    cash_account_id: int,
    run_id: int | None,
    plan: Sequence[dict],
) -> str:
    """SHA-256 over every accounting-significant field of a payment instruction."""
    payload = {
        "vendor_id": vendor_id,
        "destination_reference": destination_reference or "",
        "amount": f"{canonical_amount(amount):.2f}",
        "currency": currency,
        "method": method.value if isinstance(method, PaymentMethod) else str(method),
        "payment_date": payment_date.isoformat(),
        "cash_account_id": cash_account_id,
        "run_id": run_id,
        "plan": canonical_plan(plan),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --- lookups ------------------------------------------------------------------------------------------


def get_operation(db: Session, operation_uid: str, *, for_update: bool = False) -> PaymentOperation:
    stmt = select(PaymentOperation).where(PaymentOperation.operation_uid == operation_uid)
    if for_update:
        stmt = stmt.with_for_update()
    op = db.scalar(stmt)
    if op is None:
        from app.modules.payables.services.common import NotFound

        raise NotFound("Payment operation not found")
    return op


def by_idempotency_key(db: Session, key: str) -> PaymentOperation | None:
    return db.scalar(select(PaymentOperation).where(PaymentOperation.idempotency_key == key))


# --- intents ---------------------------------------------------------------------------------------------


def prepare(
    db: Session,
    *,
    idempotency_key: str,
    vendor_id: int,
    destination_reference: str | None,
    amount: Decimal,
    currency: str,
    method: PaymentMethod,
    payment_date: date,
    cash_account_id: int,
    run_id: int | None,
    run_version: int | None,
    plan: Sequence[dict],
    actor: str,
    correlation_id: str | None = None,
    max_attempts: int = 5,
) -> tuple[PaymentOperation, bool]:
    """Record (or find) the intent. Returns ``(operation, created)``.

    An existing key with the same fingerprint returns the existing operation (the
    caller resumes it); the same key with different instructions is a conflict.
    The caller commits: the intent must be durable before the gateway is called.
    """
    amount = canonical_amount(amount)
    fingerprint = instruction_fingerprint(
        vendor_id=vendor_id,
        destination_reference=destination_reference,
        amount=amount,
        currency=currency,
        method=method,
        payment_date=payment_date,
        cash_account_id=cash_account_id,
        run_id=run_id,
        plan=plan,
    )
    existing = by_idempotency_key(db, idempotency_key)
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise InstructionConflict(
                f"idempotency key {idempotency_key!r} was already used for a different payment instruction"
            )
        return existing, False
    op = PaymentOperation(
        operation_uid=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        run_id=run_id,
        run_version=run_version,
        vendor_id=vendor_id,
        destination_reference=destination_reference,
        amount=amount,
        currency=currency,
        payment_date=payment_date,
        method=method,
        cash_account_id=cash_account_id,
        plan_json=json.dumps(canonical_plan(plan), separators=(",", ":")),
        state=OperationState.PREPARED,
        version=0,
        attempts=0,
        max_attempts=max_attempts,
        created_by=actor,
        correlation_id=correlation_id or str(uuid.uuid4()),
    )
    db.add(op)
    db.flush()
    record_event(
        db,
        op,
        category="transition",
        detail="intent prepared",
        actor=actor,
        from_state=None,
        to_state=OperationState.PREPARED.value,
    )
    return op, True


def plan_of(op: PaymentOperation) -> list[dict]:
    return json.loads(op.plan_json)


# --- transitions ------------------------------------------------------------------------------------------


def record_event(
    db: Session,
    op: PaymentOperation,
    *,
    category: str,
    detail: str | None,
    actor: str,
    from_state: str | None = None,
    to_state: str | None = None,
) -> PaymentOperationEvent:
    last = db.execute(
        select(PaymentOperationEvent.seq, PaymentOperationEvent.event_hash)
        .where(PaymentOperationEvent.operation_id == op.id)
        .order_by(PaymentOperationEvent.seq.desc())
        .limit(1)
    ).first()
    seq = (last.seq if last else 0) + 1
    prev_hash = last.event_hash if last else None
    to_state = to_state or op.state.value
    detail = (detail or "")[:1000] or None
    event = PaymentOperationEvent(
        operation_id=op.id,
        seq=seq,
        from_state=from_state,
        to_state=to_state,
        category=category,
        detail=detail,
        actor=actor,
        prev_hash=prev_hash,
        event_hash=event_hash(
            prev_hash, op.operation_uid, seq, from_state, to_state, category, detail, actor
        ),
    )
    db.add(event)
    db.flush()
    return event


def event_hash(prev_hash, operation_uid, seq, from_state, to_state, category, detail, actor) -> str:
    """sha256 chain link over the durable event fields (mirrored in migration fmspayables06)."""
    payload = "|".join(
        [
            prev_hash or "",
            operation_uid,
            str(seq),
            from_state or "",
            to_state,
            category,
            detail or "",
            actor,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def transition(
    db: Session,
    op: PaymentOperation,
    from_states: Sequence[OperationState] | OperationState,
    to_state: OperationState,
    *,
    actor: str,
    category: str = "transition",
    detail: str | None = None,
    **fields,
) -> None:
    """Compare-and-swap on ``(state, version)``; appends an event. Does not commit."""
    if isinstance(from_states, OperationState):
        from_states = (from_states,)
    if op.state in TERMINAL_OPERATION_STATES and to_state != op.state:
        raise OperationConflict(f"operation {op.operation_uid} is {op.state.value} (terminal)")
    expected_version = op.version
    values = {"state": to_state, "version": expected_version + 1, **fields}
    result = db.execute(
        update(PaymentOperation)
        .where(
            PaymentOperation.id == op.id,
            PaymentOperation.state.in_(from_states),
            PaymentOperation.version == expected_version,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise OperationConflict(
            f"operation {op.operation_uid} changed concurrently (expected {'/'.join(s.value for s in from_states)} "
            f"v{expected_version})"
        )
    previous = op.state
    for name, value in values.items():
        setattr(op, name, value)
    record_event(
        db,
        op,
        category=category,
        detail=detail,
        actor=actor,
        from_state=previous.value,
        to_state=to_state.value,
    )


def lease(db: Session, op: PaymentOperation, *, worker_id: str, seconds: int = 300) -> bool:
    """Claim an operation for a worker (expired or free leases only). Caller commits."""
    now = utcnow()
    result = db.execute(
        update(PaymentOperation)
        .where(
            PaymentOperation.id == op.id,
            PaymentOperation.version == op.version,
            (PaymentOperation.lease_expires_at.is_(None))
            | (PaymentOperation.lease_expires_at < now)
            | (PaymentOperation.worker_id == worker_id),
        )
        .values(worker_id=worker_id, lease_expires_at=now + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    op.worker_id = worker_id
    op.lease_expires_at = now + timedelta(seconds=seconds)
    return True


def release_lease(db: Session, op: PaymentOperation) -> None:
    db.execute(
        update(PaymentOperation)
        .where(PaymentOperation.id == op.id)
        .values(worker_id=None, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    op.worker_id = None
    op.lease_expires_at = None


def incomplete_operations(
    db: Session, *, older_than: datetime | None = None, limit: int = 100
) -> list[PaymentOperation]:
    stmt = (
        select(PaymentOperation)
        .where(PaymentOperation.state.not_in(TERMINAL_OPERATION_STATES))
        .order_by(PaymentOperation.id)
        .limit(limit)
    )
    if older_than is not None:
        stmt = stmt.where(PaymentOperation.updated_at <= older_than)
    return list(db.scalars(stmt))
