import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum
from app.models.account import Account


class Frequency(str, enum.Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class RecurringTemplate(Base):
    """Blueprint for journal entries that repeat on a schedule.

    ``generate`` (see app.services.recurring) creates one *draft* per occurrence
    between ``next_run_date`` and the requested date, then advances
    ``next_run_date``. Drafts are reviewed and posted like any other entry.
    Occurrences are anchored on ``start_date`` (e.g. monthly from the 31st
    clamps to month end without drifting).
    """

    __tablename__ = "recurring_templates"
    __table_args__ = (
        CheckConstraint("end_date IS NULL OR start_date <= end_date", name="dates_ordered"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(String(500))
    reference: Mapped[str | None] = mapped_column(String(100))
    frequency: Mapped[Frequency] = mapped_column(str_enum(Frequency))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    next_run_date: Mapped[date] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["RecurringTemplateLine"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", order_by="RecurringTemplateLine.id"
    )
    entries: Mapped[list["JournalEntry"]] = relationship(  # noqa: F821
        back_populates="recurring_template", order_by="JournalEntry.scheduled_for"
    )


class RecurringTemplateLine(Base):
    __tablename__ = "recurring_template_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="non_negative"),
        CheckConstraint(
            "ROUND(debit, 2) = debit AND ROUND(credit, 2) = credit", name="whole_cents"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_templates.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    memo: Mapped[str | None] = mapped_column(String(255))

    template: Mapped[RecurringTemplate] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
