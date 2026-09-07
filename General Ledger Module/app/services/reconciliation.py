"""General Ledger reconciliation and validation checks.

``run_checks`` inspects the ledger (optionally a date range) and returns
structured findings. Every check names the affected entries, accounts and
amounts so problems can be investigated directly. The checks are GL-internal
integrity verifications, not financial reporting:

* ``unbalanced_posted_entries``   - posted entries whose debits != credits
  (impossible through the API; would indicate tampering or a bug);
* ``invalid_posted_lines``        - posted lines without an amount, with both
  sides, on a summary account, or on an inactive account at posting time is
  not knowable after the fact, so only summary accounts are checked;
* ``duplicate_source_references`` - different operations of the same source
  document with identical accounting data (probable double processing);
* ``unmatched_references``        - partial source references, reversals of
  non-posted or system entries, reversals whose lines do not mirror the
  original, recurring drafts whose template no longer exists;
* ``invalid_posting_dates``       - posted entries that were *created after*
  the period containing their date had been closed or locked (only the
  year-end ``closing`` entry may do that; anything else means the period gate
  was bypassed), plus a warning for posted entries dated outside every
  period when periods exist at all;
* ``period_state``                - overlapping periods, closed periods with
  earlier periods still open, locked periods without ``locked_at``, periods of
  a closed fiscal year that are still open, fiscal years closed without a
  closing entry although temporary accounts had activity, closing entries
  that do not belong to a closed year;
* ``balance_discrepancies``       - per account, the ``Decimal`` sum of posted
  line detail versus the database's own ``SUM()`` aggregate and versus the
  trial balance row; any difference is reported with both figures;
* ``drafts_in_closed_periods``    - informational: drafts that can never be
  posted where they are dated;
* ``hierarchy_integrity``         - cycles, parents that are not summary
  accounts of the same type, categories of the wrong type, summary accounts
  with own postings, posting accounts with children;
* ``duplicate_source_operations`` - the same (source_type, source_id,
  operation_id) on more than one entry;
* ``posting_metadata``            - posted entries without metadata, drafts
  with metadata, ``posted_at`` before ``created_at``, unknown posting
  sources, processing-sourced entries without a source reference;
* ``orphaned_records``            - aggregate rows without posted lines,
  closing records left ``in_progress`` or whose year is not closed;
* ``fiscal_periods``              - periods outside their fiscal year, gaps
  in a closed year's periods;
* ``closing_consistency``         - every closed year's attestation
  reproduced from posted history (hash, totals, closing entry lines);
* ``aggregate_balances``          - ``account_daily_totals`` versus the
  posted lines, per account and day.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CATEGORY_TYPES,
    NON_REVERSIBLE_TYPES,
    TEMPORARY_TYPES,
    Account,
    AccountDailyTotal,
    AccountingPeriod,
    ClosingStatus,
    EntryStatus,
    EntryType,
    FiscalYear,
    FiscalYearClosing,
    FiscalYearStatus,
    JournalEntry,
    JournalLine,
    PeriodStatus,
    RecurringTemplate,
)
from app.services.reports import account_totals, trial_balance

KNOWN_POSTING_SOURCES = {
    "api",
    "service",
    "batch",
    "processing",
    "reversal",
    "year_end_close",
    "opening_balance",
    "legacy",
}

ZERO = Decimal("0.00")


@dataclass
class Finding:
    message: str
    entry_ids: list[int] = field(default_factory=list)
    account_ids: list[int] = field(default_factory=list)
    amounts: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class Check:
    name: str
    severity: str  # "error" or "warning"
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass
class ReconciliationReport:
    date_from: date | None
    date_to: date | None
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.severity == "error")

    @property
    def error_count(self) -> int:
        return sum(len(c.findings) for c in self.checks if c.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(len(c.findings) for c in self.checks if c.severity == "warning")


def _entries(db: Session, date_from: date | None, date_to: date | None, status=None):
    stmt = select(JournalEntry).options(selectinload(JournalEntry.lines))
    if status is not None:
        stmt = stmt.where(JournalEntry.status == status)
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    return db.scalars(stmt.order_by(JournalEntry.id)).all()


def check_unbalanced(posted: list[JournalEntry]) -> Check:
    check = Check("unbalanced_posted_entries", "error")
    for entry in posted:
        debit = sum((line.debit for line in entry.lines), ZERO)
        credit = sum((line.credit for line in entry.lines), ZERO)
        if debit != credit or len(entry.lines) < 2:
            check.findings.append(
                Finding(
                    f"posted entry {entry.id} does not balance",
                    entry_ids=[entry.id],
                    account_ids=sorted({line.account_id for line in entry.lines}),
                    amounts={"debit": debit, "credit": credit, "difference": debit - credit},
                )
            )
    return check


def check_invalid_lines(db: Session, posted: list[JournalEntry]) -> Check:
    check = Check("invalid_posted_lines", "error")
    summary_ids = set(db.scalars(select(Account.id).where(Account.is_summary.is_(True))))
    for entry in posted:
        for line in entry.lines:
            problem = None
            if line.debit == 0 and line.credit == 0:
                problem = "has no amount"
            elif line.debit > 0 and line.credit > 0:
                problem = "has both a debit and a credit"
            elif line.debit < 0 or line.credit < 0:
                problem = "has a negative amount"
            elif line.account_id in summary_ids:
                problem = "is posted to a summary account"
            if problem:
                check.findings.append(
                    Finding(
                        f"posted entry {entry.id} line {line.id} {problem}",
                        entry_ids=[entry.id],
                        account_ids=[line.account_id],
                        amounts={"debit": line.debit, "credit": line.credit},
                    )
                )
    return check


def check_duplicate_sources(db: Session, posted: list[JournalEntry]) -> Check:
    check = Check("duplicate_source_references", "warning")
    groups: dict[tuple, list[JournalEntry]] = {}
    for entry in posted:
        if entry.source_type is None:
            continue
        key = (entry.source_type, entry.source_id, entry.request_fingerprint)
        groups.setdefault(key, []).append(entry)
    for (source_type, source_id, _fp), entries in groups.items():
        if len(entries) > 1:
            check.findings.append(
                Finding(
                    f"source {source_type}/{source_id} produced {len(entries)} entries with "
                    "identical accounting data under different operation ids",
                    entry_ids=[e.id for e in entries],
                    account_ids=sorted({line.account_id for e in entries for line in e.lines}),
                    amounts={"debit": sum((ln.debit for e in entries for ln in e.lines), ZERO)},
                )
            )
    return check


def check_unmatched_references(db: Session, entries: list[JournalEntry]) -> Check:
    check = Check("unmatched_references", "error")
    by_id = {e.id: e for e in entries}
    template_ids = set(db.scalars(select(RecurringTemplate.id)))
    for entry in entries:
        provenance = (entry.source_type, entry.source_id, entry.operation_id)
        if any(p is not None for p in provenance) and not all(p is not None for p in provenance):
            check.findings.append(
                Finding(f"entry {entry.id} has a partial source reference", entry_ids=[entry.id])
            )
        if entry.source_type is not None and entry.request_fingerprint is None:
            check.findings.append(
                Finding(f"entry {entry.id} has a source reference but no fingerprint", [entry.id])
            )
        if entry.reverses_entry_id is not None:
            original = by_id.get(entry.reverses_entry_id) or db.get(
                JournalEntry, entry.reverses_entry_id
            )
            if original is None or original.status != EntryStatus.POSTED:
                check.findings.append(
                    Finding(
                        f"reversal {entry.id} references a missing or unposted entry",
                        entry_ids=[entry.id, entry.reverses_entry_id],
                    )
                )
            elif original.entry_type in NON_REVERSIBLE_TYPES:
                check.findings.append(
                    Finding(
                        f"reversal {entry.id} reverses a system {original.entry_type.value} entry",
                        entry_ids=[entry.id, original.id],
                    )
                )
            elif entry.status == EntryStatus.POSTED:
                mirror = sorted((ln.account_id, ln.credit, ln.debit) for ln in original.lines)
                actual = sorted((ln.account_id, ln.debit, ln.credit) for ln in entry.lines)
                if mirror != actual:
                    check.findings.append(
                        Finding(
                            f"reversal {entry.id} does not mirror entry {original.id}",
                            entry_ids=[entry.id, original.id],
                            account_ids=sorted({ln.account_id for ln in entry.lines}),
                        )
                    )
        if (
            entry.recurring_template_id is not None
            and entry.recurring_template_id not in template_ids
        ):
            check.findings.append(
                Finding(
                    f"entry {entry.id} references missing recurring template "
                    f"{entry.recurring_template_id}",
                    entry_ids=[entry.id],
                )
            )
    return check


def check_posting_dates(
    db: Session, posted: list[JournalEntry], periods: list[AccountingPeriod]
) -> Check:
    check = Check("invalid_posting_dates", "error")
    for entry in posted:
        period = next((p for p in periods if p.start_date <= entry.entry_date <= p.end_date), None)
        if period is None:
            if periods:
                check.findings.append(
                    Finding(
                        f"posted entry {entry.id} is dated {entry.entry_date}, outside every "
                        "accounting period (informational)",
                        entry_ids=[entry.id],
                    )
                )
            continue
        gate_time = period.locked_at or period.closed_at
        if (
            period.status != PeriodStatus.OPEN
            and gate_time is not None
            and entry.created_at is not None
            and _utc(entry.created_at) > _utc(gate_time)
            and not (entry.entry_type == EntryType.CLOSING and period.locked_at is None)
        ):
            check.findings.append(
                Finding(
                    f"posted entry {entry.id} ({entry.entry_type.value}) was created after "
                    f"period '{period.name}' was {period.status.value}",
                    entry_ids=[entry.id],
                    amounts={"debit": sum((ln.debit for ln in entry.lines), ZERO)},
                )
            )
    return check


def _utc(value):
    """Compare timestamps regardless of whether the backend stored a timezone."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def check_period_state(
    db: Session, periods: list[AccountingPeriod], entries: list[JournalEntry]
) -> Check:
    check = Check("period_state", "error")
    for i, period in enumerate(periods):
        for other in periods[i + 1 :]:
            if period.start_date <= other.end_date and other.start_date <= period.end_date:
                check.findings.append(
                    Finding(f"periods '{period.name}' and '{other.name}' overlap")
                )
        if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
            earlier_open = [
                p.name
                for p in periods
                if p.end_date < period.start_date and p.status == PeriodStatus.OPEN
            ]
            if earlier_open:
                check.findings.append(
                    Finding(
                        f"period '{period.name}' is {period.status.value} while earlier "
                        f"periods are open: {', '.join(earlier_open)}"
                    )
                )
        if period.status == PeriodStatus.LOCKED and period.locked_at is None:
            check.findings.append(Finding(f"period '{period.name}' is locked without locked_at"))
        if period.status != PeriodStatus.OPEN and period.closed_at is None:
            check.findings.append(
                Finding(f"period '{period.name}' is {period.status.value} without closed_at")
            )

    years = db.scalars(select(FiscalYear).order_by(FiscalYear.start_date)).all()
    for year in years:
        if year.status != FiscalYearStatus.CLOSED:
            continue
        still_open = [
            p.name for p in periods if p.fiscal_year_id == year.id and p.status == PeriodStatus.OPEN
        ]
        if still_open:
            check.findings.append(
                Finding(
                    f"fiscal year '{year.name}' is closed but periods are open: "
                    f"{', '.join(still_open)}"
                )
            )
        activity = [
            e
            for e in entries
            if e.status == EntryStatus.POSTED
            and e.entry_type != EntryType.CLOSING
            and year.start_date <= e.entry_date <= year.end_date
        ]
        temporary_ids = set(db.scalars(select(Account.id).where(Account.type.in_(TEMPORARY_TYPES))))
        net = sum(
            (
                ln.debit - ln.credit
                for e in activity
                for ln in e.lines
                if ln.account_id in temporary_ids
            ),
            ZERO,
        )
        if net != 0 and year.closing_entry_id is None:
            check.findings.append(
                Finding(
                    f"fiscal year '{year.name}' is closed without a closing entry although "
                    "revenue/expense accounts have a balance",
                    amounts={"net_balance": net},
                )
            )
        if year.closing_entry_id is not None:
            closing = db.get(JournalEntry, year.closing_entry_id)
            if (
                closing is None
                or closing.status != EntryStatus.POSTED
                or closing.entry_type != EntryType.CLOSING
            ):
                check.findings.append(
                    Finding(
                        f"fiscal year '{year.name}' references an invalid closing entry",
                        entry_ids=[year.closing_entry_id],
                    )
                )
    closing_ids = {y.closing_entry_id for y in years if y.closing_entry_id is not None}
    for entry in entries:
        if entry.entry_type == EntryType.CLOSING and entry.id not in closing_ids:
            check.findings.append(
                Finding(
                    f"closing entry {entry.id} does not belong to any closed fiscal year",
                    entry_ids=[entry.id],
                )
            )
    return check


