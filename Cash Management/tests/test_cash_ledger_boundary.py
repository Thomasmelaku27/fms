"""Cash management never posts, edits or deletes accounting.

Two guards:

* dynamic - every mutating cash endpoint is exercised while the posting
  service is sabotaged (it raises if called) and while journal rows are
  watched: nothing in the ledger changes and nothing calls the pipeline;
* static - the cash package contains no code that constructs or mutates
  journal entries / lines or their status. Only ``app.services.posting``
  (used by the journal API) may change a status, and only through the
  conditional update that the database triggers re-verify.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select

import app.services.posting as posting
from app.models import JournalEntry, JournalLine
from tests.cash_helpers import (
    account,
    cash_account,
    cash_in,
    cash_line_id,
    cash_out,
    ledger_snapshot,
    match,
    open_session,
    statement_payload,
)

CASH_PACKAGE = Path(__file__).resolve().parents[1] / "app" / "modules" / "cash"

FORBIDDEN = [
    r"\bJournalEntry\(",                 # constructing entries
    r"\bJournalLine\(",                  # constructing lines
    r"update\(\s*JournalEntry",          # bulk updates of entries
    r"update\(\s*JournalLine",
    r"delete\(\s*JournalEntry",
    r"delete\(\s*JournalLine",
    r"\.status\s*=\s*EntryStatus",       # flipping a status
    r"\bpost_entry\b",                   # the pipeline itself is not called from cash
    r"INSERT INTO journal_",             # raw SQL on ledger tables
    r"UPDATE journal_",
    r"DELETE FROM journal_",
]


def test_cash_package_has_no_ledger_write_paths():
    offending = []
    for path in sorted(CASH_PACKAGE.glob("*.py")):
        source = path.read_text()
        for pattern in FORBIDDEN:
            for m in re.finditer(pattern, source):
                line = source.count("\n", 0, m.start()) + 1
                offending.append(f"{path.name}:{line}: {pattern}")
    assert offending == [], "cash module must not write to the ledger:\n" + "\n".join(offending)


@pytest.fixture
def world(client):
    ledger = account(client, "1000", "Bank", "asset")
    other = account(client, "1010", "Savings", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    rent = account(client, "5000", "Rent", "expense")
    ca = cash_account(client, ledger["id"], "Bank", minimum_balance="100.00")
    cb = cash_account(client, other["id"], "Savings")
    receipt = cash_in(client, "2026-01-05", ledger["id"], sales["id"], "100.00", "Customer")
    payment = cash_out(client, "2026-01-10", ledger["id"], rent["id"], "40.00", "Rent")
    return {"ledger": ledger["id"], "sales": sales["id"], "rent": rent["id"], "cash": ca["id"], "savings": cb["id"],
            "receipt": receipt, "payment": payment}


def test_every_cash_operation_leaves_the_ledger_untouched_and_never_calls_the_pipeline(client, world, session, monkeypatch):
    w = world
    draft = client.post("/api/v1/journal-entries", json={"entry_date": "2026-01-20", "description": "draft", "lines": [
        {"account_id": w["rent"], "debit": "9.99"}, {"account_id": w["ledger"], "credit": "9.99"}]}).json()
    before = ledger_snapshot(client)
    rows_before = session.execute(select(JournalLine.id, JournalLine.account_id, JournalLine.debit, JournalLine.credit)
                                  .order_by(JournalLine.id)).all()
    entries_before = session.execute(select(JournalEntry.id, JournalEntry.status, JournalEntry.entry_date).order_by(JournalEntry.id)).all()

    def forbidden(*args, **kwargs):
        raise AssertionError("cash management called the posting pipeline")

    monkeypatch.setattr(posting, "post_entry", forbidden)

    calls = [
        ("post", "/api/v1/cash/bank-statements", statement_payload(w["cash"], "JAN", "2026-01-01", "2026-01-31", "0", [
            {"date": "2026-01-05", "amount": "100.00", "description": "Customer"},
            {"date": "2026-01-10", "amount": "-40.00", "description": "Rent"},
            {"date": "2026-01-20", "amount": "-9.99", "description": "Fee"}])),
        ("post", "/api/v1/cash/bank-statements", statement_payload(w["cash"], "JAN", "2026-01-01", "2026-01-31", "0", [
            {"date": "2026-01-05", "amount": "100.00", "description": "Customer"},
            {"date": "2026-01-10", "amount": "-40.00", "description": "Rent"},
            {"date": "2026-01-20", "amount": "-9.99", "description": "Fee"}])),  # duplicate submission
        ("patch", f"/api/v1/cash-accounts/{w['cash']}", {"minimum_balance": "250.00"}),
        ("put", "/api/v1/cash/classifications", {"account_id": w["rent"], "activity": "operating"}),
        ("post", "/api/v1/cash/planned-flows", {"cash_account_id": w["cash"], "expected_date": "2026-03-01", "amount": "-50.00",
                                                "description": "plan", "source": "t", "external_reference": "P1"}),
        ("post", "/api/v1/cash/planned-flows", {"cash_account_id": w["cash"], "expected_date": "2026-03-01", "amount": "-50.00",
                                                "description": "plan", "source": "t", "external_reference": "P1"}),  # duplicate
    ]
    for method, url, body in calls:
        resp = getattr(client, method)(url, json=body)
        assert resp.status_code in (200, 201), (url, resp.text)

    txns = {t["description"]: t for t in client.get("/api/v1/cash/bank-transactions").json()}
    sid = open_session(client, w["cash"], period_start="2026-01-01", period_end="2026-01-31",
                       opening_balance="0", closing_balance="50.01")["id"]
    # Matching against a draft is refused; matching posted lines works; auto-match suggests.
    assert match(client, sid, txns["Fee"]["id"], cash_line_id(draft, w["ledger"]), expect=422)
    match(client, sid, txns["Customer"]["id"], cash_line_id(w["receipt"], w["ledger"]))
    client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match", json={"dry_run": True})
    client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match")
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "n"}).status_code == 200
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": "r"}).status_code == 200
    assert client.patch(f"/api/v1/cash/reconciliations/{sid}", json={"closing_balance": "50.01"}).status_code == 200
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "n"}).status_code == 200
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/recover").status_code == 409
    flow = client.get("/api/v1/cash/planned-flows").json()[0]
    assert client.patch(f"/api/v1/cash/planned-flows/{flow['id']}/status", json={"status": "realized", "journal_entry_id": w["payment"]["id"]}).status_code == 200
    assert client.patch(f"/api/v1/cash/planned-flows/{flow['id']}/status", json={"status": "realized", "journal_entry_id": draft["id"]}).status_code == 422
    for url in ("/api/v1/cash/reports/balances", "/api/v1/cash/reports/cash-flow?start=2026-01-01&end=2026-01-31",
                "/api/v1/cash/reports/daily-balances?start=2026-01-01&end=2026-01-31", "/api/v1/cash/projection?as_of=2026-01-31",
                f"/api/v1/cash/reconciliations/{sid}/summary", f"/api/v1/cash/reconciliations/{sid}/certification",
                f"/api/v1/cash/reconciliations/{sid}/history", f"/api/v1/cash/reconciliations/{sid}/unmatched"):
        assert client.get(url).status_code == 200, url
    assert client.delete(f"/api/v1/cash/planned-flows/{flow['id']}").status_code == 204
    assert client.delete(f"/api/v1/cash/classifications/{w['rent']}").status_code == 204

    # The ledger: same entries, same statuses, same lines, same amounts; the draft is still a draft.
    assert ledger_snapshot(client) == before
    session.expire_all()
    assert session.execute(select(JournalLine.id, JournalLine.account_id, JournalLine.debit, JournalLine.credit)
                           .order_by(JournalLine.id)).all() == rows_before
    assert session.execute(select(JournalEntry.id, JournalEntry.status, JournalEntry.entry_date).order_by(JournalEntry.id)).all() == entries_before
    assert client.get(f"/api/v1/journal-entries/{draft['id']}").json()["status"] == "draft"
    assert client.get(f"/api/v1/cash-accounts/{w['cash']}/balance").json()["balance"] == "60.00"

    # The one and only way to change the ledger is the journal API + posting pipeline.
    monkeypatch.undo()
    assert client.post(f"/api/v1/journal-entries/{draft['id']}/post").status_code == 200
    assert client.get(f"/api/v1/cash-accounts/{w['cash']}/balance").json()["balance"] == "50.01"
