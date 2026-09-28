"""Bank reconciliation: sessions, matches, review, close and reopen.

A session reconciles one cash account for a statement period (opening and
closing balance taken from an imported statement or entered manually).
Matches link bank transactions to *posted* journal lines of the linked ledger
account, possibly partially (an amount below either side's total).

Review figures (``summary``), all derived and never stored:

    cleared_balance        = opening + sum of matched bank movements in the period
    difference             = closing - cleared_balance
                             (0 once every bank transaction is explained by the ledger)
    ledger_balance         = posted ledger balance as of period end
    outstanding ledger     = posted lines up to period end not (fully) matched
    adjusted_bank_balance  = closing + outstanding ledger items
    adjusted_ledger_balance= ledger_balance + unmatched bank items
    unexplained_difference = adjusted_bank_balance - adjusted_ledger_balance

Nothing here posts to the ledger or changes balances. Concurrency: every
write takes the cash-account lock (see ``services.lock_cash_account``) and
status transitions are conditional updates, so two callers cannot both
succeed; the database triggers re-check the rules as a backstop.
"""

import json
from dataclasses import dataclass
from functools import wraps
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry, JournalLine
from app.modules.cash import certification
from app.modules.cash.certification import Review
from app.modules.cash.errors import CashConflict, CashError, CashNotFound, CashValidationError
from app.modules.cash.models import (
    BankStatement,
    BankTransaction,
    CashAccount,
    ReconciliationCertification,
    ReconciliationMatch,
    ReconciliationReopenEvent,
    ReconciliationSession,
    ReconciliationStatus,
)
from app.modules.cash.schemas import (
    AutoMatchRequest,
    CloseRequest,
    MatchCreate,
    ReconciliationSessionCreate,
    ReconciliationSessionUpdate,
    ReopenRequest,
)
from app.modules.cash.services import (
    ZERO,
    cash_balance,
    get_cash_account,
    lock_cash_account,
    quantize,
)
from app.modules.cash.statements import describe_transactions, get_statement, transaction_status

# --- Sessions --------------------------------------------------------------------------


def get_session(db: Session, session_id: int) -> ReconciliationSession:
    session = db.get(ReconciliationSession, session_id)
    if session is None:
        raise CashNotFound(f"Reconciliation session {session_id} not found")
    return session


def list_sessions(db: Session, cash_account_id: int | None = None) -> list[ReconciliationSession]:
    stmt = select(ReconciliationSession).order_by(
        ReconciliationSession.period_start, ReconciliationSession.id
    )
    if cash_account_id is not None:
        stmt = stmt.where(ReconciliationSession.cash_account_id == cash_account_id)
    return list(db.scalars(stmt))