def check_balances(db: Session, date_from: date | None, date_to: date | None) -> Check:
    """Line detail summed in Python (Decimal) vs. SQL aggregation vs. the trial balance row."""
    check = Check("balance_discrepancies", "error")
    detail: dict[int, tuple[Decimal, Decimal]] = {}
    stmt = (
        select(JournalLine.account_id, JournalLine.debit, JournalLine.credit)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == EntryStatus.POSTED)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    for account_id, debit, credit in db.execute(stmt):
        d, c = detail.get(account_id, (ZERO, ZERO))
        detail[account_id] = (d + debit, c + credit)
    aggregate = account_totals(db, date_from, date_to)
    tb = {
        row.account_id: (row.debit, row.credit)
        for row in trial_balance(db, date_from, date_to).rows
        if not row.is_summary
    }

    for account_id in sorted(set(detail) | set(aggregate)):
        d_detail = detail.get(account_id, (ZERO, ZERO))
        d_agg = aggregate.get(account_id, (ZERO, ZERO))
        d_tb = tb.get(account_id, (ZERO, ZERO))
        if d_detail != d_agg or d_detail != d_tb:
            check.findings.append(
                Finding(
                    f"account {account_id}: line detail, database aggregate and trial balance "
                    "disagree",
                    account_ids=[account_id],
                    amounts={
                        "detail_debit": d_detail[0],
                        "detail_credit": d_detail[1],
                        "aggregate_debit": d_agg[0],
                        "aggregate_credit": d_agg[1],
                        "trial_balance_debit": d_tb[0],
                        "trial_balance_credit": d_tb[1],
                    },
                )
            )
    return check


