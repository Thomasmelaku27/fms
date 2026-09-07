"""Journal entry lifecycle: draft editing, reclassification, deletion, reversals.

Editable vs. system-controlled fields
-------------------------------------
A draft's *accounting details* - ``entry_date``, ``description``, ``reference``
and the lines - are editable through ``update_draft`` (PUT), guarded by
optimistic locking: the caller states the ``version`` it last saw and the edit
is refused (409) if the draft changed in the meantime.

Everything that records *where the entry came from* is system-controlled and
never changes through an ordinary edit: ``entry_type``, ``recurring_template_id``
and ``scheduled_for`` (recurring provenance), ``reverses_entry_id`` (reversal
provenance) and the ``source_*`` / ``operation_id`` / ``request_fingerprint``
columns (journal-processing provenance). A PUT that names a different
``entry_type`` is rejected with 422 instead of being applied silently.

Reclassification rules (``reclassify_draft``): only a draft can be
reclassified, only between ``standard`` and ``adjusting``, and only if the
draft has no recurring, reversal or external-source provenance. It is an
explicit, versioned operation. Posted entries never change.

Reversals: a posted entry can be reversed once (see ``reverse_entry``); the
reversal is a new posted entry with the sides swapped and a link back.
``opening`` and ``closing`` entries are system entries and cannot be reversed.

Transaction ownership: ``commit=True`` commits (and rolls back on failure);
``commit=False`` flushes and leaves the transaction to the caller.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.models import (
    NON_REVERSIBLE_TYPES,
    RECLASSIFIABLE_TYPES,
    Account,
    EntryStatus,
    EntryType,
    JournalEntry,
    JournalLine,
)
from app.schemas.journal import JournalEntryCreate, JournalEntryUpdate
from app.services import hooks
from app.services.actor import PostingContext, system_context
from app.services.exceptions import ConflictError, UnprocessableError
from app.services.locking import lock_period_for_date
from app.services.posting import EntryAlreadyPosted, post_entry


class EntryNotPosted(ConflictError):
    def __init__(self, entry_id: int) -> None:
        super().__init__(f"Journal entry {entry_id} is not posted")


class EntryAlreadyReversed(ConflictError):
    def __init__(self, entry: JournalEntry) -> None:
        super().__init__(
            f"Journal entry {entry.id} was already reversed by entry {entry.reversed_by_entry_id}"
        )


class EntryNotReversible(ConflictError):
    def __init__(self, entry: JournalEntry) -> None:
        super().__init__(
            f"Journal entry {entry.id} is a system {entry.entry_type.value} entry and "
            "cannot be reversed"
        )


class VersionConflict(ConflictError):
    def __init__(self, entry: JournalEntry, expected: int) -> None:
        super().__init__(
            f"Journal entry {entry.id} is at version {entry.version}, not {expected}; "
            "reload it and retry"
        )


class ProvenanceChange(UnprocessableError):
    def __init__(self, entry: JournalEntry, requested: EntryType) -> None:
        super().__init__(
            f"Journal entry {entry.id} is a {entry.entry_type.value} entry; an ordinary edit "
            f"cannot change it to {requested.value}. Use the reclassify operation."
        )


class NotReclassifiable(UnprocessableError):
    pass


class UnknownAccounts(UnprocessableError):
    def __init__(self, account_ids: set[int]) -> None:
        super().__init__(f"Unknown account ids: {sorted(account_ids)}")


def _require_accounts(db: Session, account_ids: set[int]) -> None:
    if not account_ids:
        return
    found = set(db.scalars(select(Account.id).where(Account.id.in_(account_ids))))
    if account_ids - found:
        raise UnknownAccounts(account_ids - found)


def _require_draft(entry: JournalEntry) -> None:
    if entry.status == EntryStatus.POSTED:
        raise EntryAlreadyPosted(entry.id)


def _require_version(entry: JournalEntry, expected: int | None) -> None:
    if expected is not None and entry.version != expected:
        raise VersionConflict(entry, expected)


def has_external_provenance(entry: JournalEntry) -> bool:
    return (
        entry.recurring_template_id is not None
        or entry.reverses_entry_id is not None
        or entry.source_type is not None
    )


def _finish(
    db: Session, entry: JournalEntry, commit: bool, expected_version: int | None = None
) -> JournalEntry:
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except StaleDataError:  # another transaction changed the row first
        db.rollback()
        db.refresh(entry)
        raise VersionConflict(entry, expected_version or entry.version) from None
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(entry)
    return entry


def create_draft(db: Session, payload: JournalEntryCreate, *, commit: bool = True) -> JournalEntry:
    _require_accounts(db, {line.account_id for line in payload.lines})
    lock_period_for_date(db, payload.entry_date)  # serialise with a concurrent close
    entry = JournalEntry(
        entry_date=payload.entry_date,
        description=payload.description,
        reference=payload.reference,
        entry_type=payload.entry_type,
        lines=[JournalLine(**line.model_dump()) for line in payload.lines],
    )
    db.add(entry)
    return _finish(db, entry, commit)


def update_draft(
    db: Session, entry: JournalEntry, payload: JournalEntryUpdate, *, commit: bool = True
) -> JournalEntry:
    """Replace the accounting details (header and all lines) of a draft.

    ``payload.version`` must equal the draft's current version. Provenance
    fields are untouched; a differing ``entry_type`` in the payload is an error.
    """
    _require_draft(entry)
    _require_version(entry, payload.version)
    if payload.entry_type is not None and payload.entry_type != entry.entry_type:
        raise ProvenanceChange(entry, payload.entry_type)
    _require_accounts(db, {line.account_id for line in payload.lines})
    lock_period_for_date(db, payload.entry_date)
    if payload.entry_date != entry.entry_date:
        lock_period_for_date(db, entry.entry_date)

    try:
        entry.entry_date = payload.entry_date
        entry.description = payload.description
        entry.reference = payload.reference
        entry.lines = [JournalLine(**line.model_dump()) for line in payload.lines]
        entry.version = payload.version + 1
        hooks.checkpoint("journal.update_before_commit")
    except StaleDataError:  # may surface at any flush, not only at commit
        db.rollback()
        db.refresh(entry)
        raise VersionConflict(entry, payload.version) from None
    return _finish(db, entry, commit, payload.version)


def reclassify_draft(
    db: Session, entry: JournalEntry, entry_type: EntryType, version: int, *, commit: bool = True
) -> JournalEntry:
    """Explicitly change a draft between ``standard`` and ``adjusting``."""
    _require_draft(entry)
    _require_version(entry, version)
    if has_external_provenance(entry):
        raise NotReclassifiable(
            f"Journal entry {entry.id} was created by the system (recurring template, "
            "reversal or external source) and keeps its classification"
        )
    if entry_type not in RECLASSIFIABLE_TYPES or entry.entry_type not in RECLASSIFIABLE_TYPES:
        raise NotReclassifiable(
            f"Journal entry {entry.id} ({entry.entry_type.value}) cannot be reclassified to "
            f"{entry_type.value}; only standard <-> adjusting is allowed"
        )
    entry.entry_type = entry_type
    entry.version = version + 1
    return _finish(db, entry, commit, version)


def delete_draft(
    db: Session, entry: JournalEntry, expected_version: int | None = None, *, commit: bool = True
) -> None:
    _require_draft(entry)
    _require_version(entry, expected_version)
    db.delete(entry)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except StaleDataError:
        db.rollback()
        db.refresh(entry)
        raise VersionConflict(entry, expected_version or entry.version) from None
    except Exception:
        if commit:
            db.rollback()
        raise


def reverse_entry(
    db: Session,
    original: JournalEntry,
    reversal_date: date | None = None,
    description: str | None = None,
    *,
    ctx: PostingContext | None = None,
    commit: bool = True,
) -> JournalEntry:
    """Create and post the reversal of ``original`` dated ``reversal_date``."""
    ctx = (ctx or system_context()).with_source("reversal")
    if original.status != EntryStatus.POSTED:
        raise EntryNotPosted(original.id)
    if original.entry_type in NON_REVERSIBLE_TYPES:
        raise EntryNotReversible(original)
    if original.reversed_by is not None:
        raise EntryAlreadyReversed(original)
    reversal_date = reversal_date or date.today()
    if reversal_date < original.entry_date:
        raise UnprocessableError(
            f"reversal_date {reversal_date} is before the entry date {original.entry_date}"
        )

    default_description = f"Reversal of journal entry {original.id}: {original.description}"
    reversal = JournalEntry(
        entry_date=reversal_date,
        description=(description or default_description)[:500],
        reference=original.reference,
        entry_type=EntryType.REVERSAL,
        reverses=original,
        lines=[
            JournalLine(
                account_id=line.account_id, debit=line.credit, credit=line.debit, memo=line.memo
            )
            for line in original.lines
        ],
    )
    db.add(reversal)
    try:
        db.flush()
        # Same rules as any posting (balance, active accounts, open period).
        return post_entry(db, reversal, ctx=ctx, commit=commit)
    except Exception:
        if commit:
            db.rollback()  # the flushed draft reversal is discarded with the transaction
        raise
