from datetime import date
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field

class VendorIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)

class VendorOut(VendorIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    is_active: bool

class BillIn(BaseModel):
    vendor_id: int
    invoice_number: str = Field(min_length=1, max_length=80)
    invoice_date: date
    due_date: date
    total: Decimal = Field(gt=0, decimal_places=2)

class BillOut(BillIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
