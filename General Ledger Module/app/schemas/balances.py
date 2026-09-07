from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DiscrepancyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    entry_date: str
    ledger_debit_cents: int
    ledger_credit_cents: int
    aggregate_debit_cents: int
    aggregate_credit_cents: int


class VerificationResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    ok: bool
    checked_rows: int
    run_id: int | None
    run_at: datetime | None
    discrepancies: list[DiscrepancyRead]


class VerificationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    ok: bool
    discrepancy_count: int
    details: str
    actor_ref: str
    run_at: datetime
