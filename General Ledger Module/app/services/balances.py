"""Verification and repair of the ``account_daily_totals`` aggregate.

Source of truth: ``journal_lines`` of posted entries. ``account_daily_totals``
is a derived accelerator:

* **update rule** - a database trigger adds an entry's lines to the table in
  the same transaction that posts the entry (AFTER UPDATE of ``status``);
  posted entries are immutable, so nothing is ever subtracted;
* **invalidation** - none is needed in normal operation: the trigger keeps
  the table exact, and direct SQL cannot post an entry without firing it;
* **reconstruction** - ``rebuild_daily_totals`` recomputes the table from the
  lines in one transaction (taking an exclusive table lock on PostgreSQL so
  no posting interleaves; SQLite serialises writers anyway);
* **reconciliation guarantee** - ``verify_daily_totals`` recomputes every
  (account, day) total from the lines and compares it with the table; the
  reconciliation report runs the same comparison. Discrepancies are logged in
  ``balance_verification_runs`` with the differing cents, and a rebuild logs
  what it replaced. Journal entries and lines are never modified.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    AccountDailyTotal,
    BalanceVerificationRun,
    EntryStatus,
    JournalEntry,
    JournalLine,
)
from app.services import hooks
from app.services.reports import cents_sum, from_cents


@dataclass
class Discrepancy:
    account_id: int
    entry_date: str
    ledger_debit_cents: int
    ledger_credit_cents: int
    aggregate_debit_cents: int
    aggregate_credit_cents: int


@dataclass
class VerificationResult:
    kind: str
    ok: bool
    discrepancies: list[Discrepancy] = field(default_factory=list)
    checked_rows: int = 0
    run_id: int | None = None
    run_at: datetime | None = None


def _ledger_totals(db: Session) -> dict[tuple[int, str], tuple[int, int]]:
    stmt = (
        select(
            JournalLine.account_id,
            JournalEntry.entry_date,
            cents_sum(db, JournalLine.debit),
            cents_sum(db, JournalLine.credit),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == EntryStatus.POSTED)
        .group_by(JournalLine.account_id, JournalEntry.entry_date)
    )
    return {(acc, day.isoformat()): (int(d), int(c)) for acc, day, d, c in db.execute(stmt)}


def _aggregate_totals(db: Session) -> dict[tuple[int, str], tuple[int, int]]:
    return {
        (row.account_id, row.entry_date.isoformat()): (row.debit_cents, row.credit_cents)
        for row in db.scalars(select(AccountDailyTotal))
    }


def compare(db: Session) -> VerificationResult:
    ledger = _ledger_totals(db)
    aggregate = _aggregate_totals(db)
    result = VerificationResult(kind="verify", ok=True, checked_rows=len(ledger | aggregate))
    for key in sorted(set(ledger) | set(aggregate)):
        l_d, l_c = ledger.get(key, (0, 0))
        a_d, a_c = aggregate.get(key, (0, 0))
        if (l_d, l_c) != (a_d, a_c):
            result.discrepancies.append(Discrepancy(key[0], key[1], l_d, l_c, a_d, a_c))
    result.ok = not result.discrepancies
    return result


def _log(db: Session, result: VerificationResult, actor_ref: str) -> BalanceVerificationRun:
    run = BalanceVerificationRun(
        kind=result.kind,
        ok=result.ok,
        discrepancy_count=len(result.discrepancies),
        details=json.dumps(
            {
                "checked_rows": result.checked_rows,
                "discrepancies": [
                    {
                        "account_id": d.account_id,
                        "entry_date": d.entry_date,
                        "ledger_debit": str(from_cents(d.ledger_debit_cents)),
                        "ledger_credit": str(from_cents(d.ledger_credit_cents)),
                        "aggregate_debit": str(from_cents(d.aggregate_debit_cents)),
                        "aggregate_credit": str(from_cents(d.aggregate_credit_cents)),
                    }
                    for d in result.discrepancies[:500]
                ],
            }
        ),
        actor_ref=actor_ref,
        run_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    result.run_id, result.run_at = run.id, run.run_at
    return run


def verify_daily_totals(
    db: Session, *, actor_ref: str = "system", commit: bool = True
) -> VerificationResult:
    """Compare the aggregate with the ledger and log the outcome (journals untouched)."""
    result = compare(db)
    _log(db, result, actor_ref)
    if commit:
        db.commit()
    return result


def rebuild_daily_totals(
    db: Session, *, actor_ref: str = "system", commit: bool = True
) -> VerificationResult:
    """Recompute the aggregate from posted lines in one transaction; log what differed."""
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("LOCK TABLE account_daily_totals IN EXCLUSIVE MODE"))
        before = compare(db)
        before.kind = "rebuild"
        db.execute(text("DELETE FROM account_daily_totals"))
        for (account_id, day), (d, c) in _ledger_totals(db).items():
            db.add(
                AccountDailyTotal(
                    account_id=account_id,
                    entry_date=datetime.fromisoformat(day).date(),
                    debit_cents=d,
                    credit_cents=c,
                )
            )
        db.flush()
        after = compare(db)
        if not after.ok:  # pragma: no cover - would mean the rebuild itself is wrong
            raise RuntimeError("aggregate still differs from the ledger after rebuild")
        before.ok = True  # the rebuild fixed whatever was recorded
        _log(db, before, actor_ref)
        hooks.checkpoint("balances.rebuild_before_commit")
        if commit:
            db.commit()
    except Exception:
        if commit:
            db.rollback()
        raise
    return before


def verification_runs(db: Session, limit: int = 50) -> list[BalanceVerificationRun]:
    return db.scalars(
        select(BalanceVerificationRun).order_by(BalanceVerificationRun.id.desc()).limit(limit)
    ).all()
