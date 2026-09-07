"""Period close/lock under genuinely concurrent database sessions.

Each scenario runs two sessions in two threads. A ``Gate`` pauses one of them
at a service checkpoint (after validation, before commit) while the other one
races it. The expected outcome is the same on both backends; the mechanism
differs:

* PostgreSQL: the close holds ``FOR UPDATE`` on the period row, journal
  transactions hold ``FOR SHARE`` - the loser blocks on the row lock;
* SQLite: every write transaction begins with ``BEGIN IMMEDIATE`` - the loser
  blocks on the database write lock (busy timeout 5s).

Invariant under test: a period never closes with an unresolved (draft) journal
dated inside it, whatever the interleaving. Journals that start while a close
is in progress wait and then see the closed period.
"""

import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountingPeriod,
    AccountType,
    EntryStatus,
    JournalEntry,
    PeriodStatus,
    RecurringTemplate,
)
from app.schemas.journal import JournalEntryCreate, JournalEntryUpdate, JournalLineIn
from app.services.journal import create_draft, reverse_entry, update_draft
from app.services.periods import (
    PeriodNotClosable,
    PeriodStateConflict,
    close_period,
    lock_period,
    reopen_period,
)
from app.services.posting import EntryNotPostable, post_entry
from app.services.recurring import create_template, generate
from app.services.reports import trial_balance
from tests.helpers import Gate, Worker, clear_hooks, failing_checkpoint  # noqa: F401

JAN_1, JAN_15, JAN_31, FEB_10 = (
    date(2026, 1, 1),
    date(2026, 1, 15),
    date(2026, 1, 31),
    date(2026, 2, 10),
)


@pytest.fixture
def ledger(engine):
    """Accounts + January (open) + February (open); returns ids."""
    with Session(engine) as db:
        cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
        sales = Account(code="4000", name="Sales", type=AccountType.REVENUE)
        jan = AccountingPeriod(name="2026-01", start_date=JAN_1, end_date=JAN_31)
        feb = AccountingPeriod(
            name="2026-02", start_date=date(2026, 2, 1), end_date=date(2026, 2, 28)
        )
        db.add_all([cash, sales, jan, feb])
        db.commit()
        return {"cash": cash.id, "sales": sales.id, "jan": jan.id, "feb": feb.id}


def _lines(ledger, amount="10.00"):
    return [
        JournalLineIn(account_id=ledger["cash"], debit=Decimal(amount)),
        JournalLineIn(account_id=ledger["sales"], credit=Decimal(amount)),
    ]


def _draft(engine, ledger, on, amount="10.00") -> int:
    with Session(engine) as db:
        payload = JournalEntryCreate(entry_date=on, description="e", lines=_lines(ledger, amount))
        return create_draft(db, payload).id


def _posted(engine, ledger, on, amount="10.00") -> int:
    entry_id = _draft(engine, ledger, on, amount)
    with Session(engine) as db:
        post_entry(db, db.get(JournalEntry, entry_id))
    return entry_id


def _period_status(engine, period_id) -> PeriodStatus:
    with Session(engine) as db:
        return db.get(AccountingPeriod, period_id).status


def _entry_status(engine, entry_id) -> EntryStatus:
    with Session(engine) as db:
        return db.get(JournalEntry, entry_id).status


def _run_close(engine, period_id):
    with Session(engine) as db:
        return close_period(db, db.get(AccountingPeriod, period_id)).status


def _assert_blocked(worker: Worker, seconds: float = 0.4) -> None:
    worker.join(seconds)
    assert worker.is_alive(), "the competing transaction should have been blocked"


# --- journal first, close second: the close sees the journal --------------------------


