"""Upgrading a database that already holds AP history (SQLite and PostgreSQL).

The history is written at the ``fmspayables02`` schema with raw SQL (approved
bills with posted journal entries, payments with allocations, a reversed
payment, an applied credit), the database is upgraded to head, and the new
services and reports are checked against the numbers that were true before the
upgrade. Finally the downgrade back to ``fmspayables02`` is exercised with
v3-only rows present.
"""

from datetime import date
from decimal import Decimal

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from app.modules.payables.models import (
    AllocationKind,
    CreditApplication,
    PaymentAllocation,
    SupplierBill,
    Vendor,
    VendorPayment,
)
from tests.conftest import alembic_config, migrate

D = Decimal


def _post_entry(conn, entry_date, description, lines):
    """Insert a posted journal entry through the core tables (draft -> lines -> posted)."""
    conn.execute(text("INSERT INTO journal_entries (entry_date, description, reference, status) VALUES (:d, :desc, NULL, 'draft')"),
                 {"d": entry_date, "desc": description})
    entry_id = conn.execute(text("SELECT MAX(id) FROM journal_entries")).scalar()
    for account_id, debit, credit in lines:
        conn.execute(text("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (:e, :a, :dr, :cr)"),
                     {"e": entry_id, "a": account_id, "dr": debit, "cr": credit})
    conn.execute(text("UPDATE journal_entries SET status = 'posted' WHERE id = :e"), {"e": entry_id})
    return entry_id


