"""Certified reconciliation evidence.

Closing a session freezes exactly what was approved; later imports,
back-dated postings or matching changes are shown as *drift* and never
change the certified result. Reopening needs a reason and is kept in the
history; closing again certifies a new version chained to the previous one.
"""

import hashlib
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.cash import reconciliation
from app.modules.cash.certification import SCHEMA, canonical_json
from app.modules.cash.errors import CashConflict
from app.modules.cash.models import (
    ReconciliationCertification,
    ReconciliationReopenEvent,
    ReconciliationSession,
    ReconciliationStatus,
)
from app.modules.cash.schemas import CloseRequest
from tests.cash_helpers import (
    account,
    cash_account,
    cash_in,
    cash_line_id,
    cash_out,
    import_statement,
    ledger_snapshot,
    match,
    open_session,
    statement_payload,
    summary,
)


@pytest.fixture
def january(client):
    """January reconciled and closed: one receipt, one matching bank line."""
    ledger = account(client, "1000", "Bank", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    fees = account(client, "5100", "Bank fees", "expense")
    ca = cash_account(client, ledger["id"])
    receipt = cash_in(client, "2026-01-05", ledger["id"], sales["id"], "100.00", "Customer")
    st = import_statement(
        client,
        statement_payload(ca["id"], "JAN", "2026-01-01", "2026-01-31", "0",
                          [{"date": "2026-01-05", "amount": "100.00", "description": "Customer"}]),
    )
    session = open_session(client, ca["id"], st["statement"]["id"])
    m = match(client, session["id"], st["transactions"][0]["id"], cash_line_id(receipt, ledger["id"]))
    closed = client.post(f"/api/v1/cash/reconciliations/{session['id']}/close")
    assert closed.status_code == 200, closed.text
    return {"ledger": ledger["id"], "sales": sales["id"], "fees": fees["id"], "cash": ca["id"],
            "sid": session["id"], "match": m, "receipt": receipt, "txn": st["transactions"][0]}


def certification(client, sid, expect=200):
    resp = client.get(f"/api/v1/cash/reconciliations/{sid}/certification")
    assert resp.status_code == expect, resp.text
    return resp.json()


def history(client, sid):
    return client.get(f"/api/v1/cash/reconciliations/{sid}/history").json()["events"]


def reopen(client, sid, reason="correction", expect=200):
    resp = client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": reason})
    assert resp.status_code == expect, resp.text
    return resp.json()


# --- The reported scenario: a late import must not change a certified result ----------------


def test_late_statement_import_after_closure_is_drift_not_a_changed_result(client, january):
    sid = january["sid"]
    before = summary(client, sid)
    assert before["is_balanced"] is True and before["certification"]["is_balanced"] is True
    assert before["drift"] == []
    certified_hash = before["certification"]["content_hash"]

    result = import_statement(
        client,
        statement_payload(january["cash"], "FEB", "2026-01-20", "2026-02-28", "100.00",
                          [{"date": "2026-01-25", "amount": "-7.00", "description": "Late January fee"},
                           {"date": "2026-02-03", "amount": "-3.00", "description": "February fee"}]),
    )
    assert result["late_transactions"] == [
        {"transaction_id": result["transactions"][0]["id"], "transaction_date": "2026-01-25", "amount": "-7.00",
         "description": "Late January fee", "session_id": sid, "period_start": "2026-01-01",
         "period_end": "2026-01-31", "certification_version": 1}
    ]
    assert result["warnings"] and "certified results are unchanged" in result["warnings"][0]

    after = summary(client, sid)
    # The certified result is exactly what was approved ...
    cert = after["certification"]
    assert cert["content_hash"] == certified_hash and cert["is_balanced"] is True
    assert cert["difference"] == "0.00" and cert["matches_count"] == 1 and cert["verified"] is True
    # ... the live figures moved (that is the fact the bank imposes) ...
    assert after["is_balanced"] is False and after["unmatched_bank_count"] == 1
    assert after["unmatched_bank_total"] == "-7.00" and after["unexplained_difference"] == "7.00"
    # ... and the difference is itemised separately as drift.
    kinds = {d["kind"] for d in after["drift"]}
    assert "late_bank_transaction" in kinds and "certified_figure_changed" in kinds
    late = next(d for d in after["drift"] if d["kind"] == "late_bank_transaction")
    assert late["bank_transaction_id"] == result["transactions"][0]["id"] and late["amount"] == "-7.00"
    changed = {d["message"].split(":")[0] for d in after["drift"] if d["kind"] == "certified_figure_changed"}
    assert {"is_balanced", "unmatched_bank_count", "bank_transactions_count", "unmatched_bank_total",
            "unexplained_difference"} <= changed
    assert "difference" not in changed  # certified and live statement balances agree on that one

    detail = certification(client, sid)
    assert detail["verified"] is True and detail["active"] is True
    assert [d["kind"] for d in detail["drift"]].count("late_bank_transaction") == 1


def test_backdated_posting_after_closure_is_drift(client, january):
    sid = january["sid"]
    fee = cash_out(client, "2026-01-30", january["ledger"], january["fees"], "2.50", "Back-dated fee")
    after = summary(client, sid)
    assert after["certification"]["is_balanced"] is True and after["certification"]["ledger_balance"] == "100.00"
    assert after["ledger_balance_at_period_end"] == "97.50"  # live
    late = [d for d in after["drift"] if d["kind"] == "late_ledger_line"]
    assert len(late) == 1 and late[0]["journal_line_id"] == cash_line_id(fee, january["ledger"])
    assert late[0]["amount"] == "-2.50"


# --- Frozen, verifiable evidence ----------------------------------------------------------------


def test_certification_content_is_canonical_and_hash_verifiable(client, january):
    detail = certification(client, january["sid"])
    content = detail["content"]
    assert hashlib.sha256(content.encode("ascii")).hexdigest() == detail["content_hash"]
    doc = json.loads(content)
    assert canonical_json(doc) == content  # sorted keys, no whitespace
    assert doc["schema"] == SCHEMA and doc["version"] == 1 and doc["previous_hash"] is None
    assert doc["figures"]["is_balanced"] is True and doc["figures"]["difference"] == "0.00"
    assert [m["match_id"] for m in doc["matches"]] == [january["match"]["id"]]
    assert doc["matches"][0]["bank_fingerprint"] == january["txn"]["fingerprint"]
    assert doc["matches"][0]["journal_entry_id"] == january["receipt"]["id"]
    assert [b["id"] for b in doc["bank_transactions"]] == [january["txn"]["id"]]
    assert [l["journal_line_id"] for l in doc["ledger_lines"]] == [cash_line_id(january["receipt"], january["ledger"])]
    assert doc["session"]["period_end"] == "2026-01-31" and doc["session"]["closing_balance"] == "100.00"
    assert detail["verification_problems"] == []


def test_certified_evidence_cannot_be_altered_in_the_database(client, january, session):
    sid = january["sid"]
    cert = session.scalar(select(ReconciliationCertification).where(ReconciliationCertification.session_id == sid))

    def raw_update():
        session.execute(text("UPDATE reconciliation_certifications SET is_balanced = :b WHERE id = :id"),
                        {"b": False, "id": cert.id})
    with pytest.raises(IntegrityError, match="append-only"):
        raw_update(); session.commit()
    session.rollback()

    with pytest.raises(IntegrityError, match="append-only"):
        session.delete(cert); session.commit()
    session.rollback()

    with pytest.raises(IntegrityError, match="cannot be deleted"):
        session.execute(text("DELETE FROM reconciliation_sessions WHERE id = :id"), {"id": sid}); session.commit()
    session.rollback()

    with pytest.raises(IntegrityError, match="scope of a closed"):
        session.execute(text("UPDATE reconciliation_sessions SET closing_balance = 999 WHERE id = :id"), {"id": sid})
        session.commit()
    session.rollback()

    with pytest.raises(IntegrityError, match="only change when a session is reopened|recorded reopen event|exactly one"):
        session.execute(text("UPDATE reconciliation_sessions SET status = 'open' WHERE id = :id"), {"id": sid})
        session.commit()
    session.rollback()
    with pytest.raises(IntegrityError, match="recorded reopen event|exactly one"):
        session.execute(text("UPDATE reconciliation_sessions SET status = 'open', reopen_count = 1 WHERE id = :id"), {"id": sid})
        session.commit()
    session.rollback()
    assert certification(client, sid)["verified"] is True


def test_tampered_evidence_is_detected_on_read(client, january, session, engine):
    """If someone bypasses the triggers, verification still fails closed."""
    sid = january["sid"]
    cert = session.scalar(select(ReconciliationCertification).where(ReconciliationCertification.session_id == sid))
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE reconciliation_certifications DISABLE TRIGGER trg_reconciliation_certifications_append_only"))
            conn.execute(text("UPDATE reconciliation_certifications SET is_balanced = false WHERE id = :id"), {"id": cert.id})
            conn.execute(text("ALTER TABLE reconciliation_certifications ENABLE TRIGGER trg_reconciliation_certifications_append_only"))
        else:
            conn.execute(text("DROP TRIGGER trg_recon_certs_no_update"))
            conn.execute(text("UPDATE reconciliation_certifications SET is_balanced = 0 WHERE id = :id"), {"id": cert.id})
    detail = certification(client, sid)
    assert detail["verified"] is False
    assert any("is_balanced" in p for p in detail["verification_problems"])
    assert summary(client, sid)["certification"]["verified"] is False


def test_closed_session_without_certification_is_impossible(client, january, session):
    """The status column alone can never say "closed": the database demands the evidence."""
    s2 = open_session(client, january["cash"], period_start="2026-02-01", period_end="2026-02-28",
                      opening_balance="100.00", closing_balance="100.00")
    with pytest.raises(IntegrityError, match="latest certification that has not been superseded"):
        session.execute(text("UPDATE reconciliation_sessions SET status = 'closed' WHERE id = :id"), {"id": s2["id"]})
        session.commit()
    session.rollback()
    assert client.get(f"/api/v1/cash/reconciliations/{s2['id']}").json()["status"] == "open"


def test_certification_rows_follow_the_state_machine(client, january, session):
    sid = january["sid"]
    cert = session.scalar(select(ReconciliationCertification).where(ReconciliationCertification.session_id == sid))

    def insert_cert(session_id, version):
        session.execute(text(
            "INSERT INTO reconciliation_certifications (session_id, version, certified_at, content_hash, content, "
            "is_balanced, forced, difference, cleared_balance, ledger_balance, matches_count) "
            "VALUES (:s, :v, :t, 'x', '{}', :b, :b2, 0, 0, 0, 0)"),
            {"s": session_id, "v": version, "t": "2026-02-01 00:00:00+00:00", "b": True, "b2": False})

    with pytest.raises(IntegrityError, match="only be recorded for an open|has not been superseded"):
        insert_cert(sid, 2); session.commit()
    session.rollback()

    s2 = open_session(client, january["cash"], period_start="2026-02-01", period_end="2026-02-28",
                      opening_balance="100.00", closing_balance="100.00")
    with pytest.raises(IntegrityError, match="version must follow|has not been superseded"):
        insert_cert(s2["id"], 5); session.commit()
    session.rollback()

    def insert_reopen(session_id, cert_id, number=1):
        session.execute(text(
            "INSERT INTO reconciliation_reopen_events (session_id, certification_id, reopen_number, reason, reopened_at) "
            "VALUES (:s, :c, :n, 'raw', :t)"), {"s": session_id, "c": cert_id, "n": number, "t": "2026-02-01 00:00:00+00:00"})

    with pytest.raises(IntegrityError, match="only be recorded for a closed|latest certification"):
        insert_reopen(s2["id"], cert.id); session.commit()
    session.rollback()

    # A reopen event for the closed session must reference its latest certification.
    session.execute(text("UPDATE reconciliation_sessions SET status = 'closed' WHERE id = :id"), {"id": sid})  # no-op
    with pytest.raises(IntegrityError, match="latest certification"):
        insert_reopen(sid, cert.id + 1000); session.commit()
    session.rollback()
    with pytest.raises(IntegrityError, match="next reopening"):
        insert_reopen(sid, cert.id, number=2); session.commit()
    session.rollback()

    # Through the proper service the event is recorded and then immutable.
    reopen(client, sid, "recorded reason")
    event = session.scalar(select(ReconciliationReopenEvent).where(ReconciliationReopenEvent.session_id == sid))
    assert event.certification_id == cert.id and event.reason == "recorded reason"
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text("UPDATE reconciliation_reopen_events SET reason = 'edited' WHERE id = :id"), {"id": event.id})
        session.commit()
    session.rollback()
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text("DELETE FROM reconciliation_reopen_events WHERE id = :id"), {"id": event.id})
        session.commit()
    session.rollback()


