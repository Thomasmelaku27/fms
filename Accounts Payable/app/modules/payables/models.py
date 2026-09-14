"""Accounts payable data model.

Tables owned by this module (migrations ``fmspayables01`` .. ``fmspayables04`` on
top of the shared ``fmsmodules01`` baseline):

Master data
* ``vendors``                 supplier master data: default AP control account, terms,
                              status (active / on_hold / suspended / inactive),
                              preferred payment method, discount policy
* ``discount_policies``       configurable early-payment discount terms
                              (e.g. "2/10": 2 % if paid within 10 days)

Documents
* ``supplier_bills``          bill header: totals, status, approval / rejection stamps,
                              discount snapshot, link to the posted journal entry
* ``supplier_bill_lines``     line items (quantity x unit price = line total)
* ``bill_payment_schedules``  installments for approved bills (sum = bill total)
* ``bill_holds``              payment holds and vendor disputes on a bill, with
                              reasons, resolutions and release stamps
* ``duplicate_flags``         explainable near-duplicate suspicions awaiting review
* ``supplier_credits``        supplier credit notes and internal credit adjustments
* ``supplier_credit_lines``   line items of a credit

Settlement (every row is dated and, once written, only ever *closed*, never
deleted or amended -- so balances can be reconstructed as of any date)
* ``vendor_payments``         money paid to a vendor; ``payable_account_id`` holds any
                              unapplied (advance / on-account) remainder
* ``payment_allocations``     application of a payment to a bill: ``original`` rows are
                              written with the payment, ``advance`` rows apply an
                              unapplied remainder later and can be unallocated (dated)
* ``credit_applications``     application of a credit to a bill; unapplication is a
                              dated close, not a delete
* ``bill_discounts``          early-payment discounts taken (Dr payables / Cr discount
                              account), reversed with the payment that earned them

Payment operations
* ``payment_runs``            proposal -> review -> approval -> execution batches
* ``payment_run_items``       one proposed settlement per bill in a run

Period control
* ``ap_period_locks``         AP periods closed by the period-end process; dated
                              settlement rows may not fall on or before the lock

Links to the shared core are plain foreign keys (``accounts.id``,
``journal_entries.id``). The module never writes to the core tables directly;
see ``ledger_adapter.py``. The database enforces the AP rules that protect
recorded money (see the migrations for the trigger DDL): canonical numbers are
computed by the database; financial fields of non-draft documents, recorded
payments (including method, reference and actor) and original allocations are
frozen; applications / allocations / discounts are append-only with a single
dated close; reversals happen once with a complete record; settlement never
exceeds a bill's total or a credit's / payment's available amount; held bills
cannot be paid; nothing dated inside a closed AP period can be written.
"""

import enum
import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.sql.elements import ColumnElement

from app.db import Base, str_enum
from app.models import Account, JournalEntry

ZERO = Decimal("0.00")


# --- enums ---------------------------------------------------------------------------------


class BillStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"


# Statuses in which a bill carries an amount the company owes.
OPEN_BILL_STATUSES = (BillStatus.APPROVED, BillStatus.PARTIALLY_PAID)
SETTLEMENT_BILL_STATUSES = (BillStatus.APPROVED, BillStatus.PARTIALLY_PAID, BillStatus.PAID)


class PaymentMethod(str, enum.Enum):
    BANK_TRANSFER = "bank_transfer"
    CHEQUE = "cheque"
    CASH = "cash"
    CARD = "card"
    OTHER = "other"


class PaymentStatus(str, enum.Enum):
    RECORDED = "recorded"
    REVERSED = "reversed"


class AllocationKind(str, enum.Enum):
    ORIGINAL = "original"  # written together with the payment; immutable
    ADVANCE = "advance"  # applies an unapplied remainder later; can be unallocated once


class CreditKind(str, enum.Enum):
    CREDIT_NOTE = "credit_note"  # issued by the supplier
    ADJUSTMENT = "adjustment"  # internal correction of what is owed


class CreditStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    PARTIALLY_APPLIED = "partially_applied"
    APPLIED = "applied"
    REVERSED = "reversed"


# Statuses in which a credit still has (or may have) value to apply.
OPEN_CREDIT_STATUSES = (CreditStatus.APPROVED, CreditStatus.PARTIALLY_APPLIED)
SETTLEMENT_CREDIT_STATUSES = (
    CreditStatus.APPROVED,
    CreditStatus.PARTIALLY_APPLIED,
    CreditStatus.APPLIED,
)


class VendorStatus(str, enum.Enum):
    ACTIVE = "active"  # documents, approvals and payments allowed
    ON_HOLD = "on_hold"  # documents and approvals allowed, no payments
    SUSPENDED = "suspended"  # no new documents, approvals or payments
    INACTIVE = "inactive"  # same as suspended; the legacy "is_active = false"


DOCUMENT_VENDOR_STATUSES = (VendorStatus.ACTIVE, VendorStatus.ON_HOLD)


class HoldKind(str, enum.Enum):
    HOLD = "hold"  # internal payment hold
    DISPUTE = "dispute"  # disagreement with the supplier


class DuplicateFlagStatus(str, enum.Enum):
    PENDING = "pending"
    CLEARED = "cleared"
    CONFIRMED = "confirmed"


class PaymentRunStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunItemStatus(str, enum.Enum):
    PROPOSED = "proposed"
    EXCLUDED = "excluded"
    PAID = "paid"
    FAILED = "failed"


# --- canonical document numbers -----------------------------------------------------------------