def create_session(db: Session, payload: ReconciliationSessionCreate) -> ReconciliationSession:
    cash_account = get_cash_account(db, payload.cash_account_id)
    statement: BankStatement | None = None
    if payload.statement_id is not None:
        statement = get_statement(db, payload.statement_id)
        if statement.cash_account_id != cash_account.id:
            raise CashValidationError("Statement belongs to another cash account")

    def pick(field: str):
        value = getattr(payload, field)
        if value is None and statement is not None:
            value = getattr(statement, field)
        if value is None:
            raise CashValidationError(f"{field} is required when no statement is given")
        return value

    period_start, period_end = pick("period_start"), pick("period_end")
    if period_end < period_start:
        raise CashValidationError("period_end precedes period_start")
    session = ReconciliationSession(
        cash_account_id=cash_account.id,
        statement_id=statement.id if statement else None,
        period_start=period_start,
        period_end=period_end,
        opening_balance=quantize(pick("opening_balance")),
        closing_balance=quantize(pick("closing_balance")),
        notes=payload.notes,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise CashConflict(
            "A reconciliation session for this cash account and period already exists"
        )
    db.refresh(session)
    return session


def update_session(
    db: Session, session: ReconciliationSession, payload: ReconciliationSessionUpdate
) -> ReconciliationSession:
    """Change balances/notes of an open session; closed sessions are frozen (DB-enforced too)."""
    lock_cash_account(db, session.cash_account_id)
    try:
        _require_open(db, session)
    except CashError:
        db.rollback()
        raise
    if payload.opening_balance is not None:
        session.opening_balance = quantize(payload.opening_balance)
    if payload.closing_balance is not None:
        session.closing_balance = quantize(payload.closing_balance)
    if payload.notes is not None:
        session.notes = payload.notes
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "constraint violated"
        raise CashConflict(f"Update rejected by the database: {reason}")
    db.refresh(session)
    return session


# --- Views of both sides ----------------------------------------------------------------


@dataclass
class LedgerLine:
    line: JournalLine
    entry: JournalEntry
    matched: Decimal

    @property
    def signed_amount(self) -> Decimal:
        return self.line.debit - self.line.credit

    @property
    def total(self) -> Decimal:
        return self.line.debit + self.line.credit

    @property
    def remaining(self) -> Decimal:
        return self.total - self.matched

    def view(self) -> dict:
        return {
            "journal_line_id": self.line.id,
            "journal_entry_id": self.entry.id,
            "entry_date": self.entry.entry_date,
            "entry_description": self.entry.description,
            "entry_reference": self.entry.reference,
            "memo": self.line.memo,
            "debit": self.line.debit,
            "credit": self.line.credit,
            "signed_amount": self.signed_amount,
            "matched_amount": self.matched,
            "remaining_amount": self.remaining,
            "status": transaction_status(self.total, self.matched),
        }


def session_bank_transactions(
    db: Session, session: ReconciliationSession, include_prior: bool = False
) -> list[BankTransaction]:
    """Bank transactions of the session's cash account dated within its period.

    With ``include_prior`` every transaction up to the period end is returned
    (used for the adjusted balances, where unmatched items of earlier periods
    still explain part of the difference between bank and ledger).
    """
    stmt = select(BankTransaction).where(
        BankTransaction.cash_account_id == session.cash_account_id,
        BankTransaction.transaction_date <= session.period_end,
    )
    if not include_prior:
        stmt = stmt.where(BankTransaction.transaction_date >= session.period_start)
    return list(
        db.scalars(
            stmt.order_by(
                BankTransaction.transaction_date, BankTransaction.statement_id, BankTransaction.line_no
            )
        )
    )


def line_matched_amounts(db: Session, line_ids: list[int]) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = {lid: ZERO for lid in line_ids}
    if line_ids:
        for lid, amount in db.execute(
            select(ReconciliationMatch.journal_line_id, ReconciliationMatch.amount).where(
                ReconciliationMatch.journal_line_id.in_(line_ids)
            )
        ):
            totals[lid] += amount
    return totals


def posted_ledger_lines(
    db: Session, cash_account: CashAccount, as_of: date | None = None
) -> list[LedgerLine]:
    """Posted lines of the cash account's ledger account up to ``as_of`` (drafts never appear)."""
    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalLine.account_id == cash_account.ledger_account_id,
            JournalEntry.status == EntryStatus.POSTED,
        )
        .order_by(JournalEntry.entry_date, JournalEntry.id, JournalLine.id)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.entry_date <= as_of)
    rows = db.execute(stmt).all()
    matched = line_matched_amounts(db, [line.id for line, _ in rows])
    return [LedgerLine(line, entry, matched[line.id]) for line, entry in rows]


# --- Review ---------------------------------------------------------------------------


def _previous_closed_session(db: Session, session: ReconciliationSession):
    return db.scalar(
        select(ReconciliationSession)
        .where(
            ReconciliationSession.cash_account_id == session.cash_account_id,
            ReconciliationSession.id != session.id,
            ReconciliationSession.period_end < session.period_start,
            ReconciliationSession.status == ReconciliationStatus.CLOSED,
        )
        .order_by(ReconciliationSession.period_end.desc(), ReconciliationSession.id.desc())
        .limit(1)
    )


