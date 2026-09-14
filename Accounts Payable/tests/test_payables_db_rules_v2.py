"""Database rules added by migration ``fmspayables02`` (SQLite and PostgreSQL).

* the canonical invoice / credit number is computed by the database itself;
* supplier credits, credit lines and credit applications are guarded like bills;
* payments can be reversed exactly once and only with a complete reversal record;
* the allocation ceiling ignores reversed payments and includes credit applications;
* the downgrade restores the ``fmspayables01`` trigger set.

Everything here goes through raw SQL or the ORM, never through the service layer.
"""

from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType, JournalEntry
from app.modules.payables.models import (
    BillStatus,
    CreditApplication,
    CreditKind,
    CreditStatus,
    PaymentAllocation,
    PaymentMethod,
    SupplierBill,
    SupplierBillLine,
    SupplierCredit,
    SupplierCreditLine,
    Vendor,
    VendorPayment,
    normalize_invoice_number,
    sql_normalize,
)
from tests.conftest import alembic_config, migrate


@pytest.fixture
def world(session):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    vendor = Vendor(code="V1", name="Vendor")
    session.add_all([ap, expense, cash, vendor])
    session.commit()
    return {"ap": ap, "expense": expense, "cash": cash, "vendor": vendor}


def set_status(session, table, row_id, status):
    session.execute(text(f"UPDATE {table} SET status = :s WHERE id = :id"), {"s": status, "id": row_id})
    session.commit()


def make_bill(session, world, number="INV-1", status="approved", total="100.00"):
    bill = SupplierBill(
        vendor_id=world["vendor"].id, invoice_number=number, invoice_date=date(2026, 1, 10),
        due_date=date(2026, 2, 9), total=Decimal(total), payable_account_id=world["ap"].id,
        lines=[SupplierBillLine(line_no=1, description="x", account_id=world["expense"].id,
                                quantity=Decimal("1"), unit_price=Decimal(total), line_total=Decimal(total))],
    )
    session.add(bill)
    session.commit()
    path = {"draft": (), "rejected": ("rejected",), "approved": ("approved",),
            "partially_paid": ("approved", "partially_paid"), "paid": ("approved", "paid")}[status]
    for step in path:
        set_status(session, "supplier_bills", bill.id, step)
    session.refresh(bill)
    return bill


def make_credit(session, world, number="CN-1", status="approved", total="30.00"):
    credit = SupplierCredit(
        vendor_id=world["vendor"].id, kind=CreditKind.CREDIT_NOTE, credit_number=number,
        credit_date=date(2026, 1, 12), total=Decimal(total), payable_account_id=world["ap"].id,
        lines=[SupplierCreditLine(line_no=1, description="c", account_id=world["expense"].id,
                                  quantity=Decimal("1"), unit_price=Decimal(total), line_total=Decimal(total))],
    )
    session.add(credit)
    session.commit()
    path = {"draft": (), "rejected": ("rejected",), "approved": ("approved",),
            "partially_applied": ("approved", "partially_applied"), "applied": ("approved", "applied")}[status]
    for step in path:
        set_status(session, "supplier_credits", credit.id, step)
    session.refresh(credit)
    return credit


def make_payment(session, world, amount, allocations):
    payment = VendorPayment(
        vendor_id=world["vendor"].id, payment_date=date(2026, 1, 20), amount=Decimal(amount),
        method=PaymentMethod.CASH, cash_account_id=world["cash"].id, recorded_by="test",
        allocations=[
            PaymentAllocation(bill_id=b, amount=Decimal(a), allocation_date=date(2026, 1, 20), allocated_by="test")
            for b, a in allocations
        ],
    )
    session.add(payment)
    session.commit()
    return payment


def make_entry(session):
    entry = JournalEntry(entry_date=date(2026, 1, 20), description="x")
    session.add(entry)
    session.commit()
    return entry


def apply_credit(session, credit_id, bill_id, amount):
    session.add(CreditApplication(credit_id=credit_id, bill_id=bill_id, amount=Decimal(amount),
                                  application_date=date(2026, 1, 21), applied_by="test"))
    session.commit()


def assert_rejected(session, action, message_fragment):
    with pytest.raises(IntegrityError, match=message_fragment):
        action()
        session.commit()
    session.rollback()