# Whitespace the canonical form collapses: exactly what the database expression does.
_WHITESPACE = re.compile("[ \t\n\x0b\x0c\r]+")


def normalize_invoice_number(value: str) -> str:
    """Canonical form used for duplicate detection: trimmed, single-spaced, upper-case.

    ``" inv-001 "`` and ``"INV-001"`` are the same supplier invoice. The canonical
    value is stored next to the original so a UNIQUE constraint can enforce the
    rule in the database as well as in the API. The database recomputes it with
    :func:`sql_normalize` on every write (triggers), so this Python version is a
    convenience for in-memory objects, not the source of truth.
    """
    return _WHITESPACE.sub(" ", value).strip(" ").upper()


def sql_normalize(expr: ColumnElement | str) -> ColumnElement:
    """The canonical form as a SQL expression, identical on SQLite and PostgreSQL.

    Mirrors the migration's trigger expression: tabs / newlines / vertical tabs /
    form feeds / carriage returns become spaces, runs of spaces collapse to one
    (the classic three-REPLACE trick with two control-character sentinels), then
    TRIM and UPPER. Used by the API-layer duplicate check so that it compares
    exactly what the database stores.
    """
    out = expr
    for char in ("\t", "\n", "\x0b", "\x0c", "\r"):
        out = func.replace(out, char, " ")
    out = func.replace(out, " ", "\x01\x02")
    out = func.replace(out, "\x02\x01", "")
    out = func.replace(out, "\x01\x02", " ")
    return func.upper(func.trim(out))


def compact_invoice_number(value: str) -> str:
    """Looser key for *near*-duplicate detection: alphanumerics only, upper-case.

    ``"INV-001"``, ``"INV 001"`` and ``"inv001"`` share the same compact key; the
    canonical form keeps them distinct, so such pairs are *flagged for review*
    rather than rejected.
    """
    return "".join(ch for ch in value.upper() if ch.isalnum())


# --- master data -------------------------------------------------------------------------------


