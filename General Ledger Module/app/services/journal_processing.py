"""Journal-processing interface for other modules (AP, AR, cash management...).

Other modules never write journal rows themselves; they submit a
``JournalRequest`` and the General Ledger applies its own rules (drafts,
posting checks, period gates, immutability). The contract:

Source reference
    ``source_type`` (e.g. ``"ap_invoice"``), ``source_id`` (the document) and
    ``operation_id`` (which operation on the document: ``"post"``,
    ``"void"``, ...). Together they identify one *accounting event* and are
    unique in ``journal_entries``. They are provenance: stored on the entry,
    never changed by draft edits, immutable once posted.

Content versus workflow state
    The *content* of an event - ``entry_date``, ``entry_type`` and the
    multiset of ``(account_id, debit, credit)`` lines - is immutable and is
    what the **request fingerprint** (SHA-256 over canonical JSON) covers.
    Description, reference and memos are not material; a retry may reword
    them. ``post`` is *workflow state*, not content: it says how far the
    caller wants the event taken, and is deliberately **not** part of the
    fingerprint.

Monetary precision
    Amounts are canonicalised by ``app.money.as_money`` when a
    ``JournalRequestLine`` is constructed - before any idempotency lookup -
    so ``5``, ``"5.0"`` and ``Decimal("5.00")`` are the same amount while a
    sub-cent, negative, float or non-numeric amount is rejected outright.
    Canonical values are what the fingerprint hashes; no information that
    distinguishes two valid requests is discarded.

Idempotency
    * same key, same fingerprint -> the existing entry is returned
      (``created=False``), nothing is written;
    * same key, same fingerprint, existing entry is a draft and the request
      says ``post=True`` -> the draft is posted now (the idempotent
      draft-to-post transition). It is refused (409 ``DraftModified``) if the
      draft's current content no longer matches the fingerprint, i.e. someone
      edited it in the GL in the meantime - the caller must then resubmit
      under a new operation or post it through the GL explicitly;
    * same key, same fingerprint, existing entry posted, request ``post=False``
      -> the posted entry is returned unchanged;
    * same key, different fingerprint -> 409 ``SourceConflict``; nothing is
      written, the original entry is untouched;
    * concurrent identical requests -> the unique key makes one of them lose
      the INSERT; it rolls back to a savepoint, re-reads the winner and
      returns it.

Batches and transaction ownership
    ``process_batch`` applies a list of requests atomically. With
    ``commit=True`` the batch commits itself (and rolls back on failure);
    with ``commit=False`` it works inside the caller's transaction and never
    commits or rolls back the caller's pending work - a lost INSERT race
    only undoes the batch's own savepoint before the retry.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CLIENT_ENTRY_TYPES,
    Account,
    EntryStatus,
    EntryType,
    JournalEntry,
    JournalLine,
)
from app.money import MoneyError, as_money
from app.services import hooks
from app.services.actor import PostingContext, system_context
from app.services.exceptions import ConflictError, UnprocessableError
from app.services.locking import lock_period_for_date
from app.services.posting import EntryNotPostable, _mark_posted, posting_problems

ZERO = Decimal("0.00")


class JournalRequestLine:
    """An immutable line with canonical money amounts (validated on construction)."""

    __slots__ = ("account_id", "debit", "credit", "memo")

    def __init__(self, account_id: int, debit=ZERO, credit=ZERO, memo: str | None = None) -> None:
        try:
            object.__setattr__(self, "debit", as_money(debit, field="debit"))
            object.__setattr__(self, "credit", as_money(credit, field="credit"))
        except MoneyError as exc:
            raise UnprocessableError(str(exc)) from None
        object.__setattr__(self, "account_id", int(account_id))
        object.__setattr__(self, "memo", memo)

    def __setattr__(self, name, value):  # pragma: no cover - immutability guard
        raise AttributeError("JournalRequestLine is immutable")

    def __eq__(self, other) -> bool:
        return isinstance(other, JournalRequestLine) and self.material() == other.material()

    def __hash__(self) -> int:
        return hash(self.material())

    def __repr__(self) -> str:
        return f"JournalRequestLine({self.account_id}, {self.debit}, {self.credit}, {self.memo!r})"

    def material(self) -> tuple[int, str, str]:
        return (self.account_id, f"{self.debit:.2f}", f"{self.credit:.2f}")


@dataclass(frozen=True)
class JournalRequest:
    source_type: str
    source_id: str
    operation_id: str
    entry_date: date
    description: str
    lines: tuple[JournalRequestLine, ...]
    reference: str | None = None
    entry_type: EntryType = EntryType.STANDARD
    post: bool = True  # workflow state, not content

    def fingerprint(self) -> str:
        return content_fingerprint(
            self.entry_date, self.entry_type, [line.material() for line in self.lines]
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source_type, self.source_id, self.operation_id)


def content_fingerprint(entry_date: date, entry_type: EntryType, lines) -> str:
    """SHA-256 over the canonical material content; also used to detect edited drafts."""
    material = {
        "entry_date": entry_date.isoformat(),
        "entry_type": EntryType(entry_type).value,
        "lines": sorted(lines),
    }
    canonical = json.dumps(material, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def entry_content_fingerprint(entry: JournalEntry) -> str:
    return content_fingerprint(
        entry.entry_date,
        entry.entry_type,
        [(ln.account_id, f"{ln.debit:.2f}", f"{ln.credit:.2f}") for ln in entry.lines],
    )


@dataclass
class JournalResult:
    entry: JournalEntry
    created: bool  # False when an identical earlier request was found
    posted_now: bool = False


@dataclass
class BatchResult:
    results: list[JournalResult] = field(default_factory=list)

    @property
    def entries(self) -> list[JournalEntry]:
        return [r.entry for r in self.results]


class SourceConflict(ConflictError):
    def __init__(self, request: JournalRequest, existing: JournalEntry) -> None:
        self.existing = existing
        super().__init__(
            f"Source reference {request.source_type}/{request.source_id}/{request.operation_id} "
            f"was already used by journal entry {existing.id} with different accounting data"
        )


class DraftModified(ConflictError):
    def __init__(self, request: JournalRequest, existing: JournalEntry) -> None:
        self.existing = existing
        super().__init__(
            f"Journal entry {existing.id} (source {request.source_type}/{request.source_id}/"
            f"{request.operation_id}) was edited in the General Ledger since it was created "
            f"(version {existing.version}); post it through the GL or submit a new operation"
        )


def find_existing(db: Session, request: JournalRequest) -> JournalEntry | None:
    return db.scalar(
        select(JournalEntry).where(
            JournalEntry.source_type == request.source_type,
            JournalEntry.source_id == request.source_id,
            JournalEntry.operation_id == request.operation_id,
        )
    )


def _validate(db: Session, request: JournalRequest, allow_system_types: bool) -> None:
    if not allow_system_types and request.entry_type not in CLIENT_ENTRY_TYPES:
        raise UnprocessableError(
            f"entry_type {request.entry_type.value} is reserved for the system"
        )
    if not request.lines:
        raise UnprocessableError("a journal request needs at least one line")
    if not all(isinstance(line, JournalRequestLine) for line in request.lines):
        raise UnprocessableError("lines must be JournalRequestLine instances")
    account_ids = {line.account_id for line in request.lines}
    found = set(db.scalars(select(Account.id).where(Account.id.in_(account_ids))))
    if account_ids - found:
        raise UnprocessableError(f"Unknown account ids: {sorted(account_ids - found)}")


def _post_in_tx(db: Session, entry: JournalEntry, ctx: PostingContext) -> None:
    problems = posting_problems(db, entry, lock=True)
    if problems:
        raise EntryNotPostable(entry.id, problems)
    _mark_posted(db, entry, ctx)
    db.flush()


def _apply(
    db: Session, request: JournalRequest, allow_system_types: bool, ctx: PostingContext
) -> JournalResult:
    """Create (and post) the entry for ``request`` inside the caller's transaction; no commit."""
    fingerprint = request.fingerprint()
    existing = find_existing(db, request)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise SourceConflict(request, existing)
        if request.post and existing.status == EntryStatus.DRAFT:
            if entry_content_fingerprint(existing) != fingerprint:
                raise DraftModified(request, existing)
            _post_in_tx(db, existing, ctx)
            db.refresh(existing)
            return JournalResult(existing, created=False, posted_now=True)
        return JournalResult(existing, created=False)

    _validate(db, request, allow_system_types)
    lock_period_for_date(db, request.entry_date)
    entry = JournalEntry(
        entry_date=request.entry_date,
        description=request.description,
        reference=request.reference,
        entry_type=request.entry_type,
        source_type=request.source_type,
        source_id=request.source_id,
        operation_id=request.operation_id,
        request_fingerprint=fingerprint,
        lines=[
            JournalLine(
                account_id=line.account_id, debit=line.debit, credit=line.credit, memo=line.memo
            )
            for line in request.lines
        ],
    )
    db.add(entry)
    db.flush()
    if request.post:
        _post_in_tx(db, entry, ctx)
    return JournalResult(entry, created=True, posted_now=request.post)


