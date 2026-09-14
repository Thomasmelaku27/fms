"""Focused tests for the payables service layer and its ledger adapter.

Pure functions (money rounding, normalisation, aging buckets, FIFO schedule) run
without a database; the rest use the migrated ``session`` fixture directly to
check transaction boundaries, concurrency guards and the adapter's contract with
the core posting service.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Account, AccountType, EntryStatus, JournalEntry
from app.modules.payables import ledger_adapter, services
from app.modules.payables.integration import Integrations, NullAudit, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import (
    BillPaymentSchedule,
    BillStatus,
    SupplierBill,
    VendorPayment,
    normalize_invoice_number,
)
from app.modules.payables.schemas import (
    AllocationIn,
    BillIn,
    BillLineIn,
    PaymentIn,
    VendorIn,
    compute_line_total,
)

# --- pure functions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "unit_price", "expected"),
    [
        ("1", "10", "10.00"),
        ("3", "12.3333", "37.00"),  # 36.9999 rounds up
        ("2", "1.005", "2.01"),  # half-up, not banker's rounding
        ("0.5", "0.01", "0.01"),  # 0.005 -> 0.01
        ("1.2345", "1.2345", "1.52"),  # 1.52399 -> 1.52
        ("1000000", "0.0001", "100.00"),
    ],
)
def test_line_total_rounds_half_up_to_cents(quantity, unit_price, expected):
    assert compute_line_total(Decimal(quantity), Decimal(unit_price)) == Decimal(expected)


def test_line_total_is_decimal_not_float():
    total = compute_line_total(Decimal("0.1"), Decimal("3"))
    assert isinstance(total, Decimal)
    assert total == Decimal("0.30")
    assert str(total) == "0.30"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("INV-1", "INV-1"),
        ("inv-1", "INV-1"),
        ("  inv-1\t", "INV-1"),
        ("inv   1", "INV 1"),
        ("Inv 1", "INV 1"),
    ],
)
def test_normalize_invoice_number(raw, normalized):
    assert normalize_invoice_number(raw) == normalized


def test_normalization_keeps_distinct_numbers_distinct():
    assert normalize_invoice_number("INV-1") != normalize_invoice_number("INV 1")
    assert normalize_invoice_number("INV-1") != normalize_invoice_number("INV-10")


@pytest.mark.parametrize(
    ("days", "bucket"),
    [(-10, "current"), (0, "current"), (1, "days_1_30"), (30, "days_1_30"), (31, "days_31_60"),
     (60, "days_31_60"), (61, "days_61_90"), (90, "days_61_90"), (91, "over_90"), (400, "over_90")],
)
def test_aging_bucket_boundaries(days, bucket):
    assert services.aging_bucket(days) == bucket


def test_installments_paid_earliest_first():
    installments = [
        BillPaymentSchedule(installment_no=1, due_date=date(2026, 2, 1), amount=Decimal("50.00")),
        BillPaymentSchedule(installment_no=2, due_date=date(2026, 3, 1), amount=Decimal("30.00")),
        BillPaymentSchedule(installment_no=3, due_date=date(2026, 4, 1), amount=Decimal("20.00")),
    ]
    remaining = [
        i.amount_remaining
        for i in services._installments_with_remaining(installments, Decimal("65.00"))
    ]
    assert remaining == [Decimal("0.00"), Decimal("15.00"), Decimal("20.00")]
    assert [
        i.amount_remaining
        for i in services._installments_with_remaining(installments, Decimal("0.00"))
    ] == [Decimal("50.00"), Decimal("30.00"), Decimal("20.00")]
    assert all(
        i.amount_remaining == 0
        for i in services._installments_with_remaining(installments, Decimal("100.00"))
    )


def test_bill_line_schema_fills_or_checks_line_total():
    line = BillLineIn(description="x", account_id=1, quantity=Decimal("3"), unit_price=Decimal("12.3333"))
    assert line.line_total == Decimal("37.00")
    with pytest.raises(ValueError, match="does not equal quantity x unit_price"):
        BillLineIn(description="x", account_id=1, quantity=Decimal("3"), unit_price=Decimal("12.3333"), line_total=Decimal("36.99"))
    with pytest.raises(ValueError, match="decimal"):
        BillLineIn(description="x", account_id=1, unit_price=Decimal("1.00001"))


def test_payment_schema_allows_unapplied_remainder_but_not_over_allocation():
    with pytest.raises(ValueError, match="exceed the payment amount"):
        PaymentIn(
            vendor_id=1, payment_date=date(2026, 1, 1), amount=Decimal("10.00"), cash_account_id=1,
            recorded_by="x", allocations=[AllocationIn(bill_id=1, amount=Decimal("10.01"))],
        )
    # Allocating less than the amount leaves an advance (unapplied) remainder.
    partial = PaymentIn(
        vendor_id=1, payment_date=date(2026, 1, 1), amount=Decimal("10.00"), cash_account_id=1,
        recorded_by="x", allocations=[AllocationIn(bill_id=1, amount=Decimal("9.99"))],
    )
    assert partial.amount - sum(a.amount for a in partial.allocations) == Decimal("0.01")


# --- database-backed fixtures ------------------------------------------------------------


@pytest.fixture
def world(session):
    ap = Account(code="2000", name="AP", type=AccountType.LIABILITY)
    ap2 = Account(code="2010", name="AP other", type=AccountType.LIABILITY)
    expense = Account(code="6000", name="Expense", type=AccountType.EXPENSE)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add_all([ap, ap2, expense, cash])
    session.commit()
    vendor = services.create_vendor(
        session, VendorIn(code="V1", name="Vendor", payable_account_id=ap.id, payment_terms_days=14)
    )
    return {"ap": ap, "ap2": ap2, "expense": expense, "cash": cash, "vendor": vendor}


def draft(session, world, number="INV-1", total="100.00", **extra):
    return services.create_bill(
        session,
        BillIn(
            vendor_id=world["vendor"].id,
            invoice_number=number,
            invoice_date=date(2026, 1, 10),
            total=Decimal(total),
            lines=[BillLineIn(description="x", account_id=world["expense"].id, unit_price=Decimal(total))],
            **extra,
        ),
    )


def payment_in(world, allocations, **extra):
    return PaymentIn(
        vendor_id=world["vendor"].id,
        payment_date=date(2026, 1, 20),
        amount=sum((Decimal(a) for _, a in allocations), Decimal("0")),
        cash_account_id=world["cash"].id,
        recorded_by="bob",
        allocations=[AllocationIn(bill_id=b, amount=Decimal(a)) for b, a in allocations],
        **extra,
    )


INTEGRATIONS = Integrations()


# --- services -----------------------------------------------------------------------------


def test_due_date_defaults_to_vendor_terms(session, world):
    bill = draft(session, world)
    assert bill.due_date == date(2026, 1, 24)
    assert bill.payable_account_id == world["ap"].id
    assert bill.invoice_number_normalized == "INV-1"


def test_approval_problems_lists_every_reason(session, world):
    bill = services.create_bill(
        session,
        BillIn(vendor_id=world["vendor"].id, invoice_number="E", invoice_date=date(2026, 1, 1), total=Decimal("5.00")),
    )
    bill.payable_account_id = None
    session.commit()
    assert services.approval_problems(session, bill) == [
        "bill has no lines",
        "bill has no payable account (set one on the bill or the vendor)",
    ]

    bill = draft(session, world, number="F")
    world["ap"].is_active = False
    world["expense"].is_active = False
    session.commit()
    problems = services.approval_problems(session, bill)
    assert problems == ["Payable account 2000 is inactive", "Inactive line accounts: 6000"]


def test_approval_is_atomic_and_visible_to_other_sessions(session, engine, world):
    from sqlalchemy.orm import Session

    bill = draft(session, world)
    approved = services.approve_bill(session, bill.id, actor="alice", integrations=INTEGRATIONS)
    assert approved.status == BillStatus.APPROVED
    assert approved.approved_by == "alice"
    assert approved.approved_at is not None

    with Session(engine) as other:
        stored = other.get(SupplierBill, bill.id)
        assert stored.status == BillStatus.APPROVED
        entry = other.get(JournalEntry, stored.journal_entry_id)
        assert entry.status == EntryStatus.POSTED
        assert sum(l.debit for l in entry.lines) == sum(l.credit for l in entry.lines) == Decimal("100.00")
        assert [s.amount for s in stored.schedule] == [Decimal("100.00")]


def test_approve_detects_concurrent_state_change(session, engine, world, monkeypatch):
    """Another connection rejects the bill between our validation and our write."""
    from sqlalchemy import text

    bill = draft(session, world)
    original = services.documents.approval_problems

    def reject_from_another_connection(db, b):
        problems = original(db, b)
        with engine.begin() as other:
            other.execute(
                text("UPDATE supplier_bills SET status = 'rejected' WHERE id = :id"), {"id": b.id}
            )
        return problems

    monkeypatch.setattr(services.documents, "approval_problems", reject_from_another_connection)
    with pytest.raises(services.Conflict, match="no longer draft"):
        services.approve_bill(session, bill.id, actor="alice", integrations=INTEGRATIONS)
    session.rollback()
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(JournalEntry)) == 0
    assert session.get(SupplierBill, bill.id).status == BillStatus.REJECTED
    assert session.get(SupplierBill, bill.id).journal_entry_id is None


def test_record_payment_survives_idempotency_race(session, world, monkeypatch):
    """Two identical requests race: the second loses on the UNIQUE key and returns the first."""
    bill = draft(session, world)
    services.approve_bill(session, bill.id, actor="alice", integrations=INTEGRATIONS)
    data = payment_in(world, [(bill.id, "40.00")], idempotency_key="race")
    first, created = services.record_payment(session, data, integrations=INTEGRATIONS)
    assert created

    # Make the pre-check miss once so the INSERT hits the UNIQUE constraint.
    original = services.payments._existing_by_key
    calls = {"n": 0}

    def flaky_lookup(db, d):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(db, d)

    monkeypatch.setattr(services.payments, "_existing_by_key", flaky_lookup)
    second, created = services.record_payment(session, data, integrations=INTEGRATIONS)
    assert not created
    assert second.id == first.id
    assert session.scalar(select(func.count()).select_from(VendorPayment)) == 1
    assert session.scalar(select(func.count()).select_from(JournalEntry)) == 2


def test_payment_refused_when_cash_account_inactive(session, world):
    bill = draft(session, world)
    services.approve_bill(session, bill.id, actor="alice", integrations=INTEGRATIONS)
    world["cash"].is_active = False
    session.commit()
    with pytest.raises(services.ValidationFailed, match="Cash account 1000 is inactive"):
        services.record_payment(session, payment_in(world, [(bill.id, "1.00")]), integrations=INTEGRATIONS)
    assert session.scalar(select(func.count()).select_from(VendorPayment)) == 0


# --- ledger adapter -----------------------------------------------------------------------------


def test_adapter_drafts_balanced_entry_and_posts_through_core_service(session, world, monkeypatch):
    bill = draft(session, world, total="150.00")
    adapter = JournalLedgerAdapter()

    entry = adapter.draft_bill_entry(session, bill)
    assert entry.id is not None
    assert entry.status == EntryStatus.DRAFT  # drafted, not yet posted
    assert entry.reference == f"AP-BILL-{bill.id}"
    assert [(l.account_id, l.debit, l.credit) for l in entry.lines] == [
        (world["expense"].id, Decimal("150.00"), Decimal("0")),
        (world["ap"].id, Decimal("0"), Decimal("150.00")),
    ]

    calls = []
    real_post = ledger_adapter.post_entry

    def spy(db, e):
        calls.append(e.id)
        return real_post(db, e)

    monkeypatch.setattr(ledger_adapter, "post_entry", spy)
    posted = adapter.post(session, entry)
    assert calls == [entry.id]
    assert posted.status == EntryStatus.POSTED


def test_adapter_groups_payment_debits_by_payable_account(session, world):
    one = draft(session, world, number="A", total="60.00")
    two = draft(session, world, number="B", total="40.00", payable_account_id=world["ap2"].id)
    three = draft(session, world, number="C", total="10.00")
    for bill in (one, two, three):
        services.approve_bill(session, bill.id, actor="alice", integrations=INTEGRATIONS)

    payment, _ = services.record_payment(
        session,
        payment_in(world, [(one.id, "60.00"), (two.id, "40.00"), (three.id, "10.00")], reference="RUN-1"),
        integrations=INTEGRATIONS,
    )
    entry = session.get(JournalEntry, payment.journal_entry_id)
    assert entry.status == EntryStatus.POSTED
    assert entry.description == "Vendor payment RUN-1 - Vendor"
    assert [(l.account_id, l.debit, l.credit) for l in entry.lines] == [
        (world["ap"].id, Decimal("70.00"), Decimal("0.00")),
        (world["ap2"].id, Decimal("40.00"), Decimal("0.00")),
        (world["cash"].id, Decimal("0.00"), Decimal("110.00")),
    ]


def test_adapter_translates_core_refusal(session, world):
    """The core's own rules still apply: an inactive account blocks the AP posting."""
    bill = draft(session, world)
    adapter = JournalLedgerAdapter()
    entry = adapter.draft_bill_entry(session, bill)
    world["expense"].is_active = False
    session.flush()
    with pytest.raises(LedgerPostingRefused, match="inactive accounts: 6000"):
        adapter.post(session, entry)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(JournalEntry)) == 0