def check_drafts_in_closed_periods(
    drafts: list[JournalEntry], periods: list[AccountingPeriod]
) -> Check:
    check = Check("drafts_in_closed_periods", "warning")
    for entry in drafts:
        period = next((p for p in periods if p.start_date <= entry.entry_date <= p.end_date), None)
        if period is not None and period.status != PeriodStatus.OPEN:
            check.findings.append(
                Finding(
                    f"draft {entry.id} is dated in {period.status.value} period '{period.name}' "
                    "and can never be posted there",
                    entry_ids=[entry.id],
                )
            )
    return check


def check_hierarchy(db: Session) -> Check:
    check = Check("hierarchy_integrity", "error")
    accounts = db.scalars(select(Account).order_by(Account.code)).all()
    by_id = {a.id: a for a in accounts}
    with_lines = set(db.scalars(select(JournalLine.account_id).distinct()))
    children: dict[int, list[int]] = {}
    for account in accounts:
        if account.parent_id is not None:
            children.setdefault(account.parent_id, []).append(account.id)
    for account in accounts:
        parent = by_id.get(account.parent_id) if account.parent_id is not None else None
        if account.parent_id is not None and parent is None:
            check.findings.append(
                Finding(f"account {account.code} has a missing parent", account_ids=[account.id])
            )
        elif parent is not None:
            if not parent.is_summary or parent.type != account.type:
                check.findings.append(
                    Finding(
                        f"account {account.code} is under {parent.code}, which is not a summary "
                        f"account of type {account.type.value}",
                        account_ids=[account.id, parent.id],
                    )
                )
            seen = {account.id}
            cursor = parent
            while cursor is not None:
                if cursor.id in seen:
                    check.findings.append(
                        Finding(
                            f"account {account.code} is part of a hierarchy cycle",
                            account_ids=sorted(seen),
                        )
                    )
                    break
                seen.add(cursor.id)
                cursor = by_id.get(cursor.parent_id) if cursor.parent_id is not None else None
        if account.category is not None and CATEGORY_TYPES[account.category] != account.type:
            check.findings.append(
                Finding(
                    f"account {account.code} has category {account.category.value} of the wrong "
                    "type",
                    account_ids=[account.id],
                )
            )
        if account.is_summary and account.id in with_lines:
            check.findings.append(
                Finding(
                    f"summary account {account.code} has journal lines", account_ids=[account.id]
                )
            )
        if not account.is_summary and account.id in children:
            check.findings.append(
                Finding(
                    f"posting account {account.code} has child accounts",
                    account_ids=[account.id, *children[account.id]],
                )
            )
    return check


