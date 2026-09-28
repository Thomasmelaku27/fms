"""Starter functionality is deliberately small; each branch expands its own module."""
from app.modules.audit.models import AuditEvent
from app.modules.audit.services import record_event

def _account(c,code="1000",typ="asset"):
    r=c.post("/api/v1/accounts",json={"code":code,"name":code,"type":typ}); assert r.status_code==201,r.text;return r.json()["id"]

def test_payables_starter(client):
    v=client.post("/api/v1/payables/vendors",json={"code":"V1","name":"Supplier"});assert v.status_code==201,v.text
    bill=client.post("/api/v1/payables/bills",json={"vendor_id":v.json()["id"],"invoice_number":"B1","invoice_date":"2026-01-01","due_date":"2026-02-01","total":"120.50"});assert bill.status_code==201,bill.text
    assert bill.json()["status"]=="draft"
    assert len(client.get("/api/v1/payables/bills").json())==1

def test_receivables_starter(client):
    c=client.post("/api/v1/receivables/customers",json={"code":"C1","name":"Buyer"});assert c.status_code==201,c.text
    inv=client.post("/api/v1/receivables/invoices",json={"customer_id":c.json()["id"],"invoice_number":"I1","invoice_date":"2026-01-01","due_date":"2026-02-01","total":"95.25"});assert inv.status_code==201,inv.text
    assert inv.json()["status"]=="draft"

def test_budget_starter(client):
    account=_account(client)
    b=client.post("/api/v1/budgets",json={"name":"FY2026","start_date":"2026-01-01","end_date":"2026-12-31"});assert b.status_code==201,b.text
    line=client.post(f'/api/v1/budgets/{b.json()["id"]}/lines',json={"account_id":account,"amount":"1000.00"});assert line.status_code==201,line.text

def test_cash_and_reporting_posted_only(client):
    cash=_account(client); revenue=_account(client,"4000","revenue")
    ca=client.post("/api/v1/cash-accounts",json={"ledger_account_id":cash,"label":"Main bank"});assert ca.status_code==201,ca.text
    j=client.post("/api/v1/journal-entries",json={"entry_date":"2026-01-02","description":"sale","lines":[{"account_id":cash,"debit":"10.00"},{"account_id":revenue,"credit":"10.00"}]});assert j.status_code==201,j.text
    before=client.get(f'/api/v1/cash-accounts/{ca.json()["id"]}/balance').json();assert before["balance"]=="0"
    assert client.get("/api/v1/reports/trial-balance").json()["rows"]==[]
    assert client.post(f'/api/v1/journal-entries/{j.json()["id"]}/post').status_code==200
    after=client.get(f'/api/v1/cash-accounts/{ca.json()["id"]}/balance').json();assert after["balance"]=="10.00"
    report=client.get("/api/v1/reports/trial-balance").json();assert report["total_debit"]==report["total_credit"]=="10.00"

def test_tax_starter(client):
    r=client.post("/api/v1/tax-codes",json={"code":"TEST","description":"Test-only rate","rate":"0.065000","jurisdiction":"Test"});assert r.status_code==201,r.text
    assert len(client.get("/api/v1/tax-codes").json())==1

def test_ai_review_does_not_post(client):
    a=_account(client)
    r=client.post("/api/v1/review-suggestions",json={"description":"Expense candidate","suggested_account_id":a,"confidence":"0.800"});assert r.status_code==201,r.text
    assert r.json()["status"]=="pending"
    assert client.get("/api/v1/journal-entries").json()==[]

def test_audit_service_can_write_event(session):
    x=record_event(session,actor="test-user",action="create",resource="vendor:1")
    session.commit()
    assert session.get(AuditEvent,x.id).action=="create"
