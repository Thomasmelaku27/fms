"""Fiscal years and the year-end close.

A fiscal year groups accounting periods. The year-end close (``close_year``)
is an explicit, validated, atomic operation:

1. ``close_problems`` must be empty: the year's date range is fully covered by
   its periods, every period is closed or locked, no draft is dated inside the
   year, the retained-earnings account is an active equity posting account;
2. ``closing_balances`` computes, in ``Decimal``, the year's balance of every
   revenue and expense account (posted entries only, excluding any previous
   closing entry) and the resulting net income;
3. one balanced ``closing`` journal entry dated on the year's last day zeroes
   each temporary account and posts the net to retained earnings. It is the
   only kind of entry allowed into a *closed* period (not a locked one), so
   the period containing the year's last day must be closed - not locked -
   when the year closes;
4. the year becomes ``closed`` (final) in the same transaction. If no
   temporary account has a balance, the year closes without an entry.

Balance-sheet accounts are not touched: their cumulative balances carry
forward by construction (reports compute opening balances from all prior
posted activity), and the closing entry leaves total equity unchanged.

Authorisation and attestation: the close first inserts a
``FiscalYearClosing`` record (``in_progress``) naming the year, then creates
the closing entry and points the record at it. The database only lets a
``closing`` entry into a closed period while such an in-progress record names
that very entry, and only lets a year become ``closed`` together with its
record - so neither ordinary callers nor direct SQL can forge closing
journals. In the same transaction the record is completed with the closing
totals, the covered periods, the reconciliation verdict, a hash of the year's
posted ledger lines, the actor and a correlation id; completed records are
immutable (trigger). ``verify_closing`` recomputes everything from posted
history and compares it with the record.

Idempotency and failure: the year row is locked ``FOR UPDATE`` first; a
repeated close of a closed year is refused (409) with the existing entry's id,
and two concurrent closes serialise. Any failure (validation, posting rule,
database error, fault at commit) rolls back the entry, its lines, the record
and the status change together, leaving the ledger exactly as it was.

Recovery after a failed or interrupted close
    Nothing of a close survives a failure: there is no partial closing entry,
    no in-progress record and the year stays open. Recovery is therefore:
    (1) run ``GET /fiscal-years/{id}/close-check`` and ``GET /reconciliation``
    to confirm the year is still closable; (2) simply run the close again.
    If a closing record exists with status ``in_progress`` outside a running
    transaction, the database was modified by hand; delete that record (it is
    not protected) and re-run the close.

Corrections after close
    Posted transactions are never rewritten. A correction to a closed year is
    posted in the next open fiscal year as an ``adjusting`` entry against the
    retained-earnings account (a prior-period adjustment), or as a reversal of
    the original entry dated in an open period. Periods of a closed year cannot
    be reopened; the closing attestation of the closed year stays valid
    because it describes the ledger as it was at the time of the close.

Period closure, period locking and fiscal-year closure remain three distinct
operations: close (reversible, month-end validation), lock (final, per
period) and year close (final, posts the closing entry, needs all periods
closed and the year-end period *not yet locked*).
"""

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    TEMPORARY_TYPES,
    Account,
    AccountingPeriod,
    AccountType,
    ClosingStatus,
    EntryStatus,
    EntryType,
    FiscalYear,
    FiscalYearClosing,
    FiscalYearStatus,
    JournalEntry,
    JournalLine,
    PeriodStatus,
)
from app.services import hooks
from app.services.actor import PostingContext, system_context
from app.services.exceptions import ConflictError, UnprocessableError
from app.services.locking import lock_for_update
from app.services.periods import create_period
from app.services.posting import post_entry
from app.services.reports import trial_balance

ZERO = Decimal("0.00")


class FiscalYearStateConflict(ConflictError):
    pass


class FiscalYearNotClosable(ConflictError):
    def __init__(self, year: FiscalYear, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Fiscal year '{year.name}' cannot be closed: " + "; ".join(problems))


@dataclass
class ClosingLine:
    account_id: int
    code: str
    name: str
    type: str
    balance: Decimal  # debit-positive balance of the account for the year
    debit: Decimal  # what the closing entry debits to this account
    credit: Decimal  # what the closing entry credits to this account


@dataclass
class ClosingBalances:
    fiscal_year_id: int
    retained_earnings_account_id: int | None
    lines: list[ClosingLine]
    net_income: Decimal  # revenue minus expenses (positive = profit)

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), ZERO)


