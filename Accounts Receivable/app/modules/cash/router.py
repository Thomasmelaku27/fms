from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models import Account,AccountType
from app.modules.cash.models import CashAccount
from app.modules.cash.schemas import CashAccountIn,CashAccountOut,CashBalance
from app.modules.cash.services import cash_balance
router=APIRouter(prefix="/cash-accounts",tags=["cash"])
@router.post("",response_model=CashAccountOut,status_code=201)
def link(data:CashAccountIn,db:Session=Depends(get_db)):
    account=db.get(Account,data.ledger_account_id)
    if account is None or account.type!=AccountType.ASSET: raise HTTPException(422,"Active asset ledger account required")
    if not account.is_active: raise HTTPException(422,"Account inactive")
    if db.scalar(select(CashAccount.id).where(CashAccount.ledger_account_id==account.id)):
        raise HTTPException(409,"Already linked")
    x=CashAccount(**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("",response_model=list[CashAccountOut])
def all_cash(db:Session=Depends(get_db)):
    return list(db.scalars(select(CashAccount).order_by(CashAccount.id)))
@router.get("/{cash_id}/balance",response_model=CashBalance)
def balance(cash_id:int,db:Session=Depends(get_db)):
    x=db.get(CashAccount,cash_id)
    if x is None: raise HTTPException(404,"Cash account not found")
    return CashBalance(id=x.id,ledger_account_id=x.ledger_account_id,label=x.label,balance=cash_balance(db,x.ledger_account_id))
