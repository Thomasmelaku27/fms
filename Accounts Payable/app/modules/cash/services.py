from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import EntryStatus,JournalEntry,JournalLine

def cash_balance(db:Session,account_id:int)->Decimal:
    """Signed, posted-only cash balance; no float conversion."""
    rows=db.execute(select(JournalLine.debit,JournalLine.credit).join(JournalEntry,JournalEntry.id==JournalLine.entry_id).where(JournalLine.account_id==account_id,JournalEntry.status==EntryStatus.POSTED)).all()
    return sum((debit-credit for debit,credit in rows),Decimal("0"))
