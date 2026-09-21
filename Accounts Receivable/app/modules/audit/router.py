from fastapi import APIRouter,Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.modules.audit.models import AuditEvent
router=APIRouter(prefix="/audit-events",tags=["audit"])
@router.get("")
def events(db:Session=Depends(get_db)):
    return [{"id":x.id,"actor":x.actor,"action":x.action,"resource":x.resource} for x in db.scalars(select(AuditEvent).order_by(AuditEvent.id))]