class DiscountPolicy(Base):
    """Early-payment terms: ``discount_percent`` off if settled within ``discount_days``."""

    __tablename__ = "discount_policies"
    __table_args__ = (
        CheckConstraint("discount_percent > 0 AND discount_percent < 100", name="percent_range"),
        CheckConstraint("discount_days >= 0", name="days_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    description: Mapped[str] = mapped_column(String(200))
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    discount_days: Mapped[int] = mapped_column(Integer)
    # Account credited when a discount is taken (purchase discounts: revenue or contra-expense).
    discount_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    discount_account: Mapped[Account] = relationship()


class Vendor(Base):
    __tablename__ = "vendors"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'on_hold', 'suspended', 'inactive')", name="status_valid"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Legacy flag kept in sync with ``status`` (true for active / on_hold).
    is_active: Mapped[bool] = mapped_column(default=True)
    # Default AP control (liability) account credited when this vendor's bills are approved.
    payable_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    # Used to derive a bill's due date when none is given.
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    status: Mapped[VendorStatus] = mapped_column(
        str_enum(VendorStatus), default=VendorStatus.ACTIVE, server_default="active"
    )
    status_reason: Mapped[str | None] = mapped_column(String(500))
    status_changed_by: Mapped[str | None] = mapped_column(String(100))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preferred_method: Mapped[PaymentMethod | None] = mapped_column(str_enum(PaymentMethod))
    # Opaque bank identifier for the future payment-execution integration (never parsed here).
    bank_reference: Mapped[str | None] = mapped_column(String(100))
    discount_policy_id: Mapped[int | None] = mapped_column(ForeignKey("discount_policies.id"))

    payable_account: Mapped[Account | None] = relationship()
    discount_policy: Mapped[DiscountPolicy | None] = relationship()
    bills: Mapped[list["SupplierBill"]] = relationship(back_populates="vendor")

    @property
    def accepts_documents(self) -> bool:
        return self.status in DOCUMENT_VENDOR_STATUSES

    @property
    def accepts_payments(self) -> bool:
        return self.status == VendorStatus.ACTIVE


# --- bills ------------------------------------------------------------------------------------


class SupplierBill(Base):
    __tablename__ = "supplier_bills"
    __table_args__ = (
        # Exact duplicate (baseline) and canonical duplicate (this module) protection.
        UniqueConstraint("vendor_id", "invoice_number", name="uq_supplier_bills_vendor_invoice"),
        UniqueConstraint(
            "vendor_id",
            "invoice_number_normalized",
            name="uq_supplier_bills_vendor_invoice_normalized",
        ),
        CheckConstraint("total > 0", name="total_positive"),
        CheckConstraint("ROUND(total, 2) = total", name="total_whole_cents"),
        CheckConstraint("due_date >= invoice_date", name="due_date_after_invoice_date"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'partially_paid', 'paid')",
            name="status_valid",
        ),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    invoice_number: Mapped[str] = mapped_column(String(80))
    invoice_number_normalized: Mapped[str] = mapped_column(String(80))
    invoice_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[BillStatus] = mapped_column(
        str_enum(BillStatus), default=BillStatus.DRAFT, index=True
    )
    description: Mapped[str | None] = mapped_column(String(500))
    # AP control account credited on approval; resolved from the vendor default when
    # omitted and required before a bill can be approved.
    payable_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    # Ledger entry posted on approval (through the module's ledger adapter).
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String(100))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    # Early-payment discount snapshot taken from the vendor's policy at approval.
    discount_date: Mapped[date | None] = mapped_column(Date)
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=ZERO, server_default="0"
    )
    discount_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    vendor: Mapped[Vendor] = relationship(back_populates="bills")
    payable_account: Mapped[Account | None] = relationship(foreign_keys=[payable_account_id])
    discount_account: Mapped[Account | None] = relationship(foreign_keys=[discount_account_id])
    journal_entry: Mapped[JournalEntry | None] = relationship()
    lines: Mapped[list["SupplierBillLine"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan", order_by="SupplierBillLine.line_no"
    )
    schedule: Mapped[list["BillPaymentSchedule"]] = relationship(
        back_populates="bill",
        cascade="all, delete-orphan",
        order_by="BillPaymentSchedule.installment_no",
    )
    allocations: Mapped[list["PaymentAllocation"]] = relationship(back_populates="bill")
    credit_applications: Mapped[list["CreditApplication"]] = relationship(
        back_populates="bill", order_by="CreditApplication.id"
    )
    discounts: Mapped[list["BillDiscount"]] = relationship(
        back_populates="bill", order_by="BillDiscount.id"
    )
    holds: Mapped[list["BillHold"]] = relationship(back_populates="bill", order_by="BillHold.id")
    duplicate_flags: Mapped[list["DuplicateFlag"]] = relationship(
        back_populates="bill",
        foreign_keys="DuplicateFlag.bill_id",
        cascade="all, delete-orphan",
        order_by="DuplicateFlag.id",
    )

    @validates("invoice_number")
    def _normalize(self, key: str, value: str) -> str:
        self.invoice_number_normalized = normalize_invoice_number(value)
        return value

    @property
    def lines_total(self) -> Decimal:
        return sum((line.line_total for line in self.lines), ZERO)

    @property
    def amount_paid(self) -> Decimal:
        """Cash applied by allocations that are in force (payment recorded, not unallocated)."""
        return sum((a.amount for a in self.allocations if a.is_active), ZERO)

    @property
    def credits_applied(self) -> Decimal:
        return sum((a.amount for a in self.credit_applications if a.is_active), ZERO)

    @property
    def discounts_taken(self) -> Decimal:
        return sum((d.amount for d in self.discounts if d.is_active), ZERO)

    @property
    def amount_settled(self) -> Decimal:
        return self.amount_paid + self.credits_applied + self.discounts_taken

    @property
    def outstanding(self) -> Decimal:
        return self.total - self.amount_settled

    @property
    def active_holds(self) -> list["BillHold"]:
        return [hold for hold in self.holds if hold.released_at is None]

    @property
    def on_hold(self) -> bool:
        return bool(self.active_holds)

    @property
    def pending_duplicate_flags(self) -> list["DuplicateFlag"]:
        return [f for f in self.duplicate_flags if f.status == DuplicateFlagStatus.PENDING]

    def settled_as_of(self, as_of: date) -> tuple[Decimal, Decimal, Decimal]:
        """(cash, credits, discounts) in force on ``as_of``."""
        paid = sum((a.amount for a in self.allocations if a.in_force_on(as_of)), ZERO)
        credited = sum((a.amount for a in self.credit_applications if a.in_force_on(as_of)), ZERO)
        discounted = sum((d.amount for d in self.discounts if d.in_force_on(as_of)), ZERO)
        return paid, credited, discounted

    def outstanding_as_of(self, as_of: date) -> Decimal:
        return self.total - sum(self.settled_as_of(as_of), ZERO)

    def discount_available_on(self, pay_date: date) -> Decimal:
        """Discount still claimable when the bill is settled on ``pay_date``."""
        if self.discount_date is None or pay_date > self.discount_date:
            return ZERO
        return self.discount_amount - self.discounts_taken


class SupplierBillLine(Base):
    __tablename__ = "supplier_bill_lines"
    __table_args__ = (
        UniqueConstraint("bill_id", "line_no", name="uq_supplier_bill_lines_bill_line_no"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("line_total > 0", name="line_total_positive"),
        CheckConstraint("ROUND(line_total, 2) = line_total", name="line_total_whole_cents"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(255))
    # Expense / asset account debited when the bill is approved.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    bill: Mapped[SupplierBill] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()


class BillPaymentSchedule(Base):
    """One planned installment of an approved bill; installments sum to the bill total."""

    __tablename__ = "bill_payment_schedules"
    __table_args__ = (
        UniqueConstraint(
            "bill_id", "installment_no", name="uq_bill_payment_schedules_bill_installment"
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="amount_whole_cents"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id", ondelete="CASCADE"))
    installment_no: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    bill: Mapped[SupplierBill] = relationship(back_populates="schedule")


class BillHold(Base):
    """A payment hold or a vendor dispute on a bill. Released, never deleted."""

    __tablename__ = "bill_holds"
    __table_args__ = (CheckConstraint("kind IN ('hold', 'dispute')", name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id"), index=True)
    kind: Mapped[HoldKind] = mapped_column(str_enum(HoldKind))
    reason: Mapped[str] = mapped_column(String(500))
    placed_by: Mapped[str] = mapped_column(String(100))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_by: Mapped[str | None] = mapped_column(String(100))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(500))

    bill: Mapped[SupplierBill] = relationship(back_populates="holds")

    @property
    def is_active(self) -> bool:
        return self.released_at is None


class DuplicateFlag(Base):
    """A bill that *might* duplicate another one; explained, reviewed, never auto-rejected."""

    __tablename__ = "duplicate_flags"
    __table_args__ = (
        UniqueConstraint("bill_id", "suspected_bill_id", name="uq_duplicate_flags_pair"),
        CheckConstraint("status IN ('pending', 'cleared', 'confirmed')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_bills.id", ondelete="CASCADE"), index=True
    )
    suspected_bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id"))
    # Human-readable reasons, one per line ("same total and vendor within 7 days", ...).
    reasons: Mapped[str] = mapped_column(String(1000))
    score: Mapped[int] = mapped_column(Integer)  # 0..100, sum of rule weights
    # High-confidence suspicions block approval until reviewed; advisory ones only inform.
    blocks_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    status: Mapped[DuplicateFlagStatus] = mapped_column(
        str_enum(DuplicateFlagStatus), default=DuplicateFlagStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String(500))

    bill: Mapped[SupplierBill] = relationship(
        back_populates="duplicate_flags", foreign_keys=[bill_id]
    )
    suspected_bill: Mapped[SupplierBill] = relationship(foreign_keys=[suspected_bill_id])


# --- payments --------------------------------------------------------------------------------


class VendorPayment(Base):
    __tablename__ = "vendor_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="amount_whole_cents"),
        CheckConstraint("status IN ('recorded', 'reversed')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod))
    reference: Mapped[str | None] = mapped_column(String(100))
    # Asset (cash / bank) ledger account credited. The cash module's
    # ``cash_accounts.ledger_account_id`` points at the same ``accounts`` rows.
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    # Client-supplied key making "record payment" safe to retry.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    recorded_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Reversal: set exactly once, together with the reversing journal entry.
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus),
        default=PaymentStatus.RECORDED,
        server_default="recorded",
        index=True,
    )
    reversed_by: Mapped[str | None] = mapped_column(String(100))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_date: Mapped[date | None] = mapped_column(Date)
    reversal_reason: Mapped[str | None] = mapped_column(String(500))
    reversal_journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    # Set on the replacement payment created by a correction.
    corrects_payment_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_payments.id"))
    # AP control account that carries the unapplied (advance) remainder of the payment.
    # Debited for the remainder when the payment is posted; later advance allocations
    # must target bills posting to the same account (no ledger effect).
    payable_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))

    vendor: Mapped[Vendor] = relationship()
    cash_account: Mapped[Account] = relationship(foreign_keys=[cash_account_id])
    payable_account: Mapped[Account | None] = relationship(foreign_keys=[payable_account_id])
    journal_entry: Mapped[JournalEntry | None] = relationship(foreign_keys=[journal_entry_id])
    reversal_journal_entry: Mapped[JournalEntry | None] = relationship(
        foreign_keys=[reversal_journal_entry_id]
    )
    corrects: Mapped["VendorPayment | None"] = relationship(remote_side="VendorPayment.id")
    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", order_by="PaymentAllocation.id"
    )
    discounts: Mapped[list["BillDiscount"]] = relationship(
        back_populates="payment", order_by="BillDiscount.id"
    )

    @property
    def original_total(self) -> Decimal:
        return sum((a.amount for a in self.allocations if a.kind == AllocationKind.ORIGINAL), ZERO)

    @property
    def allocated_total(self) -> Decimal:
        """Amount currently applied to bills (original + advance allocations not unallocated)."""
        return sum((a.amount for a in self.allocations if a.unallocation_date is None), ZERO)

    @property
    def unapplied_amount(self) -> Decimal:
        """Money paid to the vendor that is not (or no longer) applied to any bill."""
        if self.status != PaymentStatus.RECORDED:
            return ZERO
        return self.amount - self.allocated_total

    def in_force_on(self, as_of: date) -> bool:
        """Does this payment count towards balances as of ``as_of``?"""
        if self.payment_date > as_of:
            return False
        return self.status == PaymentStatus.RECORDED or self.reversal_date > as_of

    def unapplied_as_of(self, as_of: date) -> Decimal:
        if not self.in_force_on(as_of):
            return ZERO
        applied = sum((a.amount for a in self.allocations if a.in_force_on(as_of)), ZERO)
        return self.amount - applied


class PaymentAllocation(Base):
    """Application of (part of) a payment to a bill.

    ``original`` rows are written with the payment and never change. ``advance``
    rows apply an unapplied remainder later; they can be *unallocated* exactly
    once by stamping the unallocation date, actor and reason -- the row itself
    stays, so any historical report still sees the allocation while it was in
    force.
    """

    __tablename__ = "payment_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="amount_whole_cents"),
        CheckConstraint("kind IN ('original', 'advance')", name="kind_valid"),
        CheckConstraint(
            "unallocation_date IS NULL OR unallocation_date >= allocation_date",
            name="unallocation_after_allocation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("vendor_payments.id", ondelete="CASCADE"), index=True
    )
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    kind: Mapped[AllocationKind] = mapped_column(
        str_enum(AllocationKind), default=AllocationKind.ORIGINAL, server_default="original"
    )
    allocation_date: Mapped[date] = mapped_column(Date)
    allocated_by: Mapped[str] = mapped_column(String(100))
    unallocation_date: Mapped[date | None] = mapped_column(Date)
    unallocated_by: Mapped[str | None] = mapped_column(String(100))
    unallocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unallocation_reason: Mapped[str | None] = mapped_column(String(500))

    payment: Mapped[VendorPayment] = relationship(back_populates="allocations")
    bill: Mapped[SupplierBill] = relationship(back_populates="allocations")

    @property
    def is_active(self) -> bool:
        return self.unallocation_date is None and self.payment.status == PaymentStatus.RECORDED

    def in_force_on(self, as_of: date) -> bool:
        if not self.payment.in_force_on(as_of) or self.allocation_date > as_of:
            return False
        return self.unallocation_date is None or self.unallocation_date > as_of


class BillDiscount(Base):
    """An early-payment discount taken on a bill (Dr payables / Cr discount account).

    Earned by the payment that settled the bill within the discount window and
    reversed together with that payment.
    """

    __tablename__ = "bill_discounts"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="amount_whole_cents"),
        CheckConstraint(
            "reversal_date IS NULL OR reversal_date >= taken_on", name="reversal_after_taken"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id"), index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_payments.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    taken_on: Mapped[date] = mapped_column(Date)
    taken_by: Mapped[str] = mapped_column(String(100))
    discount_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    reversal_date: Mapped[date | None] = mapped_column(Date)
    reversed_by: Mapped[str | None] = mapped_column(String(100))
    reversal_journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))

    bill: Mapped[SupplierBill] = relationship(back_populates="discounts")
    payment: Mapped[VendorPayment | None] = relationship(back_populates="discounts")
    journal_entry: Mapped[JournalEntry | None] = relationship(foreign_keys=[journal_entry_id])
    reversal_journal_entry: Mapped[JournalEntry | None] = relationship(
        foreign_keys=[reversal_journal_entry_id]
    )

    @property
    def is_active(self) -> bool:
        return self.reversal_date is None

    def in_force_on(self, as_of: date) -> bool:
        if self.taken_on > as_of:
            return False
        return self.reversal_date is None or self.reversal_date > as_of


