"""Recurring journal entries.

A ``RecurringTemplate`` describes an entry that repeats (weekly, monthly,
quarterly, yearly) from ``start_date``. Generation creates one *draft* per
scheduled occurrence up to a given date and advances ``next_run_date``; the
drafts carry ``entry_type = recurring``, the template id and the schedule slot
(``scheduled_for``), and are reviewed and posted like any other draft.

No duplicates: a template never generates the same slot twice (the template
remembers how far it has run, so a draft the reviewer deleted is not recreated
either), and the database enforces a unique (template, slot) pair as a backstop.

Transaction ownership
---------------------
* ``generate(db, template, until, commit=True)`` - one template. The template
  row is locked ``FOR UPDATE`` first (SQLite: writers are serialised anyway),
  so two concurrent runs of the same template cannot both pass the cursor
  check; the loser sees the advanced cursor and generates nothing.
* ``generate_due(db, until)`` - **atomic batch**: every due template is
  locked, all drafts and all cursor updates are flushed, and a single commit
  makes them visible together. If anything fails on the second or third
  template - at flush or at commit - the whole batch rolls back, no draft of
  any template is left behind and no cursor moves, so a retry regenerates
  exactly the same set without duplicates.
* ``generate_due(db, until, atomic=False)`` - the pre-existing per-template
  mode (each template commits on its own), for callers that prefer partial
  progress over all-or-nothing.
"""

import calendar
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    EntryType,
    Frequency,
    JournalEntry,
    JournalLine,
    RecurringTemplate,
    RecurringTemplateLine,
)
from app.services import hooks
from app.services.exceptions import ConflictError, UnprocessableError
from app.services.locking import lock_for_update, lock_period_for_date

MONTHS = {Frequency.MONTHLY: 1, Frequency.QUARTERLY: 3, Frequency.YEARLY: 12}


class TemplateNameTaken(ConflictError):
    pass


def add_months(anchor: date, months: int) -> date:
    """``anchor`` plus ``months``, clamped to the target month's last day (Jan 31 -> Feb 28)."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def occurrence(template: RecurringTemplate, index: int) -> date:
    """The ``index``-th occurrence (0 = start_date), anchored on start_date so it never drifts."""
    if template.frequency == Frequency.WEEKLY:
        return template.start_date + timedelta(weeks=index)
    return add_months(template.start_date, MONTHS[template.frequency] * index)


def occurrences_between(template: RecurringTemplate, first: date, last: date) -> list[date]:
    """Scheduled dates d with first <= d <= last (and <= end_date if set)."""
    dates: list[date] = []
    index = 0
    while True:
        day = occurrence(template, index)
        if day > last or (template.end_date is not None and day > template.end_date):
            return dates
        if day >= first:
            dates.append(day)
        index += 1


def next_occurrence_after(template: RecurringTemplate, day: date) -> date:
    index = 0
    while occurrence(template, index) <= day:
        index += 1
    return occurrence(template, index)


def create_template(
    db: Session,
    *,
    name: str,
    description: str,
    reference: str | None,
    frequency: Frequency,
    start_date: date,
    end_date: date | None,
    lines: list[dict],
    commit: bool = True,
) -> RecurringTemplate:
    if end_date is not None and end_date < start_date:
        raise UnprocessableError("end_date must not be before start_date")
    if db.scalar(select(RecurringTemplate).where(RecurringTemplate.name == name)):
        raise TemplateNameTaken(f"Recurring template '{name}' already exists")
    account_ids = {line["account_id"] for line in lines}
    accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    if account_ids - {a.id for a in accounts}:
        raise UnprocessableError(
            f"Unknown account ids: {sorted(account_ids - {a.id for a in accounts})}"
        )
    summary = sorted(a.code for a in accounts if a.is_summary)
    if summary:
        raise UnprocessableError(f"summary accounts cannot be posted to: {', '.join(summary)}")

    template = RecurringTemplate(
        name=name,
        description=description,
        reference=reference,
        frequency=frequency,
        start_date=start_date,
        end_date=end_date,
        next_run_date=start_date,
        lines=[RecurringTemplateLine(**line) for line in lines],
    )
    db.add(template)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(template)
    return template


def _build_drafts(db: Session, template: RecurringTemplate, until: date) -> list[JournalEntry]:
    """Create (flush) the template's pending drafts and advance its cursor; no commit."""
    if not template.is_active:
        return []
    dates = occurrences_between(template, template.next_run_date, until)
    drafts = [
        JournalEntry(
            entry_date=day,
            description=template.description,
            reference=template.reference,
            entry_type=EntryType.RECURRING,
            recurring_template=template,
            scheduled_for=day,
            lines=[
                JournalLine(
                    account_id=line.account_id, debit=line.debit, credit=line.credit, memo=line.memo
                )
                for line in template.lines
            ],
        )
        for day in dates
    ]
    if drafts:
        for day in dates:
            lock_period_for_date(db, day)  # serialise with a concurrent period close
        db.add_all(drafts)
        template.next_run_date = next_occurrence_after(template, dates[-1])
        hooks.checkpoint(f"recurring.flush:{template.name}")
        db.flush()
    return drafts


def generate(
    db: Session, template: RecurringTemplate, until: date, *, commit: bool = True
) -> list[JournalEntry]:
    """Create drafts for every occurrence from ``next_run_date`` to ``until`` (inclusive)."""
    try:
        locked = lock_for_update(db, RecurringTemplate, template.id)  # fresh cursor, exclusive
        drafts = _build_drafts(db, locked, until)
        hooks.checkpoint(f"recurring.commit:{template.name}")
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()  # all or nothing: no partial batch, next_run_date unchanged
        raise
    for draft in drafts:
        db.refresh(draft)
    return drafts


def due_templates(db: Session, until: date) -> list[RecurringTemplate]:
    return db.scalars(
        select(RecurringTemplate)
        .where(RecurringTemplate.is_active.is_(True), RecurringTemplate.next_run_date <= until)
        .order_by(RecurringTemplate.id)
    ).all()


def generate_due(db: Session, until: date, *, atomic: bool = True) -> list[JournalEntry]:
    """Run every active template whose next occurrence is due by ``until``.

    ``atomic=True``: one transaction for all templates (see module docstring).
    ``atomic=False``: each template commits separately; a failure leaves the
    earlier templates' drafts committed and reports the failing one.
    """
    if not atomic:
        created: list[JournalEntry] = []
        for template in due_templates(db, until):
            created.extend(generate(db, template, until))
        return created

    created = []
    try:
        for template in due_templates(db, until):
            locked = lock_for_update(db, RecurringTemplate, template.id)
            created.extend(_build_drafts(db, locked, until))
        hooks.checkpoint("recurring.batch_before_commit")
        db.commit()
    except Exception:
        db.rollback()
        raise
    for draft in created:
        db.refresh(draft)
    return created