def compute_review(db: Session, session: ReconciliationSession) -> Review:
    """Live review of a session from the current database state (never stored)."""
    cash_account = get_cash_account(db, session.cash_account_id)
    all_bank_rows = describe_transactions(
        db, session_bank_transactions(db, session, include_prior=True)
    )
    bank_rows = [r for r in all_bank_rows if r["transaction_date"] >= session.period_start]
    prior_unmatched = [
        r for r in all_bank_rows
        if r["transaction_date"] < session.period_start and r["status"] != "matched"
    ]
    ledger = posted_ledger_lines(db, cash_account, as_of=session.period_end)

    def signed_remaining(r: dict) -> Decimal:
        return r["remaining_amount"] if r["amount"] > 0 else -r["remaining_amount"]

    bank_total = sum((r["amount"] for r in bank_rows), ZERO)
    # Matched bank movement keeps the bank transaction's sign (inflow +, outflow -).
    matched_bank = sum(
        ((r["matched_amount"] if r["amount"] > 0 else -r["matched_amount"]) for r in bank_rows),
        ZERO,
    )
    unmatched_bank = sum((signed_remaining(r) for r in bank_rows), ZERO)
    # Unmatched bank items of *any* date up to period end, for the adjusted ledger balance.
    unmatched_bank_to_date = sum((signed_remaining(r) for r in all_bank_rows), ZERO)
    unmatched_count = sum(1 for r in bank_rows if r["status"] == "unmatched")
    partial_count = sum(1 for r in bank_rows if r["status"] == "partially_matched")

    outstanding = [l for l in ledger if l.remaining > 0]
    outstanding_total = sum(
        ((l.remaining if l.line.debit > 0 else -l.remaining) for l in outstanding), ZERO
    )
    ledger_balance = quantize(cash_balance(db, cash_account.ledger_account_id, as_of=session.period_end))

    cleared = session.opening_balance + matched_bank
    difference = session.closing_balance - cleared
    adjusted_bank = session.closing_balance + outstanding_total
    adjusted_ledger = ledger_balance + unmatched_bank_to_date

    discrepancies: list[dict] = []
    previous = _previous_closed_session(db, session)
    if previous is not None and previous.closing_balance != session.opening_balance:
        discrepancies.append(
            {
                "kind": "opening_balance_mismatch",
                "message": (
                    f"Opening balance {session.opening_balance:.2f} differs from the closing "
                    f"balance {previous.closing_balance:.2f} of the previous closed session "
                    f"#{previous.id} (period ending {previous.period_end.isoformat()})"
                ),
                "amount": session.opening_balance - previous.closing_balance,
            }
        )
    for r in prior_unmatched:
        discrepancies.append(
            {
                "kind": "unmatched_prior_bank_transaction",
                "message": f"Bank transaction {r['description']!r} dated "
                f"{r['transaction_date'].isoformat()} (before this period) still has "
                f"{r['remaining_amount']:.2f} unexplained",
                "amount": signed_remaining(r),
                "bank_transaction_id": r["id"],
            }
        )
    for r in bank_rows:
        if r["status"] == "unmatched":
            discrepancies.append(
                {
                    "kind": "unmatched_bank_transaction",
                    "message": f"Bank transaction {r['description']!r} dated "
                    f"{r['transaction_date'].isoformat()} has no posted ledger counterpart",
                    "amount": r["amount"],
                    "bank_transaction_id": r["id"],
                }
            )
        elif r["status"] == "partially_matched":
            discrepancies.append(
                {
                    "kind": "partially_matched_bank_transaction",
                    "message": f"Bank transaction {r['description']!r}: "
                    f"{r['remaining_amount']:.2f} of {abs(r['amount']):.2f} is not explained",
                    "amount": r["remaining_amount"] if r["amount"] > 0 else -r["remaining_amount"],
                    "bank_transaction_id": r["id"],
                }
            )
    for l in outstanding:
        kind = "outstanding_ledger_line" if l.matched == 0 else "partially_matched_ledger_line"
        discrepancies.append(
            {
                "kind": kind,
                "message": f"Posted line of entry #{l.entry.id} ({l.entry.description!r}, "
                f"{l.entry.entry_date.isoformat()}) has {l.remaining:.2f} not yet on a statement",
                "amount": l.remaining if l.line.debit > 0 else -l.remaining,
                "journal_line_id": l.line.id,
            }
        )
    if difference != 0:
        discrepancies.append(
            {
                "kind": "difference",
                "message": f"Statement closing balance {session.closing_balance:.2f} differs "
                f"from the cleared balance {cleared:.2f}",
                "amount": difference,
            }
        )
    unexplained = adjusted_bank - adjusted_ledger
    if unexplained != 0:
        discrepancies.append(
            {
                "kind": "unexplained_difference",
                "message": f"Adjusted bank balance {adjusted_bank:.2f} and adjusted ledger "
                f"balance {adjusted_ledger:.2f} differ; check opening balances or transactions "
                "missing from both the statement history and the ledger",
                "amount": unexplained,
            }
        )

    is_balanced = difference == 0 and unmatched_count == 0 and partial_count == 0
    figures = {
        "bank_transactions_count": len(bank_rows),
        "bank_transactions_total": bank_total,
        "matched_bank_total": matched_bank,
        "unmatched_bank_total": unmatched_bank,
        "unmatched_bank_count": unmatched_count,
        "partially_matched_bank_count": partial_count,
        "cleared_balance": cleared,
        "difference": difference,
        "ledger_balance_at_period_end": ledger_balance,
        "outstanding_ledger_count": len(outstanding),
        "outstanding_ledger_total": outstanding_total,
        "adjusted_bank_balance": adjusted_bank,
        "adjusted_ledger_balance": adjusted_ledger,
        "unexplained_difference": unexplained,
        "is_balanced": is_balanced,
    }
    return Review(
        figures=figures,
        bank_rows=bank_rows,
        ledger_rows=[l.view() for l in ledger],
        discrepancies=discrepancies,
    )


