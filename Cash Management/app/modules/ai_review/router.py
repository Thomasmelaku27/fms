from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models import Account
from app.modules.ai_review.models import ReviewSuggestion
from app.modules.ai_review.schemas import SuggestionIn,SuggestionOut
router=APIRouter(prefix="/review-suggestions",tags=["ai-review"])
@router.post("",response_model=SuggestionOut,status_code=201)
def create(data:SuggestionIn,db:Session=Depends(get_db)):
    if db.get(Account,data.suggested_account_id) is None:raise HTTPException(422,"Account not found")
    x=ReviewSuggestion(**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("",response_model=list[SuggestionOut])
def list_pending(db:Session=Depends(get_db)):
    return list(db.scalars(select(ReviewSuggestion).order_by(ReviewSuggestion.id)))