def check_duplicate_operations(entries: list[JournalEntry]) -> Check:
    check = Check("duplicate_source_operations", "error")
    seen: dict[tuple, list[int]] = {}
    for entry in entries:
        if entry.source_type is not None:
            key = (entry.source_type, entry.source_id, entry.operation_id)
            seen.setdefault(key, []).append(entry.id)
    for key, ids in seen.items():
        if len(ids) > 1:
            check.findings.append(
                Finding(f"source operation {'/'.join(key)} appears on {len(ids)} entries", ids)
            )
    return check


def check_posting_metadata(db: Session, entries: list[JournalEntry]) -> Check:
    check = Check("posting_metadata", "error")
    closing_ids = set(
        db.scalars(
            select(FiscalYearClosing.closing_entry_id).where(
                FiscalYearClosing.closing_entry_id.isnot(None)
            )
        )
    )
    for entry in entries:
        meta = (entry.posted_at, entry.posted_by, entry.posting_source, entry.correlation_id)
        if entry.status == EntryStatus.POSTED:
            if any(m is None for m in meta):
                check.findings.append(
                    Finding(f"posted entry {entry.id} lacks posting metadata", [entry.id])
                )
                continue
            if entry.created_at is not None and _utc(entry.posted_at) < _utc(entry.created_at):
                check.findings.append(
                    Finding(f"entry {entry.id} was posted before it was created", [entry.id])
                )
            if entry.posting_source not in KNOWN_POSTING_SOURCES:
                check.findings.append(
                    Finding(
                        f"entry {entry.id} has unknown posting source '{entry.posting_source}'",
                        [entry.id],
                    )
                )
            if entry.posting_source in ("processing", "opening_balance") and (
                entry.source_type is None
            ):
                check.findings.append(
                    Finding(
                        f"entry {entry.id} was posted by {entry.posting_source} but has no "
                        "source reference",
                        [entry.id],
                    )
                )
            if entry.entry_type == EntryType.CLOSING and entry.id not in closing_ids:
                check.findings.append(
                    Finding(
                        f"closing entry {entry.id} is not referenced by any fiscal year closing "
                        "record",
                        [entry.id],
                    )
                )
        elif any(m is not None for m in meta):
            check.findings.append(Finding(f"draft {entry.id} carries posting metadata", [entry.id]))
    return check


