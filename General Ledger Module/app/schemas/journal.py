from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import EntryStatus, EntryType


class JournalLineIn(BaseModel):
    """A line as saved in a draft.

    Only representability is validated here (non-negative, whole cents). A draft
    line may have no amount yet, or both sides filled in while the user decides;
    those are rejected when the entry is posted (see app.services.posting).
    """

    account_id: int
    debit: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    credit: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    memo: str | None = None


class JournalEntryCreate(BaseModel):
    entry_date: date
    description: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=100)
    # Only these kinds can be chosen by a client; "reversal" and "recurring" are
    # assigned by the system when it creates such entries.
    entry_type: Literal[EntryType.STANDARD, EntryType.ADJUSTING] = EntryType.STANDARD
    lines: list[JournalLineIn] = Field(min_length=1)


class JournalEntryUpdate(BaseModel):
    """PUT replaces the accounting details: header and the full set of lines.

    ``version`` must match the entry's current version (optimistic locking): a
    stale value means someone else changed the draft first and yields 409.
    ``entry_type`` is system-controlled provenance: it may be repeated
    unchanged, but a different value is rejected (use the reclassify operation).
    """

    model_config = ConfigDict(extra="forbid")

    entry_date: date
    description: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=100)
    entry_type: EntryType | None = None
    lines: list[JournalLineIn] = Field(min_length=1)
    version: int = Field(ge=1)


class ReclassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_type: Literal[EntryType.STANDARD, EntryType.ADJUSTING]
    version: int = Field(ge=1)


class BatchPostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_ids: list[int] = Field(min_length=1, max_length=1000)


class BatchPostResult(BaseModel):
    posted_now: list[int]
    already_posted: list[int]


class ReversalCreate(BaseModel):
    """Body of POST /journal-entries/{id}/reverse.

    Unknown fields are rejected (422) rather than ignored, so a mistyped date
    field can never silently produce a reversal dated today. ``entry_date`` is
    kept as a deprecated alias of ``reversal_date``; sending both with
    different values is a conflict.
    """

    model_config = ConfigDict(extra="forbid")

    reversal_date: date | None = Field(
        default=None, description="Date of the reversing entry; defaults to today"
    )
    entry_date: date | None = Field(
        default=None, deprecated="use reversal_date", description="Alias of reversal_date"
    )
    description: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def dates_must_agree(self) -> "ReversalCreate":
        if (
            self.reversal_date is not None
            and self.entry_date is not None
            and self.reversal_date != self.entry_date
        ):
            raise ValueError("reversal_date and entry_date conflict; send only reversal_date")
        return self

    @property
    def effective_date(self) -> date | None:
        return self.reversal_date if self.reversal_date is not None else self.entry_date


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
    entry_type: EntryType
    version: int
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    recurring_template_id: int | None
    scheduled_for: date | None
    source_type: str | None
    source_id: str | None
    operation_id: str | None
    posted_at: datetime | None
    posted_by: str | None
    posting_source: str | None
    correlation_id: str | None
    created_at: datetime
    lines: list[JournalLineRead]


class JournalEntryPage(BaseModel):
    items: list[JournalEntryRead]
    limit: int
    next_cursor: str | None