def test_adapter_never_issues_updates_or_deletes_on_journal_tables(session, world):
    """Statement-level proof: the adapter only INSERTs into journal tables, then posts."""
    from sqlalchemy import event

    statements = []

    @event.listens_for(session.bind, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    try:
        bill = draft(session, world)
        services.approve_bill(session, bill.id, actor="alice", integrations=INTEGRATIONS)
        services.record_payment(session, payment_in(world, [(bill.id, "100.00")]), integrations=INTEGRATIONS)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)

    journal_writes = [
        s for s in statements
        if s.startswith(("UPDATE JOURNAL_", "DELETE FROM JOURNAL_"))
    ]
    # The only UPDATE is the posting service's conditional draft -> posted transition
    # (one per posting: bill approval and payment); never a DELETE, never a line update.
    assert len(journal_writes) == 2, journal_writes
    for statement in journal_writes:
        assert statement.startswith("UPDATE JOURNAL_ENTRIES SET STATUS=")
        assert "WHERE JOURNAL_ENTRIES.ID = " in statement and "AND JOURNAL_ENTRIES.STATUS = " in statement


def test_default_integrations_are_production_adapters():
    integrations = get_integrations()
    assert isinstance(integrations.ledger, JournalLedgerAdapter)
    assert isinstance(integrations.audit, NullAudit)
