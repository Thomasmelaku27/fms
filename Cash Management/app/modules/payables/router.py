from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.modules.payables.models import Vendor, SupplierBill
from app.modules.payables.schemas import VendorIn, VendorOut, BillIn, BillOut

router = APIRouter(prefix="/payables", tags=["payables"])

@router.post("/vendors", response_model=VendorOut, status_code=201)
def add_vendor(data: VendorIn, db: Session = Depends(get_db)):
    if db.scalar(select(Vendor.id).where(Vendor.code == data.code)):
        raise HTTPException(409, "Vendor code exists")
    obj=Vendor(**data.model_dump());db.add(obj);db.commit();db.refresh(obj);return obj

@router.get("/vendors", response_model=list[VendorOut])
def vendors(db: Session = Depends(get_db)):
    return list(db.scalars(select(Vendor).order_by(Vendor.code)))

@router.post("/bills", response_model=BillOut, status_code=201)
def add_bill(data: BillIn, db: Session = Depends(get_db)):
    if db.get(Vendor, data.vendor_id) is None:
        raise HTTPException(422, "Vendor does not exist")
    if data.due_date < data.invoice_date:
        raise HTTPException(422, "Due date precedes invoice date")
    if db.scalar(select(SupplierBill.id).where(SupplierBill.vendor_id==data.vendor_id, SupplierBill.invoice_number==data.invoice_number)):
        raise HTTPException(409,"Duplicate vendor invoice number")
    obj=SupplierBill(**data.model_dump());db.add(obj);db.commit();db.refresh(obj);return obj

@router.get("/bills", response_model=list[BillOut])
def bills(db: Session=Depends(get_db)):
    return list(db.scalars(select(SupplierBill).order_by(SupplierBill.id)))
