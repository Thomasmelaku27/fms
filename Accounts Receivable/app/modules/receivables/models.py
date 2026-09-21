"""Accounts Receivable: customers, sales invoices, payments, adjustments.

Money is always ``Decimal`` with two decimal places (``Numeric(18, 2)``).

Invoice lifecycle (``InvoiceStatus``)::

    draft -> issued -> partially_paid -> paid
      |        |
      v        v
    void     void   (only while nothing has been settled against it)

* A *draft* is editable and has no effect on the ledger.
* *Issuing* posts ``Dr Receivables / Cr Revenue (+ Cr Tax payable)`` through the
  shared posting service and freezes the invoice (header amounts and lines are
  also protected by database triggers, see the ``receivables`` migration).
* Settlement happens through ``InvoiceAllocation`` rows that link a payment or
  a credit/write-off adjustment to an invoice. ``SalesInvoice.amount_settled``
  is the running sum of those allocations and is updated with a conditional
  ``UPDATE`` so that concurrent callers can never allocate more than the
  outstanding balance. ``balance_due`` is ``total - amount_settled``.
* *Voiding* an issued invoice posts the reversing entry; posted journal
  history is never modified.
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum

MONEY = Numeric(18, 2)
QUANTITY = Numeric(18, 4)
ZERO = Decimal("0")


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


OPEN_STATUSES = (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)
"""Statuses of invoices that still carry an outstanding balance."""


class AdjustmentKind(str, enum.Enum):
    CREDIT = "credit"
    WRITE_OFF = "write_off"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(default=True)

    invoices: Mapped[list["SalesInvoice"]] = relationship(back_populates="customer")


class ReceivableSettings(Base):
    """Ledger accounts used by the module (a single row, ``id = 1``).

    The tax module defines rates but no ledger accounts, so the account that
    collects output tax is configured here, next to the receivables control
    account and the accounts used for credit adjustments and write-offs.
    """

    __tablename__ = "receivable_settings"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    receivable_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    tax_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    credit_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    write_off_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SalesInvoice(Base):
    __tablename__ = "sales_invoices"
    __table_args__ = (
        UniqueConstraint("customer_id", "invoice_number", name="uq_sales_invoices_customer_invoice"),
        CheckConstraint("total >= 0 AND subtotal >= 0 AND tax_total >= 0", name="non_negative"),
        # SQLite stores NUMERIC as REAL, hence the ROUND (see the ledger rules).
        CheckConstraint("ROUND(subtotal + tax_total, 2) = total", name="total_is_sum"),
        CheckConstraint(
            "amount_settled >= 0 AND amount_settled <= total", name="settled_within_total"
        ),
        Index("ix_sales_invoices_status", "status"),
        Index("ix_sales_invoices_due_date", "due_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    invoice_number: Mapped[str] = mapped_column(String(80))
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, default=ZERO, server_default="0")
    tax_total: Mapped[Decimal] = mapped_column(MONEY, default=ZERO, server_default="0")
    total: Mapped[Decimal] = mapped_column(MONEY, default=ZERO)
    amount_settled: Mapped[Decimal] = mapped_column(MONEY, default=ZERO, server_default="0")
    status: Mapped[InvoiceStatus] = mapped_column(
        str_enum(InvoiceStatus), default=InvoiceStatus.DRAFT
    )
    notes: Mapped[str | None] = mapped_column(String(500))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    void_journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    void_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    customer: Mapped[Customer] = relationship(back_populates="invoices")
    lines: Mapped[list["SalesInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="SalesInvoiceLine.line_no"
    )
    allocations: Mapped[list["InvoiceAllocation"]] = relationship(
        back_populates="invoice", order_by="InvoiceAllocation.id"
    )

    @property
    def balance_due(self) -> Decimal:
        return self.total - self.amount_settled

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class SalesInvoiceLine(Base):
    """One line of an invoice. Amounts are computed by the service, never by the client.

    ``tax_rate`` is a snapshot of the tax code's rate when the line was saved,
    so a later rate change does not silently alter an existing invoice.
    """

    __tablename__ = "sales_invoice_lines"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no", name="uq_sales_invoice_lines_invoice_line_no"),
        CheckConstraint("quantity > 0", name="positive_quantity"),
        CheckConstraint("unit_price >= 0", name="non_negative_price"),
        CheckConstraint(
            "net_amount >= 0 AND tax_amount >= 0 AND line_total >= 0", name="non_negative"
        ),
        CheckConstraint("ROUND(net_amount + tax_amount, 2) = line_total", name="total_is_sum"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("sales_invoices.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column()
    description: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_price: Mapped[Decimal] = mapped_column(QUANTITY)
    revenue_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"))
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    net_amount: Mapped[Decimal] = mapped_column(MONEY)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY)
    line_total: Mapped[Decimal] = mapped_column(MONEY)

    invoice: Mapped[SalesInvoice] = relationship(back_populates="lines")


class CustomerPayment(Base):
    """Money received from a customer (``Dr deposit account / Cr receivables``).

    ``idempotency_key`` is unique: re-sending the same request (including
    concurrently) yields the original payment instead of a second one.
    ``request_hash`` detects a key being reused with a different payload.
    ``amount_allocated`` is the running sum of this payment's allocations; the
    remainder is unapplied credit on the customer's account.
    """

    __tablename__ = "customer_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_amount"),
        CheckConstraint(
            "amount_allocated >= 0 AND amount_allocated <= amount", name="allocated_within_amount"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    payment_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    amount_allocated: Mapped[Decimal] = mapped_column(MONEY, default=ZERO, server_default="0")
    deposit_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    reference: Mapped[str | None] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    customer: Mapped[Customer] = relationship()
    allocations: Mapped[list["InvoiceAllocation"]] = relationship(
        back_populates="payment", order_by="InvoiceAllocation.id"
    )

    @property
    def unallocated_amount(self) -> Decimal:
        return self.amount - self.amount_allocated


class CustomerAdjustment(Base):
    """A credit adjustment (credit note) or a write-off.

    Both reduce what the customer owes (``Cr receivables``); the debit goes to
    the configured credit or write-off account. A write-off always targets one
    invoice and is allocated in full; a credit may target an invoice or stay on
    the customer's account to be allocated later.
    """

    __tablename__ = "customer_adjustments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_amount"),
        CheckConstraint(
            "amount_allocated >= 0 AND amount_allocated <= amount", name="allocated_within_amount"
        ),
        CheckConstraint("kind IN ('credit', 'write_off')", name="kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    kind: Mapped[AdjustmentKind] = mapped_column(str_enum(AdjustmentKind))
    adjustment_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    amount_allocated: Mapped[Decimal] = mapped_column(MONEY, default=ZERO, server_default="0")
    reason: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    journal_entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    customer: Mapped[Customer] = relationship()
    allocations: Mapped[list["InvoiceAllocation"]] = relationship(
        back_populates="adjustment", order_by="InvoiceAllocation.id"
    )

    @property
    def unallocated_amount(self) -> Decimal:
        return self.amount - self.amount_allocated


class InvoiceAllocation(Base):
    """Application of part of a payment *or* an adjustment to one invoice.

    Rows are append-only (trigger-enforced); the database also refuses an
    allocation that would exceed the invoice total or the source amount, or
    that targets a draft/void invoice, as a backstop to the service checks.
    """

    __tablename__ = "invoice_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_amount"),
        CheckConstraint(
            "(payment_id IS NOT NULL AND adjustment_id IS NULL)"
            " OR (payment_id IS NULL AND adjustment_id IS NOT NULL)",
            name="exactly_one_source",
        ),
        # Replaying an allocation request (same key) must not allocate twice.
        UniqueConstraint("idempotency_key", "invoice_id", name="uq_invoice_allocations_key_invoice"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("sales_invoices.id"), index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("customer_payments.id"), index=True)
    adjustment_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_adjustments.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY)
    allocation_date: Mapped[date] = mapped_column(Date)
    # When the allocation entered the books. Reports "as of" a date only count
    # allocations that were already in force then, so applying an old payment
    # today cannot change yesterday's aging or statement figures. It is never
    # earlier than ``allocation_date`` (both documents must exist first).
    effective_date: Mapped[date] = mapped_column(Date)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    invoice: Mapped[SalesInvoice] = relationship(back_populates="allocations")
    payment: Mapped[CustomerPayment | None] = relationship(back_populates="allocations")
    adjustment: Mapped[CustomerAdjustment | None] = relationship(back_populates="allocations")


class AllocationRequest(Base):
    """One recorded allocation request, keyed by its ``idempotency_key``.

    Payments and adjustments deduplicate on their own unique key column; this
    table gives allocation batches - which are several rows at once - the same
    guarantee, and with it the same semantics everywhere:

    * the key is unique, so a concurrent replay loses on the constraint and
      reads the original request instead of applying it twice;
    * ``request_hash`` covers every significant field of the request (source,
      invoices, amounts, explicit effective date), so a retry of the identical
      request replays the original result while *any* other request sent with
      the same key is rejected as a conflict;
    * ``effective_date`` is resolved once, when the request is first accepted,
      so replays cannot move the allocation in time.

    ``fingerprint_origin`` records how trustworthy ``request_hash`` is:

    * ``request`` - computed from the request itself when it was accepted;
    * ``reconstructed`` - a pre-upgrade batch whose payload could be rebuilt
      unambiguously from its (append-only) allocation rows;
    * ``unverifiable`` - a pre-upgrade batch that cannot be rebuilt reliably;
      ``request_hash`` is NULL and every reuse of the key is a conflict.
    """

    __tablename__ = "allocation_requests"
    __table_args__ = (
        CheckConstraint(
            "(payment_id IS NOT NULL AND adjustment_id IS NULL)"
            " OR (payment_id IS NULL AND adjustment_id IS NOT NULL)",
            name="exactly_one_source",
        ),
        CheckConstraint(
            "fingerprint_origin IN ('request', 'reconstructed', 'unverifiable')",
            name="fingerprint_origin",
        ),
        # A hash is present exactly when the request can be verified.
        CheckConstraint(
            "(fingerprint_origin = 'unverifiable' AND request_hash IS NULL)"
            " OR (fingerprint_origin <> 'unverifiable' AND request_hash IS NOT NULL)",
            name="hash_matches_origin",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("customer_payments.id"), index=True)
    adjustment_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_adjustments.id"), index=True
    )
    request_hash: Mapped[str | None] = mapped_column(String(64))
    fingerprint_origin: Mapped[str] = mapped_column(
        String(20), default="request", server_default="request"
    )
    effective_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # The rows of the batch; joined on the key (which this table owns and is
    # unique on) rather than on a foreign key column.
    allocations: Mapped[list[InvoiceAllocation]] = relationship(
        primaryjoin=(
            "foreign(InvoiceAllocation.idempotency_key) == AllocationRequest.idempotency_key"
        ),
        viewonly=True,
        order_by="InvoiceAllocation.id",
    )

    @property
    def source_type(self) -> str:
        return "payment" if self.payment_id is not None else "adjustment"

    @property
    def source_id(self) -> int:
        return self.payment_id if self.payment_id is not None else self.adjustment_id  # type: ignore[return-value]