# --- credits ---------------------------------------------------------------------------------


class SupplierCredit(Base):
    """A supplier credit note or an internal credit adjustment.

    Approval posts ``Dr payable account / Cr line accounts`` (the mirror image of
    a bill). Applying the credit to bills is an AP-internal settlement with no
    ledger effect. Reversal (only while unapplied) posts the mirror entry again.
    """

    __tablename__ = "supplier_credits"
    __table_args__ = (
        UniqueConstraint("vendor_id", "credit_number", name="uq_supplier_credits_vendor_number"),
        UniqueConstraint(
            "vendor_id",
            "credit_number_normalized",
            name="uq_supplier_credits_vendor_number_normalized",
        ),
        CheckConstraint("total > 0", name="total_positive"),
        CheckConstraint("ROUND(total, 2) = total", name="total_whole_cents"),
        CheckConstraint("kind IN ('credit_note', 'adjustment')", name="kind_valid"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'partially_applied', 'applied', "
            "'reversed')",
            name="status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    kind: Mapped[CreditKind] = mapped_column(str_enum(CreditKind))
    credit_number: Mapped[str] = mapped_column(String(80))
    credit_number_normalized: Mapped[str] = mapped_column(String(80))
    credit_date: Mapped[date] = mapped_column(Date, index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[CreditStatus] = mapped_column(
        str_enum(CreditStatus), default=CreditStatus.DRAFT, index=True
    )
    description: Mapped[str | None] = mapped_column(String(500))
    # Informational link to the bill the supplier's credit note refers to.
    related_bill_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_bills.id"))
    payable_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String(100))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    reversed_by: Mapped[str | None] = mapped_column(String(100))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_date: Mapped[date | None] = mapped_column(Date)
    reversal_reason: Mapped[str | None] = mapped_column(String(500))
    reversal_journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    vendor: Mapped[Vendor] = relationship()
    related_bill: Mapped[SupplierBill | None] = relationship()
    payable_account: Mapped[Account | None] = relationship()
    journal_entry: Mapped[JournalEntry | None] = relationship(foreign_keys=[journal_entry_id])
    reversal_journal_entry: Mapped[JournalEntry | None] = relationship(
        foreign_keys=[reversal_journal_entry_id]
    )
    lines: Mapped[list["SupplierCreditLine"]] = relationship(
        back_populates="credit", cascade="all, delete-orphan", order_by="SupplierCreditLine.line_no"
    )
    applications: Mapped[list["CreditApplication"]] = relationship(
        back_populates="credit", order_by="CreditApplication.id"
    )

    @validates("credit_number")
    def _normalize(self, key: str, value: str) -> str:
        self.credit_number_normalized = normalize_invoice_number(value)
        return value

    @property
    def lines_total(self) -> Decimal:
        return sum((line.line_total for line in self.lines), ZERO)

    @property
    def amount_applied(self) -> Decimal:
        return sum((a.amount for a in self.applications if a.is_active), ZERO)

    @property
    def remaining(self) -> Decimal:
        return self.total - self.amount_applied

    def in_force_on(self, as_of: date) -> bool:
        if self.credit_date > as_of:
            return False
        return self.status != CreditStatus.REVERSED or self.reversal_date > as_of

    def remaining_as_of(self, as_of: date) -> Decimal:
        if not self.in_force_on(as_of):
            return ZERO
        applied = sum((a.amount for a in self.applications if a.in_force_on(as_of)), ZERO)
        return self.total - applied


class SupplierCreditLine(Base):
    __tablename__ = "supplier_credit_lines"
    __table_args__ = (
        UniqueConstraint("credit_id", "line_no", name="uq_supplier_credit_lines_credit_line_no"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("line_total > 0", name="line_total_positive"),
        CheckConstraint("ROUND(line_total, 2) = line_total", name="line_total_whole_cents"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    credit_id: Mapped[int] = mapped_column(ForeignKey("supplier_credits.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(255))
    # Expense / asset account credited when the credit is approved.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    credit: Mapped[SupplierCredit] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()


class CreditApplication(Base):
    """Settlement of (part of) a bill with (part of) a credit. No ledger effect.

    Unapplying stamps ``unapplication_date`` / ``unapplied_by`` / ``unapplied_at``
    (plus a reason) instead of deleting the row, so the application is still
    visible -- and still counted -- in reports as of any date while it was in force.
    """

    __tablename__ = "credit_applications"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="amount_whole_cents"),
        CheckConstraint(
            "unapplication_date IS NULL OR unapplication_date >= application_date",
            name="unapplication_after_application",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    credit_id: Mapped[int] = mapped_column(ForeignKey("supplier_credits.id"), index=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    application_date: Mapped[date] = mapped_column(Date)
    applied_by: Mapped[str] = mapped_column(String(100))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    unapplication_date: Mapped[date | None] = mapped_column(Date)
    unapplied_by: Mapped[str | None] = mapped_column(String(100))
    unapplied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unapplication_reason: Mapped[str | None] = mapped_column(String(500))

    credit: Mapped[SupplierCredit] = relationship(back_populates="applications")
    bill: Mapped[SupplierBill] = relationship(back_populates="credit_applications")

    @property
    def is_active(self) -> bool:
        return self.unapplication_date is None

    def in_force_on(self, as_of: date) -> bool:
        if self.application_date > as_of:
            return False
        return self.unapplication_date is None or self.unapplication_date > as_of


# --- payment operations -------------------------------------------------------------------


class PaymentRun(Base):
    """A batch of proposed vendor payments moving through review, approval and execution.

    Nothing touches the ledger until execution; execution records one vendor
    payment per vendor through the ordinary payment service and marks each item.
    """

    __tablename__ = "payment_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'reviewed', 'approved', 'executing', 'completed', 'failed', "
            "'cancelled')",
            name="status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[PaymentRunStatus] = mapped_column(
        str_enum(PaymentRunStatus), default=PaymentRunStatus.DRAFT, index=True
    )
    # Selection criteria (snapshotted so the proposal is explainable later).
    pay_date: Mapped[date] = mapped_column(Date)
    due_through: Mapped[date] = mapped_column(Date)
    vendor_filter: Mapped[str | None] = mapped_column(String(500))  # comma-separated vendor ids
    take_discounts: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    use_credits: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    use_advances: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_by: Mapped[str | None] = mapped_column(String(100))
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cancelled_by: Mapped[str | None] = mapped_column(String(100))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    # Optimistic versioning: every transition is a compare-and-swap on (status, version).
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Fingerprint of the material proposal content at review / approval time.
    proposal_fingerprint: Mapped[str | None] = mapped_column(String(64))
    approved_fingerprint: Mapped[str | None] = mapped_column(String(64))
    approval_invalidated_reason: Mapped[str | None] = mapped_column(String(1000))
    # Finalisation (fmspayables06): how the run ended, per the documented completion policy.
    finalization_status: Mapped[str | None] = mapped_column(String(30))
    settlement_summary_json: Mapped[str | None] = mapped_column(String(4000))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cash_account: Mapped[Account] = relationship()
    items: Mapped[list["PaymentRunItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="PaymentRunItem.id"
    )

    @property
    def total_pay_amount(self) -> Decimal:
        return sum((i.pay_amount for i in self.items if i.status != RunItemStatus.EXCLUDED), ZERO)


class PaymentRunItem(Base):
    """One bill (or installment) proposed for settlement in a run."""

    __tablename__ = "payment_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "bill_id", name="uq_payment_run_items_run_bill"),
        CheckConstraint(
            "status IN ('proposed', 'excluded', 'paid', 'failed')", name="status_valid"
        ),
        CheckConstraint("pay_amount >= 0", name="pay_amount_non_negative"),
        CheckConstraint("discount_amount >= 0", name="discount_non_negative"),
        CheckConstraint("credit_amount >= 0", name="credit_non_negative"),
        CheckConstraint("advance_amount >= 0", name="advance_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("payment_runs.id", ondelete="CASCADE"), index=True
    )
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("supplier_bills.id"), index=True)
    installment_no: Mapped[int | None] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    outstanding_snapshot: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    # Settlement plan for the bill: total settled = pay + discount + credit + advance.
    settle_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=ZERO)
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=ZERO)
    advance_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=ZERO)
    pay_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[RunItemStatus] = mapped_column(
        str_enum(RunItemStatus), default=RunItemStatus.PROPOSED
    )
    explanation: Mapped[str] = mapped_column(String(1000))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_payments.id"))
    execution_reference: Mapped[str | None] = mapped_column(String(100))
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    excluded_by: Mapped[str | None] = mapped_column(String(100))
    exclusion_reason: Mapped[str | None] = mapped_column(String(500))
    # Payment instruction snapshot (vendor bank reference at proposal time).
    destination_reference: Mapped[str | None] = mapped_column(String(100))
    # Actual settlement (fmspayables06): what was recorded, as opposed to the approved plan.
    settlement_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    actual_pay_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    actual_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    actual_credit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    actual_advance_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    settlement_note: Mapped[str | None] = mapped_column(String(500))

    run: Mapped[PaymentRun] = relationship(back_populates="items")
    vendor: Mapped[Vendor] = relationship()
    bill: Mapped[SupplierBill] = relationship()
    payment: Mapped[VendorPayment | None] = relationship()


# --- period control ---------------------------------------------------------------------------


class APPeriodLock(Base):
    """An AP period closed by the period-end process.

    Settlement rows (payments, allocations, applications, discounts, reversals)
    and approvals dated on or before ``closed_through`` are refused by the
    application and by the database. The GL's own accounting periods are a
    separate module; see ``integration.PeriodPort``.
    """

    __tablename__ = "ap_period_locks"

    id: Mapped[int] = mapped_column(primary_key=True)
    closed_through: Mapped[date] = mapped_column(Date, unique=True)
    closed_by: Mapped[str] = mapped_column(String(100))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str | None] = mapped_column(String(500))
    validation_summary: Mapped[str | None] = mapped_column(String(4000))


# --- durable payment execution ---------------------------------------------------------------------


class OperationState(str, enum.Enum):
    PREPARED = "prepared"  # intent recorded, nothing sent
    SUBMITTED = "submitted"  # instruction handed to the gateway (outcome may be unknown)
    CONFIRMATION_PENDING = "confirmation_pending"  # gateway has it, final outcome not yet known
    CONFIRMED = "confirmed"  # money moved; AP not yet recorded
    RECORDING = "recording"  # AP payment / allocations / ledger being written
    COMPLETED = "completed"  # everything recorded and posted
    FAILED = "failed"  # definitively not paid (rejected / abandoned before money moved)
    RECONCILIATION_REQUIRED = "reconciliation_required"  # money moved, recording needs attention


TERMINAL_OPERATION_STATES = (OperationState.COMPLETED, OperationState.FAILED)
MONEY_MOVED_STATES = (
    OperationState.CONFIRMED,
    OperationState.RECORDING,
    OperationState.COMPLETED,
    OperationState.RECONCILIATION_REQUIRED,
)


class PaymentOperation(Base):
    """A durable payment intent and its execution state.

    One row per (payment run, vendor) instruction. The row outlives every
    process: it is committed *before* the gateway is called, updated with a
    compare-and-swap on ``(state, version)`` after each step, and never deleted.
    A database rollback can never undo a transfer, so once the state says the
    gateway confirmed, recovery only ever completes the recording -- it never
    submits again.
    """

    __tablename__ = "payment_operations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="amount_whole_cents"),
        CheckConstraint(
            "state IN ('prepared', 'submitted', 'confirmation_pending', 'confirmed', 'recording', "
            "'completed', 'failed', 'reconciliation_required')",
            name="state_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_uid: Mapped[str] = mapped_column(String(36), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("payment_runs.id"), index=True)
    run_version: Mapped[int | None] = mapped_column(Integer)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    destination_reference: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="XXX", server_default="XXX")
    payment_date: Mapped[date] = mapped_column(Date)
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod))
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    # Canonical JSON of the settlement plan: [{bill_id, settle, discount, credit, advance, pay}].
    plan_json: Mapped[str] = mapped_column(String(8000))
    state: Mapped[OperationState] = mapped_column(
        str_enum(OperationState, length=30), default=OperationState.PREPARED, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    last_error_category: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    gateway_submission_ref: Mapped[str | None] = mapped_column(String(100))
    # One gateway confirmation and one AP payment belong to at most one operation (fmspayables06).
    gateway_confirmation_ref: Mapped[str | None] = mapped_column(String(100), unique=True)
    gateway_status: Mapped[str | None] = mapped_column(String(20))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_payments.id"), unique=True)
    recording_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    ledger_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    allocation_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    recovery_status: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    recovery_note: Mapped[str | None] = mapped_column(String(1000))
    # fmspayables06 -- accounting status is explicit and separate from the execution state:
    # pending (nothing to record yet) | unrecorded (money moved, books untouched) |
    # recorded (automatic) | corrected (authorised correction, verified) |
    # unverified (historical completion without evidence) | inconsistent (verifier findings).
    accounting_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending"
    )
    completion_kind: Mapped[str | None] = mapped_column(String(20))
    completion_evidence_json: Mapped[str | None] = mapped_column(String(4000))
    completion_idempotency_key: Mapped[str | None] = mapped_column(String(120))
    # Investigation is an operator workflow; it never changes the financial state.
    investigation_status: Mapped[str] = mapped_column(
        String(20), default="none", server_default="none"
    )
    investigation_note: Mapped[str | None] = mapped_column(String(1000))
    investigation_closed_by: Mapped[str | None] = mapped_column(String(100))
    investigation_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(100))
    correlation_id: Mapped[str] = mapped_column(String(64))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["PaymentRun | None"] = relationship()
    vendor: Mapped[Vendor] = relationship()
    payment: Mapped[VendorPayment | None] = relationship()
    events: Mapped[list["PaymentOperationEvent"]] = relationship(
        back_populates="operation", order_by="PaymentOperationEvent.seq"
    )

    @property
    def money_moved(self) -> bool:
        return self.state in MONEY_MOVED_STATES


