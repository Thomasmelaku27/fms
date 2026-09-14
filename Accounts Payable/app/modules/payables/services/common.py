"""Shared building blocks of the payables services.

Errors, lookups, account and vendor policy, row locking, conditional status
transitions and the accounting-period check. Nothing here commits.
"""

from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.models import Account, AccountType
from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    APPeriodLock,
    PaymentAllocation,
    SupplierBill,
    SupplierCredit,
    Vendor,
    VendorPayment,
)

ZERO = Decimal("0.00")


# --- errors -------------------------------------------------------------------------------------


class PayablesError(Exception):
    """Base class; ``status_code`` is the HTTP status the router maps it to."""

    status_code = 422


class NotFound(PayablesError):
    status_code = 404


class Conflict(PayablesError):
    """State conflict: wrong status for the operation, duplicate, lost race."""

    status_code = 409


class ValidationFailed(PayablesError):
    status_code = 422


class PeriodClosed(ValidationFailed):
    """The operation would write an accounting effect into a closed AP period."""

    def __init__(self, on: date, what: str) -> None:
        super().__init__(f"{what} dated {on.isoformat()} falls in a closed AP period")


class DuplicateInvoice(Conflict):
    def __init__(self, vendor_id: int, invoice_number: str) -> None:
        super().__init__(f"Invoice number {invoice_number!r} already exists for vendor {vendor_id}")


class DuplicateCredit(Conflict):
    def __init__(self, vendor_id: int, credit_number: str) -> None:
        super().__init__(f"Credit number {credit_number!r} already exists for vendor {vendor_id}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- lookups ------------------------------------------------------------------------------------


def get_vendor(db: Session, vendor_id: int) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise NotFound("Vendor not found")
    return vendor


def referenced_vendor(db: Session, vendor_id: int) -> Vendor:
    """Vendor named in a request body (422 rather than 404 when it does not exist)."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise ValidationFailed("Vendor does not exist")
    return vendor


def require_vendor_documents(vendor: Vendor, action: str) -> None:
    """New bills / credits and their approval need an active or on-hold vendor."""
    if not vendor.accepts_documents:
        raise ValidationFailed(f"Vendor {vendor.code} is {vendor.status.value}; cannot {action}")


def require_vendor_payments(vendor: Vendor, action: str) -> None:
    """Paying a vendor needs an *active* vendor (on-hold vendors receive no cash)."""
    if not vendor.accepts_payments:
        raise ValidationFailed(f"Vendor {vendor.code} is {vendor.status.value}; cannot {action}")


def get_bill(db: Session, bill_id: int) -> SupplierBill:
    bill = db.get(SupplierBill, bill_id)
    if bill is None:
        raise NotFound("Bill not found")
    return bill


def get_credit(db: Session, credit_id: int) -> SupplierCredit:
    credit = db.get(SupplierCredit, credit_id)
    if credit is None:
        raise NotFound("Credit not found")
    return credit


def get_payment(db: Session, payment_id: int) -> VendorPayment:
    payment = db.get(VendorPayment, payment_id)
    if payment is None:
        raise NotFound("Payment not found")
    return payment


def lock(db: Session, model, ids: Iterable[int]) -> dict[int, object]:
    """Load rows by id with ``FOR UPDATE`` (PostgreSQL; a no-op on SQLite)."""
    ids = list(ids)
    if not ids:
        return {}
    stmt = select(model).where(model.id.in_(ids)).with_for_update()
    return {row.id: row for row in db.scalars(stmt)}


def bill_query():
    """``select(SupplierBill)`` with everything the balance properties need eagerly loaded."""
    return select(SupplierBill).options(
        selectinload(SupplierBill.lines),
        selectinload(SupplierBill.allocations).selectinload(PaymentAllocation.payment),
        selectinload(SupplierBill.credit_applications),
        selectinload(SupplierBill.discounts),
        selectinload(SupplierBill.holds),
        selectinload(SupplierBill.duplicate_flags),
    )


# --- account policy -----------------------------------------------------------------------------


def account(db: Session, account_id: int, role: str) -> Account:
    row = db.get(Account, account_id)
    if row is None:
        raise ValidationFailed(f"{role} account {account_id} does not exist")
    if not row.is_active:
        raise ValidationFailed(f"{role} account {row.code} is inactive")
    return row


def require_payable_account(db: Session, account_id: int) -> Account:
    """AP control accounts must be active liability accounts."""
    row = account(db, account_id, "Payable")
    if row.type != AccountType.LIABILITY:
        raise ValidationFailed(f"Payable account {row.code} is not a liability account")
    return row


def require_cash_account(db: Session, account_id: int) -> Account:
    """Payments leave from active asset (cash / bank) accounts."""
    row = account(db, account_id, "Cash")
    if row.type != AccountType.ASSET:
        raise ValidationFailed(f"Cash account {row.code} is not an asset account")
    return row


def require_discount_account(db: Session, account_id: int) -> Account:
    """Discounts taken are income (or a contra-expense): revenue or expense accounts."""
    row = account(db, account_id, "Discount")
    if row.type not in (AccountType.REVENUE, AccountType.EXPENSE):
        raise ValidationFailed(f"Discount account {row.code} must be a revenue or expense account")
    return row


def require_line_accounts(db: Session, account_ids: Iterable[int]) -> None:
    wanted = set(account_ids)
    if not wanted:
        return
    rows = db.execute(
        select(Account.id, Account.code, Account.is_active).where(Account.id.in_(wanted))
    ).all()
    found = {row.id for row in rows}
    if missing := sorted(wanted - found):
        raise ValidationFailed(f"Unknown line account ids: {missing}")
    if inactive := sorted(row.code for row in rows if not row.is_active):
        raise ValidationFailed(f"Inactive line accounts: {', '.join(inactive)}")


# --- transitions ---------------------------------------------------------------------------------


def transition(db: Session, model, row, from_statuses, **values) -> None:
    """Conditional status update: a concurrent caller cannot apply the same transition twice.

    Issues ``UPDATE ... WHERE id = ? AND status IN (...)``; zero rows means the row
    changed underneath us, which is reported as a 409 after rolling back.
    """
    if not isinstance(from_statuses, (tuple, list)):
        from_statuses = (from_statuses,)
    result = db.execute(
        update(model).where(model.id == row.id, model.status.in_(from_statuses)).values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        expected = " / ".join(s.value for s in from_statuses)
        raise Conflict(f"{model.__name__} {row.id} is no longer {expected}")
    for name, value in values.items():
        setattr(row, name, value)


# --- accounting periods ---------------------------------------------------------------------------


def latest_ap_lock(db: Session) -> date | None:
    return db.scalar(
        select(APPeriodLock.closed_through).order_by(APPeriodLock.closed_through.desc())
    )


def assert_period_open(db: Session, on: date, what: str, integrations: Integrations) -> None:
    """Refuse an accounting effect dated inside a closed period.

    Takes the *shared* close lock for the rest of the transaction (PostgreSQL), so a
    period close cannot complete while this dated write is in flight and a close
    that already started blocks this write until it has committed (see
    ``services.closing``). Both the module's own AP locks and the (pluggable) GL
    period port are asked; the database re-checks the AP locks as a backstop.
    """
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text

        db.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": 0x41505F434C4F5345})
    closed_through = latest_ap_lock(db)
    if (closed_through is not None and on <= closed_through) or integrations.periods.is_closed(
        db, on
    ):
        raise PeriodClosed(on, what)
