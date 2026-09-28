"""Bank statement import.

Idempotency rules:

* A statement is identified by (cash account, statement reference). Importing
  the same payload again returns the existing statement and creates nothing.
  Importing *different* content under an existing reference is rejected (409)
  so a bank statement can never be silently replaced.
* Every transaction carries a fingerprint of (reference, date, amount,
  description, occurrence number) scoped to the cash account. A transaction
  already present - e.g. in an overlapping statement - is skipped and reported,
  never inserted twice. The database enforces this with a unique constraint.
* Imports are evidence from the bank only: nothing is posted to the ledger.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.modules.cash.errors import CashConflict, CashNotFound, CashValidationError
from app.modules.cash.models import (
    BankStatement,
    BankTransaction,
    ReconciliationMatch,
    ReconciliationSession,
    ReconciliationStatus,
)
from app.modules.cash.schemas import BankStatementImport, BankTransactionIn
from app.modules.cash.services import ZERO, get_cash_account, lock_cash_account, quantize


@dataclass
class ImportResult:
    statement: BankStatement
    created: bool
    imported_count: int
    skipped_duplicates: list[dict] = field(default_factory=list)
    late_transactions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def late_transactions(db: Session, transactions: list[BankTransaction]) -> list[dict]:
    """Imported lines that fall inside the period of an already *closed* session.

    Such a line is never blocked - the bank is the bank - but it is reported
    here and appears as drift on the closed session; the certified result of
    that session does not change.
    """
    if not transactions:
        return []
    sessions = list(
        db.scalars(
            select(ReconciliationSession).where(
                ReconciliationSession.cash_account_id == transactions[0].cash_account_id,
                ReconciliationSession.status == ReconciliationStatus.CLOSED,
            )
        )
    )
    if not sessions:
        return []
    from app.modules.cash.certification import active_certification  # local: avoid import cycle

    versions = {
        s.id: (cert.version if (cert := active_certification(db, s.id)) else None) for s in sessions
    }
    late = []
    for t in transactions:
        for s in sessions:
            if s.period_start <= t.transaction_date <= s.period_end:
                late.append(
                    {
                        "transaction_id": t.id,
                        "transaction_date": t.transaction_date,
                        "amount": t.amount,
                        "description": t.description,
                        "session_id": s.id,
                        "period_start": s.period_start,
                        "period_end": s.period_end,
                        "certification_version": versions[s.id],
                    }
                )
    return late


def _canonical_line(line: BankTransactionIn, occurrence: int) -> str:
    return "|".join(
        [
            (line.reference or "").strip(),
            line.transaction_date.isoformat(),
            f"{quantize(line.amount):.2f}",
            " ".join(line.description.split()),
            str(occurrence),
        ]
    )


def transaction_fingerprint(cash_account_id: int, line: BankTransactionIn, occurrence: int) -> str:
    return hashlib.sha256(
        f"{cash_account_id}|{_canonical_line(line, occurrence)}".encode()
    ).hexdigest()


def _with_occurrences(lines: list[BankTransactionIn]) -> list[tuple[BankTransactionIn, int]]:
    """Number identical lines within one payload (1, 2, ...) so both are kept."""
    seen: Counter[str] = Counter()
    result = []
    for line in lines:
        key = _canonical_line(line, 0)
        seen[key] += 1
        result.append((line, seen[key]))
    return result


def statement_content_hash(payload: BankStatementImport) -> str:
    head = "|".join(
        [
            str(payload.cash_account_id),
            payload.statement_reference.strip(),
            payload.period_start.isoformat(),
            payload.period_end.isoformat(),
            f"{quantize(payload.opening_balance):.2f}",
            f"{quantize(payload.closing_balance):.2f}",
        ]
    )
    lines = sorted(_canonical_line(line, occ) for line, occ in _with_occurrences(payload.transactions))
    return hashlib.sha256("\n".join([head, *lines]).encode()).hexdigest()


def validate_statement(payload: BankStatementImport) -> None:
    problems: list[str] = []
    if payload.period_end < payload.period_start:
        problems.append("period_end precedes period_start")
    for position, line in enumerate(payload.transactions, start=1):
        if line.amount == 0:
            problems.append(f"transaction {position} has a zero amount")
        if not payload.period_start <= line.transaction_date <= payload.period_end:
            problems.append(f"transaction {position} is dated outside the statement period")
    total = sum((quantize(line.amount) for line in payload.transactions), ZERO)
    expected = quantize(payload.closing_balance) - quantize(payload.opening_balance)
    if total != expected:
        problems.append(
            f"statement does not foot: transactions sum to {total:.2f} but "
            f"closing - opening is {expected:.2f}"
        )
    if problems:
        raise CashValidationError("Bank statement rejected", problems)


def _existing_statement(db: Session, cash_account_id: int, reference: str) -> BankStatement | None:
    return db.scalar(
        select(BankStatement)
        .options(selectinload(BankStatement.transactions))
        .where(
            BankStatement.cash_account_id == cash_account_id,
            BankStatement.statement_reference == reference,
        )
    )


def import_statement(db: Session, payload: BankStatementImport) -> ImportResult:
    """Import a statement idempotently; commits on success."""
    get_cash_account(db, payload.cash_account_id)
    validate_statement(payload)
    content_hash = statement_content_hash(payload)
    reference = payload.statement_reference.strip()

    existing = _existing_statement(db, payload.cash_account_id, reference)
    if existing is not None:
        return _resolve_existing(existing, content_hash)

    lock_cash_account(db, payload.cash_account_id)
    statement = BankStatement(
        cash_account_id=payload.cash_account_id,
        statement_reference=reference,
        period_start=payload.period_start,
        period_end=payload.period_end,
        opening_balance=quantize(payload.opening_balance),
        closing_balance=quantize(payload.closing_balance),
        content_hash=content_hash,
    )

    numbered = _with_occurrences(payload.transactions)
    fingerprints = {
        transaction_fingerprint(payload.cash_account_id, line, occ): (line, occ)
        for line, occ in numbered
    }
    already = dict(
        db.execute(
            select(BankTransaction.fingerprint, BankTransaction.id).where(
                BankTransaction.cash_account_id == payload.cash_account_id,
                BankTransaction.fingerprint.in_(list(fingerprints)),
            )
        ).all()
    )

    skipped: list[dict] = []
    line_no = 0
    for line, occ in numbered:
        fp = transaction_fingerprint(payload.cash_account_id, line, occ)
        if fp in already:
            skipped.append(
                {
                    "reference": line.reference,
                    "transaction_date": line.transaction_date,
                    "amount": quantize(line.amount),
                    "description": line.description,
                    "existing_transaction_id": already[fp],
                }
            )
            continue
        line_no += 1
        statement.transactions.append(
            BankTransaction(
                cash_account_id=payload.cash_account_id,
                line_no=line_no,
                transaction_date=line.transaction_date,
                reference=(line.reference or None),
                description=line.description,
                amount=quantize(line.amount),
                fingerprint=fp,
            )
        )

    db.add(statement)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent import of the same statement (or of an
        # overlapping one): fall back to the idempotent path.
        db.rollback()
        existing = _existing_statement(db, payload.cash_account_id, reference)
        if existing is None:
            raise CashConflict(
                "A concurrent import inserted one of these transactions; retry the import"
            )
        return _resolve_existing(existing, content_hash)

    db.refresh(statement)
    late = late_transactions(db, statement.transactions)
    warnings = []
    if late:
        sessions = sorted({x["session_id"] for x in late})
        warnings.append(
            f"{len(late)} transaction(s) fall inside closed reconciliation session(s) "
            f"{sessions}; the certified results are unchanged and the lines are reported as drift"
        )
    return ImportResult(
        statement=statement,
        created=True,
        imported_count=line_no,
        skipped_duplicates=skipped,
        late_transactions=late,
        warnings=warnings,
    )


def _resolve_existing(existing: BankStatement, content_hash: str) -> ImportResult:
    if existing.content_hash != content_hash:
        raise CashConflict(
            f"Statement {existing.statement_reference!r} was already imported with different "
            "content; delete it first if the bank reissued the statement"
        )
    return ImportResult(statement=existing, created=False, imported_count=0)


def get_statement(db: Session, statement_id: int) -> BankStatement:
    statement = db.get(BankStatement, statement_id)
    if statement is None:
        raise CashNotFound(f"Bank statement {statement_id} not found")
    return statement


def list_statements(db: Session, cash_account_id: int | None = None) -> list[BankStatement]:
    stmt = select(BankStatement).order_by(BankStatement.period_start, BankStatement.id)
    if cash_account_id is not None:
        stmt = stmt.where(BankStatement.cash_account_id == cash_account_id)
    return list(db.scalars(stmt))


def delete_statement(db: Session, statement_id: int) -> None:
    """Remove a statement that has no reconciled transactions and no session."""
    statement = get_statement(db, statement_id)
    matched = db.scalar(
        select(func.count(ReconciliationMatch.id))
        .join(BankTransaction, BankTransaction.id == ReconciliationMatch.bank_transaction_id)
        .where(BankTransaction.statement_id == statement_id)
    )
    if matched:
        raise CashConflict("Statement has reconciled transactions; unmatch them first")
    if db.scalar(
        select(ReconciliationSession.id).where(ReconciliationSession.statement_id == statement_id)
    ):
        raise CashConflict("Statement is referenced by a reconciliation session")
    db.delete(statement)
    db.commit()


def matched_amounts(db: Session, transaction_ids: list[int]) -> dict[int, Decimal]:
    """Sum of match amounts per bank transaction id (0 when nothing is matched)."""
    if not transaction_ids:
        return {}
    rows = db.execute(
        select(ReconciliationMatch.bank_transaction_id, ReconciliationMatch.amount).where(
            ReconciliationMatch.bank_transaction_id.in_(transaction_ids)
        )
    ).all()
    totals: dict[int, Decimal] = {tid: ZERO for tid in transaction_ids}
    for tid, amount in rows:
        totals[tid] += amount
    return totals


def transaction_status(amount: Decimal, matched: Decimal) -> str:
    if matched == 0:
        return "unmatched"
    if matched < abs(amount):
        return "partially_matched"
    return "matched"


def describe_transactions(db: Session, transactions: list[BankTransaction]) -> list[dict]:
    """Serialisable view of bank transactions with their reconciliation state."""
    matched = matched_amounts(db, [t.id for t in transactions])
    result = []
    for t in transactions:
        m = matched.get(t.id, ZERO)
        result.append(
            {
                "id": t.id,
                "statement_id": t.statement_id,
                "cash_account_id": t.cash_account_id,
                "line_no": t.line_no,
                "transaction_date": t.transaction_date,
                "reference": t.reference,
                "description": t.description,
                "amount": t.amount,
                "fingerprint": t.fingerprint,
                "matched_amount": m,
                "remaining_amount": abs(t.amount) - m,
                "status": transaction_status(t.amount, m),
            }
        )
    return result


def list_transactions(
    db: Session,
    cash_account_id: int | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    stmt = select(BankTransaction).order_by(
        BankTransaction.transaction_date, BankTransaction.statement_id, BankTransaction.line_no
    )
    if cash_account_id is not None:
        stmt = stmt.where(BankTransaction.cash_account_id == cash_account_id)
    if date_from is not None:
        stmt = stmt.where(BankTransaction.transaction_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(BankTransaction.transaction_date <= date_to)
    rows = describe_transactions(db, list(db.scalars(stmt)))
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    return rows
