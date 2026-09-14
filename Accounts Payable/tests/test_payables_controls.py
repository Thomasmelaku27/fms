"""Approval and payment controls (SQLite and PostgreSQL).

* idempotency keys compare the *whole* meaningful payment request, field by field;
* duplicate submissions of every state change are 409s with no side effects;
* inactive vendors cannot receive new documents, approvals or payments;
* concurrent operations lose cleanly (conditional updates);
* ledger refusals and crashes during credit approval, payment reversal and
  payment correction leave no partial records -- including the multi-entry
  correction, which posts two journal entries in one transaction.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import Account, AccountType, EntryStatus, JournalEntry
from app.modules.payables import services
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import (
    BillStatus,
    CreditApplication,
    CreditStatus,
    PaymentAllocation,
    PaymentStatus,
    SupplierBill,
    SupplierCredit,
    VendorPayment,
)
from app.modules.payables.schemas import (
    AllocationIn,
    BillIn,
    BillLineIn,
    CreditIn,
    CreditLineIn,
    PaymentIn,
    VendorIn,
)

API = "/api/v1/payables"


# --- helpers (API) ---------------------------------------------------------------------------


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {
        "ap": _account(client, "2000", "liability"),
        "expense": _account(client, "6000", "expense"),
        "cash": _account(client, "1000", "asset"),
        "cash2": _account(client, "1010", "asset"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(
        f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _approved_bill(client, vendor, accounts, number="INV-1", total="150.00", **extra):
    r = client.post(
        f"{API}/bills",
        json={
            "vendor_id": vendor["id"],
            "invoice_number": number,
            "invoice_date": "2026-01-10",
            "due_date": "2026-02-09",
            "total": total,
            "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}],
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _payment_payload(vendor, accounts, bill_id, **overrides):
    payload = {
        "vendor_id": vendor["id"],
        "payment_date": "2026-01-20",
        "amount": "50.00",
        "method": "bank_transfer",
        "reference": "REF-1",
        "cash_account_id": accounts["cash"],
        "recorded_by": "bob",
        "idempotency_key": "key-1",
        "allocations": [{"bill_id": bill_id, "amount": "50.00"}],
    }
    payload.update(overrides)
    return payload


def _credit(client, vendor, accounts, number="CN-1", total="30.00", approve=True, **extra):
    r = client.post(
        f"{API}/credits",
        json={
            "vendor_id": vendor["id"],
            "credit_number": number,
            "credit_date": "2026-01-15",
            "total": total,
            "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": total}],
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _count(client, model) -> int:
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _journal(client):
    return client.get("/api/v1/journal-entries").json()


def _use(client, **integrations):
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(**integrations)


class RefusingLedger(JournalLedgerAdapter):
    def post(self, db, entry):
        raise LedgerPostingRefused("simulated refusal")


class RefuseSecondPosting(JournalLedgerAdapter):
    """Posts the first entry of a unit of work normally and refuses the second."""

    def __init__(self):
        self.posted = 0

    def post(self, db, entry):
        self.posted += 1
        if self.posted == 2:
            raise LedgerPostingRefused("second posting refused")
        return super().post(db, entry)


class CrashingLedger(JournalLedgerAdapter):
    def post(self, db, entry):
        raise RuntimeError("ledger unavailable")


# --- idempotency: the whole meaningful request is compared ---------------------------------------

IDEMPOTENCY_VARIANTS = {
    "method": {"method": "cheque"},
    "reference": {"reference": "REF-2"},
    "reference_cleared": {"reference": None},
    "payment_date": {"payment_date": "2026-01-21"},
    "cash_account_id": "cash2",
    "recorded_by": {"recorded_by": "mallory"},
    "amount_and_allocation": {"amount": "60.00", "allocations": [{"bill_id": "BILL", "amount": "60.00"}]},
    "allocation_split": {
        "amount": "50.00",
        "allocations": [{"bill_id": "BILL", "amount": "20.00"}, {"bill_id": "BILL2", "amount": "30.00"}],
    },
    "vendor_id": "other_vendor",
}


@pytest.mark.parametrize("field", sorted(IDEMPOTENCY_VARIANTS))
def test_idempotency_key_reuse_with_any_changed_field_is_a_conflict(client, vendor, accounts, field):
    bill = _approved_bill(client, vendor, accounts)
    bill2 = _approved_bill(client, vendor, accounts, number="INV-2")
    first = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"]))
    assert first.status_code == 201, first.text

    variant = IDEMPOTENCY_VARIANTS[field]
    if variant == "cash2":
        overrides = {"cash_account_id": accounts["cash2"]}
    elif variant == "other_vendor":
        other = client.post(
            f"{API}/vendors", json={"code": "OTHER", "name": "Other", "payable_account_id": accounts["ap"]}
        ).json()
        their_bill = _approved_bill(client, other, accounts, number="O-1")
        overrides = {"vendor_id": other["id"], "allocations": [{"bill_id": their_bill["id"], "amount": "50.00"}]}
    else:
        overrides = dict(variant)
        if "allocations" in overrides:
            overrides["allocations"] = [
                {"bill_id": bill["id"] if a["bill_id"] == "BILL" else bill2["id"], "amount": a["amount"]}
                for a in overrides["allocations"]
            ]

    entries_before = len(_journal(client))
    repeat = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"], **overrides))
    assert repeat.status_code == 409, (field, repeat.text)
    assert "different payment" in repeat.json()["detail"]

    # Nothing was recorded for the conflicting request.
    assert _count(client, VendorPayment) == 1
    assert len(_journal(client)) == entries_before
    assert client.get(f"{API}/bills/{bill['id']}").json()["outstanding"] == "100.00"


def test_idempotent_repeat_with_identical_request_returns_original(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    payload = _payment_payload(vendor, accounts, bill["id"], method="cheque", reference="CHQ-9")
    first = client.post(f"{API}/payments", json=payload)
    assert first.status_code == 201
    for _ in range(3):
        repeat = client.post(f"{API}/payments", json=payload)
        assert repeat.status_code == 200
        assert repeat.json() == first.json()
    assert _count(client, VendorPayment) == 1


def test_idempotency_key_cannot_be_reused_by_a_correction_replacement(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    first = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"]))
    assert first.status_code == 201
    second = client.post(
        f"{API}/payments",
        json=_payment_payload(vendor, accounts, bill["id"], idempotency_key="key-2", amount="20.00",
                              allocations=[{"bill_id": bill["id"], "amount": "20.00"}]),
    )
    assert second.status_code == 201
    r = client.post(
        f"{API}/payments/{second.json()['id']}/correct",
        json={
            "actor": "carol",
            "reason": "oops",
            "reversal_date": "2026-01-25",
            "replacement": _payment_payload(
                vendor, accounts, bill["id"], payment_date="2026-01-25", amount="25.00",
                allocations=[{"bill_id": bill["id"], "amount": "25.00"}],
            ),  # reuses key-1
        },
    )
    assert r.status_code == 409
    assert "Idempotency key 'key-1'" in r.json()["detail"]
    assert client.get(f"{API}/payments/{second.json()['id']}").json()["status"] == "recorded"
    assert _count(client, VendorPayment) == 2
    assert len(_journal(client)) == 3


# --- duplicate submissions of state changes ---------------------------------------------------------


def test_duplicate_credit_workflow_submissions_are_conflicts(client, vendor, accounts):
    credit = _credit(client, vendor, accounts, approve=False)
    cid = credit["id"]
    assert client.post(f"{API}/credits/{cid}/approve", json={"actor": "alice"}).status_code == 200
    assert client.post(f"{API}/credits/{cid}/approve", json={"actor": "alice"}).status_code == 409
    assert client.post(f"{API}/credits/{cid}/reject", json={"actor": "a", "reason": "x"}).status_code == 409
    assert client.post(f"{API}/credits/{cid}/reopen", json={"actor": "a"}).status_code == 409
    assert client.put(f"{API}/credits/{cid}", json={"total": "1.00"}).status_code == 409
    assert client.delete(f"{API}/credits/{cid}").status_code == 409
    assert len(_journal(client)) == 1

    r = client.post(f"{API}/credits/{cid}/reverse", json={"actor": "carol", "reason": "mistake"})
    assert r.status_code == 200, r.text
    again = client.post(f"{API}/credits/{cid}/reverse", json={"actor": "carol", "reason": "mistake"})
    assert again.status_code == 409
    assert "already reversed" in again.json()["detail"]
    assert len(_journal(client)) == 2


def test_duplicate_credit_application_is_a_conflict_with_no_side_effects(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    credit = _credit(client, vendor, accounts, total="60.00")
    payload = {"bill_id": bill["id"], "amount": "20.00", "actor": "bob", "application_date": "2026-01-20"}
    assert client.post(f"{API}/credits/{credit['id']}/applications", json=payload).status_code == 200
    repeat = client.post(f"{API}/credits/{credit['id']}/applications", json=payload)
    assert repeat.status_code == 422
    assert "already applied to bill" in repeat.json()["detail"]
    assert _count(client, CreditApplication) == 1
    assert client.get(f"{API}/bills/{bill['id']}").json()["credits_applied"] == "20.00"
    assert client.get(f"{API}/credits/{credit['id']}").json()["remaining"] == "40.00"


def test_duplicate_reversal_requests(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    pay = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"])).json()
    body = {"actor": "carol", "reason": "duplicate run", "reversal_date": "2026-01-22"}
    assert client.post(f"{API}/payments/{pay['id']}/reverse", json=body).status_code == 200
    again = client.post(f"{API}/payments/{pay['id']}/reverse", json=body)
    assert again.status_code == 409
    corrected = client.post(
        f"{API}/payments/{pay['id']}/correct",
        json={**body, "replacement": _payment_payload(vendor, accounts, bill["id"], idempotency_key=None,
                                                      payment_date="2026-01-22")},
    )
    assert corrected.status_code == 409
    assert _count(client, VendorPayment) == 1
    assert len(_journal(client)) == 3  # approval, payment, one reversal


# --- inactive vendors ---------------------------------------------------------------------------------


def test_inactive_vendor_blocks_new_documents_approvals_and_payments(client, vendor, accounts):
    open_bill = _approved_bill(client, vendor, accounts, number="OPEN")
    draft = client.post(
        f"{API}/bills",
        json={"vendor_id": vendor["id"], "invoice_number": "DRAFT", "invoice_date": "2026-01-10",
              "total": "10.00", "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "10.00"}]},
    ).json()
    draft_credit = _credit(client, vendor, accounts, number="CN-D", approve=False)
    paid = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, open_bill["id"])).json()
    approved_credit = _credit(client, vendor, accounts, number="CN-A", total="20.00")

    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"is_active": False}).status_code == 200

    def refused(response):
        assert response.status_code == 422, response.text
        assert "inactive" in response.json()["detail"]

    refused(client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "NEW",
                                             "invoice_date": "2026-01-10", "total": "1.00"}))
    refused(client.post(f"{API}/credits", json={"vendor_id": vendor["id"], "credit_number": "CN-NEW",
                                               "credit_date": "2026-01-10", "total": "1.00"}))
    refused(client.post(f"{API}/bills/{draft['id']}/approve", json={"actor": "alice"}))
    refused(client.post(f"{API}/credits/{draft_credit['id']}/approve", json={"actor": "alice"}))
    refused(client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, open_bill["id"], idempotency_key=None)))
    refused(client.post(
        f"{API}/payments/{paid['id']}/correct",
        json={"actor": "c", "reason": "r", "reversal_date": "2026-01-25",
              "replacement": _payment_payload(vendor, accounts, open_bill["id"], idempotency_key=None, payment_date="2026-01-25")},
    ))
    assert client.get(f"{API}/payments/{paid['id']}").json()["status"] == "recorded"

    # Corrections of history and settlement of existing items remain possible.
    assert client.post(f"{API}/payments/{paid['id']}/reverse",
                       json={"actor": "c", "reason": "vendor closed", "reversal_date": "2026-01-25"}).status_code == 200
    assert client.post(f"{API}/credits/{approved_credit['id']}/applications",
                       json={"bill_id": open_bill["id"], "amount": "20.00", "actor": "bob", "application_date": "2026-01-25"}).status_code == 200
    assert client.post(f"{API}/bills/{draft['id']}/reject", json={"actor": "a", "reason": "vendor inactive"}).status_code == 200
    assert client.delete(f"{API}/credits/{draft_credit['id']}").status_code == 204


# --- ledger failures leave no partial records --------------------------------------------------------


def test_credit_approval_rolls_back_when_ledger_refuses(client, vendor, accounts):
    credit = _credit(client, vendor, accounts, approve=False)
    _use(client, ledger=RefusingLedger())
    r = client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 422
    assert "ledger refused" in r.json()["detail"]
    current = client.get(f"{API}/credits/{credit['id']}").json()
    assert current["status"] == "draft" and current["journal_entry_id"] is None
    assert _journal(client) == []
    client.app.dependency_overrides.clear()
    assert client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"}).status_code == 200


def test_payment_reversal_rolls_back_when_ledger_refuses(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    pay = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"])).json()
    _use(client, ledger=RefusingLedger())
    r = client.post(f"{API}/payments/{pay['id']}/reverse", json={"actor": "c", "reason": "r"})
    assert r.status_code == 422
    current = client.get(f"{API}/payments/{pay['id']}").json()
    assert current["status"] == "recorded" and current["reversal_journal_entry_id"] is None
    assert client.get(f"{API}/bills/{bill['id']}").json()["outstanding"] == "100.00"
    assert len(_journal(client)) == 2


def test_correction_is_atomic_when_second_posting_is_refused(client, vendor, accounts):
    """The reversal posts fine, the replacement is refused: neither survives."""
    bill = _approved_bill(client, vendor, accounts)
    pay = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"])).json()
    _use(client, ledger=RefuseSecondPosting())
    r = client.post(
        f"{API}/payments/{pay['id']}/correct",
        json={"actor": "c", "reason": "r", "reversal_date": "2026-01-25",
              "replacement": _payment_payload(vendor, accounts, bill["id"], idempotency_key=None,
                                              payment_date="2026-01-25", amount="40.00",
                                              allocations=[{"bill_id": bill["id"], "amount": "40.00"}])},
    )
    assert r.status_code == 422
    assert "second posting refused" in r.json()["detail"]
    current = client.get(f"{API}/payments/{pay['id']}").json()
    assert current["status"] == "recorded"
    assert _count(client, VendorPayment) == 1
    assert _count(client, PaymentAllocation) == 1
    assert len(_journal(client)) == 2  # approval + original payment; no reversal entry survived
    assert client.get(f"{API}/bills/{bill['id']}").json()["outstanding"] == "100.00"


def test_correction_rolls_back_when_replacement_is_invalid(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    pay = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"])).json()
    r = client.post(
        f"{API}/payments/{pay['id']}/correct",
        json={"actor": "c", "reason": "r", "reversal_date": "2026-01-25",
              "replacement": _payment_payload(vendor, accounts, bill["id"], idempotency_key=None,
                                              payment_date="2026-01-25", amount="500.00",
                                              allocations=[{"bill_id": bill["id"], "amount": "500.00"}])},
    )
    assert r.status_code == 422
    assert "exceeds its outstanding" in r.json()["detail"]
    assert client.get(f"{API}/payments/{pay['id']}").json()["status"] == "recorded"
    assert len(_journal(client)) == 2
    assert _count(client, VendorPayment) == 1


def test_unexpected_crash_during_correction_leaves_no_trace(client, vendor, accounts):
    bill = _approved_bill(client, vendor, accounts)
    pay = client.post(f"{API}/payments", json=_payment_payload(vendor, accounts, bill["id"])).json()
    _use(client, ledger=CrashingLedger())
    with TestClient(client.app, raise_server_exceptions=False) as lenient:
        r = lenient.post(
            f"{API}/payments/{pay['id']}/correct",
            json={"actor": "c", "reason": "r", "reversal_date": "2026-01-25",
                  "replacement": _payment_payload(vendor, accounts, bill["id"], idempotency_key=None,
                                                  payment_date="2026-01-25")},
        )
    assert r.status_code == 500
    assert client.get(f"{API}/payments/{pay['id']}").json()["status"] == "recorded"
    assert _count(client, VendorPayment) == 1
    assert len(_journal(client)) == 2


def test_database_backstops_for_credit_applications(client, vendor, accounts, monkeypatch):
    """Skip the application-level checks: the triggers still refuse and nothing persists."""
    bill = _approved_bill(client, vendor, accounts)
    credit = _credit(client, vendor, accounts, total="30.00")
    original = services.apply_credit

    def unchecked_apply(db, credit_id, **kw):
        # Bypass validation by writing the row directly, then syncing statuses.
        credit_row = db.get(SupplierCredit, credit_id)
        bill_row = db.get(SupplierBill, kw["bill_id"])
        db.add(CreditApplication(credit=credit_row, bill=bill_row, amount=kw["amount"],
                                 application_date=date(2026, 1, 20), applied_by=kw["actor"]))
        services._sync_bill_status(db, bill_row)
        services._sync_credit_status(db, credit_row)
        db.commit()
        return credit_row

    monkeypatch.setattr(services, "apply_credit", unchecked_apply)
    r = client.post(f"{API}/credits/{credit['id']}/applications",
                    json={"bill_id": bill["id"], "amount": "31.00", "actor": "bob"})
    assert r.status_code == 422
    assert "exceeds the remaining amount of the credit" in r.json()["detail"]
    assert _count(client, CreditApplication) == 0
    assert client.get(f"{API}/credits/{credit['id']}").json()["status"] == "approved"
    monkeypatch.setattr(services, "apply_credit", original)


# --- concurrency (service level) ----------------------------------------------------------------------


@pytest.fixture
def world(session):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add_all([ap, expense, cash])
    session.commit()
    vendor = services.create_vendor(session, VendorIn(code="V1", name="Vendor", payable_account_id=ap.id))
    return {"ap": ap, "expense": expense, "cash": cash, "vendor": vendor}


def _bill(session, world, number="INV-1", total="100.00"):
    bill = services.create_bill(
        session,
        BillIn(vendor_id=world["vendor"].id, invoice_number=number, invoice_date=date(2026, 1, 10),
               total=total, lines=[BillLineIn(description="x", account_id=world["expense"].id, unit_price=total)]),
    )
    return services.approve_bill(session, bill.id, actor="alice", integrations=Integrations())


def _payment(session, world, bill, amount="40.00", **extra):
    payment, _ = services.record_payment(
        session,
        PaymentIn(vendor_id=world["vendor"].id, payment_date=date(2026, 1, 20), amount=amount,
                  cash_account_id=world["cash"].id, recorded_by="bob",
                  allocations=[AllocationIn(bill_id=bill.id, amount=amount)], **extra),
        integrations=Integrations(),
    )
    return payment


def test_concurrent_reversal_loses_cleanly(session, engine, world, monkeypatch):
    """Another connection reverses the payment right after we loaded it (SQLite).

    On PostgreSQL the ``FOR UPDATE`` lock taken when loading the payment makes the
    other connection wait instead (covered by the row-lock test below).
    """
    if engine.dialect.name == "postgresql":
        pytest.skip("FOR UPDATE on the payment row blocks the other connection on PostgreSQL")
    bill = _bill(session, world)
    payment = _payment(session, world, bill)
    entry_count_before = session.scalar(select(func.count()).select_from(JournalEntry))
    original_lock = services.payments.lock

    def lock_then_lose_race(db, model, ids):
        rows = original_lock(db, model, ids)
        if model is VendorPayment:
            with engine.begin() as other:
                other.execute(
                    text("UPDATE vendor_payments SET status = 'reversed', reversed_by = 'other', "
                         "reversal_date = '2026-01-21', reversal_journal_entry_id = :e WHERE id = :id"),
                    {"e": payment.journal_entry_id, "id": payment.id},
                )
        return rows

    monkeypatch.setattr(services.payments, "lock", lock_then_lose_race)
    with pytest.raises(services.Conflict, match="no longer recorded"):
        services.reverse_payment(session, payment.id, actor="carol", reason="r",
                                 integrations=Integrations())
    session.rollback()
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(JournalEntry)) == entry_count_before
    assert session.get(VendorPayment, payment.id).reversed_by == "other"
    assert session.get(SupplierBill, bill.id).status == BillStatus.PARTIALLY_PAID  # untouched by us


def test_over_settlement_by_concurrent_credit_is_refused(session, engine, world, monkeypatch):
    """A credit is applied to the bill between our validation and our allocation insert."""
    bill = _bill(session, world)
    credit = services.create_credit(
        session,
        CreditIn(vendor_id=world["vendor"].id, credit_number="CN-1", credit_date=date(2026, 1, 12), total="80.00",
                 lines=[CreditLineIn(description="c", account_id=world["expense"].id, unit_price="80.00")]),
    )
    services.approve_credit(session, credit.id, actor="alice", integrations=Integrations())
    original = services.payments._allocation_problems

    def apply_credit_underneath(vendor, bills, allocations, pay_date):
        problems = original(vendor, bills, allocations, pay_date)
        if engine.dialect.name == "sqlite":
            with engine.begin() as other:
                other.execute(text(
                    "INSERT INTO credit_applications (credit_id, bill_id, amount, application_date, applied_by) "
                    "VALUES (:c, :b, 80.00, '2026-01-19', 'other')"), {"c": credit.id, "b": bill.id})
        return problems

    if engine.dialect.name == "postgresql":
        pytest.skip("FOR UPDATE on the bill row blocks the other connection on PostgreSQL")
    monkeypatch.setattr(services.payments, "_allocation_problems", apply_credit_underneath)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="allocation exceeds the outstanding amount"):
        _payment(session, world, bill, amount="40.00")
    session.rollback()
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(VendorPayment)) == 0
    assert session.get(SupplierBill, bill.id).credits_applied == 80


def test_postgres_row_locks_serialise_settlement(session, engine, world):
    """On PostgreSQL the bill row is locked FOR UPDATE while a payment is recorded."""
    if engine.dialect.name != "postgresql":
        pytest.skip("row locks are a PostgreSQL feature")
    import threading

    bill = _bill(session, world)
    locked = threading.Event()
    release = threading.Event()
    timings = {}

    def holder():
        with Session(engine) as other:
            other.execute(select(SupplierBill).where(SupplierBill.id == bill.id).with_for_update())
            locked.set()
            release.wait(timeout=10)
            other.rollback()

    import time

    t = threading.Thread(target=holder)
    t.start()
    locked.wait(timeout=10)
    started = time.monotonic()
    timer = threading.Timer(1.0, release.set)
    timer.start()
    _payment(session, world, bill)  # blocks until the holder releases the lock
    timings["elapsed"] = time.monotonic() - started
    t.join()
    assert timings["elapsed"] >= 0.9
    assert session.get(SupplierBill, bill.id).status == BillStatus.PARTIALLY_PAID


# --- the adapter's savepoint-based posting ---------------------------------------------------------


def test_adapter_post_does_not_commit(session, engine, world):
    """After ``post`` the entry is posted inside our transaction only; rollback undoes it."""
    bill = services.create_bill(
        session,
        BillIn(vendor_id=world["vendor"].id, invoice_number="X", invoice_date=date(2026, 1, 10), total="10.00",
               lines=[BillLineIn(description="x", account_id=world["expense"].id, unit_price="10.00")]),
    )
    adapter = JournalLedgerAdapter()
    entry = adapter.draft_bill_entry(session, bill)
    adapter.post(session, entry)
    assert entry.status == EntryStatus.POSTED
    with Session(engine) as other:
        assert other.scalar(select(func.count()).select_from(JournalEntry)) == 0
    session.rollback()
    assert session.scalar(select(func.count()).select_from(JournalEntry)) == 0


def test_credit_status_follows_applications(session, world):
    bill = _bill(session, world)
    credit = services.create_credit(
        session,
        CreditIn(vendor_id=world["vendor"].id, credit_number="CN", credit_date=date(2026, 1, 12), total="50.00",
                 lines=[CreditLineIn(description="c", account_id=world["expense"].id, unit_price="50.00")]),
    )
    services.approve_credit(session, credit.id, actor="a", integrations=Integrations())
    services.apply_credit(session, credit.id, bill_id=bill.id, amount=Decimal("20.00"), actor="b",
                          application_date=date(2026, 1, 20), integrations=Integrations())
    assert credit.status == CreditStatus.PARTIALLY_APPLIED and bill.status == BillStatus.PARTIALLY_PAID
    application_id = credit.applications[0].id
    services.unapply_credit(session, credit.id, application_id, actor="b", integrations=Integrations())
    assert credit.status == CreditStatus.APPROVED and bill.status == BillStatus.APPROVED
    services.apply_credit(session, credit.id, bill_id=bill.id, amount=Decimal("50.00"), actor="b",
                          application_date=date(2026, 1, 21), integrations=Integrations())
    assert credit.status == CreditStatus.APPLIED and credit.remaining == 0
    assert bill.outstanding == 50
    # A fully applied credit cannot be reversed; its status is not "approved".
    with pytest.raises(services.Conflict, match="unapply first"):
        services.reverse_credit(session, credit.id, actor="c", reason="r", integrations=Integrations())
    session.rollback()
    assert session.get(SupplierCredit, credit.id).status == CreditStatus.APPLIED
    assert session.get(SupplierCredit, credit.id).status.value == "applied"
    assert PaymentStatus.RECORDED.value == "recorded"