def test_close_waits_for_in_flight_posting_and_includes_it(engine, ledger):
    draft_id = _draft(engine, ledger, JAN_15, "40.00")
    gate = Gate("posting.before_commit")

    def posting():
        with Session(engine) as db:
            post_entry(db, db.get(JournalEntry, draft_id))

    poster = Worker(posting)
    poster.start()
    gate.wait_reached()  # posting validated + marked, not yet committed
    closer = Worker(_run_close, engine, ledger["jan"])
    closer.start()
    _assert_blocked(closer)  # cannot validate while the posting is in flight

    gate.release.set()
    poster.finish()
    closer.finish()
    assert poster.error is None and closer.error is None, (poster.error, closer.error)
    assert closer.result == PeriodStatus.CLOSED
    assert _entry_status(engine, draft_id) == EntryStatus.POSTED
    with Session(engine) as db:
        assert trial_balance(db, JAN_1, JAN_31).total_debit == Decimal("40.00")


def test_close_waits_for_in_flight_draft_and_then_refuses(engine, ledger):
    gate = Gate("journal.update_before_commit")
    feb_draft = _draft(engine, ledger, FEB_10)

    def redate_into_january():
        with Session(engine) as db:
            entry = db.get(JournalEntry, feb_draft)
            payload = JournalEntryUpdate(
                entry_date=JAN_15, description="moved", version=entry.version, lines=_lines(ledger)
            )
            update_draft(db, entry, payload)

    mover = Worker(redate_into_january)
    mover.start()
    gate.wait_reached()
    closer = Worker(_run_close, engine, ledger["jan"])
    closer.start()
    _assert_blocked(closer)

    gate.release.set()
    mover.finish()
    closer.finish()
    assert mover.error is None
    assert isinstance(closer.error, PeriodNotClosable), closer.error
    assert f"[{feb_draft}]" in str(closer.error)
    assert _period_status(engine, ledger["jan"]) == PeriodStatus.OPEN


def test_close_waits_for_in_flight_recurring_generation(engine, ledger):
    with Session(engine) as db:
        template = create_template(
            db,
            name="rent",
            description="rent",
            reference=None,
            frequency="monthly",
            start_date=JAN_31,
            end_date=None,
            lines=[
                {"account_id": ledger["cash"], "debit": Decimal("5.00")},
                {"account_id": ledger["sales"], "credit": Decimal("5.00")},
            ],
        )
        template_id = template.id
    gate = Gate("recurring.commit:rent")

    def generating():
        with Session(engine) as db:
            generate(db, db.get(RecurringTemplate, template_id), JAN_31)

    generator = Worker(generating)
    generator.start()
    gate.wait_reached()
    closer = Worker(_run_close, engine, ledger["jan"])
    closer.start()
    _assert_blocked(closer)

    gate.release.set()
    generator.finish()
    closer.finish()
    assert generator.error is None
    assert isinstance(closer.error, PeriodNotClosable)
    assert "draft journal entries dated in the period" in str(closer.error)
    assert _period_status(engine, ledger["jan"]) == PeriodStatus.OPEN


def test_close_waits_for_in_flight_reversal_and_includes_it(engine, ledger):
    original = _posted(engine, ledger, JAN_15, "25.00")
    gate = Gate("posting.before_commit")

    def reversing():
        with Session(engine) as db:
            reverse_entry(db, db.get(JournalEntry, original), reversal_date=JAN_31)

    reverser = Worker(reversing)
    reverser.start()
    gate.wait_reached()
    closer = Worker(_run_close, engine, ledger["jan"])
    closer.start()
    _assert_blocked(closer)

    gate.release.set()
    reverser.finish()
    closer.finish()
    assert reverser.error is None and closer.error is None, (reverser.error, closer.error)
    with Session(engine) as db:
        posted = db.scalars(
            select(JournalEntry).where(JournalEntry.status == EntryStatus.POSTED)
        ).all()
        assert len(posted) == 2 and {e.entry_date for e in posted} == {JAN_15, JAN_31}
    assert _period_status(engine, ledger["jan"]) == PeriodStatus.CLOSED


# --- close first, journal second: the journal sees the closed period --------------------


