"""Cash management: cash accounts, bank statements, reconciliation, planning.

Design rules (see the module README in ``app/modules/cash/__init__.py``):

* The General Ledger is the accounting source of truth. Nothing in this module
  stores a balance; every balance is derived from *posted* journal lines of the
  ledger account linked to a cash account.
* Bank statements and their transactions are *evidence from the bank*, never
  accounting. Importing a statement does not post anything.
* Reconciliation matches link a bank transaction to a posted journal line. The
  database refuses matches against draft entries, against lines of another
  ledger account, in the wrong direction, beyond either side's amount, and
  changes to matches of a closed session (triggers in the ``cashmgmt01``
  migration).
* Planned cash flows feed the liquidity projection only. They are never counted
  as posted financial transactions.

Sign convention for bank transaction and planned amounts: positive = money
received into the cash account (a debit on the asset ledger account), negative
= money paid out (a credit on the ledger account).
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
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum


class CashAccount(Base):
    """A bank/cash account, linked one-to-one to an asset ledger account."""

    __tablename__ = "cash_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ledger_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    label: Mapped[str] = mapped_column(String(100))
    # Configurable liquidity alert threshold used by the projection (None = not configured).
    minimum_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)


class BankStatement(Base):
    """An imported bank statement (header). Re-importing the same statement is a no-op."""

    __tablename__ = "bank_statements"
    __table_args__ = (
        UniqueConstraint(
            "cash_account_id", "statement_reference", name="uq_bank_statements_cash_account_reference"
        ),
        CheckConstraint("period_start <= period_end", name="period_order"),
        CheckConstraint(
            "ROUND(opening_balance, 2) = opening_balance AND ROUND(closing_balance, 2) = closing_balance",
            name="whole_cents",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("cash_accounts.id"))
    statement_reference: Mapped[str] = mapped_column(String(100))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    closing_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    # Fingerprint of the whole import payload: identical re-imports are recognised,
    # different content under the same reference is rejected.
    content_hash: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    transactions: Mapped[list["BankTransaction"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan", order_by="BankTransaction.line_no"
    )


class BankTransaction(Base):
    """One line of a bank statement.

    ``fingerprint`` identifies the transaction across statements of the same
    cash account (reference, date, amount, description and occurrence number),
    so overlapping or repeated imports never create duplicates.
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint(
            "cash_account_id", "fingerprint", name="uq_bank_transactions_cash_account_fingerprint"
        ),
        UniqueConstraint("statement_id", "line_no", name="uq_bank_transactions_statement_line"),
        CheckConstraint("amount <> 0", name="non_zero"),
        CheckConstraint("ROUND(amount, 2) = amount", name="whole_cents"),
        Index(
            "ix_bank_transactions_cash_account_id_transaction_date",
            "cash_account_id",
            "transaction_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("bank_statements.id", ondelete="CASCADE")
    )
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("cash_accounts.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    transaction_date: Mapped[date] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fingerprint: Mapped[str] = mapped_column(String(64))

    statement: Mapped[BankStatement] = relationship(back_populates="transactions")
    matches: Mapped[list["ReconciliationMatch"]] = relationship(
        back_populates="bank_transaction", order_by="ReconciliationMatch.id"
    )


class ReconciliationStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class ReconciliationSession(Base):
    """A reconciliation of one cash account against a statement period."""

    __tablename__ = "reconciliation_sessions"
    __table_args__ = (
        UniqueConstraint(
            "cash_account_id",
            "period_start",
            "period_end",
            name="uq_reconciliation_sessions_cash_account_period",
        ),
        CheckConstraint("period_start <= period_end", name="period_order"),
        CheckConstraint(
            "ROUND(opening_balance, 2) = opening_balance AND ROUND(closing_balance, 2) = closing_balance",
            name="whole_cents",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("cash_accounts.id"))
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("bank_statements.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    closing_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[ReconciliationStatus] = mapped_column(
        str_enum(ReconciliationStatus), default=ReconciliationStatus.OPEN
    )
    notes: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Number of *completed* reopenings; together with ``status`` this is the
    # authoritative state of the certification cycle (see certification.py).
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    # The reopen event that completed the latest reopening (None until the first one).
    last_reopen_event_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "reconciliation_reopen_events.id",
            use_alter=True,
            name="fk_recon_sessions_last_reopen_event",
        ),
        nullable=True,
    )

    matches: Mapped[list["ReconciliationMatch"]] = relationship(
        back_populates="session", order_by="ReconciliationMatch.id"
    )


