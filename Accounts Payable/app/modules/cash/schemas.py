from pydantic import BaseModel,ConfigDict,Field
from decimal import Decimal
class CashAccountIn(BaseModel):
    ledger_account_id: int
    label: str=Field(min_length=1,max_length=100)
class CashAccountOut(CashAccountIn):
    model_config=ConfigDict(from_attributes=True)
    id:int
class CashBalance(CashAccountOut):
    balance: Decimal
