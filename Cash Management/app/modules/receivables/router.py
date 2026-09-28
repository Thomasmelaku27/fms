from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.modules.receivables.models import Customer,SalesInvoice
from app.modules.receivables.schemas import CustomerIn,CustomerOut,InvoiceIn,InvoiceOut
router=APIRouter(prefix="/receivables",tags=["receivables"])
@router.post("/customers",response_model=CustomerOut,status_code=201)
def add_customer(data:CustomerIn,db:Session=Depends(get_db)):
    if db.scalar(select(Customer.id).where(Customer.code==data.code)):
        raise HTTPException(409,"Customer code exists")
    x=Customer(**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("/customers",response_model=list[CustomerOut])
def customers(db:Session=Depends(get_db)):
    return list(db.scalars(select(Customer).order_by(Customer.code)))
@router.post("/invoices",response_model=InvoiceOut,status_code=201)
def add_invoice(data:InvoiceIn,db:Session=Depends(get_db)):
    if db.get(Customer,data.customer_id) is None:
        raise HTTPException(422,"Customer does not exist")
    if data.due_date < data.invoice_date:
        raise HTTPException(422,"Due date precedes invoice date")
    if db.scalar(select(SalesInvoice.id).where(SalesInvoice.customer_id==data.customer_id,SalesInvoice.invoice_number==data.invoice_number)):
        raise HTTPException(409,"Duplicate customer invoice number")
    x=SalesInvoice(**data.model_dump());db.add(x);db.commit();db.refresh(x);return x
@router.get("/invoices",response_model=list[InvoiceOut])
def invoices(db:Session=Depends(get_db)):
    return list(db.scalars(select(SalesInvoice).order_by(SalesInvoice.id)))