def _validate_retained_earnings(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise UnprocessableError(f"Unknown retained earnings account id {account_id}")
    if account.type != AccountType.EQUITY:
        raise UnprocessableError(
            f"Retained earnings account {account.code} must be an equity account"
        )
    if account.is_summary:
        raise UnprocessableError(f"Retained earnings account {account.code} is a summary account")
    return account


def _month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def create_fiscal_year(
    db: Session,
    *,
    name: str,
    start_date: date,
    end_date: date,
    retained_earnings_account_id: int | None,
    generate_monthly_periods: bool = False,
    commit: bool = True,
) -> FiscalYear:
    if start_date > end_date:
        raise UnprocessableError("start_date must not be after end_date")
    if db.scalar(select(FiscalYear).where(FiscalYear.name == name)):
        raise ConflictError(f"Fiscal year '{name}' already exists")
    overlapping = db.scalar(
        select(FiscalYear).where(
            FiscalYear.start_date <= end_date, FiscalYear.end_date >= start_date
        )
    )
    if overlapping is not None:
        raise ConflictError(f"Fiscal year overlaps '{overlapping.name}'")
    if retained_earnings_account_id is not None:
        _validate_retained_earnings(db, retained_earnings_account_id)

    year = FiscalYear(
        name=name,
        start_date=start_date,
        end_date=end_date,
        retained_earnings_account_id=retained_earnings_account_id,
    )
    db.add(year)
    try:
        db.flush()
        if generate_monthly_periods:
            first = start_date
            while first <= end_date:
                last = min(_month_end(first), end_date)
                create_period(db, f"{first:%Y-%m}", first, last, fiscal_year=year, commit=False)
                first = last + timedelta(days=1)
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(year)
    return year


def year_periods(db: Session, year: FiscalYear) -> list[AccountingPeriod]:
    return db.scalars(
        select(AccountingPeriod)
        .where(AccountingPeriod.fiscal_year_id == year.id)
        .order_by(AccountingPeriod.start_date)
    ).all()


def close_problems(db: Session, year: FiscalYear) -> list[str]:
    """Year-end validation: every reason the year cannot be closed right now."""
    problems: list[str] = []
    periods = year_periods(db, year)

    cursor = year.start_date
    gaps: list[str] = []
    for period in periods:
        if period.start_date > cursor:
            gaps.append(f"{cursor} to {period.start_date - timedelta(days=1)}")
        cursor = max(cursor, period.end_date + timedelta(days=1))
    if cursor <= year.end_date:
        gaps.append(f"{cursor} to {year.end_date}")
    if not periods:
        problems.append("the fiscal year has no accounting periods")
    elif gaps:
        problems.append(f"dates not covered by any period: {', '.join(gaps)}")

    still_open = [p.name for p in periods if p.status == PeriodStatus.OPEN]
    if still_open:
        problems.append(f"periods still open: {', '.join(still_open)}")
    last = next((p for p in periods if p.start_date <= year.end_date <= p.end_date), None)
    if last is not None and last.status == PeriodStatus.LOCKED:
        problems.append(
            f"period '{last.name}' containing the year end is locked; the closing entry "
            "must be posted before locking it"
        )

    drafts = db.scalars(
        select(JournalEntry.id)
        .where(
            JournalEntry.status == EntryStatus.DRAFT,
            JournalEntry.entry_date >= year.start_date,
            JournalEntry.entry_date <= year.end_date,
        )
        .order_by(JournalEntry.id)
    ).all()
    if drafts:
        problems.append(f"draft journal entries dated in the year: {drafts}")

    if year.retained_earnings_account_id is None:
        problems.append("no retained earnings account configured")
    else:
        account = db.get(Account, year.retained_earnings_account_id)
        if account is None or account.type != AccountType.EQUITY or account.is_summary:
            problems.append("retained earnings account must be an equity posting account")
        elif not account.is_active:
            problems.append(f"retained earnings account {account.code} is inactive")

    if not trial_balance(db, year.start_date, year.end_date).is_balanced:
        problems.append("posted activity in the year does not balance")
    return problems


def closing_balances(db: Session, year: FiscalYear) -> ClosingBalances:
    """Per temporary account: the year's posted balance and the closing entry lines."""
    totals: dict[int, Decimal] = {}
    rows = db.execute(
        select(JournalLine.account_id, JournalLine.debit, JournalLine.credit)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.status == EntryStatus.POSTED,
            JournalEntry.entry_type != EntryType.CLOSING,
            JournalEntry.entry_date >= year.start_date,
            JournalEntry.entry_date <= year.end_date,
            Account.type.in_(TEMPORARY_TYPES),
        )
    ).all()
    for account_id, debit, credit in rows:
        totals[account_id] = totals.get(account_id, ZERO) + debit - credit

    lines: list[ClosingLine] = []
    net_income = ZERO
    for account in db.scalars(select(Account).where(Account.id.in_(totals)).order_by(Account.code)):
        balance = totals[account.id]
        if balance == 0:
            continue
        # Zero the account: a debit balance is credited away and vice versa.
        debit = -balance if balance < 0 else ZERO
        credit = balance if balance > 0 else ZERO
        lines.append(
            ClosingLine(
                account_id=account.id,
                code=account.code,
                name=account.name,
                type=account.type.value,
                balance=balance,
                debit=debit,
                credit=credit,
            )
        )
        net_income -= balance  # revenue has credit (negative) balance -> adds to income
    return ClosingBalances(
        fiscal_year_id=year.id,
        retained_earnings_account_id=year.retained_earnings_account_id,
        lines=lines,
        net_income=net_income,
    )


