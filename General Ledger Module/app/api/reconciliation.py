from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.reconciliation import ReconciliationRead
from app.services import reconciliation

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get("", response_model=ReconciliationRead)
def run_reconciliation(
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
):
    """Run every GL integrity check; ``ok`` is false if any error-level check has findings."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "'from' must not be after 'to'")
    return reconciliation.run_checks(db, date_from, date_to)
