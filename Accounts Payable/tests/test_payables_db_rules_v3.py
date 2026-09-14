"""Database rules added by migrations ``fmspayables03`` and ``fmspayables04`` (SQLite and
PostgreSQL), exercised through raw SQL and the ORM -- never the service layer.

* recorded payments are immutable in every column (method, reference, actor, key, ...);
* original allocations are immutable; advance allocations are unallocated exactly once;
  allocations are never deleted; advance rows need a recorded payment with enough
  unapplied remainder on the same payable account;
* credit applications are unapplied exactly once and never deleted;
* discounts are append-only, capped by the entitlement, reversed once;
* holds are released once and never deleted; allocations to held bills are refused;
* duplicate flags are reviewed once; pending blocking flags stop approval;
* AP period locks are append-only and refuse dated writes inside the closed period;
* payment runs follow their state machine and freeze their items;
* the downgrade path restores the previous revisions' trigger sets and prunes rows
  the older schema cannot represent.
"""

from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType, JournalEntry
from app.modules.payables.models import (
    AllocationKind,
    BillDiscount,
    BillHold,
    CreditApplication,
    CreditKind,
    DuplicateFlag,
    DuplicateFlagStatus,
    HoldKind,
    PaymentAllocation,
    PaymentMethod,
    PaymentRun,
    PaymentRunItem,
    PaymentRunStatus,
    RunItemStatus,
    SupplierBill,
    SupplierBillLine,
    SupplierCredit,
    SupplierCreditLine,
    Vendor,
    VendorPayment,
)
from tests.conftest import alembic_config, migrate

D = Decimal


