from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_posting_context
from app.models import FiscalYear
from app.schemas.fiscal_year import (
    ClosingBalancesRead,
    ClosingRecordRead,
    ClosingVerificationRead,
    FiscalYearCreate,
    FiscalYearRead,
    YearCloseCheckRead,
)
from app.services import fiscal_years as year_service
from app.services.actor import PostingContext

router = APIRouter(prefix="/fiscal-years", tags=["fiscal years"])


def _load(db: Session, year_id: int) -> FiscalYear:
    year = db.get(FiscalYear, year_id)
    if year is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fiscal year not found")
    return year


@router.post("", response_model=FiscalYearRead, status_code=status.HTTP_201_CREATED)
def create_fiscal_year(payload: FiscalYearCreate, db: Session = Depends(get_db)) -> FiscalYear:
    return year_service.create_fiscal_year(db, **payload.model_dump())


@router.get("", response_model=list[FiscalYearRead])
def list_fiscal_years(db: Session = Depends(get_db)) -> list[FiscalYear]:
    return list(db.scalars(select(FiscalYear).order_by(FiscalYear.start_date)))


@router.get("/{year_id}", response_model=FiscalYearRead)
def get_fiscal_year(year_id: int, db: Session = Depends(get_db)) -> FiscalYear:
    return _load(db, year_id)


@router.get("/{year_id}/close-check", response_model=YearCloseCheckRead)
def close_check(year_id: int, db: Session = Depends(get_db)) -> YearCloseCheckRead:
    """What still blocks the year-end close (nothing is changed)."""
    year = _load(db, year_id)
    problems = year_service.close_problems(db, year)
    return YearCloseCheckRead(fiscal_year_id=year.id, can_close=not problems, problems=problems)


@router.get("/{year_id}/closing-balances", response_model=ClosingBalancesRead)
def closing_balances(year_id: int, db: Session = Depends(get_db)):
    """Preview of the closing entry: each revenue/expense balance and the net income."""
    return year_service.closing_balances(db, _load(db, year_id))


@router.post("/{year_id}/close", response_model=FiscalYearRead)
def close_fiscal_year(
    year_id: int,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> FiscalYear:
    """Execute the year-end close (attestation + closing entry + closed year). 409 if blocked."""
    return year_service.close_year(db, _load(db, year_id), ctx=ctx.with_source("year_end_close"))


@router.get("/{year_id}/closing", response_model=ClosingRecordRead)
def closing_record(year_id: int, db: Session = Depends(get_db)):
    """The immutable attestation of the year-end close (404 until the year is closed)."""
    record = year_service.closing_record(db, _load(db, year_id))
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The fiscal year has not been closed")
    return record


@router.get("/{year_id}/closing/verify", response_model=ClosingVerificationRead)
def verify_closing(year_id: int, db: Session = Depends(get_db)):
    """Reproduce the close from posted history and compare it with the attestation."""
    return year_service.verify_closing(db, _load(db, year_id))