def test_draft_creation_during_close_waits_and_lands_in_closed_period(engine, ledger):
    gate = Gate("periods.close_before_commit")
    closer = Worker(_run_close, engine, ledger["jan"])
    closer.start()
    gate.wait_reached()  # validation passed: January has no drafts

    created = {}

    def creating():
        with Session(engine) as db:
            payload = JournalEntryCreate(
                entry_date=JAN_15, description="late", lines=_lines(ledger)
            )
            created["id"] = create_draft(db, payload).id

    creator = Worker(creating)
    creator.start()
    _assert_blocked(creator)  # waits for the close (row share lock / write lock)

    gate.release.set()
    closer.finish()
    creator.finish()
    assert closer.error is None and closer.result == PeriodStatus.CLOSED
    assert creator.error is None
    # The draft exists (drafts may live in closed periods) but can never be posted there,
    # and the close that was validated before it started was not invalidated.
    with Session(engine) as db:
        entry = db.get(JournalEntry, created["id"])
        with pytest.raises(EntryNotPostable, match="is closed"):
            post_entry(db, entry)


def test_posting_during_close_waits_and_is_refused(engine, ledger):
    # A draft dated in February is posted while January closes: unrelated period,
    # but on SQLite it still waits for the write lock. Then a January posting is refused.
    feb_draft = _draft(engine, ledger, FEB_10)
    gate = Gate("periods.close_before_commit")
    closer = Worker(_run_close, engine, ledger["jan"])
    closer.start()
    gate.wait_reached()

    def posting():
        with Session(engine) as db:
            return post_entry(db, db.get(JournalEntry, feb_draft)).status

    poster = Worker(posting)
    poster.start()
    time.sleep(0.2)
    gate.release.set()
    closer.finish()
    poster.finish()
    assert closer.error is None and poster.error is None
    assert poster.result == EntryStatus.POSTED
    assert _period_status(engine, ledger["jan"]) == PeriodStatus.CLOSED


def test_two_concurrent_closes_one_wins(engine, ledger):
    gate = Gate("periods.close_before_commit")
    first = Worker(_run_close, engine, ledger["jan"])
    first.start()
    gate.wait_reached()
    second = Worker(_run_close, engine, ledger["jan"])
    second.start()
    _assert_blocked(second)
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None and first.result == PeriodStatus.CLOSED
    assert isinstance(second.error, PeriodStateConflict)
    assert "already closed" in str(second.error)


def test_reopen_during_lock_sees_the_lock(engine, ledger):
    _run_close(engine, ledger["jan"])
    gate = Gate("periods.lock_before_commit")

    def locking():
        with Session(engine) as db:
            return lock_period(db, db.get(AccountingPeriod, ledger["jan"])).status

    locker = Worker(locking)
    locker.start()
    gate.wait_reached()

    def reopening():
        with Session(engine) as db:
            return reopen_period(db, db.get(AccountingPeriod, ledger["jan"])).status

    reopener = Worker(reopening)
    reopener.start()
    _assert_blocked(reopener)
    gate.release.set()
    locker.finish()
    reopener.finish()
    assert locker.result == PeriodStatus.LOCKED
    assert isinstance(reopener.error, PeriodStateConflict)
    assert "locked" in str(reopener.error)
    assert _period_status(engine, ledger["jan"]) == PeriodStatus.LOCKED


def test_close_failure_at_commit_leaves_period_open_and_ledger_unchanged(engine, ledger):
    posted = _posted(engine, ledger, JAN_15, "7.00")
    with failing_checkpoint("periods.close_before_commit"):
        with pytest.raises(RuntimeError, match="injected"):
            _run_close(engine, ledger["jan"])
    assert _period_status(engine, ledger["jan"]) == PeriodStatus.OPEN
    with Session(engine) as db:
        period = db.get(AccountingPeriod, ledger["jan"])
        assert period.closed_at is None
        assert db.get(JournalEntry, posted).status == EntryStatus.POSTED
    assert _run_close(engine, ledger["jan"]) == PeriodStatus.CLOSED  # retry succeeds
