from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import EntryStatus


class JournalLineIn(BaseModel):
    account_id: int
    debit: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    credit: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    memo: str | None = None

    @model_validator(mode="after")
    def not_both_sides(self) -> "JournalLineIn":
        # Mirrors the ck_journal_lines_not_both_sides database constraint. A line with
        # neither side is allowed in a draft ("no amount yet"); completeness is
        # checked when the entry is posted (see app.services.posting).
        if self.debit > 0 and self.credit > 0:
            raise ValueError("a line cannot have both a debit and a credit")
        return self


class JournalEntryCreate(BaseModel):
    entry_date: date
    description: str = Field(min_length=1, max_length=500)
    reference: str | None = None
    lines: list[JournalLineIn] = Field(min_length=1)


class JournalLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    debit: Decimal
    credit: Decimal
    memo: str | None


class JournalEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_date: date
    description: str
    reference: str | None
    status: EntryStatus
    created_at: datetime
    lines: list[JournalLineRead]