# --- Controlled reopening and re-certification -------------------------------------------------


def test_reopen_requires_a_reason_and_keeps_history(client, january):
    sid = january["sid"]
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/reopen").status_code == 422
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": ""}).status_code == 422
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": "   "}).status_code == 422
    assert client.get(f"/api/v1/cash/reconciliations/{sid}").json()["status"] == "closed"

    reopened = reopen(client, sid, "bank reissued the January statement")
    assert reopened["status"] == "open" and reopened["reopen_count"] == 1 and reopened["closed_at"] is None

    # The superseded certification is still there, verifiable, just not active.
    detail = certification(client, sid)
    assert detail["active"] is False and detail["verified"] is True and detail["drift"] == []
    assert summary(client, sid)["certification"] is None

    events = history(client, sid)
    assert [(e["event"], e["version"]) for e in events] == [("certified", 1), ("reopened", 1)]
    assert events[1]["reason"] == "bank reissued the January statement"
    assert events[0]["superseded"] is True and events[0]["content_hash"] == detail["content_hash"]


def test_recertification_creates_a_chained_new_version(client, january):
    sid = january["sid"]
    v1 = certification(client, sid)
    reopen(client, sid, "late fee to book")
    fee = cash_out(client, "2026-01-25", january["ledger"], january["fees"], "7.00", "January fee")
    st = import_statement(
        client,
        statement_payload(january["cash"], "JAN-2", "2026-01-01", "2026-01-31", "100.00",
                          [{"date": "2026-01-25", "amount": "-7.00", "description": "January fee"}]),
    )
    assert st["late_transactions"] == []  # the session is open again, nothing is late
    match(client, sid, st["transactions"][0]["id"], cash_line_id(fee, january["ledger"]))
    # The approved scope still says the bank closed at 100.00; correct it to the reissued 93.00.
    assert summary(client, sid)["difference"] == "7.00"
    resp = client.patch(f"/api/v1/cash/reconciliations/{sid}", json={"closing_balance": "93.00"})
    assert resp.status_code == 200 and resp.json()["closing_balance"] == "93.00"
    assert summary(client, sid)["is_balanced"] is True
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200

    v2 = certification(client, sid)
    assert v2["version"] == 2 and v2["active"] is True and v2["verified"] is True
    assert v2["previous_hash"] == v1["content_hash"] and v2["content_hash"] != v1["content_hash"]
    assert v2["matches_count"] == 2 and v2["drift"] == []
    doc = json.loads(v2["content"])
    assert doc["previous_hash"] == v1["content_hash"] and len(doc["matches"]) == 2

    events = history(client, sid)
    assert [(e["event"], e["version"]) for e in events] == [("certified", 1), ("reopened", 1), ("certified", 2)]
    assert events[2]["previous_hash"] == v1["content_hash"] and events[2]["superseded"] is False
    assert events[1]["reason"] == "late fee to book"

    # Reopen again: a reason is required again and the history keeps growing.
    reopen(client, sid, "second look")
    assert [e["event"] for e in history(client, sid)] == ["certified", "reopened", "certified", "reopened"]
    assert client.get(f"/api/v1/cash/reconciliations/{sid}").json()["reopen_count"] == 2