def check_orphans(db: Session) -> Check:
    check = Check("orphaned_records", "error")
    posted_days = set(
        db.execute(
            select(JournalLine.account_id, JournalEntry.entry_date)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(JournalEntry.status == EntryStatus.POSTED)
            .distinct()
        ).all()
    )
    for row in db.scalars(select(AccountDailyTotal)):
        if (row.account_id, row.entry_date) not in posted_days:
            check.findings.append(
                Finding(
                    f"aggregate row for account {row.account_id} on {row.entry_date} has no "
                    "posted lines",
                    account_ids=[row.account_id],
                    amounts={
                        "debit": Decimal(row.debit_cents) / 100,
                        "credit": Decimal(row.credit_cents) / 100,
                    },
                )
            )
    for record in db.scalars(select(FiscalYearClosing)):
        year = db.get(FiscalYear, record.fiscal_year_id)
        if record.status == ClosingStatus.IN_PROGRESS:
            check.findings.append(
                Finding(
                    f"fiscal year closing {record.id} is still in progress outside a closing "
                    "transaction (delete it and re-run the close)"
                )
            )
        elif year is None or year.status != FiscalYearStatus.CLOSED:
            check.findings.append(
                Finding(f"fiscal year closing {record.id} belongs to a year that is not closed")
            )
    return check


