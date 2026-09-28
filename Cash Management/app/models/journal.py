import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum
from app.models.account import Account


class EntryStatus(str, enum.Enum):
    DRAFT = "draft"
    POSTED = "posted"


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500))
    reference: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[EntryStatus] = mapped_column(str_enum(EntryStatus), default=EntryStatus.DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id"
    )


class JournalLine(Base):
    """One side of a journal entry.

    The ledger rules below are also enforced by the database (see the initial
    Alembic migration), so they hold even for direct SQL changes:

    * amounts are never negative and are whole cents (at most 2 decimals);
    * a line never carries both a debit and a credit;
    * lines of a posted entry cannot be inserted, updated or deleted (trigger);
    * an entry can only be posted when every line has an amount, there are at
      least two lines and the lines balance (trigger).

    The CHECK constraints apply to every stored line regardless of the entry's
    status. Completeness and balance are only required at posting time, so a
    draft may be unbalanced, contain lines without an amount yet (debit and
    credit both 0), have a single line or no lines at all. See
    ``app.services.posting`` for the application-side rules.
    """

    __tablename__ = "journal_lines"
    __table_args__ = (
        # Base's naming convention expands these to ck_journal_lines_<name>.
        CheckConstraint("debit >= 0 AND credit >= 0", name="non_negative"),
        CheckConstraint("debit = 0 OR credit = 0", name="not_both_sides"),
        # PostgreSQL NUMERIC(18, 2) enforces the scale itself; SQLite stores REAL and
        # would otherwise accept sub-cent values that the balance check cannot sum exactly.
        CheckConstraint(
            "ROUND(debit, 2) = debit AND ROUND(credit, 2) = credit", name="whole_cents"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    memo: Mapped[str | None] = mapped_column(String(255))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
