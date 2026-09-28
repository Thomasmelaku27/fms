"""Posting journal entries.

Posting is the transition from ``draft`` to ``posted``. It is the point where
the double-entry rules are enforced in the application; the database triggers
installed by the migrations enforce the same ledger rules as a backstop.

Usable from any caller (API routes, batch jobs, future modules):

    post_entry(db, entry)  -> the posted entry
    raises EntryAlreadyPosted / EntryNotPostable

Rules checked here, in order:

* the entry is still a draft;
* every line has an amount on exactly one side (a draft may contain lines
  without an amount yet; those must be completed or removed before posting);
* there are at least two lines;
* total debits equal total credits;
* every line's account is active.
"""

from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Account, EntryStatus, JournalEntry

MIN_LINES = 2


class PostingError(Exception):
    """Base class for errors raised when an entry cannot be posted."""


class EntryAlreadyPosted(PostingError):
    def __init__(self, entry_id: int) -> None:
        super().__init__(f"Journal entry {entry_id} is already posted")


class EntryNotPostable(PostingError):
    """The entry violates one or more posting rules (all are reported)."""

    def __init__(self, entry_id: int, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Journal entry {entry_id} cannot be posted: " + "; ".join(problems))


def posting_problems(db: Session, entry: JournalEntry) -> list[str]:
    """Return every reason ``entry`` cannot be posted (empty list = postable)."""
    problems: list[str] = []

    for position, line in enumerate(entry.lines, start=1):
        if line.debit == 0 and line.credit == 0:
            problems.append(f"line {position} has no amount")
        elif line.debit > 0 and line.credit > 0:
            problems.append(f"line {position} has both a debit and a credit")

    if len(entry.lines) < MIN_LINES:
        problems.append(f"at least {MIN_LINES} lines are required")

    total_debit = sum((line.debit for line in entry.lines), Decimal("0"))
    total_credit = sum((line.credit for line in entry.lines), Decimal("0"))
    if total_debit != total_credit:
        problems.append(f"debits ({total_debit:.2f}) do not equal credits ({total_credit:.2f})")

    account_ids = {line.account_id for line in entry.lines}
    if account_ids:
        inactive = db.scalars(
            select(Account.code)
            .where(Account.id.in_(account_ids), Account.is_active.is_(False))
            .order_by(Account.code)
        ).all()
        if inactive:
            problems.append(f"inactive accounts: {', '.join(inactive)}")

    return problems


def post_entry(db: Session, entry: JournalEntry) -> JournalEntry:
    """Validate and post ``entry``; commits on success."""
    if entry.status == EntryStatus.POSTED:
        raise EntryAlreadyPosted(entry.id)

    problems = posting_problems(db, entry)
    if problems:
        raise EntryNotPostable(entry.id, problems)

    # Conditional update so two concurrent callers cannot both "win"; the database
    # triggers re-verify balance/completeness and refuse to touch a posted row.
    result = db.execute(
        update(JournalEntry)
        .where(JournalEntry.id == entry.id, JournalEntry.status == EntryStatus.DRAFT)
        .values(status=EntryStatus.POSTED)
    )
    if result.rowcount != 1:
        db.rollback()
        raise EntryAlreadyPosted(entry.id)
    db.commit()
    db.refresh(entry)
    return entry
