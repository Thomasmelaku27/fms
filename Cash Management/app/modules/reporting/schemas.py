from datetime import date
from decimal import Decimal
from pydantic import BaseModel


class TrialBalanceRow(BaseModel):
    code: str
    name: str
    debit: Decimal
    credit: Decimal


class TrialBalance(BaseModel):
    as_of: date | None
    rows: list[TrialBalanceRow]
    total_debit: Decimal
    total_credit: Decimal
