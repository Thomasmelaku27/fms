from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import FiscalYearStatus


class FiscalYearCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    retained_earnings_account_id: int | None = None
    generate_monthly_periods: bool = Field(
        default=False, description="Create one open period per calendar month of the year"
    )


class FiscalYearRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    start_date: date
    end_date: date
    status: FiscalYearStatus
    retained_earnings_account_id: int | None
    closing_entry_id: int | None
    closed_at: datetime | None
    created_at: datetime


class YearCloseCheckRead(BaseModel):
    fiscal_year_id: int
    can_close: bool
    problems: list[str]


class ClosingLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    code: str
    name: str
    type: str
    balance: Decimal
    debit: Decimal
    credit: Decimal


class ClosingBalancesRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fiscal_year_id: int
    retained_earnings_account_id: int | None
    lines: list[ClosingLineRead]
    net_income: Decimal
    total_debit: Decimal
    total_credit: Decimal


class ClosingRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fiscal_year_id: int
    closing_entry_id: int | None
    status: str
    covered_period_ids: str
    net_income: Decimal
    total_debit: Decimal
    total_credit: Decimal
    ledger_hash: str
    reconciliation_ok: bool
    reconciliation_summary: str
    actor_ref: str
    correlation_id: str
    started_at: datetime
    completed_at: datetime | None


class ClosingVerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fiscal_year_id: int
    ok: bool
    problems: list[str]
    recorded_hash: str | None
    recomputed_hash: str
    recorded_net_income: Decimal | None
    recomputed_net_income: Decimal
