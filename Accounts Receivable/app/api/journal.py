from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db
from app.models import Account, JournalEntry, JournalLine
from app.schemas.journal import JournalEntryCreate, JournalEntryRead
from app.services.posting import post_entry

router = APIRouter(prefix="/journal-entries", tags=["journal"])


@router.post("", response_model=JournalEntryRead, status_code=status.HTTP_201_CREATED)
def create_entry(payload: JournalEntryCreate, db: Session = Depends(get_db)) -> JournalEntry:
    account_ids = {line.account_id for line in payload.lines}
    found = set(db.scalars(select(Account.id).where(Account.id.in_(account_ids))))
    missing = account_ids - found
    if missing:
        raise HTTPException(422, f"Unknown account ids: {sorted(missing)}")

    entry = JournalEntry(
        entry_date=payload.entry_date,
        description=payload.description,
        reference=payload.reference,
        lines=[JournalLine(**line.model_dump()) for line in payload.lines],
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.get("", response_model=list[JournalEntryRead])
def list_entries(db: Session = Depends(get_db)) -> list[JournalEntry]:
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .order_by(JournalEntry.entry_date, JournalEntry.id)
    )
    return list(db.scalars(stmt))


@router.get("/{entry_id}", response_model=JournalEntryRead)
def get_entry(entry_id: int, db: Session = Depends(get_db)) -> JournalEntry:
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journal entry not found")
    return entry


@router.post("/{entry_id}/post", response_model=JournalEntryRead)
def post_journal_entry(entry_id: int, db: Session = Depends(get_db)) -> JournalEntry:
    """Post a draft entry. 409 if already posted, 422 if it violates a posting rule."""
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Journal entry not found")
    return post_entry(db, entry)
