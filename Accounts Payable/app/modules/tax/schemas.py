from decimal import Decimal
from pydantic import BaseModel,ConfigDict,Field
class TaxCodeIn(BaseModel):
    code: str=Field(min_length=1,max_length=30)
    description: str=Field(min_length=1,max_length=200)
    rate: Decimal=Field(ge=0,le=1,decimal_places=6)
    jurisdiction: str=Field(min_length=1,max_length=100)
class TaxCodeOut(TaxCodeIn):
    model_config=ConfigDict(from_attributes=True)
    id:int
    is_active:bool
