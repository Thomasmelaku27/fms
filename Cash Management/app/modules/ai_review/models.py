from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime,ForeignKey,Numeric,String,func
from sqlalchemy.orm import Mapped,mapped_column
from app.db import Base
class ReviewSuggestion(Base):
    __tablename__="review_suggestions"
    id: Mapped[int]=mapped_column(primary_key=True)
    description: Mapped[str]=mapped_column(String(500))
    suggested_account_id: Mapped[int]=mapped_column(ForeignKey("accounts.id"))
    confidence: Mapped[Decimal]=mapped_column(Numeric(4,3))
    status: Mapped[str]=mapped_column(String(20),default="pending")
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
