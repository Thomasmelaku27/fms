from datetime import date
from decimal import Decimal
from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column,relationship
from app.db import Base

class Budget(Base):
    __tablename__="budgets"
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(120))
    start_date: Mapped[date]=mapped_column(Date)
    end_date: Mapped[date]=mapped_column(Date)
    status: Mapped[str]=mapped_column(String(20),default="draft")
    lines: Mapped[list["BudgetLine"]]=relationship(back_populates="budget",cascade="all, delete-orphan")

class BudgetLine(Base):
    __tablename__="budget_lines"
    __table_args__=(UniqueConstraint("budget_id","account_id",name="uq_budget_lines_budget_account"),)
    id: Mapped[int]=mapped_column(primary_key=True)
    budget_id: Mapped[int]=mapped_column(ForeignKey("budgets.id",ondelete="CASCADE"))
    account_id: Mapped[int]=mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    budget: Mapped[Budget]=relationship(back_populates="lines")
