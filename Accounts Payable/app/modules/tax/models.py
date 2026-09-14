from decimal import Decimal
from sqlalchemy import Numeric,String,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from app.db import Base
class TaxCode(Base):
    __tablename__="tax_codes"
    id: Mapped[int]=mapped_column(primary_key=True)
    code: Mapped[str]=mapped_column(String(30),unique=True)
    description: Mapped[str]=mapped_column(String(200))
    rate: Mapped[Decimal]=mapped_column(Numeric(10,6))
    jurisdiction: Mapped[str]=mapped_column(String(100))
    is_active: Mapped[bool]=mapped_column(default=True)
