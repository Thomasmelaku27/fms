"""Financial reports computed from posted journal entries only.

Drafts never appear in reports. Reversals are ordinary posted entries with the
sides swapped, so they net out naturally; the General Ledger marks them so the
history stays readable.

Aggregation runs in SQL with **exact monetary semantics** on both backends
(``cents_sum``): PostgreSQL sums ``NUMERIC`` exactly; SQLite stores REAL, so
each amount is converted to integer cents *per row* (exact, because every
stored amount is a whole number of cents - a CHECK constraint) and integers are
summed. Results come back as integer cents and become ``Decimal`` in Python.

Sources:

* trial balance and General Ledger detail read ``journal_lines`` - the source
  of truth;
* ``account_balances`` / ``hierarchy`` read the trigger-maintained
  ``account_daily_totals`` aggregate by default (``source="aggregate"``), which
  is verified against the lines by ``app.services.balances``; pass
  ``source="lines"`` to read the source of truth directly.

Pagination of the General Ledger detail is keyset-based on the deterministic
order ``(account code, entry_date, entry_id, line_id)``; the running balance
of the first line of a page is computed in SQL from everything before the
cursor, so pages are independent of each other.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import BigInteger, Integer, cast, func, select, tuple_
from sqlalchemy.orm import Session, selectinload

from app.models import Account, AccountDailyTotal, EntryStatus, JournalEntry, JournalLine

ZERO = Decimal("0.00")


def cents_sum(db: Session, column):
    """SQL expression: the exact sum of ``column`` as integer cents (dialect-aware)."""
    if db.get_bind().dialect.name == "sqlite":
        return func.coalesce(func.sum(cast(func.round(column * 100), Integer)), 0)
    return cast(func.round(func.coalesce(func.sum(column), 0) * 100), BigInteger)


def from_cents(cents) -> Decimal:
    return (Decimal(int(cents or 0)) / 100).quantize(Decimal("0.01"))


# --- Aggregation primitives ---------------------------------------------------------


def account_totals(
    db: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_ids=None,
    before: date | None = None,
) -> dict[int, tuple[Decimal, Decimal]]:
    """{account_id: (debit, credit)} over posted lines in the range, straight from SQL."""
    stmt = (
        select(
            JournalLine.account_id,
            cents_sum(db, JournalLine.debit),
            cents_sum(db, JournalLine.credit),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.status == EntryStatus.POSTED)
        .group_by(JournalLine.account_id)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    if before is not None:
        stmt = stmt.where(JournalEntry.entry_date < before)
    if account_ids is not None:
        stmt = stmt.where(JournalLine.account_id.in_(list(account_ids)))
    return {acc: (from_cents(d), from_cents(c)) for acc, d, c in db.execute(stmt)}


def aggregate_totals(
    db: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_ids=None,
    before: date | None = None,
) -> dict[int, tuple[Decimal, Decimal]]:
    """Same shape as ``account_totals`` but read from ``account_daily_totals``."""
    stmt = select(
        AccountDailyTotal.account_id,
        func.coalesce(func.sum(AccountDailyTotal.debit_cents), 0),
        func.coalesce(func.sum(AccountDailyTotal.credit_cents), 0),
    ).group_by(AccountDailyTotal.account_id)
    if date_from is not None:
        stmt = stmt.where(AccountDailyTotal.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(AccountDailyTotal.entry_date <= date_to)
    if before is not None:
        stmt = stmt.where(AccountDailyTotal.entry_date < before)
    if account_ids is not None:
        stmt = stmt.where(AccountDailyTotal.account_id.in_(list(account_ids)))
    return {acc: (from_cents(d), from_cents(c)) for acc, d, c in db.execute(stmt)}


def _totals(db, source, **kwargs):
    return (aggregate_totals if source == "aggregate" else account_totals)(db, **kwargs)


def _rollup(accounts: list[Account], own: dict[int, tuple[Decimal, Decimal]]):
    """Summary accounts get the sum of their descendants' own postings (never double counted:
    summaries have no own postings, and every posting account is counted once per ancestor)."""
    by_id = {a.id: a for a in accounts}
    rolled: dict[int, tuple[Decimal, Decimal]] = {}
    for account in accounts:
        debit, credit = own.get(account.id, (ZERO, ZERO))
        parent_id = account.parent_id
        seen = {account.id}
        while parent_id is not None and parent_id not in seen and parent_id in by_id:
            seen.add(parent_id)
            d, c = rolled.get(parent_id, (ZERO, ZERO))
            rolled[parent_id] = (d + debit, c + credit)
            parent_id = by_id[parent_id].parent_id
    return rolled


# --- Trial balance -----------------------------------------------------------------


@dataclass
class TrialBalanceRow:
    account_id: int
    code: str
    name: str
    type: str
    debit: Decimal
    credit: Decimal
    is_summary: bool = False
    parent_id: int | None = None
    normal_balance: str = "debit"

    @property
    def balance(self) -> Decimal:
        """Debit-positive net balance (debits minus credits)."""
        return self.debit - self.credit

    @property
    def normal_balance_amount(self) -> Decimal:
        """The balance expressed in the account's normal direction (credit accounts flip sign)."""
        return self.balance if self.normal_balance == "debit" else -self.balance