def test_closed_session_scope_cannot_be_patched(client, january):
    resp = client.patch(f"/api/v1/cash/reconciliations/{january['sid']}", json={"closing_balance": "1.00"})
    assert resp.status_code == 409
    assert client.get(f"/api/v1/cash/reconciliations/{january['sid']}").json()["closing_balance"] == "100.00"


def test_forced_close_is_recorded_as_such(client, january):
    sid = january["sid"]
    reopen(client, sid, "check the fee")
    import_statement(
        client,
        statement_payload(january["cash"], "JAN-3", "2026-01-01", "2026-01-31", "100.00",
                          [{"date": "2026-01-26", "amount": "-1.00", "description": "Unexplained"}]),
    )
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 409
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "1.00 under investigation"}).status_code == 200
    cert = certification(client, sid)
    assert cert["forced"] is True and cert["is_balanced"] is False and cert["difference"] == "0.00"
    assert json.loads(cert["content"])["figures"]["unmatched_bank_total"] == "-1.00"
    assert cert["notes"] == "1.00 under investigation"
    doc = json.loads(cert["content"])
    assert doc["forced"] is True and doc["notes"] == "1.00 under investigation"
    assert any(d["kind"] == "unmatched_bank_transaction" for d in doc["discrepancies"])


def test_never_certified_session_has_no_certification(client, january):
    s2 = open_session(client, january["cash"], period_start="2026-02-01", period_end="2026-02-28",
                      opening_balance="100.00", closing_balance="100.00")
    certification(client, s2["id"], expect=404)
    assert history(client, s2["id"]) == []
    assert summary(client, s2["id"])["certification"] is None
    reopen(client, s2["id"], "x", expect=409)


