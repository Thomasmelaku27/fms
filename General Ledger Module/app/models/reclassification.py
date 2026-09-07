from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccountReclassification(Base):
    """Effective-dated record of a hierarchy/category change of an account with history.

    The database only allows ``parent_id`` / ``category`` of an account that has
    journal lines to change when a matching reclassification row exists (the
    service inserts it first, in the same transaction), so every such change
    is traceable with its effective date and reason, and reports can explain
    the impact on historical roll-ups.
    """

    __tablename__ = "account_reclassifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    old_parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    new_parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    old_category: Mapped[str | None] = mapped_column(String(30))
    new_category: Mapped[str | None] = mapped_column(String(30))
    effective_from: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(500))
    actor_ref: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