class ReconciliationMatch(Base):
    """A (possibly partial) match between a bank transaction and a posted journal line.

    ``amount`` is always a positive magnitude; the direction is implied by the
    bank transaction sign (inflow <-> debit line, outflow <-> credit line).
    One row per (bank transaction, journal line) pair; partial reconciliation is
    expressed by an amount below the full amount of either side.
    """

    __tablename__ = "reconciliation_matches"
    __table_args__ = (
        UniqueConstraint(
            "bank_transaction_id",
            "journal_line_id",
            name="uq_reconciliation_matches_bank_transaction_journal_line",
        ),
        CheckConstraint("amount > 0", name="positive"),
        CheckConstraint("ROUND(amount, 2) = amount", name="whole_cents"),
        Index("ix_reconciliation_matches_journal_line_id", "journal_line_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_sessions.id"))
    bank_transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"))
    journal_line_id: Mapped[int] = mapped_column(ForeignKey("journal_lines.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session: Mapped[ReconciliationSession] = relationship(back_populates="matches")
    bank_transaction: Mapped[BankTransaction] = relationship(back_populates="matches")


class CashFlowActivity(str, enum.Enum):
    OPERATING = "operating"
    INVESTING = "investing"
    FINANCING = "financing"


class CashFlowClassification(Base):
    """Explicit operating/investing/financing classification of a ledger account.

    Accounts without a row fall back to a default by account type (see
    ``app.modules.cash.reports``); the report states which source was used.
    """

    __tablename__ = "cash_flow_classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    activity: Mapped[CashFlowActivity] = mapped_column(str_enum(CashFlowActivity))


class PlannedCashFlowStatus(str, enum.Enum):
    PLANNED = "planned"
    REALIZED = "realized"
    CANCELLED = "cancelled"


class PlannedCashFlow(Base):
    """An expected future cash movement used only by the liquidity projection.

    Planned flows are forecast data: they never touch the ledger and are never
    counted as posted transactions. Once the real movement is posted, the item is
    marked ``realized`` (optionally linked to the journal entry) so that it is
    not counted twice.
    """

    __tablename__ = "planned_cash_flows"
    __table_args__ = (
        UniqueConstraint(
            "cash_account_id",
            "source",
            "external_reference",
            name="uq_planned_cash_flows_cash_account_source_reference",
        ),
        CheckConstraint("amount <> 0", name="non_zero"),
        CheckConstraint("ROUND(amount, 2) = amount", name="whole_cents"),
        Index(
            "ix_planned_cash_flows_cash_account_id_expected_date", "cash_account_id", "expected_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("cash_accounts.id"))
    expected_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    description: Mapped[str] = mapped_column(String(500))
    activity: Mapped[CashFlowActivity | None] = mapped_column(
        str_enum(CashFlowActivity), nullable=True
    )
    source: Mapped[str] = mapped_column(String(50), default="manual")
    external_reference: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[PlannedCashFlowStatus] = mapped_column(
        str_enum(PlannedCashFlowStatus), default=PlannedCashFlowStatus.PLANNED
    )
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconciliationCertification(Base):
    """Immutable evidence of exactly what was approved when a session was closed.

    ``content`` is the canonical JSON of the approved result (figures, every
    match, the bank transactions and posted ledger lines that were in scope,
    discrepancies, notes) and ``content_hash`` its SHA-256. Certifications are
    append-only (the database refuses UPDATE and DELETE) and chained through
    ``previous_hash`` so the history of re-certifications is tamper-evident.

    Version ``n`` certifies reopen cycle ``n`` (cycle 1 = the first close, cycle
    ``n`` = after ``n - 1`` completed reopenings), so the certification is
    *active* exactly when the session is closed and it is the latest version.
    ``reopen_event_id`` names the reopen event that started its cycle (None
    for version 1).
    """

    __tablename__ = "reconciliation_certifications"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "version", name="uq_reconciliation_certifications_session_version"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_sessions.id"))
    version: Mapped[int] = mapped_column(Integer)
    certified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    is_balanced: Mapped[bool] = mapped_column()
    forced: Mapped[bool] = mapped_column()
    difference: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    cleared_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    ledger_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    matches_count: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(500))
    reopen_event_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "reconciliation_reopen_events.id",
            use_alter=True,
            name="fk_recon_certifications_reopen_event",
        ),
        nullable=True,
    )


class ReconciliationReopenEvent(Base):
    """A recorded reopening request (reason) for the latest certification.

    Append-only like certifications. ``reopen_number`` is the reopening it asks
    for (``session.reopen_count + 1`` at insert time). The database only lets a
    closed session become open again when the session points at such an event
    (``last_reopen_event_id``) - but an event on its own never changes state:
    a stray or duplicated event (partial process, raw SQL) is simply reported
    as orphaned/superseded in the history and blocks nothing.
    """

    __tablename__ = "reconciliation_reopen_events"
    __table_args__ = (
        Index("ix_reconciliation_reopen_events_certification_id", "certification_id"),
        Index(
            "ix_reconciliation_reopen_events_session_id_reopen_number",
            "session_id",
            "reopen_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_sessions.id"))
    certification_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_certifications.id"))
    reopen_number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    reopened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