# --- canonical numbers are computed by the database -------------------------------------------

RAW_BILL = (
    "INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, invoice_date, "
    "due_date, total, status) VALUES (:v, :n, :nn, '2026-01-10', '2026-02-09', 10, 'draft')"
)
RAW_CREDIT = (
    "INSERT INTO supplier_credits (vendor_id, kind, credit_number, credit_number_normalized, credit_date, "
    "total, status, created_at, updated_at) VALUES (:v, 'credit_note', :n, :nn, '2026-01-10', 10, 'draft', "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


@pytest.mark.parametrize(
    ("table", "sql", "number_col", "normalized_col"),
    [
        ("supplier_bills", RAW_BILL, "invoice_number", "invoice_number_normalized"),
        ("supplier_credits", RAW_CREDIT, "credit_number", "credit_number_normalized"),
    ],
)
def test_wrong_normalized_value_is_corrected_on_insert(session, world, table, sql, number_col, normalized_col):
    session.execute(text(sql), {"v": world["vendor"].id, "n": " inv-7\t x ", "nn": "WRONG"})
    session.commit()
    stored = session.execute(text(f"SELECT {normalized_col} FROM {table}")).scalar()
    assert stored == "INV-7 X" == normalize_invoice_number(" inv-7\t x ")


@pytest.mark.parametrize(
    ("table", "sql", "normalized_col"),
    [
        ("supplier_bills", RAW_BILL, "invoice_number_normalized"),
        ("supplier_credits", RAW_CREDIT, "credit_number_normalized"),
    ],
)
def test_duplicate_with_wrong_normalized_value_is_refused(session, world, table, sql, normalized_col):
    session.execute(text(sql), {"v": world["vendor"].id, "n": "INV-1", "nn": "INV-1"})
    session.commit()
    for attempt in ("inv-1", " INV-1 ", "Inv-1"):
        assert_rejected(
            session,
            lambda: session.execute(text(sql), {"v": world["vendor"].id, "n": attempt, "nn": "SOMETHING-ELSE"}),
            normalized_col,
        )
    assert session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == 1


def test_wrong_normalized_value_is_corrected_on_update(session, world):
    bill = make_bill(session, world, status="draft")
    other = make_bill(session, world, number="INV-2", status="draft")
    session.execute(
        text("UPDATE supplier_bills SET invoice_number = 'inv-3', invoice_number_normalized = 'NOPE' WHERE id = :id"),
        {"id": bill.id},
    )
    session.commit()
    assert session.execute(text("SELECT invoice_number_normalized FROM supplier_bills WHERE id = :id"),
                           {"id": bill.id}).scalar() == "INV-3"
    # Renaming onto another bill's number is refused even with a misleading canonical value.
    assert_rejected(
        session,
        lambda: session.execute(
            text("UPDATE supplier_bills SET invoice_number = ' inv-2 ', invoice_number_normalized = 'FREE' WHERE id = :id"),
            {"id": bill.id}),
        "invoice_number_normalized",
    )
    assert session.get(SupplierBill, other.id).invoice_number_normalized == "INV-2"
    # Fixing only the canonical column is overridden too.
    session.execute(text("UPDATE supplier_bills SET invoice_number_normalized = 'x' WHERE id = :id"), {"id": bill.id})
    session.commit()
    assert session.execute(text("SELECT invoice_number_normalized FROM supplier_bills WHERE id = :id"),
                           {"id": bill.id}).scalar() == "INV-3"


@pytest.mark.parametrize(
    "raw",
    ["INV-1", " inv-1 ", "inv\t1", "a  b   c", "\nx\r\n", "MiXeD-Case 007", "tab\tand\nnewline"],
)
def test_python_and_sql_normalization_agree(session, raw):
    from sqlalchemy import literal, select

    assert session.scalar(select(sql_normalize(literal(raw)))) == normalize_invoice_number(raw)


# --- credits: workflow guards ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [("draft", "applied"), ("draft", "partially_applied"), ("draft", "reversed"), ("approved", "draft"),
     ("approved", "rejected"), ("rejected", "approved"), ("rejected", "applied"), ("partially_applied", "reversed"),
     ("applied", "reversed"), ("applied", "draft")],
)
def test_invalid_credit_transitions_rejected(session, world, from_status, to_status):
    credit = make_credit(session, world, status=from_status)
    if to_status == "reversed":
        # Supply a complete reversal record so the transition rule is the only one violated.
        entry = make_entry(session)

        def action():
            session.execute(text(
                "UPDATE supplier_credits SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
                "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": credit.id})
    else:
        def action():
            set_status(session, "supplier_credits", credit.id, to_status)

    assert_rejected(session, action, "invalid supplier credit status transition")


