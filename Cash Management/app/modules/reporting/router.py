from datetime import date
from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.modules.reporting.services import trial_balance
from app.modules.reporting.schemas import TrialBalance
router=APIRouter(prefix="/reports",tags=["reporting"])
@router.get("/trial-balance", response_model=TrialBalance)
def report(as_of:date|None=None,db:Session=Depends(get_db)):
    return trial_balance(db,as_of)
