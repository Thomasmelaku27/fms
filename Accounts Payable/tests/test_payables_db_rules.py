"""AP rules enforced by the database itself (UNIQUE / CHECK constraints and triggers).

These tests bypass the API and the service layer (ORM writes and raw SQL) to
prove the rules hold for application bugs and manual SQL alike, on SQLite and,
when configured, PostgreSQL. Messages are identical on both backends.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType
from app.modules.payables.models import (
    BillPaymentSchedule,
    BillStatus,
    PaymentAllocation,
    PaymentMethod,
    SupplierBill,
    SupplierBillLine,
    Vendor,
    VendorPayment,
)


@pytest.fixture
def world(session):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    vendor = Vendor(code="V1", name="Vendor")
    session.add_all([ap, expense, cash, vendor])
    session.commit()
    return {"ap": ap, "expense": expense, "cash": cash, "vendor": vendor}


STATUS_PATH = {
    BillStatus.DRAFT: (),
    BillStatus.REJECTED: ("rejected",),
    BillStatus.APPROVED: ("approved",),
    BillStatus.PARTIALLY_PAID: ("approved", "partially_paid"),
    BillStatus.PAID: ("approved", "paid"),
}


def make_bill(session, world, number="INV-1", status=BillStatus.DRAFT, total="100.00", lines=True):
    """Create a draft with one line, then walk the legal transitions to ``status``."""
    bill = SupplierBill(
        vendor_id=world["vendor"].id,
        invoice_number=number,
        invoice_date=date(2026, 1, 10),
        due_date=date(2026, 2, 9),
        total=Decimal(total),
        payable_account_id=world["ap"].id,
        lines=[
            SupplierBillLine(
                line_no=1,
                description="x",
                account_id=world["expense"].id,
                quantity=Decimal("1"),
                unit_price=Decimal(total),
                line_total=Decimal(total),
            )
        ]
        if lines
        else [],
    )
    session.add(bill)
    session.commit()
    for step in STATUS_PATH[status]:
        session.execute(
            text("UPDATE supplier_bills SET status = :s WHERE id = :id"), {"s": step, "id": bill.id}
        )
        session.commit()
    session.refresh(bill)
    return bill


def make_payment(session, world, amount="40.00", allocations=()):
    payment = VendorPayment(
        vendor_id=world["vendor"].id,
        payment_date=date(2026, 1, 20),
        amount=Decimal(amount),
        method=PaymentMethod.CASH,
        cash_account_id=world["cash"].id,
        recorded_by="test",
        allocations=[
            PaymentAllocation(bill_id=b, amount=Decimal(a), allocation_date=date(2026, 1, 20), allocated_by="test")
            for b, a in allocations
        ],
    )
    session.add(payment)
    session.commit()
    return payment


def assert_rejected(session, action, message_fragment):
    with pytest.raises(IntegrityError, match=message_fragment):
        action()
        session.commit()
    session.rollback()


# --- duplicate supplier invoices -----------------------------------------------------------


def test_duplicate_invoice_number_rejected_even_when_spelled_differently(session, world):
    make_bill(session, world, "INV-1")
    assert_rejected(session, lambda: make_bill(session, world, "INV-1"), "supplier_bills")
    assert_rejected(session, lambda: make_bill(session, world, " inv-1 "), "invoice_number_normalized")


def test_normalized_unique_constraint_applies_to_raw_sql(session, world):
    make_bill(session, world, "INV-1")

    def raw_insert():
        session.execute(
            text(
                "INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, "
                "invoice_date, due_date, total, status) "
                "VALUES (:v, 'inv-1', 'INV-1', '2026-01-10', '2026-02-09', 5, 'draft')"
            ),
            {"v": world["vendor"].id},
        )

    assert_rejected(session, raw_insert, "invoice_number_normalized")


def test_same_invoice_number_allowed_for_another_vendor(session, world):
    make_bill(session, world, "INV-1")
    other = Vendor(code="V2", name="Other")
    session.add(other)
    session.commit()
    session.add(
        SupplierBill(
            vendor_id=other.id,
            invoice_number="inv-1",
            invoice_date=date(2026, 1, 10),
            due_date=date(2026, 2, 9),
            total=Decimal("1.00"),
        )
    )
    session.commit()


# --- CHECK constraints ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("total", "0", "ck_supplier_bills_total_positive"),
        ("total", "-1.00", "ck_supplier_bills_total_positive"),
        ("status", "bogus", "ck_supplier_bills_status_valid"),
        ("due_date", "2026-01-01", "ck_supplier_bills_due_date_after_invoice_date"),
    ],
)
def test_bill_check_constraints(session, world, field, value, constraint):
    def raw_insert():
        session.execute(
            text(
                "INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, "
                "invoice_date, due_date, total, status) VALUES (:v, 'X', 'X', '2026-01-10', "
                ":due_date, :total, :status)"
            ),
            {
                "v": world["vendor"].id,
                "due_date": value if field == "due_date" else "2026-02-09",
                "total": value if field == "total" else "10.00",
                "status": value if field == "status" else "draft",
            },
        )

    assert_rejected(session, raw_insert, constraint)


@pytest.mark.parametrize(
    ("quantity", "unit_price", "line_total", "constraint"),
    [
        ("0", "1", "1.00", "ck_supplier_bill_lines_quantity_positive"),
        ("1", "0", "1.00", "ck_supplier_bill_lines_unit_price_positive"),
        ("1", "1", "0", "ck_supplier_bill_lines_line_total_positive"),
    ],
)
def test_line_check_constraints(session, world, quantity, unit_price, line_total, constraint):
    bill = make_bill(session, world, lines=False)

    def bad_line():
        session.add(
            SupplierBillLine(
                bill_id=bill.id,
                line_no=1,
                description="bad",
                account_id=world["expense"].id,
                quantity=Decimal(quantity),
                unit_price=Decimal(unit_price),
                line_total=Decimal(line_total),
            )
        )

    assert_rejected(session, bad_line, constraint)


def test_sub_cent_amounts_rejected_on_sqlite(session, world):
    """PostgreSQL NUMERIC(18, 2) enforces the scale itself; SQLite needs the CHECK."""
    if session.bind.dialect.name != "sqlite":
        pytest.skip("scale is enforced by the column type on PostgreSQL")
    assert_rejected(
        session, lambda: make_bill(session, world, total="10.005"), "whole_cents"
    )


def test_payment_and_allocation_amounts_must_be_positive(session, world):
    bill = make_bill(session, world, status=BillStatus.APPROVED)
    assert_rejected(session, lambda: make_payment(session, world, amount="0"), "amount_positive")
    assert_rejected(
        session,
        lambda: make_payment(session, world, amount="1.00", allocations=[(bill.id, "-1.00")]),
        "ck_payment_allocations_amount_positive",
    )


# --- bill workflow triggers ------------------------------------------------------------------


def set_status(session, bill, status):
    def action():
        session.execute(
            text("UPDATE supplier_bills SET status = :s WHERE id = :id"),
            {"s": status, "id": bill.id},
        )

    return action


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("draft", "paid"),
        ("draft", "partially_paid"),
        ("approved", "draft"),
        ("approved", "rejected"),
        ("rejected", "approved"),
        ("rejected", "paid"),
        ("partially_paid", "draft"),
        ("partially_paid", "rejected"),
        ("paid", "draft"),
        ("paid", "rejected"),
    ],
)
def test_invalid_status_transitions_rejected(session, world, from_status, to_status):
    bill = make_bill(session, world, status=BillStatus(from_status))
    assert_rejected(session, set_status(session, bill, to_status), "invalid supplier bill status transition")
    session.expire_all()
    assert session.get(SupplierBill, bill.id).status.value == from_status


def test_valid_status_path_accepted(session, world):
    """Settlement states move in both directions (credits can be unapplied, payments reversed)."""
    bill = make_bill(session, world)
    for status in (
        "rejected", "draft", "approved", "partially_paid", "paid",
        "partially_paid", "approved", "paid", "approved", "paid",
    ):
        set_status(session, bill, status)()
        session.commit()
    session.expire_all()
    assert session.get(SupplierBill, bill.id).status == BillStatus.PAID


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE supplier_bills SET total = 1 WHERE id = :id",
        "UPDATE supplier_bills SET invoice_number = 'OTHER', invoice_number_normalized = 'OTHER' WHERE id = :id",
        "UPDATE supplier_bills SET invoice_date = '2025-01-01' WHERE id = :id",
        "UPDATE supplier_bills SET due_date = '2027-01-01' WHERE id = :id",
        "UPDATE supplier_bills SET payable_account_id = NULL WHERE id = :id",
        "UPDATE supplier_bills SET vendor_id = vendor_id + 1 WHERE id = :id",
    ],
)
def test_financial_fields_frozen_once_approved(session, world, sql):
    bill = make_bill(session, world, status=BillStatus.APPROVED)
    session.add(Vendor(code="V2", name="Other"))
    session.commit()
    assert_rejected(
        session,
        lambda: session.execute(text(sql), {"id": bill.id}),
        "financial fields of a non-draft supplier bill cannot be modified",
    )


def test_non_financial_fields_editable_after_approval(session, world):
    bill = make_bill(session, world, status=BillStatus.APPROVED)
    bill.description = "note added later"
    session.commit()


def test_draft_fields_editable(session, world):
    bill = make_bill(session, world)
    bill.total = Decimal("100.00")
    bill.lines[0].line_total = Decimal("100.00")
    bill.lines[0].unit_price = Decimal("100.00")
    bill.invoice_number = "INV-1b"
    session.commit()
    session.expire_all()
    assert session.get(SupplierBill, bill.id).invoice_number_normalized == "INV-1B"


@pytest.mark.parametrize("status", ["approved", "rejected", "partially_paid", "paid"])
def test_only_draft_bills_can_be_deleted(session, world, status):
    bill = make_bill(session, world, status=BillStatus(status), lines=False)
    assert_rejected(
        session,
        lambda: session.execute(text("DELETE FROM supplier_bills WHERE id = :id"), {"id": bill.id}),
        "only draft supplier bills can be deleted",
    )


def test_draft_bill_delete_cascades_to_lines(session, world):
    bill = make_bill(session, world)
    session.delete(bill)
    session.commit()
    assert session.query(SupplierBillLine).count() == 0


@pytest.mark.parametrize("status", ["approved", "rejected", "partially_paid", "paid"])
def test_lines_frozen_unless_draft(session, world, status):
    bill = make_bill(session, world, status=BillStatus(status))
    msg = "lines of a non-draft supplier bill cannot be modified"
    line_id = bill.lines[0].id

    assert_rejected(
        session,
        lambda: session.execute(
            text("UPDATE supplier_bill_lines SET line_total = 1 WHERE id = :id"), {"id": line_id}
        ),
        msg,
    )
    assert_rejected(
        session,
        lambda: session.execute(text("DELETE FROM supplier_bill_lines WHERE id = :id"), {"id": line_id}),
        msg,
    )
    assert_rejected(
        session,
        lambda: session.add(
            SupplierBillLine(
                bill_id=bill.id,
                line_no=2,
                description="extra",
                account_id=world["expense"].id,
                quantity=1,
                unit_price=1,
                line_total=1,
            )
        ),
        msg,
    )


# --- payment schedules ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "rejected", "paid"])
def test_schedule_only_while_approved_and_unpaid(session, world, status):
    bill = make_bill(session, world, status=BillStatus(status))
    assert_rejected(
        session,
        lambda: session.add(
            BillPaymentSchedule(bill_id=bill.id, installment_no=1, due_date=bill.due_date, amount=bill.total)
        ),
        "payment schedule can only change while the bill is approved and unpaid",
    )


def test_schedule_frozen_once_paid(session, world):
    bill = make_bill(session, world, status=BillStatus.APPROVED)
    session.add(BillPaymentSchedule(bill_id=bill.id, installment_no=1, due_date=bill.due_date, amount=bill.total))
    session.commit()
    make_payment(session, world, amount="100.00", allocations=[(bill.id, "100.00")])
    set_status(session, bill, "paid")()
    session.commit()
    assert_rejected(
        session,
        lambda: session.execute(text("DELETE FROM bill_payment_schedules WHERE bill_id = :id"), {"id": bill.id}),
        "payment schedule can only change",
    )


# --- payments and allocations ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "rejected", "paid"])
def test_allocation_requires_open_bill(session, world, status):
    bill = make_bill(session, world, status=BillStatus(status))
    assert_rejected(
        session,
        lambda: make_payment(session, world, amount="10.00", allocations=[(bill.id, "10.00")]),
        "payments can only be allocated to approved or partially paid bills",
    )
    assert session.query(VendorPayment).count() == 0


def test_allocations_cannot_exceed_bill_total(session, world):
    bill = make_bill(session, world, status=BillStatus.APPROVED, total="100.00")
    make_payment(session, world, amount="60.00", allocations=[(bill.id, "60.00")])
    assert_rejected(
        session,
        lambda: make_payment(session, world, amount="40.01", allocations=[(bill.id, "40.01")]),
        "allocation exceeds the outstanding amount of the bill",
    )
    # Exactly the remainder is fine (exact cents, no float tolerance).
    make_payment(session, world, amount="40.00", allocations=[(bill.id, "40.00")])
    assert_rejected(
        session,
        lambda: make_payment(session, world, amount="0.01", allocations=[(bill.id, "0.01")]),
        "allocation exceeds the outstanding amount of the bill",
    )


def test_overpay_guard_sums_cents_exactly(session, world):
    """0.10 + 0.20 + 0.70 must equal 1.00 even on SQLite's floating-point storage."""
    bill = make_bill(session, world, status=BillStatus.APPROVED, total="1.00")
    for amount in ("0.10", "0.20", "0.70"):
        make_payment(session, world, amount=amount, allocations=[(bill.id, amount)])
    assert_rejected(
        session,
        lambda: make_payment(session, world, amount="0.01", allocations=[(bill.id, "0.01")]),
        "allocation exceeds",
    )


