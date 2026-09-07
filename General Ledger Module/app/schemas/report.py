from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class TrialBalanceRowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    code: str
    name: str
    type: str
    debit: Decimal
    credit: Decimal
    balance: Decimal
    is_summary: bool
    parent_id: int | None
    normal_balance: str
    normal_balance_amount: Decimal


class TrialBalanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date_from: date | None
    date_to: date | None
    rows: list[TrialBalanceRowRead]
    total_debit: Decimal
    total_credit: Decimal
    is_balanced: bool


class LedgerLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entry_id: int
    line_id: int
    entry_type: str
    correlation_id: str | None
    entry_date: date
    description: str
    reference: str | None
    memo: str | None
    debit: Decimal
    credit: Decimal
    balance: Decimal
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None


class LedgerAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    code: str
    name: str
    type: str
    opening_balance: Decimal
    lines: list[LedgerLineRead]
    total_debit: Decimal
    total_credit: Decimal
    closing_balance: Decimal


class GeneralLedgerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date_from: date | None
    date_to: date | None
    accounts: list[LedgerAccountRead]
    limit: int | None
    next_cursor: str | None


class AccountBalanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    code: str
    name: str
    type: str
    is_summary: bool
    parent_id: int | None
    normal_balance: str
    opening_balance: Decimal
    debit: Decimal
    credit: Decimal
    net_change: Decimal
    closing_balance: Decimal


class AccountBalancesRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date_from: date | None
    date_to: date | None
    source: str
    rows: list[AccountBalanceRead]


class BalanceComparisonRead(BaseModel):
    current: AccountBalancesRead
    comparison: AccountBalancesRead
    differences: dict[str, Decimal]  # code -> closing balance difference (current - comparison)


class HierarchyNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    code: str
    name: str
    type: str
    is_summary: bool
    normal_balance: str
    own_debit: Decimal
    own_credit: Decimal
    rolled_debit: Decimal
    rolled_credit: Decimal
    opening_balance: Decimal
    closing_balance: Decimal
    children: list["HierarchyNodeRead"]


class HierarchyRead(BaseModel):
    date_from: date | None
    date_to: date | None
    source: str
    roots: list[HierarchyNodeRead]
