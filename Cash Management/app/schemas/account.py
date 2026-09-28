from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import AccountType


class AccountCreate(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    type: AccountType


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    type: AccountType
    is_active: bool
    created_at: datetime