@pytest.fixture
def world(session):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    ap2 = Account(code="2010", name="AP2", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    disc = Account(code="4900", name="Discounts", type=AccountType.REVENUE)
    vendor = Vendor(code="V1", name="Vendor", payable_account_id=None)
    session.add_all([ap, ap2, expense, cash, disc, vendor])
    session.commit()
    return {"ap": ap, "ap2": ap2, "expense": expense, "cash": cash, "disc": disc, "vendor": vendor}


def set_status(session, table, row_id, status):
    session.execute(text(f"UPDATE {table} SET status = :s WHERE id = :id"), {"s": status, "id": row_id})
    session.commit()


def make_bill(session, world, number="INV-1", status="approved", total="100.00", account=None, discount="0"):
    bill = SupplierBill(
        vendor_id=world["vendor"].id, invoice_number=number, invoice_date=date(2026, 1, 10),
        due_date=date(2026, 2, 9), total=D(total), payable_account_id=(account or world["ap"]).id,
        lines=[SupplierBillLine(line_no=1, description="x", account_id=world["expense"].id,
                                quantity=D("1"), unit_price=D(total), line_total=D(total))],
    )
    session.add(bill)
    session.commit()
    if discount != "0":
        session.execute(text("UPDATE supplier_bills SET discount_amount = :d, discount_date = '2026-01-20', "
                             "discount_account_id = :a WHERE id = :id"),
                        {"d": discount, "a": world["disc"].id, "id": bill.id})
        session.commit()
    path = {"draft": (), "approved": ("approved",), "partially_paid": ("approved", "partially_paid"),
            "paid": ("approved", "paid"), "rejected": ("rejected",)}[status]
    for step in path:
        set_status(session, "supplier_bills", bill.id, step)
    session.refresh(bill)
    return bill


def make_credit(session, world, number="CN-1", status="approved", total="30.00"):
    credit = SupplierCredit(
        vendor_id=world["vendor"].id, kind=CreditKind.CREDIT_NOTE, credit_number=number,
        credit_date=date(2026, 1, 12), total=D(total), payable_account_id=world["ap"].id,
        lines=[SupplierCreditLine(line_no=1, description="c", account_id=world["expense"].id,
                                  quantity=D("1"), unit_price=D(total), line_total=D(total))],
    )
    session.add(credit)
    session.commit()
    if status != "draft":
        set_status(session, "supplier_credits", credit.id, "approved")
    session.refresh(credit)
    return credit


def make_payment(session, world, amount, allocations=(), payable_account=None, when=date(2026, 1, 20)):
    payment = VendorPayment(
        vendor_id=world["vendor"].id, payment_date=when, amount=D(amount), method=PaymentMethod.CASH,
        reference="REF", cash_account_id=world["cash"].id, recorded_by="test",
        payable_account_id=payable_account.id if payable_account else None, idempotency_key=None,
        allocations=[PaymentAllocation(bill_id=b, amount=D(a), allocation_date=when, allocated_by="test")
                     for b, a in allocations],
    )
    session.add(payment)
    session.commit()
    return payment


def make_entry(session):
    entry = JournalEntry(entry_date=date(2026, 1, 20), description="x")
    session.add(entry)
    session.commit()
    return entry


def advance(session, payment, bill, amount, when=date(2026, 1, 25)):
    session.add(PaymentAllocation(payment_id=payment.id, bill_id=bill.id, amount=D(amount), kind=AllocationKind.ADVANCE,
                                  allocation_date=when, allocated_by="test"))
    session.commit()


def apply_credit(session, credit, bill, amount, when=date(2026, 1, 21)):
    session.add(CreditApplication(credit_id=credit.id, bill_id=bill.id, amount=D(amount), application_date=when, applied_by="test"))
    session.commit()


def assert_rejected(session, action, message_fragment):
    with pytest.raises(IntegrityError, match=message_fragment):
        action()
        session.commit()
    session.rollback()


def raw(session, sql, **params):
    return lambda: session.execute(text(sql), params)


# --- payments are immutable in every column -----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE vendor_payments SET method = 'cheque' WHERE id = :id",
        "UPDATE vendor_payments SET reference = 'changed' WHERE id = :id",
        "UPDATE vendor_payments SET reference = NULL WHERE id = :id",
        "UPDATE vendor_payments SET recorded_by = 'someone else' WHERE id = :id",
        "UPDATE vendor_payments SET idempotency_key = 'k' WHERE id = :id",
        "UPDATE vendor_payments SET payable_account_id = NULL WHERE id = :id",
        "UPDATE vendor_payments SET amount = 1 WHERE id = :id",
        "UPDATE vendor_payments SET payment_date = '2026-02-01' WHERE id = :id",
        "UPDATE vendor_payments SET cash_account_id = cash_account_id + 1 WHERE id = :id",
        "UPDATE vendor_payments SET vendor_id = vendor_id + 1 WHERE id = :id",
        "DELETE FROM vendor_payments WHERE id = :id",
    ],
)
def test_recorded_payment_details_cannot_change_through_sql(session, world, sql):
    payment = make_payment(session, world, "40.00", payable_account=world["ap"])
    session.add(Vendor(code="V2", name="other"))
    session.commit()
    assert_rejected(session, raw(session, sql, id=payment.id), "a recorded vendor payment cannot be modified or deleted")
    session.expire_all()
    stored = session.get(VendorPayment, payment.id)
    assert (stored.method, stored.reference, stored.recorded_by, stored.amount) == (PaymentMethod.CASH, "REF", "test", D("40.00"))