@dataclass
class TrialBalance:
    date_from: date | None
    date_to: date | None
    rows: list[TrialBalanceRow]

    @property
    def posting_rows(self) -> list[TrialBalanceRow]:
        return [row for row in self.rows if not row.is_summary]

    @property
    def total_debit(self) -> Decimal:
        """Totals over posting accounts only (summary rows are roll-ups, not activity)."""
        return sum((row.debit for row in self.posting_rows), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((row.credit for row in self.posting_rows), ZERO)

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


def trial_balance(
    db: Session, date_from: date | None = None, date_to: date | None = None
) -> TrialBalance:
    """Debit and credit totals per account for posted entries in the date range.

    Every posted entry balances and lies entirely inside or outside the range,
    so the report totals always balance. All accounts are listed, including
    those without activity. Summary accounts carry the roll-up of their
    descendants and are excluded from the totals.
    """
    totals = account_totals(db, date_from, date_to)
    accounts = db.scalars(select(Account).order_by(Account.code)).all()
    rolled = _rollup(accounts, totals)
    rows = []
    for account in accounts:
        debit, credit = (rolled if account.is_summary else totals).get(account.id, (ZERO, ZERO))
        rows.append(
            TrialBalanceRow(
                account_id=account.id,
                code=account.code,
                name=account.name,
                type=account.type.value,
                debit=debit,
                credit=credit,
                is_summary=account.is_summary,
                parent_id=account.parent_id,
                normal_balance=account.normal_balance.value,
            )
        )
    return TrialBalance(date_from=date_from, date_to=date_to, rows=rows)


# --- Account balances, hierarchy, comparisons ----------------------------------------


@dataclass
class AccountBalance:
    account_id: int
    code: str
    name: str
    type: str
    is_summary: bool
    parent_id: int | None
    normal_balance: str
    opening_balance: Decimal
    debit: Decimal
    credit: Decimal

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + self.debit - self.credit

    @property
    def net_change(self) -> Decimal:
        return self.debit - self.credit


@dataclass
class AccountBalances:
    date_from: date | None
    date_to: date | None
    source: str
    rows: list[AccountBalance]


def account_balances(
    db: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_ids=None,
    source: str = "aggregate",
) -> AccountBalances:
    """Opening balance (before ``date_from``), activity and closing balance per account.

    Summary accounts roll up their descendants (opening and activity alike).
    With ``account_ids`` the descendants needed for roll-ups are included in
    the computation but only the requested accounts are returned.
    """
    accounts = db.scalars(select(Account).order_by(Account.code)).all()
    activity = _totals(db, source, date_from=date_from, date_to=date_to)
    opening = _totals(db, source, before=date_from) if date_from is not None else {}
    rolled_activity = _rollup(accounts, activity)
    rolled_opening = _rollup(accounts, opening)
    wanted = set(account_ids) if account_ids is not None else None
    rows = []
    for account in accounts:
        if wanted is not None and account.id not in wanted:
            continue
        act = (rolled_activity if account.is_summary else activity).get(account.id, (ZERO, ZERO))
        opn = (rolled_opening if account.is_summary else opening).get(account.id, (ZERO, ZERO))
        rows.append(
            AccountBalance(
                account_id=account.id,
                code=account.code,
                name=account.name,
                type=account.type.value,
                is_summary=account.is_summary,
                parent_id=account.parent_id,
                normal_balance=account.normal_balance.value,
                opening_balance=opn[0] - opn[1],
                debit=act[0],
                credit=act[1],
            )
        )
    return AccountBalances(date_from=date_from, date_to=date_to, source=source, rows=rows)


@dataclass
class HierarchyNode:
    account_id: int
    code: str
    name: str
    type: str
    is_summary: bool
    normal_balance: str
    own_debit: Decimal
    own_credit: Decimal
    rolled_debit: Decimal
    rolled_credit: Decimal
    opening_balance: Decimal
    children: list["HierarchyNode"] = field(default_factory=list)

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + self.rolled_debit - self.rolled_credit


def hierarchy(
    db: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    root_id: int | None = None,
    source: str = "aggregate",
) -> list[HierarchyNode]:
    """Tree of accounts with own and rolled-up totals; drill down through ``children``."""
    accounts = db.scalars(select(Account).order_by(Account.code)).all()
    activity = _totals(db, source, date_from=date_from, date_to=date_to)
    opening = _totals(db, source, before=date_from) if date_from is not None else {}
    rolled_activity = _rollup(accounts, activity)
    rolled_opening = _rollup(accounts, opening)
    nodes: dict[int, HierarchyNode] = {}
    for account in accounts:
        own = activity.get(account.id, (ZERO, ZERO))
        rolled = rolled_activity.get(account.id, (ZERO, ZERO)) if account.is_summary else own
        opn = (rolled_opening if account.is_summary else opening).get(account.id, (ZERO, ZERO))
        nodes[account.id] = HierarchyNode(
            account_id=account.id,
            code=account.code,
            name=account.name,
            type=account.type.value,
            is_summary=account.is_summary,
            normal_balance=account.normal_balance.value,
            own_debit=own[0],
            own_credit=own[1],
            rolled_debit=rolled[0],
            rolled_credit=rolled[1],
            opening_balance=opn[0] - opn[1],
        )
    roots: list[HierarchyNode] = []
    for account in accounts:
        node = nodes[account.id]
        if account.parent_id is not None and account.parent_id in nodes:
            nodes[account.parent_id].children.append(node)
        else:
            roots.append(node)
    if root_id is not None:
        return [nodes[root_id]] if root_id in nodes else []
    return roots


@dataclass
class ReclassificationImpact:
    reclassification_id: int
    effective_from: date
    old_parent_id: int | None
    new_parent_id: int | None
    old_category: str | None
    new_category: str | None
    reason: str
    actor_ref: str
    balance_before_effective_date: Decimal  # cumulative balance that moved with the account
    activity_since_effective_date: Decimal


def reclassification_impact(db: Session, account: Account) -> list[ReclassificationImpact]:
    """For each reclassification of ``account``: how much history moved between parents.

    Reports use the *current* hierarchy for all dates, so after a move the
    account's cumulative balance as of the day before ``effective_from`` rolls
    up under the new parent instead of the old one. That amount is reported
    here so readers of historical roll-ups can account for the change.
    """
    from app.services.accounts import reclassifications

    impacts = []
    for record in reclassifications(db, account):
        before = account_totals(db, before=record.effective_from, account_ids=[account.id]).get(
            account.id, (ZERO, ZERO)
        )
        since = account_totals(db, date_from=record.effective_from, account_ids=[account.id]).get(
            account.id, (ZERO, ZERO)
        )
        impacts.append(
            ReclassificationImpact(
                reclassification_id=record.id,
                effective_from=record.effective_from,
                old_parent_id=record.old_parent_id,
                new_parent_id=record.new_parent_id,
                old_category=record.old_category,
                new_category=record.new_category,
                reason=record.reason,
                actor_ref=record.actor_ref,
                balance_before_effective_date=before[0] - before[1],
                activity_since_effective_date=since[0] - since[1],
            )
        )
    return impacts


# --- General Ledger detail (paginated) -------------------------------------------


@dataclass
class LedgerLine:
    entry_id: int
    line_id: int
    entry_date: date
    description: str
    reference: str | None
    memo: str | None
    debit: Decimal
    credit: Decimal
    balance: Decimal
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    entry_type: str
    correlation_id: str | None


@dataclass
class LedgerAccount:
    account_id: int
    code: str
    name: str
    type: str
    opening_balance: Decimal
    lines: list[LedgerLine] = field(default_factory=list)
    total_debit: Decimal = ZERO  # over the whole range, not just this page
    total_credit: Decimal = ZERO

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + self.total_debit - self.total_credit


@dataclass
class GeneralLedger:
    date_from: date | None
    date_to: date | None
    accounts: list[LedgerAccount]
    limit: int | None = None
    next_cursor: str | None = None


def encode_cursor(account_code: str, entry_date: date, entry_id: int, line_id: int) -> str:
    return f"{account_code}|{entry_date.isoformat()}|{entry_id}|{line_id}"


def decode_cursor(cursor: str) -> tuple[str, date, int, int]:
    try:
        code, day, entry_id, line_id = cursor.split("|")
        return code, date.fromisoformat(day), int(entry_id), int(line_id)
    except (ValueError, AttributeError):
        raise ValueError("invalid cursor") from None


def general_ledger(
    db: Session,
    account_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int | None = None,
    after: str | None = None,
) -> GeneralLedger:
    """Posted lines per account with running balances, optionally one page at a time.

    ``opening_balance`` is the net of all posted lines dated before ``date_from``
    (zero when no ``date_from`` is given); ``total_debit``/``total_credit`` and
    ``closing_balance`` cover the whole range regardless of pagination.
    """
    accounts_stmt = select(Account).where(Account.is_summary.is_(False)).order_by(Account.code)
    if account_id is not None:
        accounts_stmt = accounts_stmt.where(Account.id == account_id)
    accounts = db.scalars(accounts_stmt).all()
    ids = [a.id for a in accounts]
    totals = account_totals(db, date_from, date_to, account_ids=ids)
    opening = account_totals(db, before=date_from, account_ids=ids) if date_from else {}
    sections = {}
    for account in accounts:
        opn = opening.get(account.id, (ZERO, ZERO))
        tot = totals.get(account.id, (ZERO, ZERO))
        sections[account.id] = LedgerAccount(
            account_id=account.id,
            code=account.code,
            name=account.name,
            type=account.type.value,
            opening_balance=opn[0] - opn[1],
            total_debit=tot[0],
            total_credit=tot[1],
        )

    stmt = (
        select(JournalLine, JournalEntry, Account.code)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(JournalEntry.status == EntryStatus.POSTED, JournalLine.account_id.in_(ids))
        .options(selectinload(JournalEntry.reversed_by))
        .order_by(Account.code, JournalEntry.entry_date, JournalEntry.id, JournalLine.id)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    cursor = decode_cursor(after) if after else None
    if cursor is not None:
        stmt = stmt.where(
            tuple_(Account.code, JournalEntry.entry_date, JournalEntry.id, JournalLine.id)
            > tuple_(*cursor)
        )
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    rows = db.execute(stmt).all()
    next_cursor = None
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        last_line, last_entry, last_code = rows[-1]
        next_cursor = encode_cursor(last_code, last_entry.entry_date, last_entry.id, last_line.id)

    # Running balance at the start of each account's portion of this page.
    running: dict[int, Decimal] = {}
    for line, entry, code in rows:
        if line.account_id not in running:
            base = sections[line.account_id].opening_balance
            if cursor is not None and code == cursor[0]:
                base += _balance_before_cursor(db, line.account_id, date_from, cursor)
            running[line.account_id] = base
        running[line.account_id] += line.debit - line.credit
        sections[line.account_id].lines.append(
            LedgerLine(
                entry_id=entry.id,
                line_id=line.id,
                entry_date=entry.entry_date,
                description=entry.description,
                reference=entry.reference,
                memo=line.memo,
                debit=line.debit,
                credit=line.credit,
                balance=running[line.account_id],
                reverses_entry_id=entry.reverses_entry_id,
                reversed_by_entry_id=entry.reversed_by_entry_id,
                entry_type=entry.entry_type.value,
                correlation_id=entry.correlation_id,
            )
        )
    return GeneralLedger(
        date_from=date_from,
        date_to=date_to,
        accounts=list(sections.values()),
        limit=limit,
        next_cursor=next_cursor,
    )


def _balance_before_cursor(db: Session, account_id: int, date_from, cursor) -> Decimal:
    """Net of the account's lines in range that precede the cursor (exact, in SQL)."""
    _code, day, entry_id, line_id = cursor
    stmt = (
        select(cents_sum(db, JournalLine.debit) - cents_sum(db, JournalLine.credit))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalEntry.status == EntryStatus.POSTED,
            JournalLine.account_id == account_id,
            tuple_(JournalEntry.entry_date, JournalEntry.id, JournalLine.id)
            <= tuple_(day, entry_id, line_id),
        )
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    return from_cents(db.execute(stmt).scalar())


def day_before(day: date) -> date:
    return day - timedelta(days=1)
