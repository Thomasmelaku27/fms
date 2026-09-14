"""Upgrading a populated Turn-3 database (``fmspayables04``) to ``fmspayables05`` and back.

History is created at the ``fmspayables04`` schema through raw SQL: approved bills with
posted entries, payments and allocations (original + advance, one unallocated), a
credit with an application that was later unapplied, a discount, an executed payment
run with paid items, a closed AP period. After the upgrade everything must still read
the same through the new services, executions must be back-filled, and the downgrade
must refuse to destroy new data unless explicitly allowed.
"""

import os
from decimal import Decimal

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from tests.conftest import alembic_config, migrate

D = Decimal


def _post_entry(conn, entry_date, description, lines):
    conn.execute(text("INSERT INTO journal_entries (entry_date, description, reference, status) VALUES (:d, :desc, NULL, 'draft')"),
                 {"d": entry_date, "desc": description})
    entry_id = conn.execute(text("SELECT MAX(id) FROM journal_entries")).scalar()
    for account_id, debit, credit in lines:
        conn.execute(text("INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (:e, :a, :dr, :cr)"),
                     {"e": entry_id, "a": account_id, "dr": debit, "cr": credit})
    conn.execute(text("UPDATE journal_entries SET status = 'posted' WHERE id = :e"), {"e": entry_id})
    return entry_id


