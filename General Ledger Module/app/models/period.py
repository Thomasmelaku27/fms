import enum
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum


class PeriodStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    LOCKED = "locked"  # final: never reopened, never modified (API or SQL)


class AccountingPeriod(Base):
    """A date range that can be closed to freeze its ledger.

    Lifecycle: open -> closed -> locked, with closed -> open allowed (reopen)
    and locked being final. Entries dated inside a closed or locked period
    cannot be posted (service check and database trigger). A locked period row
    cannot be updated or deleted at all (trigger). Periods never overlap
    (service check and database trigger). Dates not covered by any period are
    unrestricted.
    """

    __tablename__ = "accounting_periods"
    __table_args__ = (CheckConstraint("start_date <= end_date", name="dates_ordered"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[PeriodStatus] = mapped_column(str_enum(PeriodStatus), default=PeriodStatus.OPEN)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fiscal_year_id: Mapped[int | None] = mapped_column(ForeignKey("fiscal_years.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    fiscal_year: Mapped["FiscalYear | None"] = relationship(back_populates="periods")  # noqa: F821
