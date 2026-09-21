"""Accounts Receivable API (``/api/v1/receivables``).

Routes are thin: validation of the payload is done by the schemas, business
rules and transactions by ``services``/``reports``. Domain errors carry the
HTTP status to use and are translated here so the shared error handlers do
not need to know about this module.
"""

from collections.abc import Callable
from datetime import date
from functools import wraps
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.modules.receivables import reports, services
from app.modules.receivables.models import Customer, CustomerAdjustment, CustomerPayment, InvoiceStatus, SalesInvoice
from app.modules.receivables.schemas import (
    AdjustmentIn,
    AdjustmentOut,
    AgingReport,
    AllocationRequest,
    CustomerBalance,
    CustomerIn,
    CustomerOut,
    CustomerStatement,
    InvoiceIn,
    InvoiceOut,
    InvoiceUpdate,
    OverdueReport,
    PaymentIn,
    PaymentOut,
    SettingsIn,
    SettingsOut,
    VoidIn,
)

router = APIRouter(prefix="/receivables", tags=["receivables"])


def domain_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Translate service/report errors into HTTP responses."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except services.ReceivablesError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        except reports.UnknownCustomer as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Customer {exc.args[0]} not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return wrapper


# --- Customers ---------------------------------------------------------------------


@router.post("/customers", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def add_customer(data: CustomerIn, db: Session = Depends(get_db)) -> Customer:
    if db.scalar(select(Customer.id).where(Customer.code == data.code)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Customer code exists")
    customer = Customer(**data.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.get("/customers", response_model=list[CustomerOut])
def customers(db: Session = Depends(get_db)) -> list[Customer]:
    return list(db.scalars(select(Customer).order_by(Customer.code)))


@router.get("/customers/{customer_id}", response_model=CustomerOut)
@domain_errors
def get_customer(customer_id: int, db: Session = Depends(get_db)) -> Customer:
    return services.get_customer(db, customer_id)


@router.get("/customers/{customer_id}/balance", response_model=CustomerBalance)
@domain_errors
def customer_balance(customer_id: int, as_of: date | None = None, db: Session = Depends(get_db)) -> dict:
    return reports.customer_balance(db, customer_id, as_of)


@router.get("/customers/{customer_id}/statement", response_model=CustomerStatement)
@domain_errors
def customer_statement(
    customer_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
) -> dict:
    return reports.customer_statement(db, customer_id, start_date, end_date)


# --- Settings ----------------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
@domain_errors
def get_settings(db: Session = Depends(get_db)) -> Any:
    return services.require_settings(db)


@router.put("/settings", response_model=SettingsOut)
@domain_errors
def put_settings(data: SettingsIn, db: Session = Depends(get_db)) -> Any:
    return services.save_settings(db, data.model_dump())


# --- Invoices ----------------------------------------------------------------------


@router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
@domain_errors
def add_invoice(data: InvoiceIn, db: Session = Depends(get_db)) -> SalesInvoice:
    return services.create_invoice(db, data)


@router.get("/invoices", response_model=list[InvoiceOut])
def invoices(
    customer_id: int | None = None,
    status_filter: InvoiceStatus | None = Query(default=None, alias="status"),
    open_only: bool = False,
    db: Session = Depends(get_db),
) -> list[SalesInvoice]:
    return services.list_invoices(db, customer_id, status_filter, open_only)


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
@domain_errors
def get_invoice(invoice_id: int, db: Session = Depends(get_db)) -> SalesInvoice:
    return services.get_invoice(db, invoice_id)


@router.put("/invoices/{invoice_id}", response_model=InvoiceOut)
@domain_errors
def update_invoice(invoice_id: int, data: InvoiceUpdate, db: Session = Depends(get_db)) -> SalesInvoice:
    return services.update_invoice(db, invoice_id, data)


@router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
@domain_errors
def issue_invoice(invoice_id: int, db: Session = Depends(get_db)) -> SalesInvoice:
    """Post the invoice to the ledger. 409 if not a draft, 422 if it cannot be posted."""
    return services.issue_invoice(db, invoice_id)


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceOut)
@domain_errors
def void_invoice(invoice_id: int, data: VoidIn | None = None, db: Session = Depends(get_db)) -> SalesInvoice:
    data = data or VoidIn()
    return services.void_invoice(db, invoice_id, data.void_date, data.reason)


# --- Payments ----------------------------------------------------------------------


@router.post(
    "/payments",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": PaymentOut, "description": "Replay of an earlier request (same idempotency key)"}},
)
@domain_errors
def add_payment(data: PaymentIn, response: Response, db: Session = Depends(get_db)) -> CustomerPayment:
    payment, created = services.register_payment(db, data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return payment


@router.get("/payments", response_model=list[PaymentOut])
def payments(customer_id: int | None = None, db: Session = Depends(get_db)) -> list[CustomerPayment]:
    return services.list_payments(db, customer_id)


@router.get("/payments/{payment_id}", response_model=PaymentOut)
@domain_errors
def get_payment(payment_id: int, db: Session = Depends(get_db)) -> CustomerPayment:
    return services.get_payment(db, payment_id)


@router.post(
    "/payments/{payment_id}/allocations",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": PaymentOut, "description": "Replay of an earlier request (same idempotency key)"}},
)
@domain_errors
def allocate_payment(
    payment_id: int, data: AllocationRequest, response: Response, db: Session = Depends(get_db)
) -> CustomerPayment:
    payment, created = services.allocate_payment(db, payment_id, data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return payment


# --- Adjustments (credit notes, write-offs) -------------------------------------------


@router.post(
    "/adjustments",
    response_model=AdjustmentOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": AdjustmentOut, "description": "Replay of an earlier request (same idempotency key)"}},
)
@domain_errors
def add_adjustment(data: AdjustmentIn, response: Response, db: Session = Depends(get_db)) -> CustomerAdjustment:
    adjustment, created = services.create_adjustment(db, data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return adjustment


@router.get("/adjustments", response_model=list[AdjustmentOut])
def adjustments(customer_id: int | None = None, db: Session = Depends(get_db)) -> list[CustomerAdjustment]:
    return services.list_adjustments(db, customer_id)


@router.get("/adjustments/{adjustment_id}", response_model=AdjustmentOut)
@domain_errors
def get_adjustment(adjustment_id: int, db: Session = Depends(get_db)) -> CustomerAdjustment:
    return services.get_adjustment(db, adjustment_id)


@router.post(
    "/adjustments/{adjustment_id}/allocations",
    response_model=AdjustmentOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": AdjustmentOut, "description": "Replay of an earlier request (same idempotency key)"}},
)
@domain_errors
def allocate_adjustment(
    adjustment_id: int, data: AllocationRequest, response: Response, db: Session = Depends(get_db)
) -> CustomerAdjustment:
    adjustment, created = services.allocate_adjustment(db, adjustment_id, data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return adjustment


# --- Reports ------------------------------------------------------------------------


@router.get("/reports/aging", response_model=AgingReport)
@domain_errors
def aging(
    as_of: date | None = None,
    buckets: list[int] | None = Query(default=None, description="Upper bounds in days, e.g. 30,60,90"),
    customer_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict:
    return reports.aging_report(db, as_of, buckets, customer_id)


@router.get("/reports/overdue", response_model=OverdueReport)
@domain_errors
def overdue(as_of: date | None = None, customer_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    return reports.overdue_invoices(db, as_of, customer_id)
