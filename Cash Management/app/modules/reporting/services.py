from datetime import date
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Account,EntryStatus,JournalEntry,JournalLine

def trial_balance(db:Session,as_of:date|None=None)->dict:
    """Unadjusted debit and credit totals from posted entries only."""
    stmt=(select(Account.code,Account.name,JournalLine.debit,JournalLine.credit)
          .join(JournalLine,Account.id==JournalLine.account_id)
          .join(JournalEntry,JournalEntry.id==JournalLine.entry_id)
          .where(JournalEntry.status==EntryStatus.POSTED))
    if as_of is not None:stmt=stmt.where(JournalEntry.entry_date<=as_of)
    totals={}
    for code,name,debit,credit in db.execute(stmt):
        item=totals.setdefault(code,{"code":code,"name":name,"net":Decimal("0")})
        item["net"]+=debit-credit
    rows=[]
    for item in sorted(totals.values(),key=lambda x:x["code"]):
        net=item["net"]
        rows.append({"code":item["code"],"name":item["name"],"debit":max(net,Decimal("0")),"credit":max(-net,Decimal("0"))})
    return {"as_of":as_of,"rows":rows,"total_debit":sum((x["debit"] for x in rows),Decimal("0")),"total_credit":sum((x["credit"] for x in rows),Decimal("0"))}
