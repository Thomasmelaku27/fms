from sqlalchemy import ForeignKey,String
from sqlalchemy.orm import Mapped,mapped_column
from app.db import Base
class CashAccount(Base):
    __tablename__="cash_accounts"
    id: Mapped[int]=mapped_column(primary_key=True)
    ledger_account_id: Mapped[int]=mapped_column(ForeignKey("accounts.id"),unique=True)
    label: Mapped[str]=mapped_column(String(100))