def test_credit_settlement_states_move_freely(session, world):
    credit = make_credit(session, world)
    for status in ("partially_applied", "applied", "partially_applied", "approved", "applied", "approved"):
        set_status(session, "supplier_credits", credit.id, status)
    session.expire_all()
    assert session.get(SupplierCredit, credit.id).status == CreditStatus.APPROVED


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE supplier_credits SET total = 1 WHERE id = :id",
        "UPDATE supplier_credits SET credit_number = 'X', credit_number_normalized = 'X' WHERE id = :id",
        "UPDATE supplier_credits SET credit_date = '2025-01-01' WHERE id = :id",
        "UPDATE supplier_credits SET kind = 'adjustment' WHERE id = :id",
        "UPDATE supplier_credits SET payable_account_id = NULL WHERE id = :id",
    ],
)
def test_credit_financial_fields_frozen_once_approved(session, world, sql):
    credit = make_credit(session, world)
    assert_rejected(session, lambda: session.execute(text(sql), {"id": credit.id}),
                    "financial fields of a non-draft supplier credit cannot be modified")


@pytest.mark.parametrize("status", ["approved", "rejected", "applied"])
def test_only_draft_credits_can_be_deleted_and_lines_are_frozen(session, world, status):
    credit = make_credit(session, world, status=status)
    assert_rejected(session, lambda: session.execute(text("DELETE FROM supplier_credits WHERE id = :id"), {"id": credit.id}),
                    "only draft supplier credits can be deleted")
    assert_rejected(session, lambda: session.execute(text("UPDATE supplier_credit_lines SET line_total = 1 WHERE credit_id = :id"),
                                                     {"id": credit.id}),
                    "lines of a non-draft supplier credit cannot be modified")
    assert_rejected(session, lambda: session.add(SupplierCreditLine(credit_id=credit.id, line_no=2, description="e",
                                                                     account_id=world["expense"].id, quantity=1,
                                                                     unit_price=1, line_total=1)),
                    "lines of a non-draft supplier credit cannot be modified")


def test_draft_credit_delete_cascades_to_lines(session, world):
    credit = make_credit(session, world, status="draft")
    session.delete(credit)
    session.commit()
    assert session.query(SupplierCreditLine).count() == 0


def test_credit_reversal_record_must_be_complete_and_set_once(session, world):
    credit = make_credit(session, world)
    entry = make_entry(session)
    msg = "a credit reversal must set actor, date and reversing entry together, exactly once"
    assert_rejected(session, lambda: set_status(session, "supplier_credits", credit.id, "reversed"), msg)
    assert_rejected(session, lambda: session.execute(
        text("UPDATE supplier_credits SET reversed_by = 'x' WHERE id = :id"), {"id": credit.id}), msg)
    session.execute(text(
        "UPDATE supplier_credits SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
        "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": credit.id})
    session.commit()
    assert_rejected(session, lambda: session.execute(
        text("UPDATE supplier_credits SET reversal_date = '2026-03-01' WHERE id = :id"), {"id": credit.id}), msg)
    # Un-reversing violates the transition rule and the set-once rule (trigger order is unspecified).
    assert_rejected(session, lambda: set_status(session, "supplier_credits", credit.id, "approved"),
                    "invalid supplier credit status transition|a credit reversal must set")


def test_applied_credit_cannot_be_reversed(session, world):
    bill = make_bill(session, world)
    credit = make_credit(session, world)
    apply_credit(session, credit.id, bill.id, "10.00")
    entry = make_entry(session)
    assert_rejected(session, lambda: session.execute(text(
        "UPDATE supplier_credits SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
        "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": credit.id}),
        "a supplier credit with applications cannot be reversed")


