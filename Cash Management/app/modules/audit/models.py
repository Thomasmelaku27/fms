from datetime import datetime
from sqlalchemy import DateTime,String,func
from sqlalchemy.orm import Mapped,mapped_column
from app.db import Base
class AuditEvent(Base):
    __tablename__="audit_events"
    id: Mapped[int]=mapped_column(primary_key=True)
    actor: Mapped[str]=mapped_column(String(100))
    action: Mapped[str]=mapped_column(String(100))
    resource: Mapped[str]=mapped_column(String(150))
    occurred_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