# --- Retries, rollback, ledger immutability -------------------------------------------------------


def test_retries_are_idempotent(client, january, session):
    sid = january["sid"]
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 409  # already closed
    assert session.scalar(select(ReconciliationCertification).where(ReconciliationCertification.session_id == sid).with_only_columns(ReconciliationCertification.id)) is not None
    assert len(history(client, sid)) == 1

    reopen(client, sid, "once")
    reopen(client, sid, "twice", expect=409)
    assert [e["event"] for e in history(client, sid)] == ["certified", "reopened"]

    # Re-sending the same match after it succeeded is a conflict and changes nothing.
    body = {"bank_transaction_id": january["txn"]["id"], "journal_line_id": cash_line_id(january["receipt"], january["ledger"])}
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/matches", json=body).status_code == 409
    assert len(client.get(f"/api/v1/cash/reconciliations/{sid}/matches").json()) == 1

    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 409
    assert [e["version"] for e in history(client, sid)] == [1, 1, 2]


def test_failed_close_rolls_back_the_certification(client, january, session, monkeypatch):
    """If the status flip fails after the certification row was written, nothing persists."""
    s2 = open_session(client, january["cash"], period_start="2026-02-01", period_end="2026-02-28",
                      opening_balance="100.00", closing_balance="100.00")
    obj = session.get(ReconciliationSession, s2["id"])

    def failing_update(*args, **kwargs):
        raise IntegrityError("UPDATE reconciliation_sessions", {}, Exception("simulated failure"))

    monkeypatch.setattr(reconciliation, "update", failing_update)
    with pytest.raises(CashConflict, match="simulated failure"):
        reconciliation.close_session(session, obj, CloseRequest())
    monkeypatch.undo()

    assert session.scalar(select(ReconciliationCertification).where(ReconciliationCertification.session_id == s2["id"])) is None
    assert client.get(f"/api/v1/cash/reconciliations/{s2['id']}").json()["status"] == "open"
    # The same session closes fine afterwards, as version 1.
    reconciliation.close_session(session, obj, CloseRequest())
    assert certification(client, s2["id"])["version"] == 1