def test_payment_provenance_survives_reversal(session, world):
    payment = make_payment(session, world, "40.00", payable_account=world["ap"])
    entry = make_entry(session)
    session.execute(text("UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
                         "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": payment.id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE vendor_payments SET reference = 'x' WHERE id = :id", id=payment.id),
                    "a recorded vendor payment cannot be modified or deleted")
    assert_rejected(session, raw(session, "UPDATE vendor_payments SET recorded_by = 'x' WHERE id = :id", id=payment.id),
                    "a recorded vendor payment cannot be modified or deleted")


# --- allocations -------------------------------------------------------------------------------------------


def test_original_allocations_are_immutable_and_never_deleted(session, world):
    bill = make_bill(session, world)
    payment = make_payment(session, world, "40.00", [(bill.id, "40.00")])
    allocation_id = payment.allocations[0].id
    msg = "payment allocations cannot be modified or deleted"
    for sql in (
        "UPDATE payment_allocations SET amount = 1 WHERE id = :id",
        "UPDATE payment_allocations SET bill_id = bill_id + 1 WHERE id = :id",
        "UPDATE payment_allocations SET allocation_date = '2026-03-01' WHERE id = :id",
        "UPDATE payment_allocations SET allocated_by = 'x' WHERE id = :id",
        "UPDATE payment_allocations SET unallocation_date = '2026-03-01', unallocated_by = 'x' WHERE id = :id",  # original kind
        "DELETE FROM payment_allocations WHERE id = :id",
    ):
        assert_rejected(session, raw(session, sql, id=allocation_id), msg)


def test_original_allocations_cannot_exceed_the_payment(session, world):
    bill = make_bill(session, world, total="500.00")
    assert_rejected(session, lambda: make_payment(session, world, "40.00", [(bill.id, "40.01")]),
                    "allocations exceed the payment amount")
    payment = make_payment(session, world, "40.00", [(bill.id, "30.00")], payable_account=world["ap"])
    assert_rejected(session, lambda: session.add(PaymentAllocation(payment_id=payment.id, bill_id=bill.id, amount=D("10.01"),
                                                                    allocation_date=date(2026, 1, 20), allocated_by="t")),
                    "allocations exceed the payment amount")


def test_advance_allocation_rules(session, world):
    bill = make_bill(session, world, total="100.00")
    other_account_bill = make_bill(session, world, number="INV-2", total="100.00", account=world["ap2"])
    payment = make_payment(session, world, "70.00", payable_account=world["ap"])
    no_account = make_payment(session, world, "70.00")
    assert_rejected(session, lambda: advance(session, payment, bill, "70.01"),
                    "advance allocation exceeds the unapplied amount of the payment")
    assert_rejected(session, lambda: advance(session, payment, other_account_bill, "10.00"),
                    "advance allocations must target a bill posting to the payable account of the payment")
    assert_rejected(session, lambda: advance(session, no_account, bill, "10.00"),
                    "advance allocations must target a bill posting to the payable account of the payment")
    advance(session, payment, bill, "50.00")
    assert_rejected(session, lambda: advance(session, payment, bill, "20.01"),
                    "advance allocation exceeds the unapplied amount of the payment")
    advance(session, payment, bill, "20.00")
    session.expire_all()
    assert session.get(VendorPayment, payment.id).unapplied_amount == 0
    assert session.get(SupplierBill, bill.id).outstanding == D("30.00")


def test_advance_requires_recorded_payment(session, world):
    bill = make_bill(session, world)
    payment = make_payment(session, world, "70.00", payable_account=world["ap"])
    entry = make_entry(session)
    session.execute(text("UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
                         "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": payment.id})
    session.commit()
    assert_rejected(session, lambda: advance(session, payment, bill, "10.00", when=date(2026, 2, 2)),
                    "advance allocations require a recorded payment")


def test_advance_allocation_is_unallocated_exactly_once(session, world):
    bill = make_bill(session, world)
    payment = make_payment(session, world, "70.00", payable_account=world["ap"])
    advance(session, payment, bill, "50.00")
    allocation_id = payment.allocations[0].id if payment.allocations else session.execute(
        text("SELECT id FROM payment_allocations")).scalar()
    msg = "payment allocations cannot be modified or deleted"
    # Partial / inconsistent closes are refused ...
    assert_rejected(session, raw(session, "UPDATE payment_allocations SET unallocation_date = '2026-03-01' WHERE id = :id", id=allocation_id), msg)
    assert_rejected(session, raw(session, "UPDATE payment_allocations SET unallocated_by = 'x' WHERE id = :id", id=allocation_id), msg)
    assert_rejected(session, raw(session, "UPDATE payment_allocations SET unallocation_date = '2026-01-01', unallocated_by = 'x' WHERE id = :id",
                                 id=allocation_id), "ck_payment_allocations_unallocation_after_allocation|CHECK")
    # ... the proper close works once ...
    session.execute(text("UPDATE payment_allocations SET unallocation_date = '2026-03-01', unallocated_by = 'x', unallocation_reason = 'r' WHERE id = :id"),
                    {"id": allocation_id})
    session.commit()
    # ... and is then frozen; the row still exists.
    assert_rejected(session, raw(session, "UPDATE payment_allocations SET unallocation_date = '2026-03-02' WHERE id = :id", id=allocation_id), msg)
    assert_rejected(session, raw(session, "UPDATE payment_allocations SET unallocated_by = 'y' WHERE id = :id", id=allocation_id), msg)
    assert_rejected(session, raw(session, "DELETE FROM payment_allocations WHERE id = :id", id=allocation_id), msg)
    session.expire_all()
    assert session.get(SupplierBill, bill.id).outstanding == D("100.00")
    assert session.get(VendorPayment, payment.id).unapplied_amount == D("70.00")
    # The freed remainder can be re-applied (a new row).
    advance(session, payment, bill, "70.00", when=date(2026, 3, 5))
    assert session.execute(text("SELECT COUNT(*) FROM payment_allocations")).scalar() == 2


def test_settlement_ceiling_counts_only_rows_in_force(session, world):
    bill = make_bill(session, world, total="100.00", discount="5.00")
    credit = make_credit(session, world, total="30.00")
    apply_credit(session, credit, bill, "30.00")
    payment = make_payment(session, world, "60.00", [(bill.id, "60.00")])
    session.add(BillDiscount(bill_id=bill.id, payment_id=payment.id, amount=D("5.00"), taken_on=date(2026, 1, 20), taken_by="t",
                             discount_account_id=world["disc"].id))
    session.commit()
    assert_rejected(session, lambda: make_payment(session, world, "5.01", [(bill.id, "5.01")]),
                    "allocation exceeds the outstanding amount of the bill")
    # Unapplying the credit frees 30 again; reversing the payment frees 60 (its discount stays until reversed).
    session.execute(text("UPDATE credit_applications SET unapplication_date = '2026-02-01', unapplied_by = 'x'"))
    session.commit()
    make_payment(session, world, "35.00", [(bill.id, "35.00")], when=date(2026, 2, 2))
    assert_rejected(session, lambda: make_payment(session, world, "0.01", [(bill.id, "0.01")], when=date(2026, 2, 3)), "exceeds")
    entry = make_entry(session)
    session.execute(text("UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-05', "
                         "reversal_journal_entry_id = :e WHERE id = :id"), {"e": entry.id, "id": payment.id})
    session.commit()
    make_payment(session, world, "60.00", [(bill.id, "60.00")], when=date(2026, 2, 6))


# --- credit applications -------------------------------------------------------------------------------------


def test_credit_application_is_closed_once_and_never_deleted(session, world):
    bill = make_bill(session, world)
    credit = make_credit(session, world)
    apply_credit(session, credit, bill, "10.00")
    app_id = session.execute(text("SELECT id FROM credit_applications")).scalar()
    msg = "credit applications cannot be modified"
    assert_rejected(session, raw(session, "UPDATE credit_applications SET amount = 5 WHERE id = :id", id=app_id), msg)
    assert_rejected(session, raw(session, "UPDATE credit_applications SET unapplication_date = '2026-02-01' WHERE id = :id", id=app_id), msg)
    assert_rejected(session, raw(session, "UPDATE credit_applications SET unapplication_date = '2026-01-01', unapplied_by = 'x' WHERE id = :id", id=app_id),
                    "ck_credit_applications_unapplication_after_application|CHECK")
    session.execute(text("UPDATE credit_applications SET unapplication_date = '2026-02-01', unapplied_by = 'x' WHERE id = :id"), {"id": app_id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE credit_applications SET unapplication_date = '2026-02-02' WHERE id = :id", id=app_id), msg)
    assert_rejected(session, raw(session, "DELETE FROM credit_applications WHERE id = :id", id=app_id),
                    "credit applications are never deleted")
    # Closed applications free the credit and the bill again.
    apply_credit(session, credit, bill, "30.00", when=date(2026, 2, 3))
    assert_rejected(session, lambda: apply_credit(session, credit, bill, "0.01", when=date(2026, 2, 4)),
                    "credit application exceeds the remaining amount of the credit")


def test_credit_with_only_closed_applications_can_be_reversed(session, world):
    bill = make_bill(session, world)
    credit = make_credit(session, world)
    apply_credit(session, credit, bill, "10.00")
    entry = make_entry(session)
    reverse = raw(session, "UPDATE supplier_credits SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-02-01', "
                           "reversal_journal_entry_id = :e WHERE id = :id", e=entry.id, id=credit.id)
    assert_rejected(session, reverse, "a supplier credit with applications cannot be reversed")
    session.execute(text("UPDATE credit_applications SET unapplication_date = '2026-01-30', unapplied_by = 'x'"))
    session.commit()
    reverse()
    session.commit()


# --- discounts ------------------------------------------------------------------------------------------------


def test_discount_rules(session, world):
    bill = make_bill(session, world, total="100.00", discount="2.00")
    payment = make_payment(session, world, "90.00", [(bill.id, "90.00")])  # 10.00 remains: entitlement binds first

    def take(amount, when=date(2026, 1, 20)):
        session.add(BillDiscount(bill_id=bill.id, payment_id=payment.id, amount=D(amount), taken_on=when, taken_by="t",
                                 discount_account_id=world["disc"].id))
        session.commit()

    assert_rejected(session, lambda: take("2.01"), "discount exceeds the discount entitlement of the bill")
    take("2.00")
    assert_rejected(session, lambda: take("0.01"), "discount exceeds")
    discount_id = session.execute(text("SELECT id FROM bill_discounts")).scalar()
    msg = "bill discounts cannot be modified or deleted"
    assert_rejected(session, raw(session, "UPDATE bill_discounts SET amount = 1 WHERE id = :id", id=discount_id), msg)
    assert_rejected(session, raw(session, "UPDATE bill_discounts SET reversal_date = '2026-02-01' WHERE id = :id", id=discount_id), msg)
    assert_rejected(session, raw(session, "DELETE FROM bill_discounts WHERE id = :id", id=discount_id), msg)
    entry = make_entry(session)
    session.execute(text("UPDATE bill_discounts SET reversal_date = '2026-02-01', reversed_by = 'x', reversal_journal_entry_id = :e WHERE id = :id"),
                    {"e": entry.id, "id": discount_id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE bill_discounts SET reversal_date = '2026-02-02' WHERE id = :id", id=discount_id), msg)
    # Once reversed, the entitlement is available again.
    take("2.00", when=date(2026, 2, 3))
    assert session.execute(text("SELECT COUNT(*) FROM bill_discounts")).scalar() == 2


def test_discount_needs_open_bill(session, world):
    bill = make_bill(session, world, status="paid", total="100.00", discount="2.00")
    assert_rejected(session, lambda: session.add(BillDiscount(bill_id=bill.id, payment_id=None, amount=D("1.00"), taken_on=date(2026, 1, 20),
                                                              taken_by="t", discount_account_id=world["disc"].id)),
                    "discounts can only be taken on approved or partially paid bills")


# --- holds and duplicate flags ----------------------------------------------------------------------------------


def test_holds_block_allocations_and_are_released_once(session, world):
    bill = make_bill(session, world)
    session.add(BillHold(bill_id=bill.id, kind=HoldKind.DISPUTE, reason="damaged", placed_by="a"))
    session.commit()
    hold_id = session.execute(text("SELECT id FROM bill_holds")).scalar()
    assert_rejected(session, lambda: make_payment(session, world, "10.00", [(bill.id, "10.00")]),
                    "payments cannot be allocated to a bill that is on hold or disputed")
    credit = make_credit(session, world)
    apply_credit(session, credit, bill, "10.00")  # credits may settle a disputed bill
    msg = "bill holds cannot be modified or deleted"
    assert_rejected(session, raw(session, "UPDATE bill_holds SET reason = 'x' WHERE id = :id", id=hold_id), msg)
    assert_rejected(session, raw(session, "UPDATE bill_holds SET released_at = CURRENT_TIMESTAMP WHERE id = :id", id=hold_id), msg)  # no actor
    assert_rejected(session, raw(session, "DELETE FROM bill_holds WHERE id = :id", id=hold_id), msg)
    session.execute(text("UPDATE bill_holds SET released_at = CURRENT_TIMESTAMP, released_by = 'd', resolution = 'ok' WHERE id = :id"), {"id": hold_id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE bill_holds SET resolution = 'changed' WHERE id = :id", id=hold_id), msg)
    make_payment(session, world, "10.00", [(bill.id, "10.00")])


def test_pending_blocking_flag_stops_approval_and_is_reviewed_once(session, world):
    original = make_bill(session, world, "INV-001")
    suspect = make_bill(session, world, "INV 001", status="draft")
    session.add(DuplicateFlag(bill_id=suspect.id, suspected_bill_id=original.id, reasons="compact_number", score=60,
                              blocks_approval=True, status=DuplicateFlagStatus.PENDING))
    session.commit()
    assert_rejected(session, lambda: set_status(session, "supplier_bills", suspect.id, "approved"),
                    "a bill with unresolved blocking duplicate flags cannot be approved")
    flag_id = session.execute(text("SELECT id FROM duplicate_flags")).scalar()
    msg = "duplicate flags are reviewed once"
    assert_rejected(session, raw(session, "UPDATE duplicate_flags SET status = 'cleared' WHERE id = :id", id=flag_id), msg)  # no reviewer
    assert_rejected(session, raw(session, "UPDATE duplicate_flags SET score = 1 WHERE id = :id", id=flag_id), msg)
    session.execute(text("UPDATE duplicate_flags SET status = 'cleared', reviewed_by = 'r' WHERE id = :id"), {"id": flag_id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE duplicate_flags SET status = 'pending' WHERE id = :id", id=flag_id), msg)
    set_status(session, "supplier_bills", suspect.id, "approved")
    # Advisory flags never block.
    advisory = make_bill(session, world, "INV-0X1", status="draft")
    session.add(DuplicateFlag(bill_id=advisory.id, suspected_bill_id=original.id, reasons="near", score=45,
                              blocks_approval=False, status=DuplicateFlagStatus.PENDING))
    session.commit()
    set_status(session, "supplier_bills", advisory.id, "approved")


# --- AP period locks ----------------------------------------------------------------------------------------------


def test_period_lock_refuses_dated_writes_and_is_append_only(session, world):
    bill = make_bill(session, world)
    credit = make_credit(session, world)
    payment = make_payment(session, world, "70.00", payable_account=world["ap"])
    draft_bill = make_bill(session, world, number="DRAFT", status="draft")
    draft_credit = make_credit(session, world, number="CN-D", status="draft")
    session.execute(text("INSERT INTO ap_period_locks (closed_through, closed_by) VALUES ('2026-01-31', 'cfo')"))
    session.commit()
    closed = "the AP period containing this date is closed"
    assert_rejected(session, lambda: make_payment(session, world, "1.00", when=date(2026, 1, 31)), closed)
    assert_rejected(session, lambda: apply_credit(session, credit, bill, "1.00", when=date(2026, 1, 15)), closed)
    assert_rejected(session, lambda: advance(session, payment, bill, "1.00", when=date(2026, 1, 25)), closed)
    assert_rejected(session, lambda: set_status(session, "supplier_bills", draft_bill.id, "approved"), closed)  # dated 2026-01-10
    assert_rejected(session, lambda: set_status(session, "supplier_credits", draft_credit.id, "approved"), closed)
    entry = make_entry(session)
    assert_rejected(session, raw(session, "UPDATE vendor_payments SET status = 'reversed', reversed_by = 'x', reversal_date = '2026-01-30', "
                                          "reversal_journal_entry_id = :e WHERE id = :id", e=entry.id, id=payment.id), closed)
    # Dates after the lock are fine; the lock itself cannot be changed or removed.
    make_payment(session, world, "1.00", when=date(2026, 2, 1))
    apply_credit(session, credit, bill, "1.00", when=date(2026, 2, 1))
    advance(session, payment, bill, "1.00", when=date(2026, 2, 1))
    assert_rejected(session, raw(session, "UPDATE ap_period_locks SET closed_through = '2025-12-31'"), "append-only")
    assert_rejected(session, raw(session, "DELETE FROM ap_period_locks"), "append-only")
    # Unapplying into the closed period is refused too.
    app_id = session.execute(text("SELECT id FROM credit_applications")).scalar()
    assert_rejected(session, raw(session, "UPDATE credit_applications SET unapplication_date = '2026-01-31', unapplied_by = 'x' WHERE id = :id", id=app_id), closed)
    session.execute(text("UPDATE credit_applications SET unapplication_date = '2026-02-02', unapplied_by = 'x' WHERE id = :id"), {"id": app_id})
    session.commit()


# --- vendors -----------------------------------------------------------------------------------------------------


def test_vendor_status_check(session, world):
    assert_rejected(session, raw(session, "UPDATE vendors SET status = 'bogus' WHERE id = :id", id=world["vendor"].id),
                    "ck_vendors_status_valid")


# --- payment runs ----------------------------------------------------------------------------------------------------


def make_run(session, world, status="draft"):
    run = PaymentRun(reference=f"RUN-{status}", status=PaymentRunStatus.DRAFT, pay_date=date(2026, 2, 10), due_through=date(2026, 2, 10),
                     cash_account_id=world["cash"].id, method=PaymentMethod.BANK_TRANSFER, created_by="ops")
    session.add(run)
    session.commit()
    bill = make_bill(session, world, number=f"R-{status}")
    session.add(PaymentRunItem(run_id=run.id, vendor_id=world["vendor"].id, bill_id=bill.id, due_date=date(2026, 2, 9),
                               outstanding_snapshot=D("100.00"), settle_amount=D("100.00"), discount_amount=D("0"), credit_amount=D("0"),
                               advance_amount=D("0"), pay_amount=D("100.00"), status=RunItemStatus.PROPOSED, explanation="x"))
    session.commit()
    path = {"draft": (), "reviewed": ("reviewed",), "approved": ("reviewed", "approved"),
            "executing": ("reviewed", "approved", "executing"), "completed": ("reviewed", "approved", "executing", "completed"),
            "failed": ("reviewed", "approved", "executing", "failed"), "cancelled": ("cancelled",)}[status]
    for step in path:
        set_status(session, "payment_runs", run.id, step)
    session.refresh(run)
    return run


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [("draft", "approved"), ("draft", "executing"), ("draft", "completed"), ("reviewed", "executing"), ("reviewed", "completed"),
     ("approved", "completed"), ("approved", "reviewed"), ("executing", "draft"), ("executing", "approved"), ("executing", "cancelled"),
     ("completed", "draft"), ("completed", "executing"), ("cancelled", "draft"), ("failed", "completed"), ("failed", "draft")],
)
def test_invalid_run_transitions(session, world, from_status, to_status):
    run = make_run(session, world, from_status)
    assert_rejected(session, lambda: set_status(session, "payment_runs", run.id, to_status), "invalid payment run status transition")


def test_run_parameters_and_items_freeze_outside_draft(session, world):
    run = make_run(session, world, "reviewed")
    item_id = session.execute(text("SELECT id FROM payment_run_items WHERE run_id = :r"), {"r": run.id}).scalar()
    assert_rejected(session, raw(session, "UPDATE payment_runs SET cash_account_id = cash_account_id + 1 WHERE id = :id", id=run.id),
                    "selection parameters of a non-draft payment run cannot be modified")
    assert_rejected(session, raw(session, "DELETE FROM payment_runs WHERE id = :id", id=run.id), "only draft payment runs can be deleted")
    frozen = "payment run items can only change while the run is draft or executing"
    assert_rejected(session, raw(session, "UPDATE payment_run_items SET pay_amount = 1 WHERE id = :id", id=item_id), frozen)
    assert_rejected(session, raw(session, "DELETE FROM payment_run_items WHERE id = :id", id=item_id), frozen)
    assert_rejected(session, lambda: session.add(PaymentRunItem(run_id=run.id, vendor_id=world["vendor"].id, bill_id=make_bill(session, world, "EXTRA").id,
                                                                due_date=date(2026, 2, 9), outstanding_snapshot=D("1"), settle_amount=D("1"), discount_amount=D("0"),
                                                                credit_amount=D("0"), advance_amount=D("0"), pay_amount=D("1"), status=RunItemStatus.PROPOSED, explanation="x")),
                    frozen)
    # While executing, only outcome columns may change.
    set_status(session, "payment_runs", run.id, "approved")
    set_status(session, "payment_runs", run.id, "executing")
    session.execute(text("UPDATE payment_run_items SET status = 'paid', execution_reference = 'SIM-1' WHERE id = :id"), {"id": item_id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE payment_run_items SET pay_amount = 1 WHERE id = :id", id=item_id), frozen)
    set_status(session, "payment_runs", run.id, "completed")
    assert_rejected(session, raw(session, "UPDATE payment_run_items SET status = 'failed' WHERE id = :id", id=item_id), frozen)


# --- downgrade path -----------------------------------------------------------------------------------------------------


def _trigger_names(engine):
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_ap%'"))
        else:
            rows = conn.execute(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_ap%'"))
        return {row[0] for row in rows}


def test_downgrade_restores_previous_revisions_and_prunes_unrepresentable_rows(db_url):
    from app.db import make_engine

    snapshots = {}
    for rev in ("fmspayables02", "fmspayables03", "fmspayables04"):
        migrate(db_url, rev)
        engine = make_engine(db_url)
        snapshots[rev] = (_trigger_names(engine), set(inspect(engine).get_table_names()))
        engine.dispose()

    # Create v3-only history at head: a closed credit application and an unallocated advance.
    engine = make_engine(db_url)
    from sqlalchemy.orm import Session as _Session

    with _Session(engine, expire_on_commit=False) as session:
        ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
        expense = Account(code="6000", name="E", type=AccountType.EXPENSE)
        cash = Account(code="1000", name="C", type=AccountType.ASSET)
        vendor = Vendor(code="V", name="V")
        session.add_all([ap, expense, cash, vendor])
        session.commit()
        world = {"ap": ap, "expense": expense, "cash": cash, "vendor": vendor, "disc": ap}
        bill = make_bill(session, world)
        credit = make_credit(session, world)
        apply_credit(session, credit, bill, "10.00")
        apply_credit(session, credit, bill, "5.00", when=date(2026, 1, 22))
        session.execute(text("UPDATE credit_applications SET unapplication_date = '2026-02-01', unapplied_by = 'x' WHERE amount = 10"))
        payment = make_payment(session, world, "70.00", payable_account=ap)
        advance(session, payment, bill, "20.00")
        advance(session, payment, bill, "30.00", when=date(2026, 1, 26))
        session.execute(text("UPDATE payment_allocations SET unallocation_date = '2026-02-02', unallocated_by = 'x' WHERE amount = 20"))
        session.commit()
        assert session.execute(text("SELECT COUNT(*) FROM credit_applications")).scalar() == 2
        assert session.execute(text("SELECT COUNT(*) FROM payment_allocations")).scalar() == 2
    engine.dispose()

    command.downgrade(alembic_config(db_url), "fmspayables03")
    engine = make_engine(db_url)
    assert (_trigger_names(engine), set(inspect(engine).get_table_names())) == snapshots["fmspayables03"]
    engine.dispose()

    command.downgrade(alembic_config(db_url), "fmspayables02")
    engine = make_engine(db_url)
    assert (_trigger_names(engine), set(inspect(engine).get_table_names())) == snapshots["fmspayables02"]
    with engine.connect() as conn:
        # Closed rows are gone (the old schema could not represent them); open rows survive.
        assert conn.execute(text("SELECT amount FROM credit_applications")).scalars().all() == [D("5.00")]
        assert conn.execute(text("SELECT amount FROM payment_allocations")).scalars().all() == [D("30.00")]
        columns = {c["name"] for c in inspect(conn).get_columns("payment_allocations")}
        assert "kind" not in columns and "unallocation_date" not in columns
        assert "status" not in {c["name"] for c in inspect(conn).get_columns("vendors")}
    engine.dispose()
    migrate(db_url)  # forward again from there
    engine = make_engine(db_url)
    with engine.connect() as conn:
        # Back-fill rules: existing allocations become original rows dated on the payment.
        row = conn.execute(text("SELECT kind, allocation_date, allocated_by FROM payment_allocations")).one()
        assert (row[0], str(row[1]), row[2]) == ("original", "2026-01-20", "test")
        assert conn.execute(text("SELECT status FROM vendors")).scalar() == "active"
    engine.dispose()