class PaymentOperationEvent(Base):
    """Append-only history of an operation: every transition, attempt and decision."""

    __tablename__ = "payment_operation_events"
    __table_args__ = (
        UniqueConstraint("operation_id", "seq", name="uq_payment_operation_events_seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("payment_operations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    category: Mapped[str] = mapped_column(
        String(40)
    )  # transition | gateway | recording | recovery | error
    detail: Mapped[str | None] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(100))  # user or worker id
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Hash chain (fmspayables06): event_hash = sha256(prev_hash | seq | from | to | category | detail | actor).
    # A deleted or edited event breaks the chain even if the append-only triggers were bypassed.
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str | None] = mapped_column(String(64))

    operation: Mapped[PaymentOperation] = relationship(back_populates="events")


class SimulatedGatewayTransfer(Base):
    """Durable store of the *simulated* payment gateway (no real transfers).

    Lives in the same database so recovery tests can restart "the worker" and
    still find what the gateway knows. Written on the gateway's own connection,
    outside the AP transaction -- like an external system.
    """

    __tablename__ = "simulated_gateway_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(80), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    vendor_code: Mapped[str] = mapped_column(String(30))
    destination_reference: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    pay_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))  # accepted | rejected | pending
    reference: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(40))
    lookups: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- period close attestations -----------------------------------------------------------------------


