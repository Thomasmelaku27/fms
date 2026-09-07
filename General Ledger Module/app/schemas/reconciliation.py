from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class FindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message: str
    entry_ids: list[int]
    account_ids: list[int]
    amounts: dict[str, Decimal]


class CheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    severity: str
    ok: bool
    findings: list[FindingRead]


class ReconciliationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date_from: date | None
    date_to: date | None
    ok: bool
    error_count: int
    warning_count: int
    checks: list[CheckRead]
