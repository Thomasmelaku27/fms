"""Reconciliation exceptions and AP period-end validation / close (SQLite and PostgreSQL)."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

API = "/api/v1/payables"


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {
        "ap": _account(client, "2000", "liability"),
        "ap2": _account(client, "2010", "liability"),
        "expense": _account(client, "6000", "expense"),
        "cash": _account(client, "1000", "asset"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]})
    assert r.status_code == 201, r.text
    return r.json()


def _bill(client, vendor, accounts, number, total, invoice_date, approve=True, **extra):
    r = client.post(f"{API}/bills", json={
        "vendor_id": vendor["id"], "invoice_number": number, "invoice_date": invoice_date, "due_date": invoice_date,
        "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}], **extra})
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _credit(client, vendor, accounts, number, total, credit_date, approve=True):
    r = client.post(f"{API}/credits", json={"vendor_id": vendor["id"], "credit_number": number, "credit_date": credit_date, "total": total,
                                            "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    if not approve:
        return r.json()
    a = client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _recon(client, as_of):
    return client.get(f"{API}/reconciliation?as_of={as_of}").json()


def _validate(client, period_end):
    r = client.get(f"{API}/period-end/validate?period_end={period_end}")
    assert r.status_code == 200, r.text
    return r.json()


def _check(result, code):
    return [c for c in result["checks"] if c["code"] == code][0]


# --- reconciliation exceptions -------------------------------------------------------------------------


def test_clean_books_have_no_exceptions(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    _credit(client, vendor, accounts, "CN", "20.00", "2026-01-12")
    assert client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-20", "amount": "50.00",
                                                "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                "allocations": [{"bill_id": bill["id"], "amount": "30.00"}]}).status_code == 201
    report = _recon(client, "2026-01-31")
    assert report["exceptions"] == []
    assert report["rows"][0]["ledger_balance"] == report["rows"][0]["subledger_balance"] == "30.00"  # 100 - 20 - 50


def test_foreign_entry_is_reported_with_entry_and_amount(client, vendor, accounts):
    _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    manual = client.post("/api/v1/journal-entries", json={"entry_date": "2026-01-25", "description": "manual accrual", "reference": "MAN-1",
                                                           "lines": [{"account_id": accounts["expense"], "debit": "7.00"}, {"account_id": accounts["ap"], "credit": "7.00"}]}).json()
    assert client.post(f"/api/v1/journal-entries/{manual['id']}/post").status_code == 200
    report = _recon(client, "2026-01-31")
    assert report["total_difference"] == "7.00"
    assert len(report["exceptions"]) == 1
    exc = report["exceptions"][0]
    assert (exc["document_type"], exc["journal_entry_id"], exc["account_code"], exc["expected"], exc["actual"], exc["difference"]) == (
        "journal_entry", manual["id"], "2000", "0.00", "7.00", "7.00")
    assert exc["cause"].startswith("foreign_entry") and "MAN-1" in exc["cause"]
    assert _recon(client, "2026-01-24")["exceptions"] == []  # dated after that as-of


def test_document_without_entry_and_entry_mismatch_are_reported(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-10")
    with Session(client.app.state.engine) as session:
        # An "approved" bill injected by raw SQL with no journal entry at all ...
        session.execute(text(
            "INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, invoice_date, due_date, total, status, "
            "payable_account_id, discount_amount, updated_at) VALUES (:v, 'SQL-1', 'SQL-1', '2026-01-12', '2026-01-12', 55, 'approved', :ap, 0, CURRENT_TIMESTAMP)"),
            {"v": vendor["id"], "ap": accounts["ap"]})
        # ... and one pointing at the first bill's entry, which moves AP by 100, not 40.
        session.execute(text(
            "INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, invoice_date, due_date, total, status, "
            "payable_account_id, journal_entry_id, discount_amount, updated_at) VALUES (:v, 'SQL-2', 'SQL-2', '2026-01-13', '2026-01-13', 40, 'approved', :ap, :e, 0, CURRENT_TIMESTAMP)"),
            {"v": vendor["id"], "ap": accounts["ap"], "e": bill["journal_entry_id"]})
        session.commit()
    report = _recon(client, "2026-01-31")
    causes = sorted(e["cause"].split(":")[0] for e in report["exceptions"])
    assert causes == ["entry_mismatch", "missing_entry"]
    by_cause = {e["cause"].split(":")[0]: e for e in report["exceptions"]}
    assert (by_cause["missing_entry"]["document_type"], by_cause["missing_entry"]["expected"], by_cause["missing_entry"]["actual"]) == ("bill", "55.00", "0.00")
    assert by_cause["missing_entry"]["vendor_code"] == "ACME"
    mismatch = by_cause["entry_mismatch"]
    assert (mismatch["journal_entry_id"], mismatch["expected"], mismatch["actual"]) == (bill["journal_entry_id"], "140.00", "100.00")
    assert report["total_difference"] == "-95.00"  # ledger 100 vs subledger 195


# --- period-end validation -----------------------------------------------------------------------------


def test_validation_reports_every_concern_with_items(client, vendor, accounts):
    paid = _bill(client, vendor, accounts, "PAID", "100.00", "2026-01-05")
    held = _bill(client, vendor, accounts, "HELD", "80.00", "2026-01-06")
    _bill(client, vendor, accounts, "OPEN", "60.00", "2026-01-09")  # proposed by the run below
    _bill(client, vendor, accounts, "DRAFT", "10.00", "2026-01-07", approve=False)
    rejected = _bill(client, vendor, accounts, "REJ", "10.00", "2026-01-08", approve=False)
    assert client.post(f"{API}/bills/{rejected['id']}/reject", json={"actor": "a", "reason": "no PO"}).status_code == 200
    _bill(client, vendor, accounts, "FEB", "30.00", "2026-02-03")
    suspect = _bill(client, vendor, accounts, "PAID-", "100.00", "2026-01-05", approve=False)  # compact duplicate of PAID
    assert suspect["duplicate_flags"][0]["blocks_approval"] is True
    assert client.post(f"{API}/bills/{held['id']}/holds", json={"kind": "dispute", "reason": "damaged", "actor": "alice"}).status_code == 200
    credit = _credit(client, vendor, accounts, "CN", "25.00", "2026-01-12")
    advance = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-15", "amount": "140.00",
                                                   "cash_account_id": accounts["cash"], "recorded_by": "bob",
                                                   "allocations": [{"bill_id": paid["id"], "amount": "100.00"}]}).json()
    run = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-01-28", "cash_account_id": accounts["cash"]}).json()
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 200
    manual = client.post("/api/v1/journal-entries", json={"entry_date": "2026-01-20", "description": "manual",
                                                           "lines": [{"account_id": accounts["expense"], "debit": "3.00"}, {"account_id": accounts["ap"], "credit": "3.00"}]}).json()
    assert client.post(f"/api/v1/journal-entries/{manual['id']}/post").status_code == 200

    result = _validate(client, "2026-01-31")
    assert result["status"] == "blocked" and result["can_close"] is False and result["already_closed"] is False
    assert {c["code"]: (c["severity"], c["count"]) for c in result["checks"]} == {
        "unapproved_documents": ("blocker", 2),        # DRAFT + the suspect draft
        "rejected_documents": ("info", 1),
        "pending_duplicate_flags": ("blocker", 1),
        "advisory_duplicate_flags": ("warning", 0),
        "held_or_disputed_bills": ("warning", 1),
        "unapplied_credits": ("warning", 1),
        "unapplied_advances": ("warning", 1),
        "reconciliation_exceptions": ("blocker", 0),
        "foreign_ledger_entries": ("warning", 1),
        "payment_runs_in_flight": ("blocker", 1),
        "unrecorded_confirmed_transfers": ("blocker", 0),
        "unverified_completed_operations": ("blocker", 0),
        "open_payment_investigations": ("warning", 0),
        "unresolved_close_exceptions": ("warning", 0),
        "post_cutoff_transactions": ("info", 1),
    }
    assert _check(result, "held_or_disputed_bills")["items"][0]["outstanding"] == "80.00"
    assert _check(result, "unapplied_credits")["items"][0] == {"credit_id": credit["id"], "vendor_id": vendor["id"], "number": "CN", "date": "2026-01-12", "remaining": "25.00"}
    assert _check(result, "unapplied_advances")["items"][0]["unapplied"] == "40.00" and _check(result, "unapplied_advances")["items"][0]["payment_id"] == advance["id"]
    assert _check(result, "payment_runs_in_flight")["items"][0]["reference"] == run["reference"]
    assert _check(result, "post_cutoff_transactions")["items"][0]["type"] == "bill"
    assert _check(result, "foreign_ledger_entries")["items"][0]["cause"].startswith("foreign_entry")
    assert _check(result, "foreign_ledger_entries")["items"][0]["journal_entry_id"] == manual["id"]
    closing = client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"})
    assert closing.status_code == 422 and "unapproved_documents (2)" in closing.json()["detail"]
    assert "payment_runs_in_flight (1)" in closing.json()["detail"]

    # Resolve the blockers one by one (warnings do not stop the close).
    draft_id = [i["id"] for i in _check(result, "unapproved_documents")["items"] if i["number"] == "DRAFT"][0]
    assert client.put(f"{API}/bills/{draft_id}", json={"invoice_date": "2026-02-01", "due_date": "2026-02-01"}).status_code == 200
    flag = suspect["duplicate_flags"][0]
    assert client.post(f"{API}/bills/{suspect['id']}/duplicate-flags/{flag['id']}/review", json={"decision": "confirmed", "actor": "rev"}).status_code == 200
    assert client.post(f"{API}/bills/{suspect['id']}/reject", json={"actor": "alice", "reason": "duplicate of PAID"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/cancel", json={"actor": "boss", "reason": "month end"}).status_code == 200
    result = _validate(client, "2026-01-31")
    assert result["status"] == "warnings" and result["can_close"] is True
    # The manual posting stays visible as an explained warning; the difference it causes is known.
    assert _check(result, "foreign_ledger_entries")["count"] == 1
    assert _recon(client, "2026-01-31")["total_difference"] == "3.00"

    closed = client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo", "note": "January"})
    assert closed.status_code == 201, closed.text
    assert closed.json()["closed_through"] == "2026-01-31"
    after = _validate(client, "2026-01-31")
    assert after["already_closed"] is True and after["can_close"] is False
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"}).status_code == 409
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-01-15", "actor": "cfo"}).status_code == 409
    with Session(client.app.state.engine) as session:
        summary = session.execute(text("SELECT validation_summary FROM ap_period_locks")).scalar()
    assert '"unapplied_credits": 1' in summary
    # February can be closed later once its own blockers are clear.
    feb = _validate(client, "2026-02-28")
    assert feb["status"] in ("warnings", "blocked") and feb["already_closed"] is False


def test_period_port_can_veto_dates(client, vendor, accounts):
    """A GL period module wired through the port closes dates the AP locks do not know about."""
    from datetime import date

    from app.modules.payables.integration import Integrations, get_integrations

    class GLPeriods:
        def __init__(self):
            self.notified = []

        def is_closed(self, db, on):
            return on <= date(2026, 1, 15)

        def notify_closed(self, db, *, closed_through, summary):
            self.notified.append((closed_through, summary["status"]))

    periods = GLPeriods()
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(periods=periods)
    early = _bill(client, vendor, accounts, "E", "10.00", "2026-01-10", approve=False)
    r = client.post(f"{API}/bills/{early['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 422 and "closed AP period" in r.json()["detail"]
    assert _bill(client, vendor, accounts, "LATER-BILL", "20.00", "2026-01-20")["status"] == "approved"
    assert client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-01-12", "amount": "1.00",
                                                "cash_account_id": accounts["cash"], "recorded_by": "bob"}).status_code == 422
    assert client.put(f"{API}/bills/{early['id']}", json={"invoice_date": "2026-01-20", "due_date": "2026-01-20"}).status_code == 200
    assert client.post(f"{API}/bills/{early['id']}/approve", json={"actor": "alice"}).status_code == 200
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"}).status_code == 201
    assert periods.notified == [(date(2026, 1, 31), "ok")]
