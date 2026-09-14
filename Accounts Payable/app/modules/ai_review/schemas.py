from decimal import Decimal
from pydantic import BaseModel,ConfigDict,Field
class SuggestionIn(BaseModel):
    description: str=Field(min_length=1,max_length=500)
    suggested_account_id: int
    confidence: Decimal=Field(ge=0,le=1,decimal_places=3)
class SuggestionOut(SuggestionIn):
    model_config=ConfigDict(from_attributes=True)
    id:int
    status:str
