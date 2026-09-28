"""Cash reports derived from posted journal entries only.

Debit/credit handling: cash accounts are asset accounts, so a debit on the
linked ledger account is money *in* and a credit is money *out*. The balance of
a cash account is ``sum(debit) - sum(credit)`` of its posted lines.

Internal transfers: a posted entry that debits one cash ledger account and
credits another moves money between the company's own accounts. The amount
``min(cash debits, cash credits)`` of such an entry is treated as a transfer:
it is shown separately and excluded from consolidated inflows/outflows and
from the activity classification. Any remainder (e.g. a bank fee on a
transfer) is classified like any other movement, by the counterpart lines.

Activity classification (operating / investing / financing) is determined by
the *counterpart* (non-cash) lines of each entry: an explicit row in
``cash_flow_classifications`` wins, otherwise a default by account type is
used (revenue/expense -> operating, asset -> investing, liability/equity ->
financing) and the report says which source applied.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Account, AccountType, EntryStatus, JournalEntry, JournalLine
from app.modules.cash.errors import CashValidationError
from app.modules.cash.models import CashAccount, CashFlowActivity, CashFlowClassification
from app.modules.cash.services import CENT, ZERO, cash_balance, get_cash_account, quantize, snapshot_read

DEFAULT_ACTIVITY = {
    AccountType.REVENUE: CashFlowActivity.OPERATING,
    AccountType.EXPENSE: CashFlowActivity.OPERATING,
    AccountType.ASSET: CashFlowActivity.INVESTING,
    AccountType.LIABILITY: CashFlowActivity.FINANCING,
    AccountType.EQUITY: CashFlowActivity.FINANCING,
}

POSTED_ONLY = "posted journal entries only (drafts, bank statements and planned flows excluded)"


@dataclass
class AccountFlow:
    inflows: Decimal = ZERO
    outflows: Decimal = ZERO
    transfers_in: Decimal = ZERO
    transfers_out: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.inflows - self.outflows + self.transfers_in - self.transfers_out


@dataclass
class ExternalLine:
    account: Account
    inflow: Decimal
    outflow: Decimal


@dataclass
class EntryFlow:
    entry: JournalEntry
    per_ledger_account: dict[int, AccountFlow] = field(default_factory=dict)
    external: list[ExternalLine] = field(default_factory=list)
    transfer_amount: Decimal = ZERO


def _allocate(total: Decimal, weights: list[tuple[int, Decimal]]) -> dict[int, Decimal]:
    """Split ``total`` over ids proportionally to their weights, exact to the cent."""
    if total == 0 or not weights:
        return {i: ZERO for i, _ in weights}
    weight_sum = sum((w for _, w in weights), ZERO)
    shares: dict[int, Decimal] = {}
    allocated = ZERO
    for i, w in weights:
        share = (total * w / weight_sum).quantize(CENT, rounding=ROUND_DOWN)
        shares[i] = shares.get(i, ZERO) + share
        allocated += share
    remainder = total - allocated
    if remainder:
        largest = max(weights, key=lambda item: item[1])[0]
        shares[largest] += remainder
    return shares


def analyse_entry(entry: JournalEntry, cash_ledger_ids: set[int]) -> EntryFlow:
    """Split one posted entry into cash movements, transfers and counterpart lines."""
    flow = EntryFlow(entry=entry)
    debit_lines = [(l.account_id, l.debit) for l in entry.lines if l.account_id in cash_ledger_ids and l.debit > 0]
    credit_lines = [(l.account_id, l.credit) for l in entry.lines if l.account_id in cash_ledger_ids and l.credit > 0]
    cash_in = sum((d for _, d in debit_lines), ZERO)
    cash_out = sum((c for _, c in credit_lines), ZERO)
    transfer = min(cash_in, cash_out)
    flow.transfer_amount = transfer

    # The transfer amount is shared among the receiving accounts in proportion to
    # their debits and among the paying accounts in proportion to their credits;
    # whatever is left on each line is an external inflow/outflow.
    transfer_in_shares = _allocate(transfer, debit_lines)
    transfer_out_shares = _allocate(transfer, credit_lines)
    for account_id in {a for a, _ in debit_lines} | {a for a, _ in credit_lines}:
        debit_total = sum((d for a, d in debit_lines if a == account_id), ZERO)
        credit_total = sum((c for a, c in credit_lines if a == account_id), ZERO)
        t_in = transfer_in_shares.get(account_id, ZERO)
        t_out = transfer_out_shares.get(account_id, ZERO)
        flow.per_ledger_account[account_id] = AccountFlow(
            inflows=debit_total - t_in,
            outflows=credit_total - t_out,
            transfers_in=t_in,
            transfers_out=t_out,
        )

    for line in entry.lines:
        if line.account_id in cash_ledger_ids:
            continue
        # A credit on a counterpart account means cash came in; a debit, cash went out.
        flow.external.append(ExternalLine(account=line.account, inflow=line.credit, outflow=line.debit))
    return flow


def _cash_accounts(db: Session) -> list[CashAccount]:
    return list(db.scalars(select(CashAccount).order_by(CashAccount.id)))


def posted_cash_entries(
    db: Session, start: date, end: date, scope_ledger_ids: set[int]
) -> list[JournalEntry]:
    """Posted entries in the period with at least one line on the scoped ledger accounts."""
    if not scope_ledger_ids:
        return []
    stmt = (
        select(JournalEntry)
        .where(
            JournalEntry.status == EntryStatus.POSTED,
            JournalEntry.entry_date >= start,
            JournalEntry.entry_date <= end,
            JournalEntry.id.in_(
                select(JournalLine.entry_id).where(JournalLine.account_id.in_(scope_ledger_ids))
            ),
        )
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .order_by(JournalEntry.entry_date, JournalEntry.id)
    )
    return list(db.scalars(stmt))


def _classifications(db: Session) -> dict[int, CashFlowActivity]:
    return dict(
        db.execute(select(CashFlowClassification.account_id, CashFlowClassification.activity)).all()
    )


def classify(account: Account, explicit: dict[int, CashFlowActivity]) -> tuple[CashFlowActivity, str]:
    if account.id in explicit:
        return explicit[account.id], "explicit"
    return DEFAULT_ACTIVITY[account.type], "default_by_type"


# --- Reports ---------------------------------------------------------------------------


def balances_report(db: Session, as_of: date | None = None) -> dict:
    snapshot_read(db)
    rows = []
    total = ZERO
    for ca in _cash_accounts(db):
        ledger = db.get(Account, ca.ledger_account_id)
        balance = quantize(cash_balance(db, ca.ledger_account_id, as_of=as_of))
        total += balance
        rows.append(
            {
                "cash_account_id": ca.id,
                "label": ca.label,
                "ledger_account_id": ca.ledger_account_id,
                "ledger_account_code": ledger.code if ledger else "",
                "balance": balance,
                "minimum_balance": ca.minimum_balance,
            }
        )
    # Internal transfers net to zero across cash accounts, so they never affect
    # the consolidated figure; it is the plain sum of posted balances.
    return {"as_of": as_of, "basis": POSTED_ONLY, "accounts": rows, "consolidated_balance": total}


def _scope(db: Session, cash_account_id: int | None) -> tuple[set[int], set[int]]:
    """(ledger ids in scope, all cash ledger ids) - transfers need the full set."""
    all_ids = {ca.ledger_account_id for ca in _cash_accounts(db)}
    if cash_account_id is None:
        return all_ids, all_ids
    ca = get_cash_account(db, cash_account_id)
    return {ca.ledger_account_id}, all_ids


def daily_flows(
    db: Session, start: date, end: date, cash_account_id: int | None = None
) -> dict[date, AccountFlow]:
    """Per-day external inflows/outflows and transfers for the scope (posted only)."""
    scope_ids, all_ids = _scope(db, cash_account_id)
    days: dict[date, AccountFlow] = {}
    for entry in posted_cash_entries(db, start, end, scope_ids):
        flow = analyse_entry(entry, all_ids)
        day = days.setdefault(entry.entry_date, AccountFlow())
        for ledger_id, af in flow.per_ledger_account.items():
            if ledger_id not in scope_ids:
                continue
            day.inflows += af.inflows
            day.outflows += af.outflows
            day.transfers_in += af.transfers_in
            day.transfers_out += af.transfers_out
    return days


def daily_balances_report(
    db: Session, start: date, end: date, cash_account_id: int | None = None
) -> dict:
    if end < start:
        raise CashValidationError("end precedes start")
    snapshot_read(db)
    scope_ids, _ = _scope(db, cash_account_id)
    opening = sum((quantize(cash_balance(db, lid, as_of=start - timedelta(days=1))) for lid in scope_ids), ZERO)
    flows = daily_flows(db, start, end, cash_account_id)
    running = opening
    rows = []
    day = start
    while day <= end:
        af = flows.get(day, AccountFlow())
        running += af.net
        rows.append(
            {
                "date": day,
                "inflows": af.inflows,
                "outflows": af.outflows,
                "transfers_in": af.transfers_in,
                "transfers_out": af.transfers_out,
                "net": af.net,
                "closing_balance": running,
            }
        )
        day += timedelta(days=1)
    return {
        "cash_account_id": cash_account_id,
        "period_start": start,
        "period_end": end,
        "opening_balance": opening,
        "closing_balance": running,
        "days": rows,
    }


def cash_flow_report(db: Session, start: date, end: date) -> dict:
    """Consolidated cash flow by activity, internal transfers excluded and listed."""
    if end < start:
        raise CashValidationError("end precedes start")
    snapshot_read(db)
    cash_accounts = _cash_accounts(db)
    all_ids = {ca.ledger_account_id for ca in cash_accounts}
    explicit = _classifications(db)

    sections: dict[CashFlowActivity, dict[int, dict]] = {a: {} for a in CashFlowActivity}
    transfers: list[dict] = []
    per_ledger: dict[int, AccountFlow] = {lid: AccountFlow() for lid in all_ids}

    for entry in posted_cash_entries(db, start, end, all_ids):
        flow = analyse_entry(entry, all_ids)
        if flow.transfer_amount > 0:
            transfers.append(
                {
                    "journal_entry_id": entry.id,
                    "entry_date": entry.entry_date,
                    "description": entry.description,
                    "amount": flow.transfer_amount,
                }
            )
        for ledger_id, af in flow.per_ledger_account.items():
            agg = per_ledger[ledger_id]
            agg.inflows += af.inflows
            agg.outflows += af.outflows
            agg.transfers_in += af.transfers_in
            agg.transfers_out += af.transfers_out
        for ext in flow.external:
            if ext.inflow == 0 and ext.outflow == 0:
                continue
            activity, source = classify(ext.account, explicit)
            line = sections[activity].setdefault(
                ext.account.id,
                {
                    "account_id": ext.account.id,
                    "account_code": ext.account.code,
                    "account_name": ext.account.name,
                    "inflows": ZERO,
                    "outflows": ZERO,
                    "net": ZERO,
                    "classification_source": source,
                },
            )
            line["inflows"] += ext.inflow
            line["outflows"] += ext.outflow
            line["net"] = line["inflows"] - line["outflows"]

    activities = []
    total_in = total_out = ZERO
    for activity in CashFlowActivity:
        lines = sorted(sections[activity].values(), key=lambda x: x["account_code"])
        inflows = sum((l["inflows"] for l in lines), ZERO)
        outflows = sum((l["outflows"] for l in lines), ZERO)
        total_in += inflows
        total_out += outflows
        activities.append(
            {
                "activity": activity,
                "inflows": inflows,
                "outflows": outflows,
                "net": inflows - outflows,
                "lines": lines,
            }
        )

    account_rows = []
    opening_total = closing_total = ZERO
    for ca in cash_accounts:
        opening = quantize(cash_balance(db, ca.ledger_account_id, as_of=start - timedelta(days=1)))
        af = per_ledger[ca.ledger_account_id]
        closing = opening + af.net
        opening_total += opening
        closing_total += closing
        account_rows.append(
            {
                "cash_account_id": ca.id,
                "label": ca.label,
                "opening_balance": opening,
                "inflows": af.inflows,
                "outflows": af.outflows,
                "transfers_in": af.transfers_in,
                "transfers_out": af.transfers_out,
                "closing_balance": closing,
            }
        )

    # Tie-out: the activity view (counterpart lines, transfers excluded) and the
    # account view (cash lines, transfers shown separately) must agree on the net
    # change, and opening + net change must equal the closing balance. If this is
    # ever False the report is inconsistent and must not be trusted.
    accounts_net = sum((r["closing_balance"] - r["opening_balance"] for r in account_rows), ZERO)
    net_change = total_in - total_out
    ties_out = accounts_net == net_change and opening_total + net_change == closing_total
    return {
        "period_start": start,
        "period_end": end,
        "basis": POSTED_ONLY,
        "opening_balance": opening_total,
        "closing_balance": closing_total,
        "total_inflows": total_in,
        "total_outflows": total_out,
        "net_change": net_change,
        "ties_out": ties_out,
        "activities": activities,
        "internal_transfers_total": sum((t["amount"] for t in transfers), ZERO),
        "internal_transfers": transfers,
        "accounts": account_rows,
    }
