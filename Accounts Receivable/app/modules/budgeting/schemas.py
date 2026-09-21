from datetime import date
from decimal import Decimal
from pydantic import BaseModel,ConfigDict,Field
class BudgetIn(BaseModel):
    name: str=Field(min_length=1,max_length=120)
    start_date: date
    end_date: date
class BudgetOut(BudgetIn):
    model_config=ConfigDict(from_attributes=True)
    id: int
    status: str
class AllocationIn(BaseModel):
    account_id: int
    amount: Decimal=Field(ge=0,decimal_places=2)
class AllocationOut(AllocationIn):
    model_config=ConfigDict(from_attributes=True)
    id: int
    budget_id: int
