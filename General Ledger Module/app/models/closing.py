import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum


class ClosingStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"  # exists only inside the closing transaction
    COMPLETED = "completed"  # immutable attestation


class FiscalYearClosing(Base):
    """Attestation of a year-end close.

    Created at the start of the closing transaction, referenced by the
    ``closing`` journal entry (which the database only lets into a closed
    period while this record is ``in_progress``), and completed in the same
    transaction with the closing totals, the covered periods, the
    reconciliation verdict, a hash of the posted ledger lines of the year
    (so the close can be reproduced from history), the actor and a
    correlation id. Completed records can never change (trigger); a close
    that fails before commit leaves no record at all.
    """

    __tablename__ = "fiscal_year_closings"

    id: Mapped[int] = mapped_column(primary_key=True)
    fiscal_year_id: Mapped[int] = mapped_column(ForeignKey("fiscal_years.id"), unique=True)
    closing_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    status: Mapped[ClosingStatus] = mapped_column(
        str_enum(ClosingStatus), default=ClosingStatus.IN_PROGRESS
    )
    covered_period_ids: Mapped[str] = mapped_column(Text)  # JSON list
    net_income: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    total_debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    total_credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    ledger_hash: Mapped[str] = mapped_column(String(64))
    reconciliation_ok: Mapped[bool] = mapped_column(default=False)
    reconciliation_summary: Mapped[str] = mapped_column(Text)  # JSON
    actor_ref: Mapped[str] = mapped_column(String(100))
    correlation_id: Mapped[str] = mapped_column(String(36))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fiscal_year: Mapped["FiscalYear"] = relationship()  # noqa: F821
