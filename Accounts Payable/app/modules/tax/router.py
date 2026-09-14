from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.modules.tax.models import TaxCode
from app.modules.tax.schemas import TaxCodeIn,TaxCodeOut
router=APIRouter(prefix="/tax-codes",tags=["tax"])
@router.post("",response_model=TaxCodeOut,status_code=201)
def create_code(data:TaxCodeIn,db:Session=Depends(get_db)):
    if db.scalar(select(TaxCode.id).where(TaxCode.code==data.code)):raise HTTPException(409,"Tax code exists")
    x=TaxCode(**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("",response_model=list[TaxCodeOut])
def codes(db:Session=Depends(get_db)):
    return list(db.scalars(select(TaxCode).order_by(TaxCode.code)))