def summary(db: Session, session: ReconciliationSession) -> dict:
    """Review for the API.

    The flat figures are always the *live* state. For a closed session the
    active certification is attached and every difference between the
    certified document and the live state is listed under ``drift`` - the
    certified result itself never changes.
    """
    review = compute_review(db, session)
    result = {"session": session, **review.figures, "discrepancies": review.discrepancies}
    cert = certification.active_certification(db, session.id)  # by session state, never by events
    if cert is not None:
        result["certification"] = certification.certification_view(db, cert)
        result["drift"] = certification.drift(db, cert, review)
    else:
        result["certification"] = None
        result["drift"] = []
    return result


def unmatched_items(db: Session, session: ReconciliationSession) -> dict:
    cash_account = get_cash_account(db, session.cash_account_id)
    bank_rows = [
        r for r in describe_transactions(db, session_bank_transactions(db, session))
        if r["status"] != "matched"
    ]
    ledger = [
        l.view()
        for l in posted_ledger_lines(db, cash_account, as_of=session.period_end)
        if l.remaining > 0
    ]
    return {"session_id": session.id, "bank_transactions": bank_rows, "ledger_lines": ledger}


def list_matches(db: Session, session: ReconciliationSession) -> list[ReconciliationMatch]:
    return list(
        db.scalars(
            select(ReconciliationMatch)
            .where(ReconciliationMatch.session_id == session.id)
            .order_by(ReconciliationMatch.id)
        )
    )


# --- Matching ---------------------------------------------------------------------------


def _rollback_on_error(func):
    """Release the cash-account lock (and any pending state) when a write fails."""

    @wraps(func)
    def wrapper(db: Session, *args, **kwargs):
        try:
            return func(db, *args, **kwargs)
        except CashError:
            db.rollback()
            raise

    return wrapper


