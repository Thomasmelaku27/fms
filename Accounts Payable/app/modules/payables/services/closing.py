"""AP period close: locking strategy, attestations, forced close.

Locking strategy
----------------
*PostgreSQL*: a closing transaction takes an **exclusive** advisory lock
(``pg_advisory_xact_lock(AP_CLOSE_LOCK)``) before it validates; every service that
writes a dated accounting effect takes the **shared** lock
(``pg_advisory_xact_lock_shared``) in :func:`common.assert_period_open`. So the
close waits for in-flight dated writers to commit (and then sees their rows during
validation), and dated writers that start after the close began wait until the
close commits -- at which point the period lock row exists and their trigger /
service check refuses the date. Validation and the lock insert happen in one
transaction: nothing can slip in between.

*SQLite*: one writer at a time. The close acquires the write lock at the very start
(a no-op write statement) so validation reads and the lock insert happen while every
other writer is blocked; competing close requests serialise on the same lock.

Outcomes
--------
A close writes an immutable :class:`APCloseAttestation` in every case:
``closed`` (validation clean), ``forced`` (blockers overridden by an authorised
actor, the exact exceptions preserved) or ``failed`` (blockers found, period state
unchanged). Fault point ``close.before_commit`` lets tests crash between the lock
insert and the commit to prove nothing partial survives.

Close packages (Turn 5)
-----------------------
Every attestation carries a ``content_hash`` and the ``obligations_json`` list of
gateway-confirmed transfers not in the books at the cutoff; a forced close writes one
immutable :class:`APCloseException` per accepted blocker item, resolved once later
(:func:`resolve_close_exception`). :func:`verify_close` re-derives the package from
AP rows and posted GL history: hash, source fingerprint, replayed liability, the
obligations' state today (verified through ``services.verification``) and the
exceptions' resolutions. Direct writes bypassing the advisory lock are caught by the
period-lock triggers (dated rows) and by these verifications (attested content).
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    APCloseAttestation,
    APCloseException,
    APPeriodLock,
    OperationState,
    PaymentOperation,
)
from app.modules.payables.schemas import PeriodValidationResult
from app.modules.payables.services.common import (
    Conflict,
    NotFound,
    ValidationFailed,
    latest_ap_lock,
    utcnow,
)
from app.modules.payables.services.reconciliation import reconciliation_report, vendor_events
from app.modules.payables.services.settlement import vendor_balances
from app.modules.payables.services.validation import BLOCKER, validate_period

AP_CLOSE_LOCK = 0x41505F434C4F5345  # 'AP_CLOSE'


def acquire_close_lock(db: Session) -> None:
    """Exclusive close lock (see module docstring). Must be called first in the transaction."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AP_CLOSE_LOCK})
    else:
        # Any write statement takes SQLite's RESERVED lock for the rest of the transaction.
        db.execute(text("UPDATE ap_period_locks SET closed_by = closed_by WHERE id < 0"))


