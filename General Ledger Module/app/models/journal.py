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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base, str_enum
from app.models.account import Account
from app.models.recurring import RecurringTemplate
from app.money import as_money


class EntryStatus(str, enum.Enum):
    DRAFT = "draft"
    POSTED = "posted"


class EntryType(str, enum.Enum):
    STANDARD = "standard"
    ADJUSTING = "adjusting"  # period-end accruals, depreciation, corrections
    RECURRING = "recurring"  # generated from a RecurringTemplate
    REVERSAL = "reversal"  # created by reversing a posted entry
    OPENING = "opening"  # opening-balance initialisation of a ledger
    CLOSING = "closing"  # year-end close of revenue/expense into retained earnings


# Types a client may choose when creating a draft; the others are system-assigned.
CLIENT_ENTRY_TYPES = (EntryType.STANDARD, EntryType.ADJUSTING)
# Types that can be switched between by explicit reclassification of a draft.
RECLASSIFIABLE_TYPES = (EntryType.STANDARD, EntryType.ADJUSTING)
# System entries that must never be reversed (they are the mechanism, not activity).
NON_REVERSIBLE_TYPES = (EntryType.OPENING, EntryType.CLOSING)


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        # One draft per template occurrence: generation can never duplicate a slot.
        UniqueConstraint(
            "recurring_template_id", "scheduled_for", name="uq_journal_entries_recurring_slot"
        ),
        # Idempotency key of the journal-processing contract (see services.journal_processing).
        UniqueConstraint(
            "source_type", "source_id", "operation_id", name="uq_journal_entries_source_operation"
        ),
        CheckConstraint(
            "(source_type IS NULL AND source_id IS NULL AND operation_id IS NULL) OR "
            "(source_type IS NOT NULL AND source_id IS NOT NULL AND operation_id IS NOT NULL)",
            name="source_complete",
        ),
        # Posting metadata exists exactly when the entry is posted.
        CheckConstraint(
            "(status = 'draft' AND posted_at IS NULL AND posted_by IS NULL "
            "AND posting_source IS NULL AND correlation_id IS NULL) OR "
            "(status = 'posted' AND posted_at IS NOT NULL AND posted_by IS NOT NULL "
            "AND posting_source IS NOT NULL AND correlation_id IS NOT NULL)",
            name="posting_metadata",
        ),
        # Reporting reads posted entries by date range; listings page by (entry_date, id).
        Index("ix_journal_entries_status_entry_date", "status", "entry_date", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500))
    reference: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[EntryStatus] = mapped_column(str_enum(EntryStatus), default=EntryStatus.DRAFT)
    entry_type: Mapped[EntryType] = mapped_column(
        str_enum(EntryType), default=EntryType.STANDARD, server_default=EntryType.STANDARD.value
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Set on a reversal: the posted entry it cancels. Unique, so an entry can be
    # reversed at most once; both entries stay posted and immutable (audit trail).
    reverses_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    # Set on drafts generated from a recurring template, with the schedule slot.
    recurring_template_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_templates.id"))
    scheduled_for: Mapped[date | None] = mapped_column(Date)
    # Provenance of entries created through the journal-processing interface by
    # other modules (AP, AR, cash...): which source document and which operation
    # on it produced this entry, plus a fingerprint of the accounting data so a
    # retry can be told apart from a conflicting reuse of the same identifiers.
    source_type: Mapped[str | None] = mapped_column(String(50))
    source_id: Mapped[str | None] = mapped_column(String(100))
    operation_id: Mapped[str | None] = mapped_column(String(100))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Posting metadata: written by the posting transition itself (same transaction,
    # same UPDATE) and immutable afterwards because posted rows never change.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(String(100))  # actor reference
    posting_source: Mapped[str | None] = mapped_column(String(30))  # api, processing, ...
    correlation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id"
    )
    reverses: Mapped["JournalEntry | None"] = relationship(
        remote_side="JournalEntry.id", back_populates="reversed_by"
    )
    reversed_by: Mapped["JournalEntry | None"] = relationship(
        back_populates="reverses", uselist=False
    )

    recurring_template: Mapped["RecurringTemplate | None"] = relationship(back_populates="entries")

    # Optimistic locking: UPDATEs are emitted as ``WHERE version = <loaded value>``
    # and raise StaleDataError if another transaction changed the row first. The
    # counter is bumped explicitly by the services (version_id_generator=False)
    # so that edits touching only the lines still count as a new version.
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}

    @property
    def reversed_by_entry_id(self) -> int | None:
        return self.reversed_by.id if self.reversed_by is not None else None


class JournalLine(Base):
    """One side of a journal entry.

    The ledger rules below are also enforced by the database (see the initial
    Alembic migration), so they hold even for direct SQL changes:

    * amounts are never negative and are whole cents (at most 2 decimals);
    * lines of a posted entry cannot be inserted, updated or deleted (trigger);
    * an entry can only be posted when every line has an amount on exactly one
      side, there are at least two lines, the lines balance and the entry date
      is not in a closed accounting period (trigger).

    The CHECK constraints apply to every stored line regardless of the entry's
    status. Everything else is only required at posting time, so a draft may be
    unbalanced, contain lines without an amount yet or with both sides filled
    in, have a single line or no lines at all. See ``app.services.posting`` for
    the application-side rules.
    """

    __tablename__ = "journal_lines"
    __table_args__ = (
        # Aggregations group by account; entry lookups join on entry_id.
        Index("ix_journal_lines_account_id", "account_id"),
        Index("ix_journal_lines_entry_id", "entry_id"),
        # Base's naming convention expands these to ck_journal_lines_<name>.
        CheckConstraint("debit >= 0 AND credit >= 0", name="non_negative"),
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

    @validates("debit", "credit")
    def _validate_amount(self, key: str, value):
        """Monetary precision is enforced for direct Python callers too, not only the API."""
        return as_money(value, field=key)
