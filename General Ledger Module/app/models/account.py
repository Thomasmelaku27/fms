import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, str_enum


class AccountType(str, enum.Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class NormalBalance(str, enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"


DEFAULT_NORMAL_BALANCE = {
    AccountType.ASSET: NormalBalance.DEBIT,
    AccountType.EXPENSE: NormalBalance.DEBIT,
    AccountType.LIABILITY: NormalBalance.CREDIT,
    AccountType.EQUITY: NormalBalance.CREDIT,
    AccountType.REVENUE: NormalBalance.CREDIT,
}


class AccountCategory(str, enum.Enum):
    CURRENT_ASSET = "current_asset"
    FIXED_ASSET = "fixed_asset"
    OTHER_ASSET = "other_asset"
    CURRENT_LIABILITY = "current_liability"
    LONG_TERM_LIABILITY = "long_term_liability"
    EQUITY = "equity"
    RETAINED_EARNINGS = "retained_earnings"
    OPERATING_REVENUE = "operating_revenue"
    OTHER_REVENUE = "other_revenue"
    OPERATING_EXPENSE = "operating_expense"
    OTHER_EXPENSE = "other_expense"


CATEGORY_TYPES = {
    AccountCategory.CURRENT_ASSET: AccountType.ASSET,
    AccountCategory.FIXED_ASSET: AccountType.ASSET,
    AccountCategory.OTHER_ASSET: AccountType.ASSET,
    AccountCategory.CURRENT_LIABILITY: AccountType.LIABILITY,
    AccountCategory.LONG_TERM_LIABILITY: AccountType.LIABILITY,
    AccountCategory.EQUITY: AccountType.EQUITY,
    AccountCategory.RETAINED_EARNINGS: AccountType.EQUITY,
    AccountCategory.OPERATING_REVENUE: AccountType.REVENUE,
    AccountCategory.OTHER_REVENUE: AccountType.REVENUE,
    AccountCategory.OPERATING_EXPENSE: AccountType.EXPENSE,
    AccountCategory.OTHER_EXPENSE: AccountType.EXPENSE,
}

# Revenue and expense balances are closed to retained earnings at year end;
# asset, liability and equity balances carry forward.
TEMPORARY_TYPES = (AccountType.REVENUE, AccountType.EXPENSE)

# The category/type mapping as a CHECK constraint, so direct SQL cannot break it.
CATEGORY_TYPE_SQL = "category IS NULL OR " + " OR ".join(
    f"(category = '{category.value}' AND type = '{type_.value}')"
    for category, type_ in CATEGORY_TYPES.items()
)


class Account(Base):
    """An entry in the chart of accounts.

    Hierarchy: ``parent_id`` points at a *summary* account of the same type.
    Summary accounts structure the chart and roll up balances; only
    non-summary ("posting") accounts may appear on journal lines (service
    check + database trigger). Rules enforced by ``app.services.accounts``:
    no cycles, parent/child types must match, and an account with journal
    history cannot change type or become a summary account.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[AccountType] = mapped_column(str_enum(AccountType))
    category: Mapped[AccountCategory | None] = mapped_column(str_enum(AccountCategory, length=30))
    normal_balance: Mapped[NormalBalance] = mapped_column(
        str_enum(NormalBalance), server_default=NormalBalance.DEBIT.value
    )
    is_summary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    parent: Mapped["Account | None"] = relationship(
        remote_side="Account.id", back_populates="children"
    )
    children: Mapped[list["Account"]] = relationship(
        back_populates="parent", order_by="Account.code"
    )

    def __init__(self, **kwargs) -> None:
        if "normal_balance" not in kwargs and "type" in kwargs:
            kwargs["normal_balance"] = DEFAULT_NORMAL_BALANCE[AccountType(kwargs["type"])]
        super().__init__(**kwargs)
