from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, get_posting_context
from app.models import EntryStatus, EntryType, JournalEntry
from app.schemas.journal import (
    BatchPostRequest,
    BatchPostResult,
    JournalEntryCreate,
    JournalEntryPage,
    JournalEntryRead,
    JournalEntryUpdate,
    ReclassifyRequest,
    ReversalCreate,
)
from app.services import journal as journal_service
from app.services.actor import PostingContext
from app.services.posting import post_entries, post_entry

router = APIRouter(prefix="/journal-entries", tags=["journal"])


def _load(db: Session, entry_id: int) -> JournalEntry:
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journal entry not found")
    return entry


@router.post("", response_model=JournalEntryRead, status_code=status.HTTP_201_CREATED)
def create_entry(payload: JournalEntryCreate, db: Session = Depends(get_db)) -> JournalEntry:
    return journal_service.create_draft(db, payload)


def _listing(status_filter, entry_type, recurring_template_id, after: str | None):
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines), selectinload(JournalEntry.reversed_by))
        .order_by(JournalEntry.entry_date, JournalEntry.id)
    )
    if status_filter is not None:
        stmt = stmt.where(JournalEntry.status == status_filter)
    if entry_type is not None:
        stmt = stmt.where(JournalEntry.entry_type == entry_type)
    if recurring_template_id is not None:
        stmt = stmt.where(JournalEntry.recurring_template_id == recurring_template_id)
    if after:
        try:
            day, entry_id = after.split("|")
            cursor = (date.fromisoformat(day), int(entry_id))
        except ValueError:
            raise HTTPException(422, "invalid cursor") from None
        stmt = stmt.where(tuple_(JournalEntry.entry_date, JournalEntry.id) > tuple_(*cursor))
    return stmt


@router.get("", response_model=list[JournalEntryRead])
def list_entries(
    status_filter: EntryStatus | None = Query(default=None, alias="status"),
    entry_type: EntryType | None = Query(default=None),
    recurring_template_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[JournalEntry]:
    """Full listing in deterministic order (entry_date, id); use /page for large histories."""
    return list(db.scalars(_listing(status_filter, entry_type, recurring_template_id, None)))


@router.get("/page", response_model=JournalEntryPage)
def list_entries_page(
    status_filter: EntryStatus | None = Query(default=None, alias="status"),
    entry_type: EntryType | None = Query(default=None),
    recurring_template_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    after: str | None = Query(default=None, description="Cursor from a previous page"),
    db: Session = Depends(get_db),
) -> JournalEntryPage:
    """Keyset-paginated listing ordered by (entry_date, id); stable across inserts."""
    stmt = _listing(status_filter, entry_type, recurring_template_id, after).limit(limit + 1)
    items = list(db.scalars(stmt))
    next_cursor = None
    if len(items) > limit:
        items = items[:limit]
        next_cursor = f"{items[-1].entry_date.isoformat()}|{items[-1].id}"
    return JournalEntryPage(items=items, limit=limit, next_cursor=next_cursor)


@router.post("/batch-post", response_model=BatchPostResult)
def batch_post(
    payload: BatchPostRequest,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> BatchPostResult:
    """Post several drafts atomically: all of them or none.

    200 with ``posted_now`` / ``already_posted``; a batch whose entries are all
    posted already is an idempotent no-op. 409 if the batch overlaps one that
    partially ran (some posted, some drafts); 422 if any draft is invalid.
    """
    posted_now, already = post_entries(db, payload.entry_ids, ctx=ctx.with_source("batch"))
    return BatchPostResult(
        posted_now=[e.id for e in posted_now], already_posted=[e.id for e in already]
    )


@router.get("/{entry_id}", response_model=JournalEntryRead)
def get_entry(entry_id: int, db: Session = Depends(get_db)) -> JournalEntry:
    return _load(db, entry_id)


@router.post("/{entry_id}/reclassify", response_model=JournalEntryRead)
def reclassify_entry(
    entry_id: int, payload: ReclassifyRequest, db: Session = Depends(get_db)
) -> JournalEntry:
    """Switch a draft between standard and adjusting (system-created drafts keep their type)."""
    return journal_service.reclassify_draft(
        db, _load(db, entry_id), payload.entry_type, payload.version
    )


@router.put("/{entry_id}", response_model=JournalEntryRead)
def update_entry(
    entry_id: int, payload: JournalEntryUpdate, db: Session = Depends(get_db)
) -> JournalEntry:
    """Replace a draft's accounting details (header and lines).

    409 if the entry is posted or ``version`` is stale; 422 if the body tries
    to change the entry type (provenance is system-controlled).
    """
    return journal_service.update_draft(db, _load(db, entry_id), payload)


def _if_match_version(if_match: str | None) -> int | None:
    """Optional ``If-Match: "<version>"`` header for optimistic deletes."""
    if if_match is None:
        return None
    value = if_match.strip().strip('"')
    if not value.isdigit():
        raise HTTPException(422, 'If-Match must be the entry version, e.g. If-Match: "3"')
    return int(value)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    entry_id: int,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db),
) -> Response:
    """Delete a draft. 409 if the entry is posted or the If-Match version is stale."""
    journal_service.delete_draft(db, _load(db, entry_id), _if_match_version(if_match))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{entry_id}/post", response_model=JournalEntryRead)
def post_journal_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> JournalEntry:
    """Post a draft entry. 409 if already posted, 422 if it violates a posting rule."""
    return post_entry(db, _load(db, entry_id), ctx=ctx)


@router.post(
    "/{entry_id}/reverse", response_model=JournalEntryRead, status_code=status.HTTP_201_CREATED
)
def reverse_journal_entry(
    entry_id: int,
    payload: ReversalCreate | None = None,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> JournalEntry:
    """Create and post the reversal of a posted entry, dated ``reversal_date``.

    409 if the entry is a draft or was already reversed; 422 if the body is
    invalid (unknown fields, conflicting dates, a date before the original) or
    the reversal itself cannot be posted (closed period, inactive account).
    """
    payload = payload or ReversalCreate()
    return journal_service.reverse_entry(
        db,
        _load(db, entry_id),
        reversal_date=payload.effective_date,
        description=payload.description,
        ctx=ctx,
    )
