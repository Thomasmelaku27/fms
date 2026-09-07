from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountDailyTotal(Base):
    """Incremental aggregate: posted debit/credit cents per account and day.

    Maintained by a database trigger in the same transaction that posts an
    entry (see revision 0006); posted entries are immutable, so rows only ever
    grow. Source of truth remains ``journal_lines`` of posted entries; this
    table is a derived accelerator that ``app.services.balances`` verifies and
    can rebuild.
    """

    __tablename__ = "account_daily_totals"

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, primary_key=True)
    debit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_cents: Mapped[int] = mapped_column(BigInteger, default=0)


class BalanceVerificationRun(Base):
    """Log of aggregate-vs-ledger verifications and rebuilds (never touches journals)."""

    __tablename__ = "balance_verification_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # verify | rebuild
    ok: Mapped[bool] = mapped_column(default=False)
    discrepancy_count: Mapped[int] = mapped_column(default=0)
    details: Mapped[str] = mapped_column(Text)  # JSON
    actor_ref: Mapped[str] = mapped_column(String(100))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