def _seed_v2_history(url: str) -> dict:
    """Write a small but complete AP history using only the fmspayables02 schema."""
    engine = make_engine(url)
    ids = {}
    with engine.begin() as conn:
        for code, name, type_ in (("2000", "AP", "liability"), ("6000", "Expense", "expense"), ("1000", "Cash", "asset")):
            conn.execute(text("INSERT INTO accounts (code, name, type, is_active) VALUES (:c, :n, :t, true)"), {"c": code, "n": name, "t": type_})
        acct = dict(conn.execute(text("SELECT code, id FROM accounts")).all())
        ids["ap"], ids["expense"], ids["cash"] = acct["2000"], acct["6000"], acct["1000"]
        conn.execute(text("INSERT INTO vendors (code, name, is_active, payable_account_id, payment_terms_days) VALUES ('OLD', 'Legacy Supplies', true, :ap, 30)"),
                     {"ap": ids["ap"]})
        conn.execute(text("INSERT INTO vendors (code, name, is_active, payable_account_id, payment_terms_days) VALUES ('GONE', 'Former Supplier', false, :ap, 30)"),
                     {"ap": ids["ap"]})
        ids["vendor"] = conn.execute(text("SELECT id FROM vendors WHERE code = 'OLD'")).scalar()

        def bill(number, total, invoice_date, due_date):
            entry = _post_entry(conn, invoice_date, f"Supplier bill {number}", [(ids["expense"], total, 0), (ids["ap"], 0, total)])
            conn.execute(text(
                "INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, invoice_date, due_date, total, status, "
                "payable_account_id, journal_entry_id, approved_by, updated_at) "
                "VALUES (:v, :n, :n, :inv, :due, :t, 'draft', :ap, NULL, 'legacy', CURRENT_TIMESTAMP)"),
                {"v": ids["vendor"], "n": number, "inv": invoice_date, "due": due_date, "t": total, "ap": ids["ap"]})
            bill_id = conn.execute(text("SELECT MAX(id) FROM supplier_bills")).scalar()
            conn.execute(text("INSERT INTO supplier_bill_lines (bill_id, line_no, description, account_id, quantity, unit_price, line_total) "
                              "VALUES (:b, 1, 'legacy line', :a, 1, :t, :t)"), {"b": bill_id, "a": ids["expense"], "t": total})
            conn.execute(text("UPDATE supplier_bills SET status = 'approved', journal_entry_id = :e WHERE id = :b"), {"e": entry, "b": bill_id})
            conn.execute(text("INSERT INTO bill_payment_schedules (bill_id, installment_no, due_date, amount) VALUES (:b, 1, :due, :t)"),
                         {"b": bill_id, "due": due_date, "t": total})
            return bill_id

        ids["b1"] = bill("OLD-1", 1000, "2025-11-05", "2025-12-05")
        ids["b2"] = bill("OLD-2", 400, "2025-11-20", "2025-12-20")
        ids["b3"] = bill("OLD-3", 250, "2025-12-10", "2026-01-09")

        def payment(amount, when, allocations, reference):
            entry = _post_entry(conn, when, f"Vendor payment {reference}", [(ids["ap"], amount, 0), (ids["cash"], 0, amount)])
            conn.execute(text(
                "INSERT INTO vendor_payments (vendor_id, payment_date, amount, method, reference, cash_account_id, journal_entry_id, "
                "recorded_by, status) VALUES (:v, :d, :a, 'bank_transfer', :r, :cash, :e, 'legacy', 'recorded')"),
                {"v": ids["vendor"], "d": when, "a": amount, "r": reference, "cash": ids["cash"], "e": entry})
            payment_id = conn.execute(text("SELECT MAX(id) FROM vendor_payments")).scalar()
            for bill_id, alloc in allocations:
                conn.execute(text("INSERT INTO payment_allocations (payment_id, bill_id, amount) VALUES (:p, :b, :a)"),
                             {"p": payment_id, "b": bill_id, "a": alloc})
            return payment_id

        ids["p1"] = payment(600, "2025-11-28", [(ids["b1"], 600)], "TRF-1")  # partial on OLD-1
        conn.execute(text("UPDATE supplier_bills SET status = 'partially_paid' WHERE id = :b"), {"b": ids["b1"]})
        ids["p2"] = payment(400, "2025-12-15", [(ids["b2"], 400)], "TRF-2")  # OLD-2 paid ...
        conn.execute(text("UPDATE supplier_bills SET status = 'paid' WHERE id = :b"), {"b": ids["b2"]})
        # ... then reversed in January (cheque bounced).
        reversal = _post_entry(conn, "2026-01-08", "Reversal of TRF-2", [(ids["cash"], 400, 0), (ids["ap"], 0, 400)])
        conn.execute(text("UPDATE vendor_payments SET status = 'reversed', reversed_by = 'legacy', reversal_date = '2026-01-08', "
                          "reversal_reason = 'bounced', reversal_journal_entry_id = :e WHERE id = :p"), {"e": reversal, "p": ids["p2"]})
        conn.execute(text("UPDATE supplier_bills SET status = 'approved' WHERE id = :b"), {"b": ids["b2"]})
        # A credit note applied to OLD-3.
        credit_entry = _post_entry(conn, "2025-12-20", "Supplier credit note CN-OLD", [(ids["ap"], 50, 0), (ids["expense"], 0, 50)])
        conn.execute(text(
            "INSERT INTO supplier_credits (vendor_id, kind, credit_number, credit_number_normalized, credit_date, total, status, "
            "payable_account_id, journal_entry_id, approved_by, created_at, updated_at) "
            "VALUES (:v, 'credit_note', 'CN-OLD', 'CN-OLD', '2025-12-20', 50, 'draft', :ap, NULL, 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
            {"v": ids["vendor"], "ap": ids["ap"]})
        ids["credit"] = conn.execute(text("SELECT MAX(id) FROM supplier_credits")).scalar()
        conn.execute(text("INSERT INTO supplier_credit_lines (credit_id, line_no, description, account_id, quantity, unit_price, line_total) "
                          "VALUES (:c, 1, 'return', :a, 1, 50, 50)"), {"c": ids["credit"], "a": ids["expense"]})
        conn.execute(text("UPDATE supplier_credits SET status = 'approved', journal_entry_id = :e WHERE id = :c"), {"e": credit_entry, "c": ids["credit"]})
        conn.execute(text("INSERT INTO credit_applications (credit_id, bill_id, amount, application_date, applied_by) VALUES (:c, :b, 50, '2025-12-22', 'legacy')"),
                     {"c": ids["credit"], "b": ids["b3"]})
        conn.execute(text("UPDATE supplier_credits SET status = 'applied' WHERE id = :c"), {"c": ids["credit"]})
        conn.execute(text("UPDATE supplier_bills SET status = 'partially_paid' WHERE id = :b"), {"b": ids["b3"]})
    engine.dispose()
    return ids


def test_history_written_before_the_upgrade_survives_and_reconstructs(db_url):
    migrate(db_url, "fmspayables02")
    ids = _seed_v2_history(db_url)
    # Numbers that were true at fmspayables02: OLD-1 400 open, OLD-2 400 open (reversed payment), OLD-3 200 open.
    migrate(db_url)  # to head

    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as client:
        api = "/api/v1/payables"
        # Back-filled columns and untouched history.
        with Session(app.state.engine) as session:
            allocations = session.query(PaymentAllocation).order_by(PaymentAllocation.id).all()
            assert [(a.kind, a.allocation_date, a.allocated_by) for a in allocations] == [
                (AllocationKind.ORIGINAL, date(2025, 11, 28), "legacy"), (AllocationKind.ORIGINAL, date(2025, 12, 15), "legacy")]
            assert all(a.unallocation_date is None for a in allocations)
            vendors = {v.code: v for v in session.query(Vendor)}
            assert (vendors["OLD"].status.value, vendors["OLD"].is_active) == ("active", True)
            assert (vendors["GONE"].status.value, vendors["GONE"].is_active) == ("inactive", False)
            reversed_payment = session.get(VendorPayment, ids["p2"])
            assert reversed_payment.status.value == "reversed" and reversed_payment.reversal_reason == "bounced"
            assert reversed_payment.payable_account_id is None and reversed_payment.unapplied_amount == 0
            application = session.query(CreditApplication).one()
            assert application.is_active and application.application_date == date(2025, 12, 22)
            assert session.get(SupplierBill, ids["b1"]).discount_amount == 0

        outstanding = client.get(f"{api}/outstanding?as_of=2026-01-31").json()
        assert {r["invoice_number"]: r["outstanding"] for r in outstanding["rows"]} == {"OLD-1": "400.00", "OLD-2": "400.00", "OLD-3": "200.00"}
        # Historical dates before the reversal / application still show the old state.
        assert {r["invoice_number"]: r["outstanding"] for r in client.get(f"{api}/outstanding?as_of=2025-12-31").json()["rows"]} == {
            "OLD-1": "400.00", "OLD-3": "200.00"}
        assert {r["invoice_number"]: r["outstanding"] for r in client.get(f"{api}/outstanding?as_of=2025-12-21").json()["rows"]} == {
            "OLD-1": "400.00", "OLD-3": "250.00"}
        for as_of in ("2025-11-30", "2025-12-31", "2026-01-31"):
            recon = client.get(f"{api}/reconciliation?as_of={as_of}").json()
            assert recon["total_difference"] == "0.00" and recon["exceptions"] == [], as_of
        statement = client.get(f"{api}/vendors/{ids['vendor']}/statement?date_from=2025-11-01&date_to=2026-01-31").json()
        assert statement["closing_balance"] == "1000.00" and statement["difference"] == "0.00"
        assert [l["type"] for l in statement["lines"]] == ["bill", "bill", "payment", "bill", "payment", "credit", "payment_reversal"]
        balance = client.get(f"{api}/vendors/{ids['vendor']}/balance?as_of=2026-01-31").json()
        assert (balance["bills_outstanding"], balance["net_owed"], balance["open_bill_count"]) == ("1000.00", "1000.00", 3)
        aging = client.get(f"{api}/aging?as_of=2026-01-31").json()
        assert aging["totals"]["total"] == "1000.00"

        # The legacy rows are governed by the new rules: immutable payments, dated unapplication, advances.
        legacy_payment = client.get(f"{api}/payments/{ids['p1']}").json()
        assert legacy_payment["allocations"][0]["kind"] == "original" and legacy_payment["unapplied_amount"] == "0.00"
        credit = client.get(f"{api}/credits/{ids['credit']}").json()
        r = client.post(f"{api}/credits/{ids['credit']}/applications/{credit['applications'][0]['id']}/unapply",
                        json={"actor": "now", "reason": "re-allocate", "unapplication_date": "2026-02-01"})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert client.get(f"{api}/outstanding?as_of=2026-01-31").json()["total_outstanding"] == "1000.00"  # January unchanged
        assert client.get(f"{api}/outstanding?as_of=2026-02-01").json()["total_outstanding"] == "1050.00"
        advance = client.post(f"{api}/payments", json={"vendor_id": ids["vendor"], "payment_date": "2026-02-02", "amount": "500.00",
                                                       "cash_account_id": ids["cash"], "recorded_by": "now",
                                                       "allocations": [{"bill_id": ids["b1"], "amount": "400.00"}]})
        assert advance.status_code == 201, advance.text
        assert advance.json()["unapplied_amount"] == "100.00"
        assert client.get(f"{api}/bills/{ids['b1']}").json()["status"] == "paid"
        run = client.post(f"{api}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-10", "cash_account_id": ids["cash"]}).json()
        assert {i["bill_id"]: i["advance_amount"] for i in run["items"]} == {ids["b2"]: "100.00", ids["b3"]: "0.00"}
        recon = client.get(f"{api}/reconciliation?as_of=2026-02-28").json()
        assert recon["total_difference"] == "0.00" and recon["exceptions"] == []
        with Session(app.state.engine) as session:
            # Direct SQL can no longer touch the legacy payment's descriptive fields.
            from sqlalchemy.exc import IntegrityError
            import pytest

            with pytest.raises(IntegrityError, match="cannot be modified"):
                session.execute(text("UPDATE vendor_payments SET reference = 'tamper' WHERE id = :p"), {"p": ids["p1"]})
                session.commit()
            session.rollback()
    app.state.engine.dispose()

    # Downgrade with v3-only rows present (the closed application): pruned, history intact otherwise.
    command.downgrade(alembic_config(db_url), "fmspayables02")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM credit_applications")).scalar() == 0  # the only application was closed
        assert conn.execute(text("SELECT COUNT(*) FROM payment_allocations")).scalar() == 3  # two legacy + the new original one
        assert conn.execute(text("SELECT COUNT(*) FROM vendor_payments")).scalar() == 3
        assert conn.execute(text("SELECT COUNT(*) FROM journal_entries WHERE status = 'posted'")).scalar() == 8
    engine.dispose()
    migrate(db_url)