def acquire_dated_write_lock(db: Session) -> None:
    """Shared lock taken by dated accounting writes (PostgreSQL only; SQLite serialises writers)."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": AP_CLOSE_LOCK})


def source_fingerprint(db: Session, period_end: date) -> str:
    """Hash of every subledger event dated on or before the cutoff (what the close attests to)."""
    events = [e for e in vendor_events(db) if e["date"] <= period_end]
    payload = [
        (
            e["vendor_id"],
            e["date"].isoformat(),
            e["type"],
            e["document_id"],
            f"{e['amount']:.2f}",
            e["journal_entry_id"],
        )
        for e in events
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _attest(
    db: Session,
    *,
    period_end: date,
    outcome: str,
    actor: str,
    forced: bool,
    force_reason: str | None,
    result: PeriodValidationResult,
    lock_row: APPeriodLock | None,
    gl_reference: str | None,
) -> APCloseAttestation:
    recon = reconciliation_report(db, as_of=period_end)
    balances = vendor_balances(db, as_of=period_end)
    liability = sum((b.bills_outstanding for b in balances), Decimal("0.00"))
    attestation = APCloseAttestation(
        period_end=period_end,
        outcome=outcome,
        attempted_by=actor,
        forced=forced,
        force_reason=force_reason,
        liability_balance=liability,
        open_bill_count=sum(b.open_bill_count for b in balances),
        vendor_count=sum(
            1
            for b in balances
            if b.bills_outstanding or b.unapplied_credits or b.unapplied_advances
        ),
        checks_json=json.dumps(
            [
                {"code": c.code, "severity": c.severity, "count": c.count, "items": c.items[:50]}
                for c in result.checks
            ],
            default=str,
        )[:8000],
        exceptions_json=json.dumps([e.model_dump(mode="json") for e in recon.exceptions])[:8000],
        reconciliation_json=json.dumps(
            {
                "total_ledger": f"{recon.total_ledger:.2f}",
                "total_subledger": f"{recon.total_subledger:.2f}",
                "total_difference": f"{recon.total_difference:.2f}",
                "rows": [r.model_dump(mode="json") for r in recon.rows],
            }
        )[:4000],
        source_fingerprint=source_fingerprint(db, period_end),
        gl_period_reference=gl_reference,
        lock_id=lock_row.id if lock_row else None,
        obligations_json=json.dumps([o.model_dump(mode="json") for o in recon.pending_obligations])[
            :8000
        ],
    )
    attestation.content_hash = attestation_content_hash(attestation)
    db.add(attestation)
    db.flush()
    if forced:
        # Every blocker item the actor accepted becomes its own immutable exception record.
        for check in result.checks:
            if check.severity != BLOCKER or not check.count:
                continue
            for item in check.items:
                db.add(
                    APCloseException(
                        attestation_id=attestation.id,
                        period_end=period_end,
                        code=check.code,
                        severity=check.severity,
                        item_json=json.dumps(item, default=str)[:2000],
                        accepted_by=actor,
                        accept_reason=(force_reason or "")[:500],
                    )
                )
        db.flush()
    return attestation


def attestation_content_hash(attestation: APCloseAttestation) -> str:
    """sha256 over the attested content (mirrored in migration fmspayables06)."""
    normal = {
        "period_end": attestation.period_end.isoformat(),
        "outcome": attestation.outcome,
        "attempted_by": attestation.attempted_by,
        "forced": bool(attestation.forced),
        "force_reason": attestation.force_reason,
        "liability_balance": f"{Decimal(str(attestation.liability_balance)):.2f}",
        "open_bill_count": attestation.open_bill_count,
        "vendor_count": attestation.vendor_count,
        "checks_json": attestation.checks_json,
        "exceptions_json": attestation.exceptions_json,
        "reconciliation_json": attestation.reconciliation_json,
        "source_fingerprint": attestation.source_fingerprint,
        "gl_period_reference": attestation.gl_period_reference,
        "lock_id": attestation.lock_id,
        "obligations_json": attestation.obligations_json,
    }
    return hashlib.sha256(
        json.dumps(normal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def close_period(
    db: Session,
    *,
    period_end: date,
    actor: str,
    note: str | None,
    integrations: Integrations,
    force: bool = False,
    force_reason: str | None = None,
    gl_period_reference: str | None = None,
) -> APPeriodLock:
    """Close AP through ``period_end`` under the close lock; always leaves an attestation.

    Transaction: [close lock] -> validate -> (failed attestation, commit, 422) |
    (period lock + attestation, notify GL port, commit). A failure before the commit
    rolls everything back: the previous period state is untouched.
    """
    acquire_close_lock(db)
    latest = latest_ap_lock(db)
    if latest is not None and period_end <= latest:
        db.rollback()
        raise Conflict(f"AP is already closed through {latest.isoformat()}")
    result = validate_period(db, period_end, gateway=integrations.gateway)
    blockers = [c for c in result.checks if c.severity == BLOCKER and c.count]
    if blockers and not force:
        _attest(
            db,
            period_end=period_end,
            outcome="failed",
            actor=actor,
            forced=False,
            force_reason=None,
            result=result,
            lock_row=None,
            gl_reference=gl_period_reference,
        )
        db.commit()
        raise ValidationFailed(
            f"AP period {period_end} cannot be closed: "
            + "; ".join(f"{c.code} ({c.count})" for c in blockers)
        )
    if blockers and force:
        if not force_reason:
            db.rollback()
            raise ValidationFailed("A forced close requires a reason")
        if not integrations.authorization.authorize(
            actor, "payables.period.force_close", f"ap_period:{period_end}"
        ):
            db.rollback()
            raise ValidationFailed(f"Actor {actor!r} is not authorised to force a period close")
    summary = {
        "status": result.status,
        "forced": bool(blockers and force),
        "checks": {c.code: c.count for c in result.checks},
    }
    lock_row = APPeriodLock(
        closed_through=period_end,
        closed_by=actor,
        note=note,
        validation_summary=json.dumps(summary)[:4000],
    )
    db.add(lock_row)
    db.flush()
    _attest(
        db,
        period_end=period_end,
        outcome="forced" if blockers else "closed",
        actor=actor,
        forced=bool(blockers),
        force_reason=force_reason if blockers else None,
        result=result,
        lock_row=lock_row,
        gl_reference=gl_period_reference,
    )
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.period.closed" if not blockers else "payables.period.force_closed",
        resource=f"ap_period_lock:{lock_row.id}",
    )
    integrations.periods.notify_closed(db, closed_through=period_end, summary=summary)
    integrations.faults.check("close.before_commit")
    db.commit()
    db.refresh(lock_row)
    return lock_row


def list_attestations(db: Session, *, period_end: date | None = None) -> list[APCloseAttestation]:
    stmt = select(APCloseAttestation).order_by(APCloseAttestation.id)
    if period_end is not None:
        stmt = stmt.where(APCloseAttestation.period_end == period_end)
    return list(db.scalars(stmt))


# --- close package verification -----------------------------------------------------------------------


@dataclass
class CloseFinding:
    code: str
    severity: str
    attestation_id: int
    period_end: date
    expected: str | None
    actual: str | None
    remediation: str
    detail: str = ""


@dataclass
class CloseVerification:
    attestation_id: int
    period_end: date
    outcome: str
    content_hash_verified: bool
    source_fingerprint_verified: bool
    liability_verified: bool
    obligations: list[dict]
    exceptions: list[dict]
    findings: list[CloseFinding]

    @property
    def verified(self) -> bool:
        return not any(f.severity in ("critical", "error") for f in self.findings)

    def as_dict(self) -> dict:
        return {
            "attestation_id": self.attestation_id,
            "period_end": self.period_end,
            "outcome": self.outcome,
            "verified": self.verified,
            "content_hash_verified": self.content_hash_verified,
            "source_fingerprint_verified": self.source_fingerprint_verified,
            "liability_verified": self.liability_verified,
            "obligations": self.obligations,
            "exceptions": self.exceptions,
            "findings": [asdict(f) for f in self.findings],
        }


def verify_close(db: Session, attestation_id: int, *, gateway=None) -> CloseVerification:
    """Re-derive a close package from authoritative AP rows and posted GL history.

    Checks: the attestation's content hash (tampering), the source fingerprint of the
    subledger events dated on or before the cutoff (backdated changes after the close),
    the attested liability against an independent replay, the state today of every
    obligation the close carried (confirmed transfers not recorded at the time), and the
    accepted exceptions of a forced close against their resolutions.
    """
    from app.modules.payables.services import reconstruction, verification

    attestation = db.get(APCloseAttestation, attestation_id)
    if attestation is None:
        raise NotFound("Close attestation not found")
    period_end = attestation.period_end
    findings: list[CloseFinding] = []

    def add(code, severity, *, expected=None, actual=None, remediation, detail=""):
        findings.append(
            CloseFinding(
                code, severity, attestation.id, period_end, expected, actual, remediation, detail
            )
        )

    expected_hash = attestation_content_hash(attestation)
    hash_ok = attestation.content_hash == expected_hash
    if attestation.content_hash is None:
        add(
            "attestation_unhashed",
            "warning",
            remediation="attestation predates the content hash; only its rows were verified",
        )
        hash_ok = True
    elif not hash_ok:
        add(
            "attestation_content_modified",
            "critical",
            expected=expected_hash[:16],
            actual=(attestation.content_hash or "")[:16],
            remediation="the close record was altered after it was written; investigate (append-only guard bypassed)",
        )
    current_fingerprint = source_fingerprint(db, period_end)
    fingerprint_ok = current_fingerprint == attestation.source_fingerprint
    if not fingerprint_ok:
        add(
            "subledger_changed_after_close",
            "critical" if attestation.outcome != "failed" else "info",
            expected=attestation.source_fingerprint[:16],
            actual=current_fingerprint[:16],
            remediation="dated AP rows on or before the cutoff differ from what was attested (backdated write after the close?)",
        )
    replay = reconstruction.replay(db, period_end)
    liability = sum(
        (max(v, Decimal("0")) for v in replay.bill_outstanding.values()), Decimal("0.00")
    )
    liability_ok = liability == Decimal(str(attestation.liability_balance))
    if not liability_ok:
        add(
            "liability_differs_from_replay",
            "critical" if attestation.outcome != "failed" else "info",
            expected=f"{Decimal(str(attestation.liability_balance)):.2f}",
            actual=f"{liability:.2f}",
            remediation="the attested AP liability no longer matches an independent replay of the cutoff",
        )
    # Obligations carried by the close: where are they now?
    obligations = []
    for item in json.loads(attestation.obligations_json or "[]"):
        op = db.scalar(
            select(PaymentOperation).where(PaymentOperation.operation_uid == item["operation_uid"])
        )
        now = {
            "operation_uid": item["operation_uid"],
            "amount": item["amount"],
            "attested_state": item["state"],
        }
        if op is None:
            now["current_state"] = "missing"
            add(
                "obligation_missing",
                "critical",
                actual=item["operation_uid"],
                remediation="an obligation recorded at close no longer exists",
            )
        else:
            now["current_state"] = op.state.value
            now["accounting_status"] = op.accounting_status
            if op.state == OperationState.COMPLETED:
                v = verification.verify_operation(db, op, gateway=gateway)
                now["verified"] = v.completion_policy_satisfied
                if not v.completion_policy_satisfied:
                    add(
                        "obligation_completed_without_evidence",
                        "error",
                        actual=op.operation_uid,
                        remediation="the obligation was completed but its evidence does not verify",
                    )
            else:
                now["verified"] = False
                add(
                    "obligation_outstanding",
                    "warning",
                    actual=f"{op.operation_uid} {op.state.value}",
                    remediation="a transfer confirmed before the close is still not in the books",
                )
        obligations.append(now)
    exceptions = [
        {
            "id": e.id,
            "code": e.code,
            "status": e.status,
            "accepted_by": e.accepted_by,
            "accept_reason": e.accept_reason,
            "resolved_by": e.resolved_by,
            "resolution_reference": e.resolution_reference,
            "item": json.loads(e.item_json),
        }
        for e in attestation.exceptions
    ]
    if attestation.outcome == "forced" and not exceptions:
        add(
            "forced_close_without_exceptions",
            "error",
            remediation="a forced close must list the blockers it accepted",
        )
    for e in exceptions:
        if e["status"] == "accepted":
            add(
                "exception_unresolved",
                "warning",
                actual=f"{e['code']} #{e['id']}",
                remediation="an exception accepted at close has not been resolved",
            )
    return CloseVerification(
        attestation_id=attestation.id,
        period_end=period_end,
        outcome=attestation.outcome,
        content_hash_verified=hash_ok,
        source_fingerprint_verified=fingerprint_ok,
        liability_verified=liability_ok,
        obligations=obligations,
        exceptions=exceptions,
        findings=findings,
    )


def resolve_close_exception(
    db: Session,
    exception_id: int,
    *,
    actor: str,
    note: str,
    reference: str | None,
    integrations: Integrations,
) -> APCloseException:
    """Record, once, how an accepted exception was resolved (the record itself never changes)."""
    exc = db.get(APCloseException, exception_id)
    if exc is None:
        raise NotFound("Close exception not found")
    if exc.status == "resolved":
        raise Conflict("This exception is already resolved")
    if not integrations.authorization.authorize(
        actor, "payables.period.resolve_exception", f"ap_close_exception:{exc.id}"
    ):
        raise ValidationFailed(f"Actor {actor!r} is not authorised to resolve close exceptions")
    exc.status = "resolved"
    exc.resolved_by = actor
    exc.resolved_at = utcnow()
    exc.resolution_note = note[:1000]
    exc.resolution_reference = (reference or None) and reference[:200]
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.period.exception_resolved",
        resource=f"ap_close_exception:{exc.id}",
    )
    db.commit()
    db.refresh(exc)
    return exc


def list_close_exceptions(
    db: Session, *, attestation_id: int | None = None
) -> list[APCloseException]:
    stmt = select(APCloseException).order_by(APCloseException.id)
    if attestation_id is not None:
        stmt = stmt.where(APCloseException.attestation_id == attestation_id)
    return list(db.scalars(stmt))
