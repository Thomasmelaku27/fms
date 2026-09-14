from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models import Account
from app.modules.budgeting.models import Budget,BudgetLine
from app.modules.budgeting.schemas import BudgetIn,BudgetOut,AllocationIn,AllocationOut
router=APIRouter(prefix="/budgets",tags=["budgeting"])
@router.post("",response_model=BudgetOut,status_code=201)
def create_budget(data:BudgetIn,db:Session=Depends(get_db)):
    if data.end_date < data.start_date: raise HTTPException(422,"End date before start date")
    x=Budget(**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("",response_model=list[BudgetOut])
def budgets(db:Session=Depends(get_db)):
    return list(db.scalars(select(Budget).order_by(Budget.id)))
@router.post("/{budget_id}/lines",response_model=AllocationOut,status_code=201)
def add_line(budget_id:int,data:AllocationIn,db:Session=Depends(get_db)):
    budget=db.get(Budget,budget_id)
    if budget is None: raise HTTPException(404,"Budget not found")
    if budget.status!="draft": raise HTTPException(409,"Budget is not editable")
    if db.get(Account,data.account_id) is None: raise HTTPException(422,"Account not found")
    if db.scalar(select(BudgetLine.id).where(BudgetLine.budget_id==budget_id,BudgetLine.account_id==data.account_id)):
        raise HTTPException(409,"Account already has an allocation")
    x=BudgetLine(budget_id=budget_id,**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("/{budget_id}/lines",response_model=list[AllocationOut])
def lines(budget_id:int,db:Session=Depends(get_db)):
    if db.get(Budget,budget_id) is None: raise HTTPException(404,"Budget not found")
    return list(db.scalars(select(BudgetLine).where(BudgetLine.budget_id==budget_id).order_by(BudgetLine.id)))
