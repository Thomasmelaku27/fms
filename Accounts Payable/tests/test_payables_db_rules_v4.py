"""Database rules added by ``fmspayables05`` (SQLite and PostgreSQL), via raw SQL / ORM only.

* payment operations: state machine, frozen identity / instruction fields, set-once
  gateway confirmation and payment links, never deleted, unique keys;
* operation events and close attestations are append-only;
* payment runs additionally allow approved -> draft (approval invalidated).
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType
from app.modules.payables.models import OperationState, PaymentMethod, PaymentOperation, PaymentOperationEvent, Vendor

D = Decimal


@pytest.fixture
def world(session):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    cash = Account(code="1000", name="C", type=AccountType.ASSET)
    vendor = Vendor(code="V1", name="V")
    session.add_all([ap, cash, vendor])
    session.commit()
    return {"ap": ap, "cash": cash, "vendor": vendor}


def make_op(session, world, key="k1", uid="11111111-1111-1111-1111-111111111111", state="prepared"):
    op = PaymentOperation(operation_uid=uid, idempotency_key=key, fingerprint="f" * 64, vendor_id=world["vendor"].id, amount=D("100.00"),
                          currency="XXX", payment_date=date(2026, 2, 10), method=PaymentMethod.BANK_TRANSFER, cash_account_id=world["cash"].id,
                          plan_json=json.dumps([]), state=OperationState.PREPARED, created_by="t", correlation_id="c")
    session.add(op)
    session.commit()
    path = {"prepared": (), "submitted": ("submitted",), "confirmed": ("submitted", "confirmed"),
            "recording": ("submitted", "confirmed", "recording"), "completed": ("submitted", "confirmed", "recording", "completed"),
            "failed": ("failed",), "confirmation_pending": ("submitted", "confirmation_pending"),
            "reconciliation_required": ("submitted", "confirmed", "reconciliation_required")}[state]
    for step in path:
        step_to(session, world, op, step)
    session.refresh(op)
    return op


def evidence_payment(session, world, op):
    """A recorded vendor payment with a posted entry matching the operation (Turn-5 completion evidence)."""
    from app.models import EntryStatus, JournalEntry, JournalLine
    from app.modules.payables.models import PaymentStatus, VendorPayment

    entry = JournalEntry(entry_date=op.payment_date, description="pay", reference="AP-PAY-T",
                         lines=[JournalLine(account_id=world["ap"].id, debit=op.amount), JournalLine(account_id=world["cash"].id, credit=op.amount)])
    session.add(entry)
    session.flush()
    session.execute(text("UPDATE journal_entries SET status = 'posted' WHERE id = :id"), {"id": entry.id})
    payment = VendorPayment(vendor_id=op.vendor_id, payment_date=op.payment_date, amount=op.amount, method=op.method,
                            reference=op.gateway_confirmation_ref, cash_account_id=op.cash_account_id, journal_entry_id=entry.id,
                            recorded_by="t", status=PaymentStatus.RECORDED, payable_account_id=world["ap"].id)
    session.add(payment)
    session.commit()
    session.refresh(entry)
    assert entry.status == EntryStatus.POSTED
    return payment


def step_to(session, world, op, step):
    """Direct-SQL transition carrying the evidence fmspayables06 demands for confirmed / completed."""
    if step == "confirmed":
        session.execute(text("UPDATE payment_operations SET state = 'confirmed', gateway_status = 'accepted', "
                             "gateway_confirmation_ref = COALESCE(gateway_confirmation_ref, :ref) WHERE id = :id"),
                        {"ref": f"REF-{op.operation_uid[:8]}-{op.id}", "id": op.id})
    elif step == "completed":
        session.refresh(op)
        if op.gateway_confirmation_ref is None:
            session.execute(text("UPDATE payment_operations SET gateway_status = 'accepted', gateway_confirmation_ref = :ref WHERE id = :id"),
                            {"ref": f"REF-{op.operation_uid[:8]}-{op.id}", "id": op.id})
            session.commit()
            session.refresh(op)
        payment = op.payment_id and session.get(__import__("app.modules.payables.models", fromlist=["VendorPayment"]).VendorPayment, op.payment_id)
        if payment is None:
            payment = evidence_payment(session, world, op)
        session.execute(text("UPDATE payment_operations SET state = 'completed', payment_id = :p, accounting_status = 'recorded' WHERE id = :id"),
                        {"p": payment.id, "id": op.id})
    else:
        session.execute(text("UPDATE payment_operations SET state = :s WHERE id = :id"), {"s": step, "id": op.id})
    session.commit()


def assert_rejected(session, action, fragment):
    with pytest.raises(IntegrityError, match=fragment):
        action()
        session.commit()
    session.rollback()


def raw(session, sql, **params):
    return lambda: session.execute(text(sql), params)


@pytest.mark.parametrize(("frm", "to"), [
    ("prepared", "confirmed"), ("prepared", "recording"), ("prepared", "completed"), ("submitted", "prepared"), ("submitted", "recording"),
    ("submitted", "completed"), ("confirmed", "submitted"), ("confirmed", "completed"), ("confirmed", "failed"), ("recording", "submitted"),
    ("recording", "failed"), ("completed", "prepared"), ("completed", "recording"), ("failed", "prepared"), ("failed", "submitted"),
    ("reconciliation_required", "failed"), ("reconciliation_required", "submitted"), ("confirmation_pending", "recording"),
])
def test_invalid_operation_transitions(session, world, frm, to):
    op = make_op(session, world, state=frm)
    assert_rejected(session, raw(session, "UPDATE payment_operations SET state = :s WHERE id = :id", s=to, id=op.id),
                    "invalid payment operation state transition")


def test_valid_operation_paths(session, world):
    for n, path in enumerate((("submitted", "confirmed", "recording", "completed"), ("submitted", "confirmation_pending", "confirmed", "recording",
                              "reconciliation_required", "recording", "completed"), ("submitted", "failed"), ("failed",),
                              ("submitted", "confirmation_pending", "reconciliation_required", "completed"))):
        op = make_op(session, world, key=f"k{n}", uid=f"{n:08d}-0000-0000-0000-000000000000")
        for step in path:
            step_to(session, world, op, step)


@pytest.mark.parametrize("sql", [
    "UPDATE payment_operations SET amount = 1 WHERE id = :id",
    "UPDATE payment_operations SET vendor_id = vendor_id + 1 WHERE id = :id",
    "UPDATE payment_operations SET payment_date = '2026-03-01' WHERE id = :id",
    "UPDATE payment_operations SET method = 'cheque' WHERE id = :id",
    "UPDATE payment_operations SET currency = 'EUR' WHERE id = :id",
    "UPDATE payment_operations SET plan_json = '[1]' WHERE id = :id",
    "UPDATE payment_operations SET idempotency_key = 'other' WHERE id = :id",
    "UPDATE payment_operations SET operation_uid = '22222222-2222-2222-2222-222222222222' WHERE id = :id",
    "UPDATE payment_operations SET fingerprint = 'x' WHERE id = :id",
    "UPDATE payment_operations SET cash_account_id = cash_account_id + 1 WHERE id = :id",
    "DELETE FROM payment_operations WHERE id = :id",
])
def test_operation_identity_and_instructions_are_frozen(session, world, sql):
    op = make_op(session, world, state="submitted")
    session.add(Vendor(code="V2", name="other"))
    session.commit()
    assert_rejected(session, raw(session, sql, id=op.id), "payment operation")


def test_gateway_confirmation_and_payment_link_are_set_once(session, world):
    op = make_op(session, world, state="submitted")
    session.execute(text("UPDATE payment_operations SET state = 'confirmed', gateway_status = 'accepted', gateway_confirmation_ref = 'REF-1' WHERE id = :id"), {"id": op.id})
    session.commit()
    assert_rejected(session, raw(session, "UPDATE payment_operations SET gateway_confirmation_ref = 'REF-2' WHERE id = :id", id=op.id),
                    "identity and instruction fields")
    assert_rejected(session, raw(session, "UPDATE payment_operations SET gateway_confirmation_ref = NULL WHERE id = :id", id=op.id),
                    "identity and instruction fields")


def test_duplicate_keys_are_refused_by_unique_constraints(session, world):
    make_op(session, world)
    assert_rejected(session, lambda: make_op(session, world, key="k1", uid="33333333-3333-3333-3333-333333333333"), "idempotency_key")
    assert_rejected(session, lambda: make_op(session, world, key="k9", uid="11111111-1111-1111-1111-111111111111"), "operation_uid")


def test_events_and_attestations_are_append_only(session, world):
    op = make_op(session, world)
    session.add(PaymentOperationEvent(operation_id=op.id, seq=1, from_state=None, to_state="prepared", category="transition", actor="t"))
    session.commit()
    assert_rejected(session, raw(session, "UPDATE payment_operation_events SET detail = 'x'"), "append-only")
    assert_rejected(session, raw(session, "DELETE FROM payment_operation_events"), "append-only")
    session.execute(text("INSERT INTO ap_close_attestations (period_end, outcome, attempted_by, forced, liability_balance, open_bill_count, vendor_count, "
                         "checks_json, exceptions_json, reconciliation_json, source_fingerprint) VALUES ('2026-01-31', 'closed', 'cfo', false, 10, 1, 1, '[]', '[]', '{}', 'f')"))
    session.commit()
    assert_rejected(session, raw(session, "UPDATE ap_close_attestations SET liability_balance = 0"), "append-only")
    assert_rejected(session, raw(session, "DELETE FROM ap_close_attestations"), "append-only")
    assert_rejected(session, raw(session, "INSERT INTO ap_close_attestations (period_end, outcome, attempted_by, forced, liability_balance, open_bill_count, "
                                          "vendor_count, checks_json, exceptions_json, reconciliation_json, source_fingerprint) "
                                          "VALUES ('2026-01-31', 'maybe', 'cfo', false, 10, 1, 1, '[]', '[]', '{}', 'f')"), "ck_ap_close_attestations_outcome_valid")


def test_run_approval_can_be_invalidated_back_to_draft(session, world):
    session.execute(text("INSERT INTO payment_runs (reference, status, pay_date, due_through, take_discounts, use_credits, use_advances, cash_account_id, "
                         "method, created_by, attempts, version, updated_at) VALUES ('R', 'draft', '2026-02-10', '2026-02-10', true, true, true, :c, "
                         "'bank_transfer', 'ops', 0, 0, CURRENT_TIMESTAMP)"), {"c": world["cash"].id})
    session.commit()
    for step in ("reviewed", "approved", "draft", "reviewed", "approved", "executing", "completed"):
        session.execute(text("UPDATE payment_runs SET status = :s WHERE reference = 'R'"), {"s": step})
        session.commit()
    assert_rejected(session, raw(session, "UPDATE payment_runs SET status = 'draft' WHERE reference = 'R'"), "invalid payment run status transition")