class APCloseAttestation(Base):
    """Immutable record of a close attempt: what was true and who decided what.

    Written for successful, forced and failed closes alike; a failed attempt
    leaves the period state unchanged but still documents the blockers found.
    """

    __tablename__ = "ap_close_attestations"
    __table_args__ = (
        CheckConstraint("outcome IN ('closed', 'forced', 'failed')", name="outcome_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    attempted_by: Mapped[str] = mapped_column(String(100))
    forced: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    force_reason: Mapped[str | None] = mapped_column(String(500))
    liability_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    open_bill_count: Mapped[int] = mapped_column(Integer)
    vendor_count: Mapped[int] = mapped_column(Integer)
    checks_json: Mapped[str] = mapped_column(String(8000))
    exceptions_json: Mapped[str] = mapped_column(String(8000))
    reconciliation_json: Mapped[str] = mapped_column(String(4000))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    gl_period_reference: Mapped[str | None] = mapped_column(String(100))
    lock_id: Mapped[int | None] = mapped_column(ForeignKey("ap_period_locks.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # fmspayables06: sha256 over the attested content; recomputed by the close verifier.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    # Obligations the close could not settle (confirmed transfers not yet recorded, ...), as a
    # separate, immutable list the verifier re-checks against later periods.
    obligations_json: Mapped[str | None] = mapped_column(String(8000))

    exceptions: Mapped[list["APCloseException"]] = relationship(
        back_populates="attestation", order_by="APCloseException.id"
    )


class APCloseException(Base):
    """One blocker accepted by a forced close, and -- later -- how it was resolved.

    Attempted closes are attestations; the decision to accept an exception is this
    row (actor, reason); its resolution is written once (status, resolver, reference).
    Nothing here is ever deleted or rewritten.
    """

    __tablename__ = "ap_close_exceptions"
    __table_args__ = (CheckConstraint("status IN ('accepted', 'resolved')", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    attestation_id: Mapped[int] = mapped_column(ForeignKey("ap_close_attestations.id"), index=True)
    period_end: Mapped[date] = mapped_column(Date)
    code: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(20))
    item_json: Mapped[str] = mapped_column(String(2000))
    accepted_by: Mapped[str] = mapped_column(String(100))
    accept_reason: Mapped[str] = mapped_column(String(500))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(20), default="accepted", server_default="accepted")
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(String(1000))
    resolution_reference: Mapped[str | None] = mapped_column(String(200))

    attestation: Mapped[APCloseAttestation] = relationship(back_populates="exceptions")


# --- supplier statement imports --------------------------------------------------------------------------


class StatementLineKind(str, enum.Enum):
    INVOICE = "invoice"
    CREDIT = "credit"
    PAYMENT = "payment"


class StatementMatchStatus(str, enum.Enum):
    MATCHED = "matched"
    NEAR_MATCH = "near_match"
    AMOUNT_DISCREPANCY = "amount_discrepancy"
    TIMING_DIFFERENCE = "timing_difference"
    UNMATCHED = "unmatched"


class SupplierStatementImport(Base):
    """An imported supplier statement: external evidence, never posted to the ledger."""

    __tablename__ = "supplier_statement_imports"
    __table_args__ = (
        UniqueConstraint("vendor_id", "content_hash", name="uq_supplier_statement_imports_content"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    statement_date: Mapped[date] = mapped_column(Date)
    imported_by: Mapped[str] = mapped_column(String(100))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    line_count: Mapped[int] = mapped_column(Integer)
    summary_json: Mapped[str] = mapped_column(String(4000))

    vendor: Mapped[Vendor] = relationship()
    lines: Mapped[list["SupplierStatementLine"]] = relationship(
        back_populates="statement_import",
        cascade="all, delete-orphan",
        order_by="SupplierStatementLine.line_no",
    )


class SupplierStatementLine(Base):
    __tablename__ = "supplier_statement_lines"
    __table_args__ = (
        UniqueConstraint("import_id", "line_no", name="uq_supplier_statement_lines_import_line"),
        CheckConstraint("kind IN ('invoice', 'credit', 'payment')", name="kind_valid"),
        CheckConstraint(
            "match_status IN ('matched', 'near_match', 'amount_discrepancy', 'timing_difference', "
            "'unmatched')",
            name="match_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_statement_imports.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[StatementLineKind] = mapped_column(str_enum(StatementLineKind))
    reference: Mapped[str] = mapped_column(String(80))
    normalized_reference: Mapped[str] = mapped_column(String(80))
    line_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    match_status: Mapped[StatementMatchStatus] = mapped_column(str_enum(StatementMatchStatus))
    matched_type: Mapped[str | None] = mapped_column(String(20))
    matched_id: Mapped[int | None] = mapped_column(Integer)
    internal_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    internal_date: Mapped[date | None] = mapped_column(Date)
    explanation: Mapped[str] = mapped_column(String(1000))
    review_status: Mapped[str] = mapped_column(String(20), default="open", server_default="open")
    resolution_note: Mapped[str | None] = mapped_column(String(500))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    statement_import: Mapped[SupplierStatementImport] = relationship(back_populates="lines")