def test_stale_close_after_concurrent_close_is_rejected_by_the_database(client, january, session, engine):
    s2 = open_session(client, january["cash"], period_start="2026-02-01", period_end="2026-02-28",
                      opening_balance="100.00", closing_balance="100.00")
    stale = session.get(ReconciliationSession, s2["id"])
    assert stale.status == ReconciliationStatus.OPEN
    with Session(engine, expire_on_commit=False) as other:
        reconciliation.close_session(other, other.get(ReconciliationSession, s2["id"]), CloseRequest())
    with pytest.raises(CashConflict, match="not open"):
        reconciliation.close_session(session, stale, CloseRequest())
    assert len(history(client, s2["id"])) == 1


def test_certification_never_touches_the_ledger(client, january, session):
    sid = january["sid"]
    before = ledger_snapshot(client)
    reopen(client, sid, "r1")
    client.post(f"/api/v1/cash/reconciliations/{sid}/close")
    reopen(client, sid, "r2")
    client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "n"})
    assert ledger_snapshot(client) == before
    assert all(e["status"] == "posted" for e in before)

    # The matched, certified posted line is as immutable as any posted line.
    line_id = cash_line_id(january["receipt"], january["ledger"])
    with pytest.raises(IntegrityError, match="posted journal entry cannot be modified"):
        session.execute(text("UPDATE journal_lines SET debit = 1 WHERE id = :id"), {"id": line_id}); session.commit()
    session.rollback()
    with pytest.raises(IntegrityError, match="posted journal entry cannot be modified"):
        session.execute(text("DELETE FROM journal_entries WHERE id = :id"), {"id": january["receipt"]["id"]}); session.commit()
    session.rollback()
    with pytest.raises(IntegrityError, match="cannot be modified or deleted"):
        session.execute(text("UPDATE journal_entries SET status = 'draft' WHERE id = :id"), {"id": january["receipt"]["id"]}); session.commit()
    session.rollback()
    assert certification(client, sid)["verified"] is True