def process_journal(
    db: Session,
    request: JournalRequest,
    *,
    ctx: PostingContext | None = None,
    commit: bool = True,
    allow_system_types: bool = False,
) -> JournalResult:
    """Apply one request with the idempotency contract above."""
    return process_batch(
        db, [request], ctx=ctx, commit=commit, allow_system_types=allow_system_types
    ).results[0]


def process_batch(
    db: Session,
    requests: list[JournalRequest],
    *,
    ctx: PostingContext | None = None,
    commit: bool = True,
    allow_system_types: bool = False,
) -> BatchResult:
    """Apply all requests atomically (see module docstring).

    ``allow_system_types`` is for GL-internal callers (opening balances); API
    callers may only submit ``standard`` or ``adjusting`` entries.
    """
    keys = [r.key for r in requests]
    if len(set(keys)) != len(keys):
        raise UnprocessableError("the batch repeats a source reference")
    ctx = ctx or system_context("processing")
    attempts = 0
    while True:
        attempts += 1
        try:
            with db.begin_nested():  # SAVEPOINT: a lost race only undoes this attempt
                result = BatchResult(
                    [_apply(db, request, allow_system_types, ctx) for request in requests]
                )
            hooks.checkpoint("journal_processing.before_commit")
            if commit:
                db.commit()
            else:
                db.flush()
            for item in result.results:
                db.refresh(item.entry)
            return result
        except IntegrityError as exc:
            # A concurrent request with the same key won the INSERT. Start over:
            # the re-run finds the winner and treats our request as a retry.
            if attempts >= 3 or not _is_source_key_violation(exc):
                if commit:
                    db.rollback()
                raise
            hooks.checkpoint("journal_processing.retry")
        except Exception:
            if commit:
                db.rollback()
            raise


def _is_source_key_violation(exc: IntegrityError) -> bool:
    message = str(exc.orig)
    return "uq_journal_entries_source_operation" in message or (
        "UNIQUE constraint failed" in message and "journal_entries.source_type" in message
    )
