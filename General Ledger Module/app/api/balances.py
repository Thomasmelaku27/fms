from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_posting_context
from app.schemas.balances import VerificationResultRead, VerificationRunRead
from app.services import balances as balance_service
from app.services.actor import PostingContext

router = APIRouter(prefix="/balances", tags=["balances"])


@router.post("/verify", response_model=VerificationResultRead)
def verify(db: Session = Depends(get_db), ctx: PostingContext = Depends(get_posting_context)):
    """Recompute every account/day total from posted lines and compare with the aggregate."""
    return balance_service.verify_daily_totals(db, actor_ref=ctx.actor_ref)


@router.post("/rebuild", response_model=VerificationResultRead)
def rebuild(db: Session = Depends(get_db), ctx: PostingContext = Depends(get_posting_context)):
    """Rebuild the aggregate from posted lines (journals untouched); logs what differed."""
    return balance_service.rebuild_daily_totals(db, actor_ref=ctx.actor_ref)


@router.get("/verification-runs", response_model=list[VerificationRunRead])
def runs(db: Session = Depends(get_db)):
    return balance_service.verification_runs(db)