# --- credit applications ---------------------------------------------------------------------------


@pytest.mark.parametrize("bill_status", ["draft", "rejected", "paid"])
def test_application_requires_open_bill(session, world, bill_status):
    bill = make_bill(session, world, status=bill_status)
    credit = make_credit(session, world)
    assert_rejected(session, lambda: apply_credit(session, credit.id, bill.id, "10.00"),
                    "credits can only be applied to approved or partially paid bills")


@pytest.mark.parametrize("credit_status", ["draft", "rejected", "applied"])
def test_application_requires_open_credit(session, world, credit_status):
    bill = make_bill(session, world)
    credit = make_credit(session, world, status=credit_status)
    assert_rejected(session, lambda: apply_credit(session, credit.id, bill.id, "10.00"),
                    "only approved or partially applied credits can be applied")


def test_application_cannot_exceed_credit_or_bill(session, world):
    small = make_bill(session, world, number="SMALL", total="20.00")
    big = make_bill(session, world, number="BIG", total="500.00")
    credit = make_credit(session, world, total="30.00")
    assert_rejected(session, lambda: apply_credit(session, credit.id, small.id, "20.01"),
                    "credit application exceeds the outstanding amount of the bill")
    apply_credit(session, credit.id, small.id, "20.00")
    assert_rejected(session, lambda: apply_credit(session, credit.id, big.id, "10.01"),
                    "credit application exceeds the remaining amount of the credit")
    apply_credit(session, credit.id, big.id, "10.00")
    assert session.query(CreditApplication).count() == 2


def test_application_cannot_be_updated_or_deleted(session, world):
    """Since fmspayables03 applications are never deleted either: unapplying closes them."""
    bill = make_bill(session, world)
    credit = make_credit(session, world)
    apply_credit(session, credit.id, bill.id, "10.00")
    assert_rejected(session, lambda: session.execute(text("UPDATE credit_applications SET amount = 5")),
                    "credit applications cannot be modified")
    assert_rejected(session, lambda: session.execute(text("DELETE FROM credit_applications")),
                    "credit applications are never deleted")
    assert session.query(CreditApplication).count() == 1


def test_same_credit_can_be_applied_again_after_unapplication(session, world):
    """The (credit, bill) pair is no longer unique: apply -> unapply -> apply is a valid history."""
    bill = make_bill(session, world)
    credit = make_credit(session, world)
    apply_credit(session, credit.id, bill.id, "10.00")
    session.execute(text(
        "UPDATE credit_applications SET unapplication_date = '2026-01-25', unapplied_by = 'x' WHERE credit_id = :c"),
        {"c": credit.id})
    session.commit()
    apply_credit(session, credit.id, bill.id, "5.00")
    assert session.query(CreditApplication).count() == 2


# --- payments: reversal and the allocation ceiling --------------------------------------------------


def test_payment_reversal_record_must_be_complete_and_set_once(session, world):
    bill = make_bill(session, world)
    payment = make_payment(session, world, "40.00", [(bill.id, "40.00")])
    entry = make_entry(session)
    msg = "a payment reversal must set status, actor, date and reversing entry together, exactly once"
    assert_rejected(session, lambda: set_status(session, "vendor_payments", payment.id, "reversed"), msg)
    assert_rejected(session, lambda: session.execute(
        text("UPDATE vendor_payments SET reversed_by = 'x' WHERE id = :id"), {"id": payment.id}), msg)
    assert_rejected(session, lambda: session.execute(text(
        "UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01' WHERE id = :id"),
        {"id": payment.id}), msg)
    session.execute(text(
        "UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
        "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": payment.id})
    session.commit()
    other = make_entry(session)
    for sql in ("UPDATE vendor_payments SET status = 'recorded' WHERE id = :id",
                "UPDATE vendor_payments SET reversal_date = '2026-03-01' WHERE id = :id",
                "UPDATE vendor_payments SET reversal_journal_entry_id = :e WHERE id = :id",
                "UPDATE vendor_payments SET reversed_by = 'y' WHERE id = :id",
                "UPDATE vendor_payments SET reversal_reason = 'rewritten' WHERE id = :id"):
        assert_rejected(session, lambda: session.execute(text(sql), {"id": payment.id, "e": other.id}), msg)
    # Non-reversal fields of a reversed payment remain as frozen as before.
    assert_rejected(session, lambda: session.execute(text("UPDATE vendor_payments SET amount = 1 WHERE id = :id"),
                                                     {"id": payment.id}),
                    "a recorded vendor payment cannot be modified or deleted")
    assert_rejected(session, lambda: session.execute(text("UPDATE vendor_payments SET corrects_payment_id = :id WHERE id = :id"),
                                                     {"id": payment.id}),
                    "a recorded vendor payment cannot be modified or deleted")
    # Since fmspayables03 descriptive fields are frozen too.
    assert_rejected(session, lambda: session.execute(
        text("UPDATE vendor_payments SET reference = 'late note' WHERE id = :id"), {"id": payment.id}),
        "a recorded vendor payment cannot be modified or deleted")


