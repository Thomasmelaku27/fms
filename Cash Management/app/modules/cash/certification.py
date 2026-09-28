"""Certified reconciliation evidence.

When a session is closed the approved result is frozen into a
``ReconciliationCertification``: a canonical JSON document (figures, every
match, the bank transactions and posted ledger lines in scope, discrepancies,
notes) plus its SHA-256. The row is append-only in the database and chained
to the previous certification's hash, so the history of re-certifications is
tamper-evident.

Reading back a closed session never recomputes "the" result: the certified
document is what was approved. The live state is still computed and anything
that differs from the certification (late statement imports, back-dated
postings, ...) is reported separately as *drift*, never as a change to the
certified figures.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JournalEntry, JournalLine
from app.modules.cash.models import (
    BankTransaction,
    ReconciliationCertification,
    ReconciliationMatch,
    ReconciliationReopenEvent,
    ReconciliationSession,
    ReconciliationStatus,
)

SCHEMA = "fms.cash.reconciliation-certification/1"

FIGURE_KEYS = (
    "bank_transactions_count",
    "bank_transactions_total",
    "matched_bank_total",
    "unmatched_bank_total",
    "unmatched_bank_count",
    "partially_matched_bank_count",
    "cleared_balance",
    "difference",
    "ledger_balance_at_period_end",
    "outstanding_ledger_count",
    "outstanding_ledger_total",
    "adjusted_bank_balance",
    "adjusted_ledger_balance",
    "unexplained_difference",
    "is_balanced",
)


def _json_default(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not serialisable: {type(value).__name__}")


def canonical_json(content: dict) -> str:
    """Deterministic serialisation: sorted keys, no whitespace, ASCII only."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_json_default)


def content_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


@dataclass
class Review:
    """Everything the summary is built from (live, never stored)."""

    figures: dict
    bank_rows: list[dict]  # in period, with matched/remaining amounts
    ledger_rows: list[dict]  # posted lines up to period end, with matched/remaining amounts
    discrepancies: list[dict] = field(default_factory=list)


def match_records(db: Session, session_id: int) -> list[dict]:
    rows = db.execute(
        select(ReconciliationMatch, BankTransaction, JournalLine, JournalEntry)
        .join(BankTransaction, BankTransaction.id == ReconciliationMatch.bank_transaction_id)
        .join(JournalLine, JournalLine.id == ReconciliationMatch.journal_line_id)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(ReconciliationMatch.session_id == session_id)
        .order_by(ReconciliationMatch.id)
    ).all()
    return [
        {
            "match_id": m.id,
            "amount": m.amount,
            "bank_transaction_id": bt.id,
            "bank_fingerprint": bt.fingerprint,
            "transaction_date": bt.transaction_date,
            "bank_amount": bt.amount,
            "journal_line_id": jl.id,
            "journal_entry_id": je.id,
            "entry_date": je.entry_date,
            "entry_status": je.status,
            "debit": jl.debit,
            "credit": jl.credit,
        }
        for m, bt, jl, je in rows
    ]


def build_content(
    db: Session,
    session: ReconciliationSession,
    ledger_account_id: int,
    review: Review,
    *,
    version: int,
    previous_hash: str | None,
    certified_at: datetime,
    forced: bool,
    notes: str | None,
) -> dict:
    return {
        "schema": SCHEMA,
        "version": version,
        "previous_hash": previous_hash,
        "certified_at": certified_at,
        "forced": forced,
        "notes": notes,
        "session": {
            "id": session.id,
            "cash_account_id": session.cash_account_id,
            "ledger_account_id": ledger_account_id,
            "statement_id": session.statement_id,
            "period_start": session.period_start,
            "period_end": session.period_end,
            "opening_balance": session.opening_balance,
            "closing_balance": session.closing_balance,
        },
        "figures": {k: review.figures[k] for k in FIGURE_KEYS},
        "matches": match_records(db, session.id),
        "bank_transactions": [
            {
                "id": r["id"],
                "fingerprint": r["fingerprint"],
                "transaction_date": r["transaction_date"],
                "amount": r["amount"],
                "matched_amount": r["matched_amount"],
            }
            for r in review.bank_rows
        ],
        "ledger_lines": [
            {
                "journal_line_id": r["journal_line_id"],
                "journal_entry_id": r["journal_entry_id"],
                "entry_date": r["entry_date"],
                "debit": r["debit"],
                "credit": r["credit"],
                "matched_amount": r["matched_amount"],
            }
            for r in review.ledger_rows
        ],
        "discrepancies": review.discrepancies,
    }


