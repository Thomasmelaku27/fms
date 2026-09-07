"""Row-level locking strategy shared by the services.

PostgreSQL (READ COMMITTED):
    * ``lock_for_update`` takes ``SELECT ... FOR UPDATE`` on one row; used by
      state transitions (close/lock a period, post an entry, run a template).
    * ``lock_period_for_date`` takes ``SELECT ... FOR SHARE`` on the accounting
      period containing a date. Every transaction that creates, re-dates,
      posts or reverses a journal into a period takes this share lock, and a
      period close/lock takes the exclusive lock. Hence a close can only start
      once in-flight journals in the period have committed (and its validation
      sees them), and journals that start during a close wait for it and then
      re-read the period's new status. Validation therefore stays valid until
      the close commits.

SQLite:
    ``app.db.make_engine`` opens every transaction with ``BEGIN IMMEDIATE``,
    which takes the database's single write lock up front. Competing writers
    are fully serialised (they wait, up to the busy timeout), so the row locks
    are unnecessary; SQLite's compiler simply ignores ``FOR UPDATE``.

The guarantees differ in one respect: PostgreSQL allows unrelated writers to
proceed concurrently and only serialises writers touching the same period,
while SQLite serialises all writers. Both give the same accounting outcome.
"""

from datetime import date
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import AccountingPeriod

T = TypeVar("T", bound=Base)


def lock_for_update(db: Session, model: type[T], row_id: int) -> T | None:
    """Re-read ``row_id`` under an exclusive row lock (fresh state, blocks other lockers).

    ``populate_existing`` makes the locked row's *current* values overwrite a
    stale copy already in the session - the whole point of locking first.
    """
    return db.execute(
        select(model).where(model.id == row_id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()


def lock_period_for_date(db: Session, on: date) -> AccountingPeriod | None:
    """The period containing ``on`` under a share lock (None if no period covers it)."""
    return db.execute(
        select(AccountingPeriod)
        .where(AccountingPeriod.start_date <= on, AccountingPeriod.end_date >= on)
        .with_for_update(read=True),
        execution_options={"populate_existing": True},
    ).scalar_one_or_none()
