"""Request/response models of the Accounts Receivable API.

All monetary values are ``Decimal``; amounts computed by the service (line and
invoice totals, balances) are never accepted from the client.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from app.modules.receivables.models import AdjustmentKind, InvoiceStatus

# Monetary output is always rendered with two decimals ("12.50", "0.00").
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json")]


# --- Customers --------------------------------------------------------------------


class CustomerIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)


class CustomerOut(CustomerIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool


# --- Settings ---------------------------------------------------------------------


class SettingsIn(BaseModel):
    receivable_account_id: int
    tax_account_id: int
    credit_account_id: int
    write_off_account_id: int


class SettingsOut(SettingsIn):
    model_config = ConfigDict(from_attributes=True)

    updated_at: datetime


# --- Invoices ---------------------------------------------------------------------


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit_price: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    revenue_account_id: int
    tax_code_id: int | None = None


class InvoiceIn(BaseModel):
    # ``total`` used to be a request field; it is now computed from the lines.
    # Unknown fields are rejected so a client still sending it is told so
    # instead of having its value silently ignored.
    model_config = ConfigDict(extra="forbid")

    customer_id: int
    invoice_number: str = Field(min_length=1, max_length=80)
    invoice_date: date
    due_date: date
    notes: str | None = Field(default=None, max_length=500)
    lines: list[InvoiceLineIn] = Field(min_length=1)

    @model_validator(mode="after")
    def due_after_invoice_date(self) -> "InvoiceIn":
        if self.due_date < self.invoice_date:
            raise ValueError("due_date precedes invoice_date")
        return self


class InvoiceUpdate(BaseModel):
    """Full replacement of a draft (header and lines)."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str = Field(min_length=1, max_length=80)
    invoice_date: date
    due_date: date
    notes: str | None = Field(default=None, max_length=500)
    lines: list[InvoiceLineIn] = Field(min_length=1)

    @model_validator(mode="after")
    def due_after_invoice_date(self) -> "InvoiceUpdate":
        if self.due_date < self.invoice_date:
            raise ValueError("due_date precedes invoice_date")
        return self


class InvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    revenue_account_id: int
    tax_code_id: int | None
    tax_rate: Decimal | None
    net_amount: Money
    tax_amount: Money
    line_total: Money


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_id: int
    payment_id: int | None
    adjustment_id: int | None
    amount: Money
    allocation_date: date
    effective_date: date
    idempotency_key: str | None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    invoice_number: str
    invoice_date: date
    due_date: date
    status: InvoiceStatus
    subtotal: Money
    tax_total: Money
    total: Money
    amount_settled: Money
    balance_due: Money
    notes: str | None
    journal_entry_id: int | None
    void_journal_entry_id: int | None
    void_date: date | None
    created_at: datetime
    lines: list[InvoiceLineOut]
    allocations: list[AllocationOut]


class VoidIn(BaseModel):
    void_date: date | None = None
    reason: str | None = Field(default=None, max_length=255)


# --- Payments and allocations -------------------------------------------------------


class AllocationIn(BaseModel):
    invoice_id: int
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


def _no_duplicate_invoices(allocations: list[AllocationIn]) -> None:
    seen: set[int] = set()
    for allocation in allocations:
        if allocation.invoice_id in seen:
            raise ValueError(f"invoice {allocation.invoice_id} is allocated more than once")
        seen.add(allocation.invoice_id)


class PaymentIn(BaseModel):
    customer_id: int
    payment_date: date
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    deposit_account_id: int
    reference: str | None = Field(default=None, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=64)
    allocations: list[AllocationIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def allocations_within_amount(self) -> "PaymentIn":
        _no_duplicate_invoices(self.allocations)
        allocated = sum((a.amount for a in self.allocations), Decimal("0"))
        if allocated > self.amount:
            raise ValueError(f"allocations ({allocated}) exceed the payment amount ({self.amount})")
        return self


class AllocationRequest(BaseModel):
    """Allocate (part of) an existing payment or credit to invoices.

    ``effective_date`` is when the allocation enters the books for "as of"
    reporting; it defaults to today so that applying an old payment does not
    change an earlier period, and may not precede the payment/credit or any of
    the invoices. Retrying a request with the same ``idempotency_key`` replays
    the original result; the same key with any other request is a conflict.
    """

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)
    effective_date: date | None = None
    allocations: list[AllocationIn] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_invoices(self) -> "AllocationRequest":
        _no_duplicate_invoices(self.allocations)
        return self


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    payment_date: date
    amount: Money
    amount_allocated: Money
    unallocated_amount: Money
    deposit_account_id: int
    reference: str | None
    idempotency_key: str
    journal_entry_id: int
    created_at: datetime
    allocations: list[AllocationOut]


# --- Adjustments ---------------------------------------------------------------------


class AdjustmentIn(BaseModel):
    customer_id: int
    kind: AdjustmentKind
    adjustment_date: date
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    reason: str = Field(min_length=1, max_length=255)
    invoice_id: int | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def write_off_needs_invoice(self) -> "AdjustmentIn":
        if self.kind == AdjustmentKind.WRITE_OFF and self.invoice_id is None:
            raise ValueError("a write-off must reference the invoice being written off")
        return self


class AdjustmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    kind: AdjustmentKind
    adjustment_date: date
    amount: Money
    amount_allocated: Money
    unallocated_amount: Money
    reason: str
    idempotency_key: str | None
    journal_entry_id: int
    created_at: datetime
    allocations: list[AllocationOut]


# --- Reports -------------------------------------------------------------------------


class CustomerBalance(BaseModel):
    customer_id: int
    code: str
    name: str
    as_of: date
    open_invoices: int
    outstanding: Money
    overdue: Money
    unapplied_credit: Money
    balance: Money


class StatementLine(BaseModel):
    entry_date: date
    kind: str
    reference: str
    description: str
    debit: Money
    credit: Money
    running_balance: Money


class CustomerStatement(BaseModel):
    customer_id: int
    code: str
    name: str
    start_date: date | None
    end_date: date
    opening_balance: Money
    lines: list[StatementLine]
    total_debits: Money
    total_credits: Money
    closing_balance: Money


class AgingRow(BaseModel):
    customer_id: int
    code: str
    name: str
    current: Money
    buckets: list[Money]
    total_outstanding: Money
    unapplied_credit: Money
    balance: Money


class AgingReport(BaseModel):
    as_of: date
    bucket_days: list[int]
    bucket_labels: list[str]
    rows: list[AgingRow]
    totals: AgingRow


class OverdueInvoice(BaseModel):
    invoice_id: int
    invoice_number: str
    customer_id: int
    customer_code: str
    customer_name: str
    invoice_date: date
    due_date: date
    days_overdue: int
    total: Money
    balance_due: Money
    status: InvoiceStatus


class OverdueReport(BaseModel):
    as_of: date
    invoices: list[OverdueInvoice]
    total_overdue: Money
