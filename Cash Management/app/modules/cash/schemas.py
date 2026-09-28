"""Request/response models of the cash management API.

All monetary fields are ``Decimal`` with at most two decimals; nothing is ever
converted through ``float``.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.cash.models import (
    CashFlowActivity,
    PlannedCashFlowStatus,
    ReconciliationStatus,
)


# --- Cash accounts -----------------------------------------------------------------


class CashAccountIn(BaseModel):
    ledger_account_id: int
    label: str = Field(min_length=1, max_length=100)
    minimum_balance: Decimal | None = Field(
        default=None,
        decimal_places=2,
        description="Liquidity alert threshold for the projection (None = not configured).",
    )


class CashAccountOut(CashAccountIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class CashBalance(CashAccountOut):
    balance: Decimal
    as_of: date | None = None


class CashAccountUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    minimum_balance: Decimal | None = Field(default=None, decimal_places=2)
    clear_minimum_balance: bool = False


# --- Bank statements -----------------------------------------------------------------


class BankTransactionIn(BaseModel):
    transaction_date: date
    amount: Decimal = Field(
        decimal_places=2,
        description="Signed: positive = money received into the account, negative = paid out.",
    )
    description: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=100)


class BankStatementImport(BaseModel):
    cash_account_id: int
    statement_reference: str = Field(min_length=1, max_length=100)
    period_start: date
    period_end: date
    opening_balance: Decimal = Field(decimal_places=2)
    closing_balance: Decimal = Field(decimal_places=2)
    transactions: list[BankTransactionIn] = Field(default_factory=list)


class BankTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    statement_id: int
    cash_account_id: int
    line_no: int
    transaction_date: date
    reference: str | None
    description: str
    amount: Decimal
    fingerprint: str
    matched_amount: Decimal
    remaining_amount: Decimal
    status: str  # unmatched | partially_matched | matched


class BankStatementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cash_account_id: int
    statement_reference: str
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    content_hash: str
    imported_at: datetime


class SkippedDuplicate(BaseModel):
    reference: str | None
    transaction_date: date
    amount: Decimal
    description: str
    existing_transaction_id: int


class LateTransaction(BaseModel):
    """An imported line dated inside the period of an already certified session."""

    transaction_id: int
    transaction_date: date
    amount: Decimal
    description: str
    session_id: int
    period_start: date
    period_end: date
    certification_version: int | None


class BankStatementImportResult(BaseModel):
    statement: BankStatementOut
    created: bool
    imported_count: int
    skipped_duplicates: list[SkippedDuplicate]
    transactions: list[BankTransactionOut]
    late_transactions: list[LateTransaction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BankStatementDetail(BankStatementOut):
    transactions: list[BankTransactionOut]


# --- Reconciliation --------------------------------------------------------------------


class ReconciliationSessionCreate(BaseModel):
    cash_account_id: int
    statement_id: int | None = Field(
        default=None, description="Take period and balances from this imported statement."
    )
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Decimal | None = Field(default=None, decimal_places=2)
    closing_balance: Decimal | None = Field(default=None, decimal_places=2)
    notes: str | None = Field(default=None, max_length=500)


class ReconciliationSessionUpdate(BaseModel):
    """Correct the scope of an *open* session (e.g. after the bank reissued a statement)."""

    opening_balance: Decimal | None = Field(default=None, decimal_places=2)
    closing_balance: Decimal | None = Field(default=None, decimal_places=2)
    notes: str | None = Field(default=None, max_length=500)


class ReconciliationSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cash_account_id: int
    statement_id: int | None
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    status: ReconciliationStatus
    notes: str | None
    created_at: datetime
    closed_at: datetime | None
    reopen_count: int
    last_reopen_event_id: int | None = None


class MatchCreate(BaseModel):
    bank_transaction_id: int
    journal_line_id: int
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        decimal_places=2,
        description="Positive magnitude; defaults to the smaller remaining amount of both sides.",
    )


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    bank_transaction_id: int
    journal_line_id: int
    amount: Decimal
    created_at: datetime


class LedgerLineView(BaseModel):
    journal_line_id: int
    journal_entry_id: int
    entry_date: date
    entry_description: str
    entry_reference: str | None
    memo: str | None
    debit: Decimal
    credit: Decimal
    signed_amount: Decimal  # debit - credit, i.e. cash in (+) / cash out (-)
    matched_amount: Decimal
    remaining_amount: Decimal
    status: str


class Discrepancy(BaseModel):
    kind: str
    message: str
    amount: Decimal | None = None
    bank_transaction_id: int | None = None
    journal_line_id: int | None = None


class CertificationOut(BaseModel):
    id: int
    session_id: int
    version: int
    certified_at: datetime
    content_hash: str
    previous_hash: str | None
    is_balanced: bool
    forced: bool
    difference: Decimal
    cleared_balance: Decimal
    ledger_balance: Decimal
    matches_count: int
    notes: str | None
    reopen_event_id: int | None = None
    active: bool
    verified: bool
    verification_problems: list[str]


class CertificationDetail(CertificationOut):
    content: str  # canonical JSON the hash was computed over
    drift: list[Discrepancy]


class HistoryEvent(BaseModel):
    event: str  # certified | reopened
    at: datetime
    certification_id: int
    version: int
    content_hash: str | None
    previous_hash: str | None
    is_balanced: bool | None
    forced: bool | None
    difference: Decimal | None
    notes: str | None
    superseded: bool
    reason: str | None
    event_id: int | None = None
    reopen_number: int | None = None
    state: str  # certified: active | superseded | orphaned; reopened: effective | superseded | orphaned


class IntegrityReport(BaseModel):
    """Invariants of the certification cycle. ``problems`` concern the current state
    (``consistent`` is False); ``anomalies`` are preserved historical irregularities that
    block nothing; orphaned / superseded reopen events are tolerated and only listed."""

    status: ReconciliationStatus
    reopen_count: int
    active_certification_version: int | None
    consistent: bool
    problems: list[str]
    anomalies: list[str]
    orphaned_reopen_event_ids: list[int]
    superseded_reopen_event_ids: list[int]
    orphaned_certification_version: int | None
    certifications: int
    reopen_events: int


class CertificationHistory(BaseModel):
    session_id: int
    events: list[HistoryEvent]
    integrity: IntegrityReport


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ReconciliationSummary(BaseModel):
    """Live review. For a closed session ``certification`` is the frozen, approved
    result and ``drift`` lists what differs from it now; the flat figures are live."""

    session: ReconciliationSessionOut
    # Statement side
    bank_transactions_count: int
    bank_transactions_total: Decimal
    matched_bank_total: Decimal
    unmatched_bank_total: Decimal
    unmatched_bank_count: int
    partially_matched_bank_count: int
    cleared_balance: Decimal  # opening + matched bank movements
    difference: Decimal  # closing - cleared_balance (0 when every bank item is explained)
    # Ledger side (posted entries only)
    ledger_balance_at_period_end: Decimal
    outstanding_ledger_count: int
    outstanding_ledger_total: Decimal
    adjusted_bank_balance: Decimal
    adjusted_ledger_balance: Decimal
    unexplained_difference: Decimal
    is_balanced: bool
    discrepancies: list[Discrepancy]
    certification: CertificationOut | None = None
    drift: list[Discrepancy] = Field(default_factory=list)


class UnmatchedItems(BaseModel):
    session_id: int
    bank_transactions: list[BankTransactionOut]
    ledger_lines: list[LedgerLineView]


class CloseRequest(BaseModel):
    force: bool = Field(
        default=False, description="Close even if the session is not balanced (notes required)."
    )
    notes: str | None = Field(default=None, max_length=500)


class AutoMatchRequest(BaseModel):
    date_tolerance_days: int = Field(default=3, ge=0, le=60)
    dry_run: bool = Field(default=False, description="Only return the suggestions.")


class AutoMatchSuggestion(BaseModel):
    bank_transaction_id: int
    journal_line_id: int
    amount: Decimal
    reason: str
    applied: bool


class AutoMatchResult(BaseModel):
    session_id: int
    dry_run: bool
    suggestions: list[AutoMatchSuggestion]
    ambiguous: list[dict]
    applied_count: int


# --- Classification / planned flows ----------------------------------------------------


class ClassificationIn(BaseModel):
    account_id: int
    activity: CashFlowActivity


class ClassificationOut(ClassificationIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class PlannedCashFlowIn(BaseModel):
    cash_account_id: int
    expected_date: date
    amount: Decimal = Field(decimal_places=2, description="Signed: + inflow, - outflow.")
    description: str = Field(min_length=1, max_length=500)
    activity: CashFlowActivity | None = None
    source: str = Field(default="manual", min_length=1, max_length=50)
    external_reference: str | None = Field(default=None, max_length=100)


class PlannedCashFlowOut(PlannedCashFlowIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: PlannedCashFlowStatus
    journal_entry_id: int | None
    created_at: datetime


class PlannedCashFlowStatusUpdate(BaseModel):
    status: PlannedCashFlowStatus
    journal_entry_id: int | None = None


# --- Reports --------------------------------------------------------------------------


class CashBalanceRow(BaseModel):
    cash_account_id: int
    label: str
    ledger_account_id: int
    ledger_account_code: str
    balance: Decimal
    minimum_balance: Decimal | None


class CashBalancesReport(BaseModel):
    as_of: date | None
    basis: str
    accounts: list[CashBalanceRow]
    consolidated_balance: Decimal


class DailyBalanceRow(BaseModel):
    date: date
    inflows: Decimal
    outflows: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    net: Decimal
    closing_balance: Decimal


class DailyBalancesReport(BaseModel):
    cash_account_id: int | None
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    days: list[DailyBalanceRow]


class ActivityLine(BaseModel):
    account_id: int
    account_code: str
    account_name: str
    inflows: Decimal
    outflows: Decimal
    net: Decimal
    classification_source: str  # explicit | default_by_type


class ActivitySection(BaseModel):
    activity: CashFlowActivity
    inflows: Decimal
    outflows: Decimal
    net: Decimal
    lines: list[ActivityLine]


class TransferEntry(BaseModel):
    journal_entry_id: int
    entry_date: date
    description: str
    amount: Decimal


class AccountFlowRow(BaseModel):
    cash_account_id: int
    label: str
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    closing_balance: Decimal


class CashFlowReport(BaseModel):
    period_start: date
    period_end: date
    basis: str
    opening_balance: Decimal
    closing_balance: Decimal
    total_inflows: Decimal
    total_outflows: Decimal
    net_change: Decimal
    ties_out: bool = True
    activities: list[ActivitySection]
    internal_transfers_total: Decimal
    internal_transfers: list[TransferEntry]
    accounts: list[AccountFlowRow]


# --- Projection -----------------------------------------------------------------------


class ProjectionItem(BaseModel):
    kind: str  # posted_journal_entry | planned_cash_flow
    id: int
    cash_account_id: int | None
    description: str
    amount: Decimal
    overdue: bool = False


class ProjectionDay(BaseModel):
    date: date
    posted_net: Decimal
    planned_inflows: Decimal
    planned_outflows: Decimal
    net: Decimal
    projected_balance: Decimal
    below_threshold: bool
    items: list[ProjectionItem]


class ProjectionAlert(BaseModel):
    date: date
    projected_balance: Decimal
    threshold: Decimal
    shortfall: Decimal
    first_breach: bool = False
    reason: str = ""
    # What moved the balance on that day (same items as the day row) and the
    # largest outflows since the start of the horizon that led to the shortfall.
    items: list[ProjectionItem] = Field(default_factory=list)
    drivers: list[ProjectionItem] = Field(default_factory=list)


class HistoryDay(BaseModel):
    date: date
    net: Decimal
    closing_balance: Decimal


class LiquidityProjection(BaseModel):
    as_of: date
    horizon_start: date
    horizon_end: date
    horizon_days: int
    cash_account_id: int | None
    basis: str
    opening_balance: Decimal
    threshold: Decimal | None
    threshold_source: str | None
    days: list[ProjectionDay]
    closing_projected_balance: Decimal
    lowest_projected_balance: Decimal
    lowest_projected_date: date
    alerts: list[ProjectionAlert]
    history: list[HistoryDay]
    counted: dict[str, int]
    excluded: dict[str, int]
    warnings: list[str]
