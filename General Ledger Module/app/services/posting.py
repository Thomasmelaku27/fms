"""Posting journal entries.

Posting is the transition from ``draft`` to ``posted``. It is the point where
the double-entry rules are enforced in the application; the database triggers
installed by the migrations enforce the same ledger rules as a backstop.

Transaction ownership: every function works inside the caller's session
transaction. With ``commit=True`` (the default, used by the API) the function
commits on success; with ``commit=False`` it only flushes and the caller owns
the transaction - that is how reversals, the year-end close and batch posting
compose several steps atomically. On failure nothing is committed; callers
that own the transaction roll back.

Locking: before posting, the accounting period containing the entry date is
read under a share lock (PostgreSQL ``FOR SHARE``; SQLite serialises writers
with ``BEGIN IMMEDIATE``), so a concurrent period close must wait for this
posting to commit or the posting waits for the close and then sees the
period's new status. See ``app.services.locking``.

Posting metadata (``posted_at``, ``posted_by``, ``posting_source``,
``correlation_id``) is written by the same conditional UPDATE that flips the
status, from the caller's ``PostingContext``; posted rows are immutable, so
the metadata can never change afterwards, and a CHECK constraint guarantees
it is present exactly when an entry is posted.

Rules checked here, in order:

* the entry is still a draft;
* every line has an amount on exactly one side (a draft may contain lines
  without an amount yet, or with both sides filled in; those must be fixed
  before posting);
* there are at least two lines;
* total debits equal total credits;
* every line's account is active and a posting (non-summary) account;
* the entry date does not fall in a closed or locked accounting period
  (year-end ``closing`` entries are the one exception for *closed* periods).
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Account, AccountingPeriod, EntryStatus, EntryType, JournalEntry, PeriodStatus
from app.services import hooks
from app.services.actor import PostingContext, system_context
from app.services.exceptions import ConflictError, UnprocessableError
from app.services.locking import lock_for_update, lock_period_for_date

MIN_LINES = 2


class PostingError(Exception):
    """Marker base class for errors raised when an entry cannot be posted."""


class EntryAlreadyPosted(ConflictError, PostingError):
    def __init__(self, entry_id: int) -> None:
        super().__init__(f"Journal entry {entry_id} is already posted")


class EntryNotPostable(UnprocessableError, PostingError):
    """The entry violates one or more posting rules (all are reported)."""

    def __init__(self, entry_id: int, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Journal entry {entry_id} cannot be posted: " + "; ".join(problems))


class BatchNotPostable(UnprocessableError, PostingError):
    """At least one entry of a batch violates a posting rule; nothing was posted."""

    def __init__(self, problems: dict[int, list[str]]) -> None:
        self.problems = problems
        detail = "; ".join(
            f"entry {entry_id}: {', '.join(reasons)}" for entry_id, reasons in problems.items()
        )
        super().__init__(f"Batch cannot be posted: {detail}")


def balance_problem(entry: JournalEntry) -> str | None:
    """The double-entry rule itself: total debits must equal total credits (Decimal, exact)."""
    total_debit = sum((line.debit for line in entry.lines), Decimal("0"))
    total_credit = sum((line.credit for line in entry.lines), Decimal("0"))
    if total_debit != total_credit:
        return f"debits ({total_debit:.2f}) do not equal credits ({total_credit:.2f})"
    return None


def period_blocks(period: AccountingPeriod | None, entry: JournalEntry) -> bool:
    """Whether ``period`` refuses the posting of ``entry`` dated inside it."""
    if period is None:
        return False
    if period.status == PeriodStatus.LOCKED:
        return True
    return period.status == PeriodStatus.CLOSED and entry.entry_type != EntryType.CLOSING


def posting_problems(db: Session, entry: JournalEntry, *, lock: bool = False) -> list[str]:
    """Return every reason ``entry`` cannot be posted (empty list = postable).

    With ``lock=True`` the period row is read under a share lock so the answer
    stays valid until the caller's transaction ends.
    """
    problems: list[str] = []

    for position, line in enumerate(entry.lines, start=1):
        if line.debit == 0 and line.credit == 0:
            problems.append(f"line {position} has no amount")
        elif line.debit > 0 and line.credit > 0:
            problems.append(f"line {position} has both a debit and a credit")

    if len(entry.lines) < MIN_LINES:
        problems.append(f"at least {MIN_LINES} lines are required")

    imbalance = balance_problem(entry)
    if imbalance is not None:
        problems.append(imbalance)

    account_ids = {line.account_id for line in entry.lines}
    if account_ids:
        accounts = db.scalars(
            select(Account).where(Account.id.in_(account_ids)).order_by(Account.code)
        ).all()
        inactive = [a.code for a in accounts if not a.is_active]
        if inactive:
            problems.append(f"inactive accounts: {', '.join(inactive)}")
        summary = [a.code for a in accounts if a.is_summary]
        if summary:
            problems.append(f"summary accounts cannot be posted to: {', '.join(summary)}")

    if lock:
        period = lock_period_for_date(db, entry.entry_date)
    else:
        period = db.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.start_date <= entry.entry_date,
                AccountingPeriod.end_date >= entry.entry_date,
            )
        )
    if period_blocks(period, entry):
        problems.append(f"accounting period '{period.name}' is {period.status.value}")

    return problems


def _mark_posted(db: Session, entry: JournalEntry, ctx: PostingContext) -> None:
    """Conditional update: only a draft row can become posted, whoever is quickest.

    The posting metadata travels in the same statement, so it is exactly as
    atomic and as immutable as the status change itself.
    """
    result = db.execute(
        update(JournalEntry)
        .where(JournalEntry.id == entry.id, JournalEntry.status == EntryStatus.DRAFT)
        .values(
            status=EntryStatus.POSTED,
            posted_at=datetime.now(timezone.utc),
            posted_by=ctx.actor_ref,
            posting_source=ctx.source,
            correlation_id=ctx.correlation_id,
        )
    )
    if result.rowcount != 1:
        raise EntryAlreadyPosted(entry.id)


def post_entry(
    db: Session,
    entry: JournalEntry,
    *,
    ctx: PostingContext | None = None,
    commit: bool = True,
) -> JournalEntry:
    """Validate and post ``entry`` on behalf of ``ctx`` (a fresh system context by default).

    Raises EntryAlreadyPosted (409) or EntryNotPostable (422). With
    ``commit=True`` the change is committed (and rolled back on failure); with
    ``commit=False`` the caller owns the transaction.
    """
    if entry.status == EntryStatus.POSTED:
        raise EntryAlreadyPosted(entry.id)
    ctx = ctx or system_context()
    try:
        problems = posting_problems(db, entry, lock=True)
        if problems:
            raise EntryNotPostable(entry.id, problems)
        _mark_posted(db, entry, ctx)
        hooks.checkpoint("posting.before_commit")
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(entry)
    return entry


def post_entries(
    db: Session,
    entry_ids: Sequence[int],
    *,
    ctx: PostingContext | None = None,
    commit: bool = True,
) -> tuple[list[JournalEntry], list[JournalEntry]]:
    """Post several drafts atomically: either all of them are posted or none.

    Returns ``(posted_now, already_posted)``. Semantics for repeated and
    overlapping requests:

    * all listed entries already posted -> nothing to do, success (a retry
      after a successful batch is idempotent);
    * a mix of posted and draft entries -> 409: the batch overlaps one that
      already ran; the caller must reconcile instead of posting the rest;
    * any draft invalid -> 422 listing every problem, nothing posted;
    * unknown id -> 422.

    Rows are locked ``FOR UPDATE`` in id order (deadlock-free), so two
    concurrent identical batches serialise: the second one finds everything
    posted and succeeds without doing anything.
    """
    unique_ids = sorted(set(entry_ids))
    if not unique_ids:
        raise UnprocessableError("the batch contains no journal entries")
    ctx = ctx or system_context("batch")
    try:
        entries = [lock_for_update(db, JournalEntry, entry_id) for entry_id in unique_ids]
        missing = [eid for eid, entry in zip(unique_ids, entries) if entry is None]
        if missing:
            raise UnprocessableError(f"Unknown journal entry ids: {missing}")

        already = [e for e in entries if e.status == EntryStatus.POSTED]
        drafts = [e for e in entries if e.status == EntryStatus.DRAFT]
        if already and drafts:
            raise ConflictError(
                "Batch overlaps an earlier batch: entries "
                f"{[e.id for e in already]} are already posted while "
                f"{[e.id for e in drafts]} are still drafts"
            )
        problems = {
            e.id: reasons for e in drafts if (reasons := posting_problems(db, e, lock=True))
        }
        if problems:
            raise BatchNotPostable(problems)
        for entry in drafts:
            _mark_posted(db, entry, ctx)  # one correlation id for the whole batch
        hooks.checkpoint("posting.batch_before_commit")
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    for entry in entries:
        db.refresh(entry)
    return drafts, already
