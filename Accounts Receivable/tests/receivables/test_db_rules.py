"""AR rules enforced by the database itself (CHECK constraints and triggers).

Like ``tests/test_ledger_rules.py`` these bypass the API and write through the
ORM or raw SQL, so the rules are shown to hold for application bugs and manual
SQL alike, on SQLite and (when configured) PostgreSQL.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType, JournalEntry, JournalLine
from app.modules.receivables.models import (
    AdjustmentKind,
    Customer,
    CustomerAdjustment,
    CustomerPayment,
    InvoiceAllocation,
    InvoiceStatus,
    ReceivableSettings,
    SalesInvoice,
    SalesInvoiceLine,
)

D = Decimal


@pytest.fixture
def world(session):
    """Accounts, a customer, a posted journal entry to link to, and a helper to make invoices."""
    ar = Account(code="1200", name="AR", type=AccountType.ASSET)
    sales = Account(code="4000", name="Sales", type=AccountType.REVENUE)
    bank = Account(code="1000", name="Bank", type=AccountType.ASSET)
    customer = Customer(code="C1", name="Acme")
    session.add_all([ar, sales, bank, customer])
    session.commit()
    entry = JournalEntry(
        entry_date=date(2026, 1, 10), description="x",
        lines=[JournalLine(account_id=ar.id, debit=D("100.00")), JournalLine(account_id=sales.id, credit=D("100.00"))],
    )
    session.add(entry)
    session.commit()
    entry_id = entry.id  # captured: a later rollback expires ``entry`` (autoflush on refresh)

    def invoice(number="INV-1", status=InvoiceStatus.ISSUED, total="100.00", settled="0"):
        inv = SalesInvoice(
            customer_id=customer.id, invoice_number=number, invoice_date=date(2026, 1, 10),
            due_date=date(2026, 2, 9), subtotal=D(total), tax_total=D("0"), total=D(total),
            amount_settled=D(settled), status=InvoiceStatus.DRAFT,
            lines=[SalesInvoiceLine(line_no=1, description="w", quantity=D("1"), unit_price=D(total),
                                    revenue_account_id=sales.id, net_amount=D(total), tax_amount=D("0"), line_total=D(total))],
        )
        session.add(inv)
        session.commit()
        if status != InvoiceStatus.DRAFT:
            inv.status = status
            inv.journal_entry_id = entry_id
            session.commit()
        return inv

    def payment(amount="100.00", key="k1"):
        pay = CustomerPayment(customer_id=customer.id, payment_date=date(2026, 1, 20), amount=D(amount),
                              deposit_account_id=bank.id, idempotency_key=key, request_hash="h", journal_entry_id=entry_id)
        session.add(pay)
        session.commit()
        return pay

    def adjustment(amount="100.00"):
        adj = CustomerAdjustment(customer_id=customer.id, kind=AdjustmentKind.CREDIT, adjustment_date=date(2026, 1, 20),
                                 amount=D(amount), reason="r", journal_entry_id=entry_id)
        session.add(adj)
        session.commit()
        return adj

    return {"ar": ar, "sales": sales, "bank": bank, "customer": customer, "entry": entry,
            "invoice": invoice, "payment": payment, "adjustment": adjustment}


def assert_rejected(session, action, message_fragment):
    with pytest.raises(IntegrityError, match=message_fragment):
        action()
        session.commit()
    session.rollback()


# --- CHECK constraints ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("total", "-1", "ck_sales_invoices_non_negative"),
        ("subtotal", "99", "ck_sales_invoices_total_is_sum"),
        ("amount_settled", "100.01", "ck_sales_invoices_settled_within_total"),
        ("amount_settled", "-0.01", "ck_sales_invoices_settled_within_total"),
    ],
)
def test_invoice_amount_checks(session, world, column, value, constraint):
    inv = world["invoice"](status=InvoiceStatus.DRAFT)

    def raw_update():
        session.execute(text(f"UPDATE sales_invoices SET {column} = {value} WHERE id = :id"), {"id": inv.id})

    assert_rejected(session, raw_update, constraint)


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"quantity": "0"}, "ck_sales_invoice_lines_positive_quantity"),
        ({"unit_price": "-1"}, "ck_sales_invoice_lines_non_negative_price"),
        ({"tax_amount": "-1"}, "ck_sales_invoice_lines_non_negative"),
        ({"line_total": "99"}, "ck_sales_invoice_lines_total_is_sum"),
    ],
)
def test_invoice_line_checks(session, world, values, constraint):
    inv = world["invoice"](status=InvoiceStatus.DRAFT)
    fields = {"line_no": 2, "description": "x", "quantity": "1", "unit_price": "10", "net_amount": "10",
              "tax_amount": "0", "line_total": "10"} | values
    line = SalesInvoiceLine(invoice_id=inv.id, revenue_account_id=world["sales"].id,
                            **{k: (D(v) if k not in ("line_no", "description") else v) for k, v in fields.items()})
    assert_rejected(session, lambda: session.add(line), constraint)


def test_payment_and_adjustment_checks(session, world):
    assert_rejected(session, lambda: world["payment"]("0"), "ck_customer_payments_positive_amount")
    pay = world["payment"]("50.00")

    def over_allocate():
        session.execute(text("UPDATE customer_payments SET amount_allocated = 50.01 WHERE id = :id"), {"id": pay.id})

    assert_rejected(session, over_allocate, "ck_customer_payments_allocated_within_amount")
    assert_rejected(session, lambda: world["payment"]("1.00", key="k1"), "uq_customer_payments_idempotency_key|UNIQUE")
    # backends evaluate CHECKs in different orders; a negative amount violates both
    assert_rejected(session, lambda: world["adjustment"]("-5"), "ck_customer_adjustments_positive_amount|ck_customer_adjustments_allocated_within_amount")

    def bad_kind():
        session.execute(text("INSERT INTO customer_adjustments (customer_id, kind, adjustment_date, amount, amount_allocated, reason, journal_entry_id) "
                             "VALUES (:c, 'refund', '2026-01-01', 1, 0, 'r', :e)"), {"c": world["customer"].id, "e": world["entry"].id})

    assert_rejected(session, bad_kind, "ck_customer_adjustments_kind")


def test_allocation_must_have_exactly_one_source(session, world):
    inv = world["invoice"]()
    pay = world["payment"]()
    adj = world["adjustment"]()
    for payment_id, adjustment_id in ((None, None), (pay.id, adj.id)):
        assert_rejected(
            session,
            lambda p=payment_id, a=adjustment_id: session.add(InvoiceAllocation(
                invoice_id=inv.id, payment_id=p, adjustment_id=a, amount=D("1.00"),
                allocation_date=date(2026, 1, 20), effective_date=date(2026, 1, 20))),
            "ck_invoice_allocations_exactly_one_source",
        )
    assert_rejected(
        session,
        lambda: session.add(InvoiceAllocation(invoice_id=inv.id, payment_id=pay.id, amount=D("0"),
                                              allocation_date=date(2026, 1, 20), effective_date=date(2026, 1, 20))),
        "ck_invoice_allocations_positive_amount",
    )


def test_settings_is_a_single_row(session, world):
    a = world["ar"].id
    session.add(ReceivableSettings(id=1, receivable_account_id=a, tax_account_id=a, credit_account_id=a, write_off_account_id=a))
    session.commit()
    assert_rejected(
        session,
        lambda: session.add(ReceivableSettings(id=2, receivable_account_id=a, tax_account_id=a, credit_account_id=a, write_off_account_id=a)),
        "ck_receivable_settings_single_row",
    )


# --- Triggers: allocations ---------------------------------------------------------------


def _alloc(session, inv, amount, payment=None, adjustment=None):
    session.add(InvoiceAllocation(invoice_id=inv.id, payment_id=payment.id if payment else None,
                                  adjustment_id=adjustment.id if adjustment else None,
                                  amount=D(amount), allocation_date=date(2026, 1, 20),
                                  effective_date=date(2026, 1, 20)))


def test_allocations_cannot_exceed_invoice_total(session, world):
    inv = world["invoice"](total="100.00")
    pay = world["payment"]("500.00")
    _alloc(session, inv, "60.00", payment=pay)
    session.commit()
    assert_rejected(session, lambda: _alloc(session, inv, "40.01", payment=pay), "allocation exceeds the invoice total")
    _alloc(session, inv, "40.00", payment=pay)  # exactly the remainder is fine
    session.commit()
    assert_rejected(session, lambda: _alloc(session, inv, "0.01", payment=pay), "allocation exceeds the invoice total")


def test_allocation_sum_is_checked_in_exact_cents(session, world):
    """0.10 + 0.20 + 0.70 must equal 1.00 even where NUMERIC is stored as REAL."""
    inv = world["invoice"](total="1.00")
    pay = world["payment"]("5.00")
    for amount in ("0.10", "0.20", "0.70"):
        _alloc(session, inv, amount, payment=pay)
        session.commit()
    assert_rejected(session, lambda: _alloc(session, inv, "0.01", payment=pay), "allocation exceeds the invoice total")


def test_allocations_cannot_exceed_payment_or_adjustment_amount(session, world):
    a = world["invoice"]("A", total="100.00")
    b = world["invoice"]("B", total="100.00")
    pay = world["payment"]("80.00")
    _alloc(session, a, "50.00", payment=pay)
    session.commit()
    assert_rejected(session, lambda: _alloc(session, b, "30.01", payment=pay), "allocation exceeds the payment or adjustment amount")
    adj = world["adjustment"]("20.00")
    _alloc(session, a, "15.00", adjustment=adj)
    session.commit()
    assert_rejected(session, lambda: _alloc(session, b, "5.01", adjustment=adj), "allocation exceeds the payment or adjustment amount")


def test_allocation_to_draft_or_void_invoice_rejected(session, world):
    pay = world["payment"]()
    draft = world["invoice"]("D", status=InvoiceStatus.DRAFT)
    assert_rejected(session, lambda: _alloc(session, draft, "1.00", payment=pay), "cannot allocate to a draft or void invoice")
    void = world["invoice"]("V", status=InvoiceStatus.VOID)
    assert_rejected(session, lambda: _alloc(session, void, "1.00", payment=pay), "cannot allocate to a draft or void invoice")
    paid = world["invoice"]("P", status=InvoiceStatus.PAID, settled="0")  # status alone does not block; totals do
    _alloc(session, paid, "1.00", payment=pay)
    session.commit()


def test_allocations_are_append_only(session, world):
    inv = world["invoice"]()
    pay = world["payment"]()
    _alloc(session, inv, "10.00", payment=pay)
    session.commit()
    row = session.query(InvoiceAllocation).one()

    def change():
        row.amount = D("5.00")

    assert_rejected(session, change, "invoice allocations cannot be modified or deleted")
    assert_rejected(session, lambda: session.execute(text("DELETE FROM invoice_allocations")), "invoice allocations cannot be modified or deleted")
    assert session.query(InvoiceAllocation).count() == 1


# --- Triggers: issued invoices are frozen ---------------------------------------------------


def test_lines_of_issued_invoice_are_frozen(session, world):
    inv = world["invoice"]()
    line = inv.lines[0]

    def edit():
        line.unit_price = D("1.00")

    assert_rejected(session, edit, "lines of an issued invoice cannot be modified")
    assert_rejected(session, lambda: session.execute(text("DELETE FROM sales_invoice_lines WHERE invoice_id = :id"), {"id": inv.id}),
                    "lines of an issued invoice cannot be modified")
    assert_rejected(
        session,
        lambda: session.add(SalesInvoiceLine(invoice_id=inv.id, line_no=2, description="x", quantity=D("1"), unit_price=D("1"),
                                             revenue_account_id=world["sales"].id, net_amount=D("1"), tax_amount=D("0"), line_total=D("1"))),
        "lines of an issued invoice cannot be modified",
    )
    draft = world["invoice"]("D", status=InvoiceStatus.DRAFT)
    assert_rejected(
        session,
        lambda: session.execute(text("UPDATE sales_invoice_lines SET invoice_id = :issued WHERE invoice_id = :draft"),
                                {"issued": inv.id, "draft": draft.id}),
        "lines of an issued invoice cannot be modified",
    )
    session.expire_all()
    assert len(session.get(SalesInvoice, inv.id).lines) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sales_invoices SET total = 200, subtotal = 200 WHERE id = :id",
        "UPDATE sales_invoices SET invoice_number = 'OTHER' WHERE id = :id",
        "UPDATE sales_invoices SET invoice_date = '2030-01-01' WHERE id = :id",
        "UPDATE sales_invoices SET journal_entry_id = NULL WHERE id = :id",
        "UPDATE sales_invoices SET status = 'draft' WHERE id = :id",
        "DELETE FROM sales_invoices WHERE id = :id",
    ],
)
def test_issued_invoice_header_is_frozen(session, world, sql):
    inv = world["invoice"]()
    assert_rejected(session, lambda: session.execute(text(sql), {"id": inv.id}), "cannot be modified or deleted|cannot return to draft")
    session.expire_all()
    fresh = session.get(SalesInvoice, inv.id)
    assert fresh.total == D("100.00") and fresh.status == InvoiceStatus.ISSUED


def test_issued_invoice_still_allows_settlement_and_due_date_changes(session, world):
    inv = world["invoice"]()
    session.execute(text("UPDATE sales_invoices SET amount_settled = 40, status = 'partially_paid', due_date = '2026-03-01' WHERE id = :id"), {"id": inv.id})
    session.commit()
    session.expire_all()
    assert session.get(SalesInvoice, inv.id).status == InvoiceStatus.PARTIALLY_PAID


def test_void_invoice_is_terminal(session, world):
    inv = world["invoice"](status=InvoiceStatus.VOID)
    assert_rejected(session, lambda: session.execute(text("UPDATE sales_invoices SET status = 'issued' WHERE id = :id"), {"id": inv.id}),
                    "cannot be modified or deleted")
    assert_rejected(session, lambda: session.execute(text("UPDATE sales_invoices SET amount_settled = 1 WHERE id = :id"), {"id": inv.id}),
                    "cannot be modified or deleted")


def test_drafts_remain_editable_and_deletable(session, world):
    inv = world["invoice"](status=InvoiceStatus.DRAFT)
    inv.lines[0].unit_price = D("5.00")
    inv.lines.append(SalesInvoiceLine(line_no=2, description="y", quantity=D("1"), unit_price=D("1"),
                                      revenue_account_id=world["sales"].id, net_amount=D("1"), tax_amount=D("0"), line_total=D("1")))
    inv.invoice_number = "EDITED"
    session.commit()
    session.delete(inv)
    session.commit()
    assert session.get(SalesInvoice, inv.id) is None
    assert session.query(SalesInvoiceLine).count() == 0