def _require_open(db: Session, session: ReconciliationSession) -> None:
    status = db.scalar(
        select(ReconciliationSession.status).where(ReconciliationSession.id == session.id)
    )
    if status != ReconciliationStatus.OPEN:
        raise CashConflict(f"Reconciliation session {session.id} is not open")


def _load_line(db: Session, line_id: int) -> tuple[JournalLine, JournalEntry]:
    row = db.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalLine.id == line_id)
    ).first()
    if row is None:
        raise CashNotFound(f"Journal line {line_id} not found")
    return row[0], row[1]


def _validate_pair(
    cash_account: CashAccount, bank_transaction: BankTransaction, line: JournalLine, entry: JournalEntry
) -> list[str]:
    problems = []
    if entry.status != EntryStatus.POSTED:
        problems.append(f"journal entry #{entry.id} is a draft; only posted entries can be reconciled")
    if line.account_id != cash_account.ledger_account_id:
        problems.append(
            f"journal line {line.id} is not on ledger account {cash_account.ledger_account_id} "
            f"of cash account {cash_account.id}"
        )
    if bank_transaction.amount > 0 and line.debit <= 0:
        problems.append("a bank inflow can only be matched with a debit to the cash ledger account")
    if bank_transaction.amount < 0 and line.credit <= 0:
        problems.append("a bank outflow can only be matched with a credit to the cash ledger account")
    return problems


@_rollback_on_error
def add_match(db: Session, session: ReconciliationSession, payload: MatchCreate) -> ReconciliationMatch:
    """Match (part of) a bank transaction with (part of) a posted journal line; commits."""
    cash_account = get_cash_account(db, session.cash_account_id)
    lock_cash_account(db, cash_account.id)
    _require_open(db, session)

    bank_transaction = db.get(BankTransaction, payload.bank_transaction_id)
    if bank_transaction is None:
        raise CashNotFound(f"Bank transaction {payload.bank_transaction_id} not found")
    if bank_transaction.cash_account_id != session.cash_account_id:
        raise CashValidationError("Bank transaction belongs to another cash account")
    if bank_transaction.transaction_date > session.period_end:
        raise CashValidationError("Bank transaction is dated after the session period")
    line, entry = _load_line(db, payload.journal_line_id)
    problems = _validate_pair(cash_account, bank_transaction, line, entry)
    if problems:
        raise CashValidationError("Cannot match", problems)

    if db.scalar(
        select(ReconciliationMatch.id).where(
            ReconciliationMatch.bank_transaction_id == bank_transaction.id,
            ReconciliationMatch.journal_line_id == line.id,
        )
    ):
        raise CashConflict("This bank transaction and journal line are already matched")

    # Fresh sums straight from the database (never from cached state) so a
    # concurrent matcher that committed first is taken into account.
    matched_bank = sum(
        db.scalars(
            select(ReconciliationMatch.amount).where(
                ReconciliationMatch.bank_transaction_id == bank_transaction.id
            )
        ),
        ZERO,
    )
    matched_line = sum(
        db.scalars(
            select(ReconciliationMatch.amount).where(ReconciliationMatch.journal_line_id == line.id)
        ),
        ZERO,
    )
    remaining_bank = abs(bank_transaction.amount) - matched_bank
    remaining_line = (line.debit + line.credit) - matched_line
    amount = quantize(payload.amount) if payload.amount is not None else min(remaining_bank, remaining_line)
    if remaining_bank <= 0:
        raise CashConflict(f"Bank transaction {bank_transaction.id} is already fully matched")
    if remaining_line <= 0:
        raise CashConflict(f"Journal line {line.id} is already fully matched")
    if amount <= 0:
        raise CashValidationError("Match amount must be positive")
    if amount > remaining_bank:
        raise CashConflict(
            f"Match amount {amount:.2f} exceeds the unmatched bank amount {remaining_bank:.2f}"
        )
    if amount > remaining_line:
        raise CashConflict(
            f"Match amount {amount:.2f} exceeds the unmatched journal line amount {remaining_line:.2f}"
        )

    match = ReconciliationMatch(
        session_id=session.id,
        bank_transaction_id=bank_transaction.id,
        journal_line_id=line.id,
        amount=amount,
    )
    db.add(match)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "constraint violated"
        raise CashConflict(f"Match rejected by the database: {reason}")
    db.refresh(match)
    return match


