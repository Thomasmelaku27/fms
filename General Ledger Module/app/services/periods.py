"""Accounting periods: open -> closed -> locked date ranges that gate posting.

Lifecycle and rules:

* ``open``    : entries dated inside can be posted.
* ``closed``  : nothing dated inside can be posted or reversed into (except the
                fiscal year's ``closing`` entry); can be reopened unless the
                fiscal year is closed. Closing runs the month-end validation.
* ``locked``  : final. Cannot be reopened, re-dated, renamed or deleted, via the
                API or direct SQL (database trigger). Only a closed period can
                be locked.

Month-end validation (``close_problems``) - closing is refused while:

* a draft journal entry is dated inside the period (it could never be posted
  once the period is closed, so it must be posted, re-dated or deleted first);
* an earlier period is still open (periods close in order);
* the period's posted activity does not balance (cannot happen unless the
  ledger was tampered with; checked anyway as a final sanity gate).

The first two rules are also enforced by database triggers, so a direct SQL
``UPDATE ... SET status = 'closed'`` cannot bypass them either.

Concurrency (see ``app.services.locking``): ``close_period`` and
``lock_period`` re-read the period row under an exclusive lock *before*
validating and keep it until commit. Every transaction that creates,
re-dates, posts, generates or reverses a journal into the period holds a
share lock on the same row while it runs. Therefore:

* a journal transaction that started before the close commits first, and the
  close's validation sees it;
* a journal transaction that starts while the close is in progress waits,
  then re-reads the period and finds it closed (posting is refused; drafts
  may still be created, they simply cannot be posted);
* two concurrent closes serialise and the second finds the period already
  closed (409).

On SQLite all writers are serialised by ``BEGIN IMMEDIATE``, which gives the
same outcome with coarser granularity. Dates not covered by any period are
unrestricted.
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountingPeriod,
    EntryStatus,
    FiscalYear,
    FiscalYearStatus,
    JournalEntry,
    PeriodStatus,
)
from app.services import hooks
from app.services.exceptions import ConflictError, UnprocessableError
from app.services.locking import lock_for_update
from app.services.reports import trial_balance

BLOCKING_STATUSES = (PeriodStatus.CLOSED, PeriodStatus.LOCKED)


class PeriodOverlap(ConflictError):
    def __init__(self, other: AccountingPeriod) -> None:
        super().__init__(f"Period overlaps '{other.name}' ({other.start_date} to {other.end_date})")


class PeriodStateConflict(ConflictError):
    pass


class PeriodNotClosable(ConflictError):
    def __init__(self, period: AccountingPeriod, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Period '{period.name}' cannot be closed: " + "; ".join(problems))


class PeriodNameTaken(ConflictError):
    pass


def period_for(db: Session, on: date) -> AccountingPeriod | None:
    return db.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.start_date <= on, AccountingPeriod.end_date >= on
        )
    )


def closed_period_for(db: Session, on: date) -> AccountingPeriod | None:
    """The closed *or locked* period containing ``on``, if any."""
    period = period_for(db, on)
    return period if period is not None and period.status in BLOCKING_STATUSES else None


def create_period(
    db: Session,
    name: str,
    start_date: date,
    end_date: date,
    fiscal_year: FiscalYear | None = None,
    *,
    commit: bool = True,
) -> AccountingPeriod:
    if start_date > end_date:
        raise UnprocessableError("start_date must not be after end_date")
    if db.scalar(select(AccountingPeriod).where(AccountingPeriod.name == name)):
        raise PeriodNameTaken(f"Period '{name}' already exists")
    overlapping = db.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.start_date <= end_date, AccountingPeriod.end_date >= start_date
        )
    )
    if overlapping is not None:
        raise PeriodOverlap(overlapping)
    if fiscal_year is not None:
        if fiscal_year.status != FiscalYearStatus.OPEN:
            raise PeriodStateConflict(f"Fiscal year '{fiscal_year.name}' is closed")
        if start_date < fiscal_year.start_date or end_date > fiscal_year.end_date:
            raise UnprocessableError(
                f"Period must lie within fiscal year '{fiscal_year.name}' "
                f"({fiscal_year.start_date} to {fiscal_year.end_date})"
            )

    period = AccountingPeriod(
        name=name, start_date=start_date, end_date=end_date, fiscal_year=fiscal_year
    )
    db.add(period)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(period)
    return period


def close_problems(db: Session, period: AccountingPeriod) -> list[str]:
    """Month-end validation: every reason the period cannot be closed right now."""
    problems: list[str] = []

    drafts = db.scalars(
        select(JournalEntry.id)
        .where(
            JournalEntry.status == EntryStatus.DRAFT,
            JournalEntry.entry_date >= period.start_date,
            JournalEntry.entry_date <= period.end_date,
        )
        .order_by(JournalEntry.id)
    ).all()
    if drafts:
        problems.append(f"draft journal entries dated in the period: {drafts}")

    earlier_open = db.scalars(
        select(AccountingPeriod.name)
        .where(
            AccountingPeriod.end_date < period.start_date,
            AccountingPeriod.status == PeriodStatus.OPEN,
        )
        .order_by(AccountingPeriod.start_date)
    ).all()
    if earlier_open:
        problems.append(f"earlier periods are still open: {', '.join(earlier_open)}")

    if not trial_balance(db, period.start_date, period.end_date).is_balanced:
        problems.append("posted activity in the period does not balance")

    return problems


def _locked(db: Session, period: AccountingPeriod) -> AccountingPeriod:
    """Re-read ``period`` under an exclusive row lock; the fresh state is authoritative."""
    fresh = lock_for_update(db, AccountingPeriod, period.id)
    if fresh is None:
        raise PeriodStateConflict(f"Period {period.id} no longer exists")
    return fresh


def _commit(db: Session, period: AccountingPeriod, commit: bool) -> AccountingPeriod:
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(period)
    return period


def close_period(db: Session, period: AccountingPeriod, *, commit: bool = True) -> AccountingPeriod:
    try:
        period = _locked(db, period)
        if period.status == PeriodStatus.CLOSED:
            raise PeriodStateConflict(f"Period '{period.name}' is already closed")
        if period.status == PeriodStatus.LOCKED:
            raise PeriodStateConflict(f"Period '{period.name}' is locked")
        problems = close_problems(db, period)
        if problems:
            raise PeriodNotClosable(period, problems)
        hooks.checkpoint("periods.close_before_commit")
        period.status = PeriodStatus.CLOSED
        period.closed_at = datetime.now(timezone.utc)
    except Exception:
        if commit:
            db.rollback()
        raise
    return _commit(db, period, commit)


def reopen_period(
    db: Session, period: AccountingPeriod, *, commit: bool = True
) -> AccountingPeriod:
    try:
        period = _locked(db, period)
        if period.status == PeriodStatus.OPEN:
            raise PeriodStateConflict(f"Period '{period.name}' is already open")
        if period.status == PeriodStatus.LOCKED:
            raise PeriodStateConflict(f"Period '{period.name}' is locked and cannot be reopened")
        year = period.fiscal_year
        if year is not None and year.status == FiscalYearStatus.CLOSED:
            raise PeriodStateConflict(
                f"Period '{period.name}' belongs to closed fiscal year '{year.name}' "
                "and cannot be reopened"
            )
        period.status = PeriodStatus.OPEN
        period.closed_at = None
    except Exception:
        if commit:
            db.rollback()
        raise
    return _commit(db, period, commit)


def lock_period(db: Session, period: AccountingPeriod, *, commit: bool = True) -> AccountingPeriod:
    try:
        period = _locked(db, period)
        if period.status == PeriodStatus.LOCKED:
            raise PeriodStateConflict(f"Period '{period.name}' is already locked")
        if period.status == PeriodStatus.OPEN:
            raise PeriodStateConflict(
                f"Period '{period.name}' must be closed before it can be locked"
            )
        hooks.checkpoint("periods.lock_before_commit")
        period.status = PeriodStatus.LOCKED
        period.locked_at = datetime.now(timezone.utc)
    except Exception:
        if commit:
            db.rollback()
        raise
    return _commit(db, period, commit)
