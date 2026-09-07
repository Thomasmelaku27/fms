from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import AccountingPeriod, FiscalYear
from app.schemas.period import CloseCheckRead, PeriodCreate, PeriodRead
from app.services import periods as period_service

router = APIRouter(prefix="/periods", tags=["periods"])


def _load(db: Session, period_id: int) -> AccountingPeriod:
    period = db.get(AccountingPeriod, period_id)
    if period is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Accounting period not found")
    return period


@router.post("", response_model=PeriodRead, status_code=status.HTTP_201_CREATED)
def create_period(payload: PeriodCreate, db: Session = Depends(get_db)) -> AccountingPeriod:
    """Create an open period. 409 if it overlaps an existing one or reuses a name."""
    year = None
    if payload.fiscal_year_id is not None:
        year = db.get(FiscalYear, payload.fiscal_year_id)
        if year is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Fiscal year not found")
    return period_service.create_period(
        db, payload.name, payload.start_date, payload.end_date, fiscal_year=year
    )


@router.get("", response_model=list[PeriodRead])
def list_periods(db: Session = Depends(get_db)) -> list[AccountingPeriod]:
    return list(db.scalars(select(AccountingPeriod).order_by(AccountingPeriod.start_date)))


@router.get("/{period_id}", response_model=PeriodRead)
def get_period(period_id: int, db: Session = Depends(get_db)) -> AccountingPeriod:
    return _load(db, period_id)


@router.get("/{period_id}/close-check", response_model=CloseCheckRead)
def close_check(period_id: int, db: Session = Depends(get_db)) -> CloseCheckRead:
    """Month-end validation without changing anything: what still blocks closing."""
    period = _load(db, period_id)
    problems = period_service.close_problems(db, period)
    return CloseCheckRead(period_id=period.id, can_close=not problems, problems=problems)


@router.post("/{period_id}/close", response_model=PeriodRead)
def close_period(period_id: int, db: Session = Depends(get_db)) -> AccountingPeriod:
    """Close the period after month-end validation. 409 if it cannot be closed or is not open."""
    return period_service.close_period(db, _load(db, period_id))


@router.post("/{period_id}/reopen", response_model=PeriodRead)
def reopen_period(period_id: int, db: Session = Depends(get_db)) -> AccountingPeriod:
    """Reopen a closed period. 409 if already open or locked."""
    return period_service.reopen_period(db, _load(db, period_id))


@router.post("/{period_id}/lock", response_model=PeriodRead)
def lock_period(period_id: int, db: Session = Depends(get_db)) -> AccountingPeriod:
    """Permanently lock a closed period. 409 unless the period is closed."""
    return period_service.lock_period(db, _load(db, period_id))