@_rollback_on_error
def remove_match(db: Session, session: ReconciliationSession, match_id: int) -> None:
    lock_cash_account(db, session.cash_account_id)
    _require_open(db, session)
    match = db.get(ReconciliationMatch, match_id)
    if match is None or match.session_id != session.id:
        raise CashNotFound(f"Match {match_id} not found in session {session.id}")
    db.delete(match)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "constraint violated"
        raise CashConflict(f"Unmatch rejected by the database: {reason}")


@_rollback_on_error
def auto_match(db: Session, session: ReconciliationSession, request: AutoMatchRequest) -> dict:
    """Propose (and unless ``dry_run`` apply) unambiguous one-to-one matches.

    A bank transaction is matched with a posted ledger line when exactly one
    unmatched line has the same direction and amount and is dated within the
    tolerance. If several qualify, a line whose entry reference equals the bank
    reference wins; otherwise the transaction is reported as ambiguous and left
    for manual matching. Every suggestion carries a human-readable reason.
    """
    cash_account = get_cash_account(db, session.cash_account_id)
    if not request.dry_run:
        lock_cash_account(db, cash_account.id)
    _require_open(db, session)

    bank_rows = [
        r for r in describe_transactions(db, session_bank_transactions(db, session))
        if r["status"] == "unmatched"
    ]
    candidates = [
        l for l in posted_ledger_lines(db, cash_account, as_of=session.period_end) if l.matched == 0
    ]
    tolerance = timedelta(days=request.date_tolerance_days)
    used: set[int] = set()
    suggestions: list[dict] = []
    ambiguous: list[dict] = []

    for r in bank_rows:
        amount = abs(r["amount"])
        inflow = r["amount"] > 0
        found = [
            l
            for l in candidates
            if l.line.id not in used
            and l.total == amount
            and ((l.line.debit > 0) if inflow else (l.line.credit > 0))
            and abs(l.entry.entry_date - r["transaction_date"]) <= tolerance
        ]
        reason = f"same amount {amount:.2f}, same direction, date within {request.date_tolerance_days} day(s)"
        if len(found) > 1 and r["reference"]:
            by_ref = [l for l in found if l.entry.reference and l.entry.reference == r["reference"]]
            if by_ref:
                found = by_ref
                reason += f", reference {r['reference']!r} matches the entry reference"
        if len(found) == 1:
            chosen = found[0]
            used.add(chosen.line.id)
            suggestions.append(
                {
                    "bank_transaction_id": r["id"],
                    "journal_line_id": chosen.line.id,
                    "amount": amount,
                    "reason": reason,
                    "applied": False,
                }
            )
        elif found:
            ambiguous.append(
                {
                    "bank_transaction_id": r["id"],
                    "candidate_journal_line_ids": [l.line.id for l in found],
                    "reason": f"{len(found)} posted lines qualify; match manually",
                }
            )

    applied = 0
    if not request.dry_run and suggestions:
        for s in suggestions:
            db.add(
                ReconciliationMatch(
                    session_id=session.id,
                    bank_transaction_id=s["bank_transaction_id"],
                    journal_line_id=s["journal_line_id"],
                    amount=s["amount"],
                )
            )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "constraint violated"
            raise CashConflict(f"Auto-match rejected by the database: {reason}")
        for s in suggestions:
            s["applied"] = True
        applied = len(suggestions)

    return {
        "session_id": session.id,
        "dry_run": request.dry_run,
        "suggestions": suggestions,
        "ambiguous": ambiguous,
        "applied_count": applied,
    }


# --- Close / reopen ----------------------------------------------------------------------


def _db_reason(exc: IntegrityError) -> str:
    return str(exc.orig).strip().splitlines()[0] if exc.orig else "constraint violated"


