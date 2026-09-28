"""Short-term liquidity projection.

The projection is a read-only calculation; it never writes to the ledger or to
any other table. Its components are identified explicitly in the response:

* ``opening_balance``  - historical: posted ledger balance as of ``as_of``;
* ``history``          - historical: actual posted daily net flows before ``as_of``;
* per-day items of kind ``posted_journal_entry`` - posted entries dated inside
  the horizon (post-dated accounting facts);
* per-day items of kind ``planned_cash_flow`` - forecast items with status
  ``planned`` (items already overdue are carried to the first horizon day and
  flagged);
* ``excluded``         - what was deliberately *not* counted: draft entries,
  unmatched bank transactions, realized/cancelled planned flows.

An alert is raised for every day whose projected balance is below the
threshold (query parameter, else the cash account's ``minimum_balance``, else
the sum of configured minimums for the consolidated view).
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry, JournalLine
from app.modules.cash.errors import CashValidationError
from app.modules.cash.models import (
    BankTransaction,
    CashAccount,
    PlannedCashFlow,
    PlannedCashFlowStatus,
    ReconciliationMatch,
)
from app.modules.cash.reports import POSTED_ONLY, daily_flows
from app.modules.cash.services import ZERO, cash_balance, get_cash_account, quantize, snapshot_read


def _threshold(
    db: Session, cash_account: CashAccount | None, override: Decimal | None
) -> tuple[Decimal | None, str | None]:
    if override is not None:
        return quantize(override), "query"
    if cash_account is not None:
        if cash_account.minimum_balance is None:
            return None, None
        return cash_account.minimum_balance, "cash_account"
    minimums = [
        m for m in db.scalars(select(CashAccount.minimum_balance)) if m is not None
    ]
    if not minimums:
        return None, None
    return sum(minimums, ZERO), "sum_of_cash_accounts"


def _unmatched_bank_count(db: Session, cash_account_ids: list[int], as_of: date) -> int:
    matched = (
        select(ReconciliationMatch.bank_transaction_id, func.sum(ReconciliationMatch.amount).label("m"))
        .group_by(ReconciliationMatch.bank_transaction_id)
        .subquery()
    )
    stmt = (
        select(func.count(BankTransaction.id))
        .outerjoin(matched, matched.c.bank_transaction_id == BankTransaction.id)
        .where(
            BankTransaction.cash_account_id.in_(cash_account_ids),
            BankTransaction.transaction_date <= as_of,
            func.coalesce(matched.c.m, 0) < func.abs(BankTransaction.amount),
        )
    )
    return int(db.scalar(stmt) or 0)


def liquidity_projection(
    db: Session,
    as_of: date,
    horizon_days: int = 30,
    cash_account_id: int | None = None,
    threshold: Decimal | None = None,
    history_days: int = 30,
) -> dict:
    if horizon_days < 1 or horizon_days > 366:
        raise CashValidationError("horizon_days must be between 1 and 366")
    if history_days < 0 or history_days > 366:
        raise CashValidationError("history_days must be between 0 and 366")

    snapshot_read(db)
    cash_account = get_cash_account(db, cash_account_id) if cash_account_id is not None else None
    cash_accounts = [cash_account] if cash_account else list(db.scalars(select(CashAccount).order_by(CashAccount.id)))
    ledger_ids = {ca.ledger_account_id for ca in cash_accounts}
    ledger_to_cash = {ca.ledger_account_id: ca.id for ca in cash_accounts}
    cash_ids = [ca.id for ca in cash_accounts]

    horizon_start = as_of + timedelta(days=1)
    horizon_end = as_of + timedelta(days=horizon_days)
    opening = sum((quantize(cash_balance(db, lid, as_of=as_of)) for lid in ledger_ids), ZERO)
    limit, limit_source = _threshold(db, cash_account, threshold)

    days: dict[date, dict] = {}
    day = horizon_start
    while day <= horizon_end:
        days[day] = {
            "date": day,
            "posted_net": ZERO,
            "planned_inflows": ZERO,
            "planned_outflows": ZERO,
            "items": [],
        }
        day += timedelta(days=1)

    counted = {"posted_journal_entries": 0, "planned_cash_flows": 0, "overdue_planned_cash_flows": 0}

    # Posted entries dated in the horizon: accounting facts, just future-dated.
    if ledger_ids:
        stmt = (
            select(JournalEntry, JournalLine)
            .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.entry_date >= horizon_start,
                JournalEntry.entry_date <= horizon_end,
                JournalLine.account_id.in_(ledger_ids),
            )
            .order_by(JournalEntry.entry_date, JournalEntry.id, JournalLine.id)
        )
        seen_entries: set[int] = set()
        for entry, line in db.execute(stmt):
            amount = line.debit - line.credit
            if cash_account is None and _is_internal_transfer_line(entry, ledger_ids):
                # Transfers between our own accounts do not change consolidated cash.
                continue
            bucket = days[entry.entry_date]
            bucket["posted_net"] += amount
            bucket["items"].append(
                {
                    "kind": "posted_journal_entry",
                    "id": entry.id,
                    "cash_account_id": ledger_to_cash[line.account_id],
                    "description": entry.description,
                    "amount": amount,
                }
            )
            seen_entries.add(entry.id)
        counted["posted_journal_entries"] = len(seen_entries)

    # Planned flows: forecast only.
    planned = db.scalars(
        select(PlannedCashFlow)
        .where(
            PlannedCashFlow.cash_account_id.in_(cash_ids) if cash_ids else False,
            PlannedCashFlow.status == PlannedCashFlowStatus.PLANNED,
            PlannedCashFlow.expected_date <= horizon_end,
        )
        .order_by(PlannedCashFlow.expected_date, PlannedCashFlow.id)
    ).all()
    # Double-count guard: a planned item already linked to a *posted* entry is the
    # same money as that entry; it is excluded even if its status was not updated.
    linked_posted = set(
        db.scalars(
            select(PlannedCashFlow.id)
            .join(JournalEntry, JournalEntry.id == PlannedCashFlow.journal_entry_id)
            .where(
                PlannedCashFlow.id.in_([p.id for p in planned]) if planned else False,
                JournalEntry.status == EntryStatus.POSTED,
            )
        )
    )
    for item in planned:
        if item.id in linked_posted:
            continue
        overdue = item.expected_date < horizon_start
        bucket = days[horizon_start if overdue else item.expected_date]
        if item.amount > 0:
            bucket["planned_inflows"] += item.amount
        else:
            bucket["planned_outflows"] += -item.amount
        bucket["items"].append(
            {
                "kind": "planned_cash_flow",
                "id": item.id,
                "cash_account_id": item.cash_account_id,
                "description": item.description,
                "amount": item.amount,
                "overdue": overdue,
            }
        )
        counted["planned_cash_flows"] += 1
        if overdue:
            counted["overdue_planned_cash_flows"] += 1

    running = opening
    rows = []
    alerts = []
    lowest = (opening, as_of)
    outflows_so_far: list[dict] = []  # every negative item since the horizon start
    for day in sorted(days):
        bucket = days[day]
        net = bucket["posted_net"] + bucket["planned_inflows"] - bucket["planned_outflows"]
        running += net
        outflows_so_far.extend(i for i in bucket["items"] if i["amount"] < 0)
        below = limit is not None and running < limit
        if below:
            alerts.append(
                {
                    "date": day,
                    "projected_balance": running,
                    "threshold": limit,
                    "shortfall": limit - running,
                    "first_breach": not alerts,
                    "reason": _alert_reason(day, running, limit, opening, bucket, outflows_so_far, limit_source),
                    "items": list(bucket["items"]),
                    "drivers": sorted(outflows_so_far, key=lambda i: i["amount"])[:5],
                }
            )
        if running < lowest[0]:
            lowest = (running, day)
        rows.append({**bucket, "net": net, "projected_balance": running, "below_threshold": below})

    # Back-dated postings: recorded after their entry date. They are legitimate
    # ledger facts and are counted, but they explain why the opening balance or
    # the history may differ from an earlier projection.
    backdated_ids = []
    if ledger_ids:
        backdated_ids = list(
            db.scalars(
                select(func.distinct(JournalEntry.id))
                .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
                .where(
                    JournalEntry.status == EntryStatus.POSTED,
                    JournalEntry.entry_date <= as_of,
                    JournalEntry.entry_date >= as_of - timedelta(days=max(history_days, 1) - 1),
                    func.date(JournalEntry.created_at) > JournalEntry.entry_date,
                    JournalLine.account_id.in_(ledger_ids),
                )
                .order_by(JournalEntry.id)
            )
        )
    counted["backdated_posted_entries_in_history"] = len(backdated_ids)

    # Explainability: what was left out, and why the numbers may still move.
    excluded = {
        "draft_journal_entries_in_horizon": int(
            db.scalar(
                select(func.count(func.distinct(JournalEntry.id)))
                .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
                .where(
                    JournalEntry.status == EntryStatus.DRAFT,
                    JournalEntry.entry_date <= horizon_end,
                    JournalLine.account_id.in_(ledger_ids) if ledger_ids else False,
                )
            )
            or 0
        ),
        "unmatched_bank_transactions": _unmatched_bank_count(db, cash_ids, as_of) if cash_ids else 0,
        "realized_or_cancelled_planned_cash_flows": int(
            db.scalar(
                select(func.count(PlannedCashFlow.id)).where(
                    PlannedCashFlow.cash_account_id.in_(cash_ids) if cash_ids else False,
                    PlannedCashFlow.status != PlannedCashFlowStatus.PLANNED,
                    PlannedCashFlow.expected_date <= horizon_end,
                )
            )
            or 0
        ),
    }
    excluded["planned_cash_flows_linked_to_posted_entries"] = len(linked_posted)
    warnings = []
    if backdated_ids:
        shown = ", ".join(f"#{i}" for i in backdated_ids[:5]) + (" ..." if len(backdated_ids) > 5 else "")
        warnings.append(
            f"{len(backdated_ids)} posted entr(y/ies) in the history window were recorded after their "
            f"entry date ({shown}); they are counted in the opening balance and history, which may "
            "therefore differ from an earlier projection"
        )
    if linked_posted:
        warnings.append(
            f"{len(linked_posted)} planned cash flow(s) are linked to posted journal entries and were "
            "not counted (the posted entry is); mark them realized"
        )
    if excluded["draft_journal_entries_in_horizon"]:
        warnings.append(
            f"{excluded['draft_journal_entries_in_horizon']} draft journal entr(y/ies) touch the "
            "cash accounts and are not counted until posted"
        )
    if excluded["unmatched_bank_transactions"]:
        warnings.append(
            f"{excluded['unmatched_bank_transactions']} bank transaction(s) up to {as_of.isoformat()} "
            "are not (fully) reconciled with the ledger; the opening balance may be incomplete"
        )
    if limit is None:
        warnings.append("no threshold configured: set minimum_balance on the cash account(s) or pass threshold")

    history = []
    if history_days:
        hist_start = as_of - timedelta(days=history_days - 1)
        flows = daily_flows(db, hist_start, as_of, cash_account_id)
        running_hist = sum((quantize(cash_balance(db, lid, as_of=hist_start - timedelta(days=1))) for lid in ledger_ids), ZERO)
        day = hist_start
        while day <= as_of:
            net = flows[day].net if day in flows else ZERO
            running_hist += net
            history.append({"date": day, "net": net, "closing_balance": running_hist})
            day += timedelta(days=1)

    return {
        "as_of": as_of,
        "horizon_start": horizon_start,
        "horizon_end": horizon_end,
        "horizon_days": horizon_days,
        "cash_account_id": cash_account_id,
        "basis": POSTED_ONLY + "; planned cash flows are forecast items, not accounting",
        "opening_balance": opening,
        "threshold": limit,
        "threshold_source": limit_source,
        "days": rows,
        "closing_projected_balance": running,
        "lowest_projected_balance": lowest[0],
        "lowest_projected_date": lowest[1],
        "alerts": alerts,
        "history": history,
        "counted": counted,
        "excluded": excluded,
        "warnings": warnings,
    }


def _is_internal_transfer_line(entry: JournalEntry, ledger_ids: set[int]) -> bool:
    """True when the entry moves money only between the scoped cash accounts.

    Mixed entries (transfer plus a fee, say) are kept: their net effect on
    consolidated cash is what the lines add up to, which is what we sum.
    """
    return all(line.account_id in ledger_ids for line in entry.lines)


def _alert_reason(day, balance, limit, opening, bucket, outflows_so_far, limit_source) -> str:
    """Human-readable explanation of one alert day."""
    day_net = bucket["posted_net"] + bucket["planned_inflows"] - bucket["planned_outflows"]
    parts = [
        f"projected balance {balance:.2f} is below the {limit_source or 'configured'} threshold "
        f"{limit:.2f} by {limit - balance:.2f}",
        f"opening {opening:.2f} moved by {balance - opening:+.2f} since the start of the horizon",
    ]
    if bucket["items"]:
        labels = [f"{i['description']} ({i['amount']:+.2f}, {i['kind'].replace('_', ' ')})" for i in bucket["items"][:3]]
        parts.append(f"on this day: {day_net:+.2f} from " + "; ".join(labels))
    else:
        parts.append("no movement on this day; the shortfall was reached earlier")
    if outflows_so_far:
        largest = sorted(outflows_so_far, key=lambda i: i["amount"])[:3]
        parts.append(
            "largest outflows so far: "
            + "; ".join(f"{i['description']} {i['amount']:+.2f}" for i in largest)
        )
    return ". ".join(parts)