def test_allocation_ceiling_ignores_reversed_payments_and_counts_credits(session, world):
    bill = make_bill(session, world, total="100.00")
    first = make_payment(session, world, "60.00", [(bill.id, "60.00")])
    assert_rejected(session, lambda: make_payment(session, world, "40.01", [(bill.id, "40.01")]),
                    "allocation exceeds the outstanding amount of the bill")
    entry = make_entry(session)
    session.execute(text(
        "UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
        "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": first.id})
    session.commit()
    credit = make_credit(session, world, total="30.00")
    apply_credit(session, credit.id, bill.id, "30.00")
    assert_rejected(session, lambda: make_payment(session, world, "70.01", [(bill.id, "70.01")]),
                    "allocation exceeds the outstanding amount of the bill")
    make_payment(session, world, "70.00", [(bill.id, "70.00")])  # 100 - 30 credit; the reversed 60 is free again
    assert_rejected(session, lambda: make_payment(session, world, "0.01", [(bill.id, "0.01")]), "allocation exceeds")
    assert_rejected(session, lambda: apply_credit(session, make_credit(session, world, number="CN-2").id, bill.id, "0.01"),
                    "credit application exceeds the outstanding amount of the bill")


def test_bill_settlement_states_move_freely_but_never_back_to_draft(session, world):
    bill = make_bill(session, world, status="paid")
    for status in ("partially_paid", "approved", "paid", "approved"):
        set_status(session, "supplier_bills", bill.id, status)
    for status in ("draft", "rejected"):
        assert_rejected(session, lambda: set_status(session, "supplier_bills", bill.id, status),
                        "invalid supplier bill status transition")
    session.expire_all()
    assert session.get(SupplierBill, bill.id).status == BillStatus.APPROVED


# --- migration ----------------------------------------------------------------------------------------


def _trigger_names(engine) -> set[str]:
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_ap%'"))
        else:
            rows = conn.execute(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_ap%'"))
        return {row[0] for row in rows}


def test_downgrade_to_fmspayables01_restores_previous_rules(db_url):
    from app.db import make_engine

    migrate(db_url, "fmspayables01")
    engine = make_engine(db_url)
    v1_triggers = _trigger_names(engine)
    v1_tables = set(inspect(engine).get_table_names())
    engine.dispose()

    migrate(db_url)  # to head (fmspayables02)
    engine = make_engine(db_url)
    assert {"supplier_credits", "supplier_credit_lines", "credit_applications"} <= set(inspect(engine).get_table_names())
    assert "status" in {c["name"] for c in inspect(engine).get_columns("vendor_payments")}
    assert len(_trigger_names(engine)) > len(v1_triggers)
    engine.dispose()

    command.downgrade(alembic_config(db_url), "fmspayables01")
    engine = make_engine(db_url)
    assert set(inspect(engine).get_table_names()) == v1_tables
    assert _trigger_names(engine) == v1_triggers
    assert "status" not in {c["name"] for c in inspect(engine).get_columns("vendor_payments")}
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            functions = set(conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'fms\\_ap\\_%'")).scalars())
        assert "fms_ap_normalize_number" not in functions
        assert "fms_ap_supplier_credits_guard" not in functions
    engine.dispose()
    migrate(db_url)  # and forward again