def test_payments_and_allocations_are_append_only(session, world):
    bill = make_bill(session, world, status=BillStatus.APPROVED)
    payment = make_payment(session, world, amount="40.00", allocations=[(bill.id, "40.00")])
    allocation_id = payment.allocations[0].id

    for sql, msg in [
        ("UPDATE payment_allocations SET amount = 1 WHERE id = :id", "payment allocations cannot be modified or deleted"),
        ("DELETE FROM payment_allocations WHERE id = :id", "payment allocations cannot be modified or deleted"),
    ]:
        assert_rejected(session, lambda: session.execute(text(sql), {"id": allocation_id}), msg)

    for sql in [
        "UPDATE vendor_payments SET amount = 1 WHERE id = :id",
        "UPDATE vendor_payments SET payment_date = '2030-01-01' WHERE id = :id",
        "UPDATE vendor_payments SET cash_account_id = cash_account_id + 1 WHERE id = :id",
        "DELETE FROM vendor_payments WHERE id = :id",
    ]:
        assert_rejected(
            session,
            lambda: session.execute(text(sql), {"id": payment.id}),
            "a recorded vendor payment cannot be modified or deleted",
        )

    # Since fmspayables03 even the descriptive fields are frozen (corrections are explicit
    # reversal / replacement workflows); the ledger link may be set once but not changed.
    assert_rejected(
        session,
        lambda: session.execute(text("UPDATE vendor_payments SET reference = 'late' WHERE id = :id"), {"id": payment.id}),
        "a recorded vendor payment cannot be modified or deleted",
    )
    session.execute(text("UPDATE vendor_payments SET journal_entry_id = NULL WHERE id = :id"), {"id": payment.id})
    session.commit()


def test_payment_ledger_link_set_once(session, world):
    from app.models import JournalEntry

    bill = make_bill(session, world, status=BillStatus.APPROVED)
    payment = make_payment(session, world, amount="40.00", allocations=[(bill.id, "40.00")])
    first = JournalEntry(entry_date=date(2026, 1, 20), description="first")
    second = JournalEntry(entry_date=date(2026, 1, 20), description="second")
    session.add_all([first, second])
    session.commit()

    payment.journal_entry_id = first.id
    session.commit()
    assert_rejected(
        session,
        lambda: session.execute(
            text("UPDATE vendor_payments SET journal_entry_id = :e WHERE id = :id"),
            {"e": second.id, "id": payment.id},
        ),
        "a recorded vendor payment cannot be modified or deleted",
    )