def _content_matches(existing: ReconciliationCertification, fresh: dict) -> bool:
    """Same approved result, ignoring when/with which note it was recorded."""
    try:
        stored = json.loads(existing.content)
    except ValueError:
        return False
    volatile = ("certified_at", "notes")
    a = {k: v for k, v in stored.items() if k not in volatile}
    b = json.loads(certification.canonical_json({k: v for k, v in fresh.items() if k not in volatile}))
    return a == b and certification.content_hash(existing.content) == existing.content_hash


@_rollback_on_error
def close_session(db: Session, session: ReconciliationSession, request: CloseRequest) -> ReconciliationSession:
    """Close the session and certify exactly what was approved (one transaction).

    Order matters for the database rules: the certification is inserted while
    the session is still open, then the status flips to closed (which the
    database only allows when the certification of the current cycle exists).

    Tolerance: if a certification for the current cycle already exists although
    the session is open (a close committed half-way), it is adopted when it
    still matches the current state - a retry then simply completes the close.
    If it does not match, the close is refused with guidance (see
    ``recover_session``): evidence is never overwritten.
    """
    lock_cash_account(db, session.cash_account_id)
    _require_open(db, session)
    state = certification.session_state(db, session.id)
    cash_account = get_cash_account(db, session.cash_account_id)
    review = compute_review(db, session)
    forced = not review.figures["is_balanced"]
    if forced:
        if not request.force:
            raise CashConflict(
                f"Session is not balanced (difference {review.figures['difference']:.2f}, "
                f"{review.figures['unmatched_bank_count']} unmatched and "
                f"{review.figures['partially_matched_bank_count']} partially matched bank transactions); "
                "match the remaining items or close with force=true and a note"
            )
        if not (request.notes and request.notes.strip()):
            raise CashValidationError("notes are required when closing with a difference")

    previous = certification.latest_certification(db, session.id)
    orphan = certification.orphaned_certification(db, session.id)
    certified_at = datetime.now(timezone.utc)
    if orphan is not None:
        # Evidence for this cycle exists already; only a matching one may be adopted.
        fresh = certification.build_content(
            db, session, cash_account.ledger_account_id, review,
            version=orphan.version, previous_hash=orphan.previous_hash,
            certified_at=certified_at, forced=orphan.forced, notes=orphan.notes,
        )
        if not _content_matches(orphan, fresh):
            raise CashConflict(
                f"Certification version {orphan.version} already exists for the current cycle but does "
                "not match the current state; call recover to complete that close, then reopen with a "
                "reason and certify again (existing evidence is never overwritten)"
            )
        cert = orphan
    else:
        content = certification.build_content(
            db,
            session,
            cash_account.ledger_account_id,
            review,
            version=(previous.version + 1) if previous else 1,
            previous_hash=previous.content_hash if previous else None,
            certified_at=certified_at,
            forced=forced,
            notes=request.notes,
        )
        canonical = certification.canonical_json(content)
        cert = ReconciliationCertification(
            session_id=session.id,
            version=content["version"],
            certified_at=certified_at,
            content_hash=certification.content_hash(canonical),
            previous_hash=content["previous_hash"],
            content=canonical,
            is_balanced=review.figures["is_balanced"],
            forced=forced,
            difference=review.figures["difference"],
            cleared_balance=review.figures["cleared_balance"],
            ledger_balance=review.figures["ledger_balance_at_period_end"],
            matches_count=len(content["matches"]),
            notes=request.notes,
            reopen_event_id=state.last_reopen_event_id,
        )
        db.add(cert)
    try:
        db.flush()
        _flip_closed(db, session, state, certified_at, request.notes)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CashConflict(f"Close rejected by the database: {_db_reason(exc)}")
    db.refresh(session)
    return session


