from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import Frequency
from app.schemas.journal import JournalLineIn


class RecurringTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=100)
    frequency: Frequency
    start_date: date
    end_date: date | None = None
    lines: list[JournalLineIn] = Field(min_length=1)


class RecurringTemplateUpdate(BaseModel):
    """Partial update of the schedule controls; lines and dates of past drafts are unaffected."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    end_date: date | None = None


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: date = Field(description="Generate drafts for occurrences up to this date (inclusive)")


class GenerateDueRequest(GenerateRequest):
    atomic: bool = Field(
        default=True,
        description="True: all templates commit together or not at all; "
        "False: each template commits separately (partial progress on failure)",
    )


class RecurringTemplateLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    debit: Decimal
    credit: Decimal
    memo: str | None


class RecurringTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    reference: str | None
    frequency: Frequency
    start_date: date
    end_date: date | None
    next_run_date: date
    is_active: bool
    created_at: datetime
    lines: list[RecurringTemplateLineRead]