def check_fiscal_periods(db: Session, periods: list[AccountingPeriod]) -> Check:
    check = Check("fiscal_periods", "error")
    years = db.scalars(select(FiscalYear).order_by(FiscalYear.start_date)).all()
    for year in years:
        own = sorted(
            (p for p in periods if p.fiscal_year_id == year.id), key=lambda p: p.start_date
        )
        for period in own:
            if period.start_date < year.start_date or period.end_date > year.end_date:
                check.findings.append(
                    Finding(f"period '{period.name}' lies outside fiscal year '{year.name}'")
                )
        if year.status == FiscalYearStatus.CLOSED:
            cursor = year.start_date
            for period in own:
                if period.start_date > cursor:
                    check.findings.append(
                        Finding(
                            f"closed fiscal year '{year.name}' has no period covering "
                            f"{cursor} to {period.start_date - timedelta(days=1)}"
                        )
                    )
                cursor = max(cursor, period.end_date + timedelta(days=1))
            if cursor <= year.end_date:
                check.findings.append(
                    Finding(
                        f"closed fiscal year '{year.name}' has no period covering {cursor} "
                        f"to {year.end_date}"
                    )
                )
    return check


def check_closing_consistency(db: Session) -> Check:
    from app.services.fiscal_years import verify_closing

    check = Check("closing_consistency", "error")
    for year in db.scalars(select(FiscalYear).where(FiscalYear.status == FiscalYearStatus.CLOSED)):
        verification = verify_closing(db, year)
        for problem in verification.problems:
            check.findings.append(
                Finding(
                    f"fiscal year '{year.name}': {problem}",
                    entry_ids=[year.closing_entry_id] if year.closing_entry_id else [],
                    amounts={
                        "recorded_net_income": verification.recorded_net_income or Decimal("0"),
                        "recomputed_net_income": verification.recomputed_net_income,
                    },
                )
            )
    return check


def check_aggregate_balances(db: Session) -> Check:
    from app.services.balances import compare

    check = Check("aggregate_balances", "error")
    for d in compare(db).discrepancies:
        check.findings.append(
            Finding(
                f"account {d.account_id} on {d.entry_date}: aggregate differs from posted lines",
                account_ids=[d.account_id],
                amounts={
                    "ledger_debit": Decimal(d.ledger_debit_cents) / 100,
                    "ledger_credit": Decimal(d.ledger_credit_cents) / 100,
                    "aggregate_debit": Decimal(d.aggregate_debit_cents) / 100,
                    "aggregate_credit": Decimal(d.aggregate_credit_cents) / 100,
                },
            )
        )
    return check


def run_checks(
    db: Session, date_from: date | None = None, date_to: date | None = None
) -> ReconciliationReport:
    entries = _entries(db, date_from, date_to)
    posted = [e for e in entries if e.status == EntryStatus.POSTED]
    drafts = [e for e in entries if e.status == EntryStatus.DRAFT]
    periods = db.scalars(select(AccountingPeriod).order_by(AccountingPeriod.start_date)).all()
    checks = [
        check_unbalanced(posted),
        check_invalid_lines(db, posted),
        check_duplicate_sources(db, posted),
        check_duplicate_operations(entries),
        check_unmatched_references(db, entries),
        check_posting_dates(db, posted, periods),
        check_posting_metadata(db, entries),
        check_period_state(db, periods, entries),
        check_fiscal_periods(db, periods),
        check_closing_consistency(db),
        check_hierarchy(db),
        check_orphans(db),
        check_balances(db, date_from, date_to),
        check_aggregate_balances(db),
        check_drafts_in_closed_periods(drafts, periods),
    ]
    return ReconciliationReport(date_from=date_from, date_to=date_to, checks=checks)
