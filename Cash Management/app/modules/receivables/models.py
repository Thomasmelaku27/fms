from datetime import date
from decimal import Decimal
from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

class Customer(Base):
    __tablename__="customers"
    id: Mapped[int]=mapped_column(primary_key=True)
    code: Mapped[str]=mapped_column(String(30),unique=True)
    name: Mapped[str]=mapped_column(String(200))
    is_active: Mapped[bool]=mapped_column(default=True)

class SalesInvoice(Base):
    __tablename__="sales_invoices"
    __table_args__=(UniqueConstraint("customer_id","invoice_number",name="uq_sales_invoices_customer_invoice"),)
    id: Mapped[int]=mapped_column(primary_key=True)
    customer_id: Mapped[int]=mapped_column(ForeignKey("customers.id"))
    invoice_number: Mapped[str]=mapped_column(String(80))
    invoice_date: Mapped[date]=mapped_column(Date)
    due_date: Mapped[date]=mapped_column(Date)
    total: Mapped[Decimal]=mapped_column(Numeric(18,2))
    status: Mapped[str]=mapped_column(String(20),default="draft")