def ledger_hash(db: Session, year: FiscalYear) -> str:
    """SHA-256 over the year's posted lines (excluding closing entries), in a canonical order."""
    rows = db.execute(
        select(
            JournalEntry.id,
            JournalEntry.entry_date,
            JournalEntry.entry_type,
            JournalLine.id,
            JournalLine.account_id,
            JournalLine.debit,
            JournalLine.credit,
        )
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalEntry.status == EntryStatus.POSTED,
            JournalEntry.entry_type != EntryType.CLOSING,
            JournalEntry.entry_date >= year.start_date,
            JournalEntry.entry_date <= year.end_date,
        )
        .order_by(JournalEntry.id, JournalLine.id)
    ).all()
    digest = hashlib.sha256()
    for entry_id, day, entry_type, line_id, account_id, debit, credit in rows:
        digest.update(
            f"{entry_id}|{day.isoformat()}|{EntryType(entry_type).value}|{line_id}|{account_id}|"
            f"{debit:.2f}|{credit:.2f}\n".encode()
        )
    return digest.hexdigest()


def closing_record(db: Session, year: FiscalYear) -> FiscalYearClosing | None:
    return db.scalar(select(FiscalYearClosing).where(FiscalYearClosing.fiscal_year_id == year.id))


@dataclass
class ClosingVerification:
    fiscal_year_id: int
    ok: bool
    problems: list[str]
    recorded_hash: str | None
    recomputed_hash: str
    recorded_net_income: Decimal | None
    recomputed_net_income: Decimal


def verify_closing(db: Session, year: FiscalYear) -> ClosingVerification:
    """Reproduce the close from posted history and compare with the attestation."""
    record = closing_record(db, year)
    recomputed = closing_balances(db, year)
    current_hash = ledger_hash(db, year)
    problems: list[str] = []
    if record is None:
        problems.append("no closing record exists for this fiscal year")
        return ClosingVerification(
            year.id, False, problems, None, current_hash, None, recomputed.net_income
        )
    if record.status != ClosingStatus.COMPLETED:
        problems.append(f"closing record is {record.status.value}")
    if record.ledger_hash not in (current_hash, "legacy-backfill"):
        problems.append("posted ledger history of the year changed since the close")
    if record.ledger_hash != "legacy-backfill" and record.net_income != recomputed.net_income:
        problems.append(
            f"recorded net income {record.net_income} differs from recomputed "
            f"{recomputed.net_income}"
        )
    entry = db.get(JournalEntry, record.closing_entry_id) if record.closing_entry_id else None
    if record.closing_entry_id is not None and (
        entry is None or entry.status != EntryStatus.POSTED or entry.entry_type != EntryType.CLOSING
    ):
        problems.append("closing entry is missing, unposted or not a closing entry")
    if entry is not None and record.ledger_hash != "legacy-backfill":
        expected = sorted(
            (ln.account_id, f"{ln.debit:.2f}", f"{ln.credit:.2f}") for ln in recomputed.lines
        )
        actual = sorted(
            (ln.account_id, f"{ln.debit:.2f}", f"{ln.credit:.2f}")
            for ln in entry.lines
            if ln.account_id != year.retained_earnings_account_id
        )
        if expected != actual:
            problems.append("closing entry lines do not match the recomputed closing balances")
    if year.closing_entry_id != record.closing_entry_id:
        problems.append("fiscal year and closing record disagree about the closing entry")
    return ClosingVerification(
        year.id,
        not problems,
        problems,
        record.ledger_hash,
        current_hash,
        record.net_income,
        recomputed.net_income,
    )


