from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import PeriodStatus


class PeriodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    fiscal_year_id: int | None = None


class PeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    start_date: date
    end_date: date
    status: PeriodStatus
    closed_at: datetime | None
    locked_at: datetime | None
    fiscal_year_id: int | None
    created_at: datetime


class CloseCheckRead(BaseModel):
    period_id: int
    can_close: bool
    problems: list[str]