def _flip_closed(db, session, state, closed_at, notes):
    values = {"status": ReconciliationStatus.CLOSED, "closed_at": closed_at}
    if notes is not None:
        values["notes"] = notes
    result = db.execute(
        update(ReconciliationSession)
        .where(
            ReconciliationSession.id == session.id,
            ReconciliationSession.status == ReconciliationStatus.OPEN,
            ReconciliationSession.reopen_count == state.reopen_count,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise CashConflict(f"Reconciliation session {session.id} is already closed")


@_rollback_on_error
def reopen_session(
    db: Session, session: ReconciliationSession, request: ReopenRequest
) -> ReconciliationSession:
    """Controlled reopening: records the reason, then completes the reopening.

    The event is inserted with ``reopen_number = reopen_count + 1`` and the
    session is flipped in the same transaction, pointing at *this* event. Stray
    events left by a partial process are ignored: they neither supersede the
    certification nor block this reopening (they show as "orphaned" /
    "superseded" in the history). The certification stays in the history
    untouched; closing again creates a new version chained to it.
    """
    if not request.reason.strip():
        raise CashValidationError("a reason is required to reopen a certified reconciliation")
    lock_cash_account(db, session.cash_account_id)
    state = certification.session_state(db, session.id)
    if state.status != ReconciliationStatus.CLOSED:
        raise CashConflict(f"Reconciliation session {session.id} is not closed")
    active = certification.active_certification(db, session.id)
    if active is None:
        raise CashConflict(
            f"Reconciliation session {session.id} is closed without an active certification; "
            "its history was altered behind the database rules (see the integrity report)"
        )
    event = ReconciliationReopenEvent(
        session_id=session.id,
        certification_id=active.id,
        reopen_number=state.reopen_count + 1,
        reason=request.reason.strip(),
        reopened_at=datetime.now(timezone.utc),
    )
    db.add(event)
    try:
        db.flush()
        result = db.execute(
            update(ReconciliationSession)
            .where(
                ReconciliationSession.id == session.id,
                ReconciliationSession.status == ReconciliationStatus.CLOSED,
                ReconciliationSession.reopen_count == state.reopen_count,
            )
            .values(
                status=ReconciliationStatus.OPEN,
                closed_at=None,
                reopen_count=state.reopen_count + 1,
                last_reopen_event_id=event.id,
            )
        )
        if result.rowcount != 1:
            raise CashConflict(f"Reconciliation session {session.id} is not closed")
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CashConflict(f"Reopen rejected by the database: {_db_reason(exc)}")
    db.refresh(session)
    return session


@_rollback_on_error
def recover_session(db: Session, session: ReconciliationSession) -> dict:
    """Complete a close that was committed half-way (certification without status flip).

    Nothing is deleted or rewritten: the existing certification becomes the
    closed session's evidence (its ``verified`` flag tells whether it is sound)
    and the normal reopen-with-reason -> certify-again path is available
    again. Returns the integrity report; 409 when there is nothing to recover.
    """
    lock_cash_account(db, session.cash_account_id)
    orphan = certification.orphaned_certification(db, session.id)
    if orphan is None:
        raise CashConflict(f"Reconciliation session {session.id} has nothing to recover")
    state = certification.session_state(db, session.id)
    try:
        _flip_closed(db, session, state, orphan.certified_at, None)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CashConflict(f"Recovery rejected by the database: {_db_reason(exc)}")
    db.refresh(session)
    return certification.integrity(db, session.id)


def certification_detail(db: Session, session: ReconciliationSession) -> dict:
    """Latest certification with verification result and drift against the live state."""
    cert = certification.latest_certification(db, session.id)
    if cert is None:
        raise CashNotFound(f"Reconciliation session {session.id} has never been certified")
    view = certification.certification_view(db, cert)
    review = compute_review(db, session)
    return {
        **view,
        "content": cert.content,
        "drift": certification.drift(db, cert, review) if view["active"] else [],
    }


def certification_history(db: Session, session: ReconciliationSession) -> dict:
    return {
        "session_id": session.id,
        "events": certification.history(db, session.id),
        "integrity": certification.integrity(db, session.id),
    }