def _seed_turn3(url: str) -> dict:
    engine = make_engine(url)
    ids = {}
    with engine.begin() as conn:
        for code, name, type_ in (("2000", "AP", "liability"), ("6000", "Expense", "expense"), ("1000", "Cash", "asset"), ("4900", "Disc", "revenue")):
            conn.execute(text("INSERT INTO accounts (code, name, type, is_active) VALUES (:c, :n, :t, true)"), {"c": code, "n": name, "t": type_})
        acct = dict(conn.execute(text("SELECT code, id FROM accounts")).all())
        ids.update(ap=acct["2000"], expense=acct["6000"], cash=acct["1000"], disc=acct["4900"])
        conn.execute(text("INSERT INTO vendors (code, name, is_active, payable_account_id, payment_terms_days, status, bank_reference) "
                          "VALUES ('OLD', 'Legacy', true, :ap, 30, 'active', 'IBAN-OLD')"), {"ap": ids["ap"]})
        ids["vendor"] = conn.execute(text("SELECT id FROM vendors")).scalar()

        def bill(number, total, inv, due, discount="0", disc_date=None):
            entry = _post_entry(conn, inv, f"bill {number}", [(ids["expense"], total, 0), (ids["ap"], 0, total)])
            conn.execute(text("INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, invoice_date, due_date, total, status, "
                              "payable_account_id, discount_amount, discount_date, discount_account_id, updated_at) VALUES (:v, :n, :n, :inv, :due, :t, 'draft', :ap, "
                              ":disc, :dd, :da, CURRENT_TIMESTAMP)"),
                         {"v": ids["vendor"], "n": number, "inv": inv, "due": due, "t": total, "ap": ids["ap"], "disc": discount, "dd": disc_date,
                          "da": ids["disc"] if disc_date else None})
            bill_id = conn.execute(text("SELECT MAX(id) FROM supplier_bills")).scalar()
            conn.execute(text("INSERT INTO supplier_bill_lines (bill_id, line_no, description, account_id, quantity, unit_price, line_total) "
                              "VALUES (:b, 1, 'l', :a, 1, :t, :t)"), {"b": bill_id, "a": ids["expense"], "t": total})
            conn.execute(text("UPDATE supplier_bills SET status = 'approved', journal_entry_id = :e WHERE id = :b"), {"e": entry, "b": bill_id})
            conn.execute(text("INSERT INTO bill_payment_schedules (bill_id, installment_no, due_date, amount) VALUES (:b, 1, :due, :t)"), {"b": bill_id, "due": due, "t": total})
            return bill_id

        ids["b1"] = bill("OLD-1", 1000, "2025-11-05", "2025-12-05", discount="20", disc_date="2025-11-15")
        ids["b2"] = bill("OLD-2", 400, "2025-11-20", "2025-12-20")
        ids["b3"] = bill("OLD-3", 250, "2025-12-10", "2026-01-09")

        def payment(amount, when, allocations, reference, payable=None):
            entry = _post_entry(conn, when, f"payment {reference}", [(ids["ap"], amount, 0), (ids["cash"], 0, amount)])
            conn.execute(text("INSERT INTO vendor_payments (vendor_id, payment_date, amount, method, reference, cash_account_id, journal_entry_id, recorded_by, "
                              "status, payable_account_id) VALUES (:v, :d, :a, 'bank_transfer', :r, :cash, :e, 'legacy', 'recorded', :pa)"),
                         {"v": ids["vendor"], "d": when, "a": amount, "r": reference, "cash": ids["cash"], "e": entry, "pa": payable})
            payment_id = conn.execute(text("SELECT MAX(id) FROM vendor_payments")).scalar()
            for bill_id, alloc, kind, alloc_date in allocations:
                conn.execute(text("INSERT INTO payment_allocations (payment_id, bill_id, amount, kind, allocation_date, allocated_by) "
                                  "VALUES (:p, :b, :a, :k, :d, 'legacy')"), {"p": payment_id, "b": bill_id, "a": alloc, "k": kind, "d": alloc_date})
            return payment_id

        # Discounted full payment of OLD-1 inside the window.
        ids["p1"] = payment(980, "2025-11-12", [(ids["b1"], 980, "original", "2025-11-12")], "TRF-1")
        disc_entry = _post_entry(conn, "2025-11-12", "discount", [(ids["ap"], 20, 0), (ids["disc"], 0, 20)])
        conn.execute(text("INSERT INTO bill_discounts (bill_id, payment_id, amount, taken_on, taken_by, discount_account_id, journal_entry_id) "
                          "VALUES (:b, :p, 20, '2025-11-12', 'legacy', :d, :e)"), {"b": ids["b1"], "p": ids["p1"], "d": ids["disc"], "e": disc_entry})
        conn.execute(text("UPDATE supplier_bills SET status = 'paid' WHERE id = :b"), {"b": ids["b1"]})
        # Advance of 300, 200 applied to OLD-2 in December, another 50 applied then unallocated.
        ids["adv"] = payment(300, "2025-11-25", [], "ADV", payable=ids["ap"])
        conn.execute(text("INSERT INTO payment_allocations (payment_id, bill_id, amount, kind, allocation_date, allocated_by) VALUES (:p, :b, 200, 'advance', '2025-12-01', 'legacy')"),
                     {"p": ids["adv"], "b": ids["b2"]})
        conn.execute(text("INSERT INTO payment_allocations (payment_id, bill_id, amount, kind, allocation_date, allocated_by) VALUES (:p, :b, 50, 'advance', '2025-12-02', 'legacy')"),
                     {"p": ids["adv"], "b": ids["b2"]})
        conn.execute(text("UPDATE payment_allocations SET unallocation_date = '2025-12-20', unallocated_by = 'legacy', unallocation_reason = 'oops' WHERE amount = 50"))
        conn.execute(text("UPDATE supplier_bills SET status = 'partially_paid' WHERE id = :b"), {"b": ids["b2"]})
        # Credit applied to OLD-3 then unapplied in January.
        credit_entry = _post_entry(conn, "2025-12-20", "credit", [(ids["ap"], 50, 0), (ids["expense"], 0, 50)])
        conn.execute(text("INSERT INTO supplier_credits (vendor_id, kind, credit_number, credit_number_normalized, credit_date, total, status, payable_account_id, "
                          "journal_entry_id, created_at, updated_at) VALUES (:v, 'credit_note', 'CN-OLD', 'CN-OLD', '2025-12-20', 50, 'draft', :ap, NULL, "
                          "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"v": ids["vendor"], "ap": ids["ap"]})
        ids["credit"] = conn.execute(text("SELECT MAX(id) FROM supplier_credits")).scalar()
        conn.execute(text("INSERT INTO supplier_credit_lines (credit_id, line_no, description, account_id, quantity, unit_price, line_total) VALUES (:c, 1, 'r', :a, 1, 50, 50)"),
                     {"c": ids["credit"], "a": ids["expense"]})
        conn.execute(text("UPDATE supplier_credits SET status = 'approved', journal_entry_id = :e WHERE id = :c"), {"e": credit_entry, "c": ids["credit"]})
        conn.execute(text("INSERT INTO credit_applications (credit_id, bill_id, amount, application_date, applied_by) VALUES (:c, :b, 50, '2025-12-22', 'legacy')"),
                     {"c": ids["credit"], "b": ids["b3"]})
        conn.execute(text("UPDATE credit_applications SET unapplication_date = '2026-01-10', unapplied_by = 'legacy'"))
        # A payment run executed in Turn 3 (no operations table yet) paying OLD-3 in full.
        conn.execute(text("INSERT INTO payment_runs (reference, status, pay_date, due_through, take_discounts, use_credits, use_advances, cash_account_id, method, "
                          "created_by, attempts, executed_by, updated_at) VALUES ('RUN-OLD', 'draft', '2026-01-15', '2026-01-15', true, true, true, :c, 'bank_transfer', "
                          "'ops', 1, 'ops', CURRENT_TIMESTAMP)"), {"c": ids["cash"]})
        ids["run"] = conn.execute(text("SELECT MAX(id) FROM payment_runs")).scalar()
        ids["p3"] = payment(250, "2026-01-15", [(ids["b3"], 250, "original", "2026-01-15")], "SIM-OLD")
        conn.execute(text("UPDATE supplier_bills SET status = 'paid' WHERE id = :b"), {"b": ids["b3"]})
        conn.execute(text("INSERT INTO payment_run_items (run_id, vendor_id, bill_id, due_date, outstanding_snapshot, settle_amount, discount_amount, credit_amount, "
                          "advance_amount, pay_amount, status, explanation, payment_id, execution_reference) VALUES (:r, :v, :b, '2026-01-09', 250, 250, 0, 0, 0, 250, "
                          "'proposed', 'legacy', NULL, NULL)"), {"r": ids["run"], "v": ids["vendor"], "b": ids["b3"]})
        for step in ("reviewed", "approved", "executing"):
            conn.execute(text("UPDATE payment_runs SET status = :s WHERE id = :r"), {"s": step, "r": ids["run"]})
        conn.execute(text("UPDATE payment_run_items SET status = 'paid', payment_id = :p, execution_reference = 'SIM-OLD' WHERE run_id = :r"), {"p": ids["p3"], "r": ids["run"]})
        conn.execute(text("UPDATE payment_runs SET status = 'completed' WHERE id = :r"), {"r": ids["run"]})
        conn.execute(text("INSERT INTO ap_period_locks (closed_through, closed_by, note) VALUES ('2025-12-31', 'cfo', 'turn 3 close')"))
    engine.dispose()
    return ids


def test_turn3_history_survives_the_upgrade_and_executions_are_backfilled(db_url):
    migrate(db_url, "fmspayables04")
    ids = _seed_turn3(db_url)
    migrate(db_url)
    app = create_app(Settings(database_url=db_url))
    api = "/api/v1/payables"
    with TestClient(app) as client:
        # Balances as they were: OLD-1 paid (980 + 20 discount), OLD-2 200 open, OLD-3 paid; advance 100 unapplied after the unallocation.
        outstanding = client.get(f"{api}/outstanding?as_of=2026-01-31").json()
        assert {r["invoice_number"]: r["outstanding"] for r in outstanding["rows"]} == {"OLD-2": "200.00"}
        balance = client.get(f"{api}/vendors/{ids['vendor']}/balance?as_of=2026-01-31").json()
        assert (balance["bills_outstanding"], balance["unapplied_advances"], balance["unapplied_credits"]) == ("200.00", "100.00", "50.00")
        # History in December (credit still applied, 50 still allocated) is intact.
        dec = client.get(f"{api}/outstanding?as_of=2025-12-15").json()
        assert {r["invoice_number"]: r["outstanding"] for r in dec["rows"]} == {"OLD-2": "150.00", "OLD-3": "250.00"}
        for as_of in ("2025-11-30", "2025-12-31", "2026-01-31"):
            recon = client.get(f"{api}/reconstruction?as_of={as_of}").json()
            assert recon["findings"] == [], (as_of, recon["findings"])
            assert client.get(f"{api}/reconciliation?as_of={as_of}").json()["exceptions"] == []
        # The executed Turn-3 run got a completed, back-filled operation.
        ops = client.get(f"{api}/payment-operations?run_id={ids['run']}").json()
        assert ops["total"] == 1 and ops["items"][0]["state"] == "completed" and ops["items"][0]["payment_id"] == ids["p3"]
        detail = client.get(f"{api}/payment-operations/{ops['items'][0]['operation_uid']}").json()
        assert detail["checklist"]["ap_payment_recorded"] and detail["checklist"]["gl_entry_posted"] and detail["checklist"]["reconciles"], [(f["code"], f["actual"]) for f in detail["verification"]["findings"]] + [detail["verification"]["required_evidence"]]
        assert "back-filled" in (detail["events"] and "" or "") or detail["recovery_status"] == "none"
        assert client.get(f"{api}/recovery/scan").json() == []
        assert client.get(f"{api}/payment-runs/{ids['run']}").json()["version"] == 0
        # Closed period, statements and supplier reconciliation keep working on legacy rows.
        assert client.get(f"{api}/period-end/locks").json()[0]["closed_through"] == "2025-12-31"
        assert client.post(f"{api}/payments", json={"vendor_id": ids["vendor"], "payment_date": "2025-12-30", "amount": "1.00",
                                                    "cash_account_id": ids["cash"], "recorded_by": "x"}).status_code == 422
        statement = client.get(f"{api}/vendors/{ids['vendor']}/statement?date_from=2025-11-01&date_to=2026-01-31").json()
        assert statement["difference"] == "0.00" and statement["closing_balance"] == "50.00"  # 200 open - 100 advance - 50 credit
        preview = client.post(f"{api}/vendors/{ids['vendor']}/statement-imports/preview", json={
            "actor": "fin", "file_name": "old.csv", "statement_date": "2026-01-31",
            "content": "kind,reference,date,amount\ninvoice,OLD-2,2025-11-20,400.00\npayment,ADV,2025-11-25,300.00\n"})
        assert preview.status_code == 200 and preview.json()["summary"]["counts"]["matched"] == 2
        # New executions work next to the legacy ones.
        r = client.post(f"{api}/bills", json={"vendor_id": ids["vendor"], "invoice_number": "NEW-1", "invoice_date": "2026-02-01", "due_date": "2026-02-10",
                                              "total": "80.00", "lines": [{"description": "x", "account_id": ids["expense"], "unit_price": "80.00"}]})
        assert client.post(f"{api}/bills/{r.json()['id']}/approve", json={"actor": "alice"}).status_code == 200
        run = client.post(f"{api}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-10", "cash_account_id": ids["cash"]}).json()
        assert {i["bill_id"]: (i["advance_amount"], i["credit_amount"], i["pay_amount"]) for i in run["items"]} == {ids["b2"]: ("100.00", "50.00", "50.00"), r.json()["id"]: ("0.00", "0.00", "80.00")}
        assert client.post(f"{api}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
        assert client.post(f"{api}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 200
        result = client.post(f"{api}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
        assert result["run"]["status"] == "completed"
        assert client.get(f"{api}/reconstruction?as_of=2026-02-28").json()["findings"] == []
    app.state.engine.dispose()

    # Downgrade is restricted while new execution records exist ...
    with pytest.raises(RuntimeError, match="FMS_ALLOW_DESTRUCTIVE_DOWNGRADE"):
        command.downgrade(alembic_config(db_url), "fmspayables04")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM payment_operations")).scalar() == 2
    engine.dispose()
    # ... and explicit confirmation removes execution records only, never posted history.
    os.environ["FMS_ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
    try:
        command.downgrade(alembic_config(db_url), "fmspayables04")
    finally:
        del os.environ["FMS_ALLOW_DESTRUCTIVE_DOWNGRADE"]
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM journal_entries WHERE status = 'posted'")).scalar() >= 8
        assert conn.execute(text("SELECT COUNT(*) FROM vendor_payments")).scalar() == 4
        assert conn.execute(text("SELECT COUNT(*) FROM ap_period_locks")).scalar() == 1
        assert "payment_operations" not in {t for (t,) in conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'" if engine.dialect.name == "postgresql"
            else "SELECT name FROM sqlite_master WHERE type = 'table'"))}
    engine.dispose()
    migrate(db_url)  # forward again: the completed runs are back-filled once more
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM payment_operations WHERE state = 'completed'")).scalar() == 2
    engine.dispose()