def close_year(
    db: Session,
    year: FiscalYear,
    *,
    ctx: PostingContext | None = None,
    commit: bool = True,
) -> FiscalYear:
    """Execute the year-end close: attest, post the closing entry, close the year - atomically."""
    from app.services.reconciliation import run_checks  # local import: reconciliation uses reports

    ctx = ctx or system_context("year_end_close")
    try:
        fresh = lock_for_update(db, FiscalYear, year.id)
        if fresh is None:
            raise FiscalYearStateConflict(f"Fiscal year {year.id} no longer exists")
        year = fresh
        if year.status == FiscalYearStatus.CLOSED:
            raise FiscalYearStateConflict(
                f"Fiscal year '{year.name}' is already closed"
                + (
                    f" (closing entry {year.closing_entry_id})"
                    if year.closing_entry_id is not None
                    else ""
                )
            )
        for period in year_periods(db, year):
            lock_for_update(db, AccountingPeriod, period.id)  # no reopen/lock meanwhile
        problems = close_problems(db, year)
        if problems:
            raise FiscalYearNotClosable(year, problems)

        # Reconcile the year as it stands *before* the close; the verdict is attested.
        reconciliation = run_checks(db, year.start_date, year.end_date)
        summary = json.dumps(
            {
                "error_count": reconciliation.error_count,
                "warning_count": reconciliation.warning_count,
                "checks": {c.name: len(c.findings) for c in reconciliation.checks},
            }
        )
        if not reconciliation.ok:
            raise FiscalYearNotClosable(
                year, [f"reconciliation reports {reconciliation.error_count} error(s)"]
            )
        balances = closing_balances(db, year)
        periods = year_periods(db, year)
        record = FiscalYearClosing(
            fiscal_year_id=year.id,
            status=ClosingStatus.IN_PROGRESS,
            covered_period_ids=json.dumps([p.id for p in periods]),
            net_income=balances.net_income,
            total_debit=balances.total_debit,
            total_credit=balances.total_credit,
            ledger_hash=ledger_hash(db, year),
            reconciliation_ok=True,
            reconciliation_summary=summary,
            actor_ref=ctx.actor_ref,
            correlation_id=ctx.correlation_id,
        )
        db.add(record)
        db.flush()
        closing_entry = None
        if balances.lines:
            retained = _validate_retained_earnings(db, year.retained_earnings_account_id)
            lines = [
                JournalLine(account_id=line.account_id, debit=line.debit, credit=line.credit)
                for line in balances.lines
            ]
            net = balances.net_income
            if net != 0:  # with a zero result the temporary lines already balance
                lines.append(
                    JournalLine(
                        account_id=retained.id,
                        debit=-net if net < 0 else ZERO,  # a loss reduces retained earnings
                        credit=net if net > 0 else ZERO,
                        memo="net income for the year" if net > 0 else "net loss for the year",
                    )
                )
            closing_entry = JournalEntry(
                entry_date=year.end_date,
                description=f"Year-end close {year.name}",
                reference=year.name,
                entry_type=EntryType.CLOSING,
                lines=lines,
            )
            db.add(closing_entry)
            db.flush()
            record.closing_entry_id = closing_entry.id  # authorises exactly this entry
            db.flush()
            post_entry(db, closing_entry, ctx=ctx.with_source("year_end_close"), commit=False)

        record.status = ClosingStatus.COMPLETED
        record.completed_at = datetime.now(timezone.utc)
        year.status = FiscalYearStatus.CLOSED
        year.closed_at = record.completed_at
        year.closing_entry_id = closing_entry.id if closing_entry is not None else None
        hooks.checkpoint("fiscal_years.close_before_commit")
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(year)
    return year
