import enum
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum


class FiscalYearStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"  # final: year-end close executed


class FiscalYear(Base):
    """A fiscal year groups accounting periods and owns the year-end close.

    Closing a year posts one balanced ``closing`` journal entry that moves every
    revenue and expense balance of the year into ``retained_earnings_account``;
    balance-sheet accounts carry forward untouched. Requires every period of
    the year to be closed or locked (see ``app.services.fiscal_years``). A
    closed year is final: its periods cannot be reopened (trigger) and the
    year row cannot change (trigger).
    """

    __tablename__ = "fiscal_years"
    __table_args__ = (CheckConstraint("start_date <= end_date", name="dates_ordered"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[FiscalYearStatus] = mapped_column(
        str_enum(FiscalYearStatus), default=FiscalYearStatus.OPEN
    )
    retained_earnings_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    closing_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    periods: Mapped[list["AccountingPeriod"]] = relationship(  # noqa: F821
        back_populates="fiscal_year", order_by="AccountingPeriod.start_date"
    )
