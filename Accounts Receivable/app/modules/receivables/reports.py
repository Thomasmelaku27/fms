"""Accounts Receivable reports: customer balances, statements, aging, overdue.

All reports are "as of" a date and are derived from the AR documents:

* an invoice counts from its ``invoice_date`` (the date of its ledger entry)
  until its ``void_date``; drafts and never-issued voids are ignored;
* an allocation reduces the invoice balance - and the unapplied credit of its
  payment/adjustment - from its ``effective_date``, the date it entered the
  books. Allocating an old payment today therefore leaves last month's figures
  exactly as they were reported then;
* payments and credits that are not (yet) allocated are *unapplied credit*;
* ``balance = outstanding invoices - unapplied credit``, which equals the
  customer's share of the posted receivables control account.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.receivables.models import (
    Customer,
    CustomerAdjustment,
    CustomerPayment,
    InvoiceAllocation,
    InvoiceStatus,
    SalesInvoice,
)

ZERO = Decimal("0")
DEFAULT_BUCKETS = (30, 60, 90)


class UnknownCustomer(Exception):
    pass


def _customer(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise UnknownCustomer(customer_id)
    return customer


# --- Building blocks -------------------------------------------------------------------


def invoice_balances(db: Session, as_of: date, customer_id: int | None = None) -> list[tuple[SalesInvoice, Decimal]]:
    """Issued invoices that existed as of ``as_of``, with the balance they had then.

    Only allocations in force on ``as_of`` (``effective_date <= as_of``) count.
    """
    settled = (
        select(InvoiceAllocation.invoice_id, func.sum(InvoiceAllocation.amount).label("settled"))
        .where(InvoiceAllocation.effective_date <= as_of)
        .group_by(InvoiceAllocation.invoice_id)
        .subquery()
    )
    stmt = (
        select(SalesInvoice, func.coalesce(settled.c.settled, 0))
        .outerjoin(settled, settled.c.invoice_id == SalesInvoice.id)
        .where(
            SalesInvoice.journal_entry_id.is_not(None),  # was issued
            SalesInvoice.invoice_date <= as_of,
            or_(SalesInvoice.status != InvoiceStatus.VOID, SalesInvoice.void_date > as_of),
        )
        .order_by(SalesInvoice.due_date, SalesInvoice.id)
    )
    if customer_id is not None:
        stmt = stmt.where(SalesInvoice.customer_id == customer_id)
    return [(invoice, invoice.total - Decimal(settled)) for invoice, settled in db.execute(stmt)]


def unapplied_credit(db: Session, as_of: date, customer_id: int | None = None) -> dict[int, Decimal]:
    """Unallocated payment and adjustment amounts per customer as of ``as_of``.

    Uses the same ``effective_date`` cut-off as ``invoice_balances``, so
    ``outstanding - unapplied credit`` stays equal to the posted receivables
    balance at any date.
    """
    result: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for model, link in ((CustomerPayment, InvoiceAllocation.payment_id), (CustomerAdjustment, InvoiceAllocation.adjustment_id)):
        date_col = model.payment_date if model is CustomerPayment else model.adjustment_date
        allocated = (
            select(link.label("source_id"), func.sum(InvoiceAllocation.amount).label("allocated"))
            .where(link.is_not(None), InvoiceAllocation.effective_date <= as_of)
            .group_by(link)
            .subquery()
        )
        stmt = (
            select(model.customer_id, model.amount, func.coalesce(allocated.c.allocated, 0))
            .outerjoin(allocated, allocated.c.source_id == model.id)
            .where(date_col <= as_of)
        )
        if customer_id is not None:
            stmt = stmt.where(model.customer_id == customer_id)
        for cid, amount, alloc in db.execute(stmt):
            result[cid] += amount - Decimal(alloc)
    return dict(result)


# --- Customer balance ---------------------------------------------------------------------


def customer_balance(db: Session, customer_id: int, as_of: date | None = None) -> dict:
    customer = _customer(db, customer_id)
    as_of = as_of or date.today()
    balances = [(inv, bal) for inv, bal in invoice_balances(db, as_of, customer.id) if bal > ZERO]
    outstanding = sum((bal for _, bal in balances), ZERO)
    overdue = sum((bal for inv, bal in balances if inv.due_date < as_of), ZERO)
    credit = unapplied_credit(db, as_of, customer.id).get(customer.id, ZERO)
    return {
        "customer_id": customer.id,
        "code": customer.code,
        "name": customer.name,
        "as_of": as_of,
        "open_invoices": len(balances),
        "outstanding": outstanding,
        "overdue": overdue,
        "unapplied_credit": credit,
        "balance": outstanding - credit,
    }


# --- Customer statement --------------------------------------------------------------------

_KIND_ORDER = {"invoice": 0, "payment": 1, "credit": 2, "write_off": 3, "void": 4}


def _statement_events(db: Session, customer_id: int) -> list[dict]:
    events: list[dict] = []
    invoices = db.scalars(
        select(SalesInvoice).where(
            SalesInvoice.customer_id == customer_id, SalesInvoice.journal_entry_id.is_not(None)
        )
    )
    for inv in invoices:
        events.append(
            {"entry_date": inv.invoice_date, "kind": "invoice", "reference": inv.invoice_number,
             "description": f"Invoice {inv.invoice_number} (due {inv.due_date.isoformat()})",
             "debit": inv.total, "credit": ZERO, "_id": inv.id}
        )
        if inv.status == InvoiceStatus.VOID and inv.void_date is not None:
            events.append(
                {"entry_date": inv.void_date, "kind": "void", "reference": inv.invoice_number,
                 "description": f"Void invoice {inv.invoice_number}", "debit": ZERO, "credit": inv.total, "_id": inv.id}
            )
    for pay in db.scalars(select(CustomerPayment).where(CustomerPayment.customer_id == customer_id)):
        events.append(
            {"entry_date": pay.payment_date, "kind": "payment", "reference": pay.reference or f"PAY-{pay.id}",
             "description": "Payment received" + (f" ({pay.reference})" if pay.reference else ""),
             "debit": ZERO, "credit": pay.amount, "_id": pay.id}
        )
    for adj in db.scalars(select(CustomerAdjustment).where(CustomerAdjustment.customer_id == customer_id)):
        events.append(
            {"entry_date": adj.adjustment_date, "kind": adj.kind.value, "reference": f"ADJ-{adj.id}",
             "description": ("Credit adjustment: " if adj.kind.value == "credit" else "Write-off: ") + adj.reason,
             "debit": ZERO, "credit": adj.amount, "_id": adj.id}
        )
    events.sort(key=lambda e: (e["entry_date"], _KIND_ORDER[e["kind"]], e["_id"]))
    return events


def customer_statement(db: Session, customer_id: int, start_date: date | None, end_date: date | None) -> dict:
    customer = _customer(db, customer_id)
    end_date = end_date or date.today()
    if start_date is not None and start_date > end_date:
        raise ValueError("start_date is after end_date")

    opening = ZERO
    lines: list[dict] = []
    running = ZERO
    for event in _statement_events(db, customer.id):
        if event["entry_date"] > end_date:
            continue
        if start_date is not None and event["entry_date"] < start_date:
            opening += event["debit"] - event["credit"]
            continue
        if not lines:
            running = opening
        running += event["debit"] - event["credit"]
        lines.append({k: v for k, v in event.items() if k != "_id"} | {"running_balance": running})
    closing = lines[-1]["running_balance"] if lines else opening
    return {
        "customer_id": customer.id,
        "code": customer.code,
        "name": customer.name,
        "start_date": start_date,
        "end_date": end_date,
        "opening_balance": opening,
        "lines": lines,
        "total_debits": sum((line["debit"] for line in lines), ZERO),
        "total_credits": sum((line["credit"] for line in lines), ZERO),
        "closing_balance": closing,
    }


# --- Aging and overdue ----------------------------------------------------------------------


def bucket_labels(bucket_days: list[int]) -> list[str]:
    labels = []
    lower = 1
    for upper in bucket_days:
        labels.append(f"{lower}-{upper}")
        lower = upper + 1
    labels.append(f"{bucket_days[-1]}+")
    return labels


def status_as_of(invoice: SalesInvoice, balance: Decimal) -> InvoiceStatus:
    """The status the invoice had at the reporting date, derived from its balance then.

    The stored ``status`` is the current one; an allocation made later must not
    make a past report describe the invoice as paid while showing it as
    outstanding.
    """
    if balance <= ZERO:
        return InvoiceStatus.PAID
    if balance < invoice.total:
        return InvoiceStatus.PARTIALLY_PAID
    return InvoiceStatus.ISSUED


def _bucket_index(days_overdue: int, bucket_days: list[int]) -> int:
    for index, upper in enumerate(bucket_days):
        if days_overdue <= upper:
            return index
    return len(bucket_days)


def aging_report(db: Session, as_of: date | None = None, bucket_days: list[int] | None = None, customer_id: int | None = None) -> dict:
    as_of = as_of or date.today()
    bucket_days = list(bucket_days or DEFAULT_BUCKETS)
    if any(d <= 0 for d in bucket_days) or bucket_days != sorted(set(bucket_days)):
        raise ValueError("bucket_days must be strictly increasing positive integers")
    if customer_id is not None:
        _customer(db, customer_id)

    n_buckets = len(bucket_days) + 1
    rows: dict[int, dict] = {}

    def row_for(cid: int) -> dict:
        if cid not in rows:
            rows[cid] = {"customer_id": cid, "current": ZERO, "buckets": [ZERO] * n_buckets,
                         "total_outstanding": ZERO, "unapplied_credit": ZERO}
        return rows[cid]

    for invoice, balance in invoice_balances(db, as_of, customer_id):
        if balance <= ZERO:
            continue
        row = row_for(invoice.customer_id)
        days_overdue = (as_of - invoice.due_date).days
        if days_overdue <= 0:
            row["current"] += balance
        else:
            row["buckets"][_bucket_index(days_overdue, bucket_days)] += balance
        row["total_outstanding"] += balance
    for cid, credit in unapplied_credit(db, as_of, customer_id).items():
        if credit != ZERO:
            row_for(cid)["unapplied_credit"] += credit

    customers = {c.id: c for c in db.scalars(select(Customer).where(Customer.id.in_(rows)))} if rows else {}
    result_rows = []
    for cid in sorted(rows, key=lambda c: customers[c].code):
        row = rows[cid]
        row["code"] = customers[cid].code
        row["name"] = customers[cid].name
        row["balance"] = row["total_outstanding"] - row["unapplied_credit"]
        result_rows.append(row)

    totals = {"customer_id": 0, "code": "*", "name": "Total", "current": ZERO,
              "buckets": [ZERO] * n_buckets, "total_outstanding": ZERO, "unapplied_credit": ZERO, "balance": ZERO}
    for row in result_rows:
        totals["current"] += row["current"]
        totals["buckets"] = [a + b for a, b in zip(totals["buckets"], row["buckets"], strict=True)]
        for key in ("total_outstanding", "unapplied_credit", "balance"):
            totals[key] += row[key]
    return {"as_of": as_of, "bucket_days": bucket_days, "bucket_labels": bucket_labels(bucket_days),
            "rows": result_rows, "totals": totals}


def overdue_invoices(db: Session, as_of: date | None = None, customer_id: int | None = None) -> dict:
    as_of = as_of or date.today()
    if customer_id is not None:
        _customer(db, customer_id)
    items = []
    for invoice, balance in invoice_balances(db, as_of, customer_id):
        if balance <= ZERO or invoice.due_date >= as_of:
            continue
        items.append(
            {"invoice_id": invoice.id, "invoice_number": invoice.invoice_number, "customer_id": invoice.customer_id,
             "customer_code": invoice.customer.code, "customer_name": invoice.customer.name,
             "invoice_date": invoice.invoice_date, "due_date": invoice.due_date,
             "days_overdue": (as_of - invoice.due_date).days, "total": invoice.total,
             "balance_due": balance, "status": status_as_of(invoice, balance)}
        )
    items.sort(key=lambda i: (-i["days_overdue"], i["customer_code"], i["invoice_id"]))
    return {"as_of": as_of, "invoices": items, "total_overdue": sum((i["balance_due"] for i in items), ZERO)}


