from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import EntryType
from app.schemas.journal import JournalEntryRead, JournalLineIn


class JournalRequestIn(BaseModel):
    """One accounting event submitted by another module (see services.journal_processing)."""

    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(min_length=1, max_length=50, examples=["ap_invoice"])
    source_id: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1, max_length=100, examples=["post", "void"])
    entry_date: date
    description: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=100)
    entry_type: Literal[EntryType.STANDARD, EntryType.ADJUSTING] = EntryType.STANDARD
    post: bool = Field(default=True, description="Post immediately (True) or leave a draft")
    lines: list[JournalLineIn] = Field(min_length=1)


class JournalResultRead(BaseModel):
    entry: JournalEntryRead
    created: bool
    posted_now: bool


class BatchRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[JournalRequestIn] = Field(min_length=1, max_length=1000)


class BatchResultRead(BaseModel):
    results: list[JournalResultRead]


class OpeningBalanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    debit: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    credit: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)


class OpeningBalancesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_id: str = Field(min_length=1, max_length=100, description="Identifies the import")
    as_of: date
    balances: list[OpeningBalanceIn] = Field(min_length=1)
    balancing_account_id: int | None = Field(
        default=None, description="Absorbs any difference (e.g. opening balance equity)"
    )
    description: str | None = Field(default=None, max_length=500)
