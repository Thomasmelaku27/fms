from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import AccountCategory, AccountType, NormalBalance


class AccountCreate(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    type: AccountType
    category: AccountCategory | None = None
    normal_balance: NormalBalance | None = Field(
        default=None, description="Defaults from the type: asset/expense debit, others credit"
    )
    is_summary: bool = Field(default=False, description="Summary accounts cannot be posted to")
    parent_id: int | None = Field(default=None, description="A summary account of the same type")


class AccountUpdate(BaseModel):
    """Partial update; only the given fields change.

    ``type`` and ``is_summary`` can only change while the account has no
    journal lines; ``parent_id`` must keep the hierarchy acyclic and type-consistent.
    """

    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: AccountType | None = None
    category: AccountCategory | None = None
    normal_balance: NormalBalance | None = None
    is_summary: bool | None = None
    parent_id: int | None = None


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    type: AccountType
    category: AccountCategory | None
    normal_balance: NormalBalance
    is_summary: bool
    parent_id: int | None
    is_active: bool
    created_at: datetime


class ReclassifyAccountRequest(BaseModel):
    """Effective-dated move/recategorisation of an account (required for accounts with history)."""

    model_config = ConfigDict(extra="forbid")

    effective_from: date
    reason: str = Field(min_length=1, max_length=500)
    parent_id: int | None = Field(default=None)
    category: AccountCategory | None = Field(default=None)
    change_parent: bool = Field(default=False, description="Apply parent_id (also to clear it)")
    change_category: bool = Field(default=False, description="Apply category (also to clear it)")


class ReclassificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    old_parent_id: int | None
    new_parent_id: int | None
    old_category: str | None
    new_category: str | None
    effective_from: date
    reason: str
    actor_ref: str
    created_at: datetime


class ReclassificationImpactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reclassification_id: int
    effective_from: date
    old_parent_id: int | None
    new_parent_id: int | None
    old_category: str | None
    new_category: str | None
    reason: str
    actor_ref: str
    balance_before_effective_date: Decimal
    activity_since_effective_date: Decimal