def latest_certification(db: Session, session_id: int) -> ReconciliationCertification | None:
    return db.scalar(
        select(ReconciliationCertification)
        .where(ReconciliationCertification.session_id == session_id)
        .order_by(ReconciliationCertification.version.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class SessionState:
    """Fresh, authoritative certification-cycle state of a session (never cached)."""

    status: ReconciliationStatus
    reopen_count: int
    last_reopen_event_id: int | None


def session_state(db: Session, session_id: int) -> SessionState:
    row = db.execute(
        select(
            ReconciliationSession.status,
            ReconciliationSession.reopen_count,
            ReconciliationSession.last_reopen_event_id,
        ).where(ReconciliationSession.id == session_id)
    ).one()
    return SessionState(*row)


def effective_event_ids(db: Session, session_id: int, state: SessionState | None = None) -> set[int]:
    """Events that actually completed a reopening.

    Purely structural: the session's own pointer plus every event a later
    certification names as the start of its cycle. Stray events (partial
    processes, raw SQL) are never in this set, whatever their number.
    """
    state = state or session_state(db, session_id)
    ids = set(
        db.scalars(
            select(ReconciliationCertification.reopen_event_id).where(
                ReconciliationCertification.session_id == session_id,
                ReconciliationCertification.reopen_event_id.is_not(None),
            )
        )
    )
    if state.last_reopen_event_id is not None:
        ids.add(state.last_reopen_event_id)
    return ids


def superseded_certification_ids(db: Session, session_id: int, state: SessionState | None = None) -> set[int]:
    ids = effective_event_ids(db, session_id, state)
    if not ids:
        return set()
    return set(
        db.scalars(
            select(ReconciliationReopenEvent.certification_id).where(
                ReconciliationReopenEvent.id.in_(ids)
            )
        )
    )


def is_superseded(db: Session, certification: ReconciliationCertification) -> bool:
    """True once a *completed* reopening was recorded for this certification."""
    return certification.id in superseded_certification_ids(db, certification.session_id)


def active_certification(db: Session, session_id: int) -> ReconciliationCertification | None:
    """The latest certification while the session is closed and it is not superseded."""
    state = session_state(db, session_id)
    if state.status != ReconciliationStatus.CLOSED:
        return None
    latest = latest_certification(db, session_id)
    if latest is None or latest.id in superseded_certification_ids(db, session_id, state):
        return None
    return latest


def orphaned_certification(db: Session, session_id: int) -> ReconciliationCertification | None:
    """A not-superseded latest certification although the session is open.

    Left behind when a close was committed half-way (certification row written,
    status never flipped). ``close_session`` adopts it when it matches the
    current state, otherwise ``recover_session`` completes the close so the
    normal reopen -> recertify path becomes available again.
    """
    state = session_state(db, session_id)
    if state.status == ReconciliationStatus.CLOSED:
        return None
    latest = latest_certification(db, session_id)
    if latest is not None and latest.id not in superseded_certification_ids(db, session_id, state):
        return latest
    return None


def verify(db: Session, certification: ReconciliationCertification) -> list[str]:
    """Recompute the evidence from what is stored; return every inconsistency found."""
    problems: list[str] = []
    recomputed = content_hash(certification.content)
    if recomputed != certification.content_hash:
        problems.append("content hash does not match the stored content")
    try:
        content = json.loads(certification.content)
    except ValueError:
        return problems + ["stored content is not valid JSON"]
    if content.get("schema") != SCHEMA:
        problems.append(f"unexpected schema {content.get('schema')!r}")
    if canonical_json(content) != certification.content:
        problems.append("stored content is not in canonical form")
    figures = content.get("figures", {})
    column_checks = {
        "difference": f"{certification.difference:.2f}",
        "cleared_balance": f"{certification.cleared_balance:.2f}",
        "ledger_balance_at_period_end": f"{certification.ledger_balance:.2f}",
        "is_balanced": certification.is_balanced,
    }
    for key, expected in column_checks.items():
        if figures.get(key) != expected:
            problems.append(f"column {key} ({expected}) differs from the certified content ({figures.get(key)})")
    if len(content.get("matches", [])) != certification.matches_count:
        problems.append("matches_count differs from the certified match list")
    if content.get("version") != certification.version:
        problems.append("version differs from the certified content")
    if content.get("session", {}).get("id") != certification.session_id:
        problems.append("certified content belongs to another session")
    if content.get("previous_hash") != certification.previous_hash:
        problems.append("previous_hash differs from the certified content")
    if certification.version > 1:
        previous = db.scalar(
            select(ReconciliationCertification).where(
                ReconciliationCertification.session_id == certification.session_id,
                ReconciliationCertification.version == certification.version - 1,
            )
        )
        if previous is None:
            problems.append("previous certification is missing")
        elif previous.content_hash != certification.previous_hash:
            problems.append("hash chain is broken: previous_hash does not match the previous certification")
    elif certification.previous_hash is not None:
        problems.append("first certification must not have a previous hash")
    return problems


def drift(db: Session, certification: ReconciliationCertification, live: Review) -> list[dict]:
    """Differences between the certified document and the current state, itemised."""
    try:
        content = json.loads(certification.content)
        if not isinstance(content, dict) or content.get("schema") != SCHEMA:
            raise ValueError("not a certification document")
        content.setdefault("figures", {})
        for key in ("bank_transactions", "ledger_lines", "matches"):
            content.setdefault(key, [])
    except ValueError as exc:
        return [
            {
                "kind": "certified_content_unreadable",
                "message": f"Certified content of version {certification.version} cannot be compared: {exc}; "
                "verification flags this certification",
            }
        ]
    out: list[dict] = []

    certified_figures = content["figures"]
    live_figures = {k: _json_default(v) if isinstance(v, (Decimal, date, Enum)) else v for k, v in live.figures.items() if k in FIGURE_KEYS}
    for key in FIGURE_KEYS:
        if certified_figures.get(key) != live_figures.get(key):
            out.append(
                {
                    "kind": "certified_figure_changed",
                    "message": f"{key}: certified {certified_figures.get(key)}, live {live_figures.get(key)}",
                    "amount": _delta(certified_figures.get(key), live_figures.get(key)),
                }
            )

    certified_bank = {b["id"]: b for b in content["bank_transactions"]}
    live_bank = {r["id"]: r for r in live.bank_rows}
    for tid in sorted(set(live_bank) - set(certified_bank)):
        r = live_bank[tid]
        out.append(
            {
                "kind": "late_bank_transaction",
                "message": f"Bank transaction {r['description']!r} dated {r['transaction_date'].isoformat()} "
                f"({r['amount']:.2f}) was imported after the session was certified",
                "amount": r["amount"],
                "bank_transaction_id": tid,
            }
        )
    for tid in sorted(set(certified_bank) - set(live_bank)):
        b = certified_bank[tid]
        out.append(
            {
                "kind": "certified_bank_transaction_missing",
                "message": f"Certified bank transaction {tid} ({b['amount']}) no longer exists in the period",
                "amount": Decimal(b["amount"]),
                "bank_transaction_id": tid,
            }
        )
    for tid in sorted(set(certified_bank) & set(live_bank)):
        if certified_bank[tid]["matched_amount"] != f"{live_bank[tid]['matched_amount']:.2f}":
            out.append(
                {
                    "kind": "bank_transaction_matching_changed",
                    "message": f"Bank transaction {tid}: certified matched {certified_bank[tid]['matched_amount']}, "
                    f"live {live_bank[tid]['matched_amount']:.2f}",
                    "bank_transaction_id": tid,
                }
            )

    certified_lines = {l["journal_line_id"]: l for l in content["ledger_lines"]}
    live_lines = {r["journal_line_id"]: r for r in live.ledger_rows}
    for lid in sorted(set(live_lines) - set(certified_lines)):
        r = live_lines[lid]
        out.append(
            {
                "kind": "late_ledger_line",
                "message": f"Posted line of entry #{r['journal_entry_id']} dated {r['entry_date'].isoformat()} "
                f"({r['signed_amount']:.2f}) was posted after the session was certified",
                "amount": r["signed_amount"],
                "journal_line_id": lid,
            }
        )
    for lid in sorted(set(certified_lines) - set(live_lines)):
        out.append(
            {
                "kind": "certified_ledger_line_missing",
                "message": f"Certified posted line {lid} no longer exists up to the period end",
                "journal_line_id": lid,
            }
        )

    certified_matches = {(m["match_id"], m["amount"]) for m in content["matches"]}
    live_matches = {(m["match_id"], f"{m['amount']:.2f}") for m in match_records(db, certification.session_id)}
    if certified_matches != live_matches:
        out.append(
            {
                "kind": "matches_changed",
                "message": f"{len(certified_matches)} certified match(es) vs {len(live_matches)} live; "
                "the certified match list is authoritative",
            }
        )
    return out


def _delta(certified, live):
    try:
        return Decimal(str(live)) - Decimal(str(certified))
    except (ArithmeticError, TypeError, ValueError):
        return None


def event_state(event: ReconciliationReopenEvent, state: SessionState, effective: set[int]) -> str:
    if event.id in effective:
        return "effective"
    if event.reopen_number > state.reopen_count:
        return "orphaned"  # recorded, but the reopening it asked for never completed
    return "superseded"  # another event completed that reopening


def history(db: Session, session_id: int) -> list[dict]:
    """Chronological certification / reopen events of a session (all of them)."""
    state = session_state(db, session_id)
    effective = effective_event_ids(db, session_id, state)
    superseded_certs = superseded_certification_ids(db, session_id, state)
    certifications = list(
        db.scalars(
            select(ReconciliationCertification)
            .where(ReconciliationCertification.session_id == session_id)
            .order_by(ReconciliationCertification.version)
        )
    )
    events_by_cert: dict[int, list[ReconciliationReopenEvent]] = {}
    for e in db.scalars(
        select(ReconciliationReopenEvent)
        .where(ReconciliationReopenEvent.session_id == session_id)
        .order_by(ReconciliationReopenEvent.reopen_number, ReconciliationReopenEvent.id)
    ):
        events_by_cert.setdefault(e.certification_id, []).append(e)

    latest_id = certifications[-1].id if certifications else None
    events: list[dict] = []
    for cert in certifications:
        superseded = cert.id in superseded_certs
        if superseded:
            cert_state = "superseded"
        elif cert.id == latest_id and state.status == ReconciliationStatus.CLOSED:
            cert_state = "active"
        else:
            cert_state = "orphaned"
        events.append(
            {
                "event": "certified",
                "at": cert.certified_at,
                "certification_id": cert.id,
                "version": cert.version,
                "content_hash": cert.content_hash,
                "previous_hash": cert.previous_hash,
                "is_balanced": cert.is_balanced,
                "forced": cert.forced,
                "difference": cert.difference,
                "notes": cert.notes,
                "superseded": superseded,
                "reason": None,
                "event_id": None,
                "reopen_number": None,
                "state": cert_state,
            }
        )
        for ev in events_by_cert.get(cert.id, []):
            events.append(
                {
                    "event": "reopened",
                    "at": ev.reopened_at,
                    "certification_id": cert.id,
                    "version": cert.version,
                    "content_hash": None,
                    "previous_hash": None,
                    "is_balanced": None,
                    "forced": None,
                    "difference": None,
                    "notes": None,
                    "superseded": ev.id not in effective,
                    "reason": ev.reason,
                    "event_id": ev.id,
                    "reopen_number": ev.reopen_number,
                    "state": event_state(ev, state, effective),
                }
            )
    return events


def integrity(db: Session, session_id: int) -> dict:
    """Check the certification cycle of a session.

    * ``problems`` - the *current* state is invalid or evidence is unverifiable
      (half-done close, altered rows, broken hash chain). The session may
      need ``recover``; ``consistent`` is False.
    * ``anomalies`` - irregularities in the *history* (e.g. a database migrated
      from cashmgmt02 with reopen events that never completed, or
      certifications without a completed reopening in between). They are
      preserved as they are, do not affect the current state and block nothing.
    * orphaned / superseded reopen events are tolerated and only listed.
    """
    state = session_state(db, session_id)
    certs = list(
        db.scalars(
            select(ReconciliationCertification)
            .where(ReconciliationCertification.session_id == session_id)
            .order_by(ReconciliationCertification.version)
        )
    )
    events = list(
        db.scalars(
            select(ReconciliationReopenEvent)
            .where(ReconciliationReopenEvent.session_id == session_id)
            .order_by(ReconciliationReopenEvent.id)
        )
    )
    effective = effective_event_ids(db, session_id, state)
    superseded_certs = superseded_certification_ids(db, session_id, state)
    problems: list[str] = []
    anomalies: list[str] = []
    by_id = {c.id: c for c in certs}
    by_event_id = {e.id: e for e in events}

    if [c.version for c in certs] != list(range(1, len(certs) + 1)):
        anomalies.append("certification versions are not contiguous")
    latest = certs[-1] if certs else None
    active = None
    orphaned_cert = None
    if state.status == ReconciliationStatus.CLOSED:
        if latest is None:
            problems.append("session is closed but has no certification")
        elif latest.id in superseded_certs:
            problems.append(
                f"session is closed but its latest certification (version {latest.version}) was "
                "superseded by a completed reopening"
            )
        else:
            active = latest
    elif latest is not None and latest.id not in superseded_certs:
        orphaned_cert = latest
        problems.append(
            f"session is open but certification version {latest.version} has not been superseded "
            "(a close was committed half-way); close again to adopt it if it still matches, or "
            "call recover to complete the close and then reopen with a reason"
        )
    if state.reopen_count and state.last_reopen_event_id is None:
        anomalies.append(
            f"reopen_count is {state.reopen_count} but no event completed the latest reopening "
            "(history predates cashmgmt03 or was altered)"
        )
    if state.last_reopen_event_id is not None:
        ev = by_event_id.get(state.last_reopen_event_id)
        if ev is None:
            problems.append("last_reopen_event_id points at an event of another session or a missing one")
        elif ev.reopen_number != state.reopen_count:
            anomalies.append(
                f"last completed reopening is event {ev.id} (number {ev.reopen_number}) while "
                f"reopen_count is {state.reopen_count}"
            )
    for cert in certs:
        for problem in verify(db, cert):
            problems.append(f"certification version {cert.version}: {problem}")
        if cert.version == 1:
            if cert.reopen_event_id is not None:
                anomalies.append("certification version 1 references a reopen event")
            continue
        ev = by_event_id.get(cert.reopen_event_id) if cert.reopen_event_id else None
        previous = certs[certs.index(cert) - 1]
        if ev is None:
            anomalies.append(
                f"certification version {cert.version} is not linked to a completed reopening of "
                f"version {previous.version}"
            )
        elif ev.certification_id != previous.id:
            anomalies.append(
                f"certification version {cert.version} is linked to a reopening of another version"
            )
    for event in events:
        cert = by_id.get(event.certification_id)
        if cert is None:
            problems.append(f"reopen event {event.id} references a certification of another session or a missing one")
        elif event.reopen_number != cert.version and event.id in effective:
            anomalies.append(
                f"effective reopen event {event.id} has number {event.reopen_number} but supersedes "
                f"version {cert.version}"
            )
    if len(effective) > state.reopen_count:
        anomalies.append(
            f"{len(effective)} completed reopening(s) are recorded but reopen_count is "
            f"{state.reopen_count} (history predates cashmgmt03 or was altered)"
        )
    numbers = sorted(by_event_id[i].reopen_number for i in effective if i in by_event_id)
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        anomalies.append(f"completed reopenings carry numbers {numbers} instead of 1..{len(numbers)}")
    orphaned = sorted(e.id for e in events if event_state(e, state, effective) == "orphaned")
    superseded = sorted(e.id for e in events if event_state(e, state, effective) == "superseded")
    return {
        "status": state.status,
        "reopen_count": state.reopen_count,
        "active_certification_version": active.version if active else None,
        "consistent": not problems,
        "problems": problems,
        "anomalies": anomalies,
        "orphaned_reopen_event_ids": orphaned,
        "superseded_reopen_event_ids": superseded,
        "orphaned_certification_version": orphaned_cert.version if orphaned_cert else None,
        "certifications": len(certs),
        "reopen_events": len(events),
    }


def certification_view(db: Session, cert: ReconciliationCertification) -> dict:
    problems = verify(db, cert)
    active = active_certification(db, cert.session_id)
    return {
        "id": cert.id,
        "session_id": cert.session_id,
        "version": cert.version,
        "certified_at": cert.certified_at,
        "content_hash": cert.content_hash,
        "previous_hash": cert.previous_hash,
        "is_balanced": cert.is_balanced,
        "forced": cert.forced,
        "difference": cert.difference,
        "cleared_balance": cert.cleared_balance,
        "ledger_balance": cert.ledger_balance,
        "matches_count": cert.matches_count,
        "notes": cert.notes,
        "reopen_event_id": cert.reopen_event_id,
        "active": active is not None and active.id == cert.id,
        "verified": not problems,
        "verification_problems": problems,
    }
