from sqlalchemy.orm import Session
from app.modules.audit.models import AuditEvent

def record_event(db:Session,*,actor:str,action:str,resource:str)->AuditEvent:
    """Append an event within the caller's transaction; caller commits atomically."""
    x=AuditEvent(actor=actor,action=action,resource=resource);db.add(x);return x
