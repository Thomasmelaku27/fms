from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_posting_context
from app.schemas.processing import (
    BatchRequestIn,
    BatchResultRead,
    JournalRequestIn,
    JournalResultRead,
    OpeningBalancesIn,
)
from app.services import journal_processing as processing
from app.services.actor import PostingContext
from app.services.opening_balances import OpeningBalance, apply_opening_balances, build_request

router = APIRouter(tags=["journal processing"])


def _request(payload: JournalRequestIn) -> processing.JournalRequest:
    return processing.JournalRequest(
        source_type=payload.source_type,
        source_id=payload.source_id,
        operation_id=payload.operation_id,
        entry_date=payload.entry_date,
        description=payload.description,
        reference=payload.reference,
        entry_type=payload.entry_type,
        post=payload.post,
        lines=tuple(
            processing.JournalRequestLine(
                account_id=line.account_id, debit=line.debit, credit=line.credit, memo=line.memo
            )
            for line in payload.lines
        ),
    )


def _result(result: processing.JournalResult) -> JournalResultRead:
    return JournalResultRead(
        entry=result.entry, created=result.created, posted_now=result.posted_now
    )


@router.post("/journal-processing/entries", response_model=JournalResultRead)
def process_entry(
    payload: JournalRequestIn,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> JournalResultRead:
    """Apply one accounting event from another module (idempotent on the source reference).

    200 with ``created`` telling whether this request created the entry or
    found an identical earlier one; 409 if the source reference was already
    used with different accounting data; 422 for posting problems.
    """
    return _result(
        processing.process_journal(db, _request(payload), ctx=ctx.with_source("processing"))
    )


@router.post("/journal-processing/batches", response_model=BatchResultRead)
def process_batch(
    payload: BatchRequestIn,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> BatchResultRead:
    """Apply several events atomically: all entries are created/posted or none."""
    batch = processing.process_batch(
        db, [_request(item) for item in payload.entries], ctx=ctx.with_source("processing")
    )
    return BatchResultRead(results=[_result(r) for r in batch.results])


@router.post(
    "/opening-balances", response_model=JournalResultRead, status_code=status.HTTP_201_CREATED
)
def apply_opening(
    payload: OpeningBalancesIn,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> JournalResultRead:
    """Initialise a ledger from imported balances with one posted ``opening`` entry.

    Repeating the same ``import_id`` with the same balances returns the
    existing entry (201 with ``created=false``); different balances -> 409.
    """
    request = build_request(
        import_id=payload.import_id,
        as_of=payload.as_of,
        balances=[OpeningBalance(b.account_id, b.debit, b.credit) for b in payload.balances],
        balancing_account_id=payload.balancing_account_id,
        description=payload.description,
    )
    return _result(apply_opening_balances(db, request, ctx=ctx))
