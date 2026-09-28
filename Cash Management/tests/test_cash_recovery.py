"""The certification state machine tolerates partial processes.

The session row (status, reopen_count, last_reopen_event_id) is the only
authoritative state. Reopen events and certifications are evidence the
database demands before a transition, but a stray / duplicated / half-written
row never hides a certification, never blocks a legitimate reopening or
re-certification, and is never deleted: it is reported in the history.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import Account, AccountType
from app.modules.cash import certification, reconciliation
from app.modules.cash.errors import CashConflict
from app.modules.cash.models import (
    CashAccount,
    ReconciliationCertification,
    ReconciliationReopenEvent,
    ReconciliationSession,
    ReconciliationStatus,
)
from app.modules.cash.schemas import CloseRequest, ReopenRequest
from tests.cash_helpers import (
    account,
    cash_account,
    cash_in,
    cash_line_id,
    import_statement,
    ledger_snapshot,
    match,
    open_session,
    statement_payload,
    summary,
)
from tests.conftest import alembic_config, migrate
from tests.test_cash_concurrency import assert_conflict_or_busy, run_parallel

RAW_EVENT = text(
    "INSERT INTO reconciliation_reopen_events (session_id, certification_id, reopen_number, reason, reopened_at) "
    "VALUES (:s, :c, :n, :reason, :t)"
)


@pytest.fixture
def january(client):
    ledger = account(client, "1000", "Bank", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    ca = cash_account(client, ledger["id"])
    receipt = cash_in(client, "2026-01-05", ledger["id"], sales["id"], "100.00", "Customer")
    st = import_statement(
        client,
        statement_payload(ca["id"], "JAN", "2026-01-01", "2026-01-31", "0",
                          [{"date": "2026-01-05", "amount": "100.00", "description": "Customer"}]),
    )
    s = open_session(client, ca["id"], st["statement"]["id"])
    match(client, s["id"], st["transactions"][0]["id"], cash_line_id(receipt, ledger["id"]))
    assert client.post(f"/api/v1/cash/reconciliations/{s['id']}/close").status_code == 200
    return {"sid": s["id"], "cash": ca["id"], "ledger": ledger["id"], "sales": sales["id"], "receipt": receipt}


def cert_id(session, sid, version=1):
    return session.scalar(select(ReconciliationCertification.id).where(
        ReconciliationCertification.session_id == sid, ReconciliationCertification.version == version))


def orphan_event(session, sid, number=1, reason="orphan"):
    """Simulate a partial process: the event is committed, the status never flips."""
    session.execute(RAW_EVENT, {"s": sid, "c": cert_id(session, sid, number), "n": number, "reason": reason,
                                "t": "2026-02-01 00:00:00+00:00"})
    session.commit()
    return session.scalar(select(func.max(ReconciliationReopenEvent.id)))


def get(client, sid, what=""):
    r = client.get(f"/api/v1/cash/reconciliations/{sid}{what}")
    assert r.status_code == 200, r.text
    return r.json()


def reopen(client, sid, reason="legitimate", expect=200):
    r = client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": reason})
    assert r.status_code == expect, r.text
    return r.json()


# --- Orphaned and duplicated reopen events ----------------------------------------------------


def test_orphaned_event_hides_nothing_and_blocks_nothing(client, january, session):
    sid = january["sid"]
    orphan = orphan_event(session, sid)

    # The certification is still the active, verified evidence of the closed session.
    s = summary(client, sid)
    assert s["certification"] is not None and s["certification"]["active"] is True and s["certification"]["verified"] is True
    assert get(client, sid, "/certification")["active"] is True
    assert get(client, sid)["status"] == "closed" and get(client, sid)["reopen_count"] == 0

    hist = get(client, sid, "/history")
    assert [(e["event"], e["state"]) for e in hist["events"]] == [("certified", "active"), ("reopened", "orphaned")]
    assert hist["integrity"]["consistent"] is True
    assert hist["integrity"]["orphaned_reopen_event_ids"] == [orphan]

    # A legitimate reopening succeeds with its own reason and completes the cycle.
    reopened = reopen(client, sid, "legitimate reason")
    assert reopened["status"] == "open" and reopened["reopen_count"] == 1
    assert reopened["last_reopen_event_id"] not in (None, orphan)

    hist = get(client, sid, "/history")
    assert [(e["event"], e["state"], e["reason"]) for e in hist["events"]] == [
        ("certified", "superseded", None),
        ("reopened", "superseded", "orphan"),
        ("reopened", "effective", "legitimate reason"),
    ]
    assert hist["integrity"]["consistent"] is True
    assert hist["integrity"]["superseded_reopen_event_ids"] == [orphan]

    # ... and re-certification chains to version 1 and names the effective event.
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    v2 = get(client, sid, "/certification")
    assert v2["version"] == 2 and v2["active"] is True and v2["verified"] is True
    assert v2["reopen_event_id"] == reopened["last_reopen_event_id"]
    assert v2["previous_hash"] == summary(client, sid)["certification"]["previous_hash"]
    assert get(client, sid, "/history")["integrity"]["consistent"] is True
    # Evidence is preserved: both certifications, both events.
    assert session.scalar(select(func.count(ReconciliationCertification.id))) == 2
    assert session.scalar(select(func.count(ReconciliationReopenEvent.id))) == 2


def test_duplicate_events_are_tolerated_and_kept(client, january, session):
    sid = january["sid"]
    first = orphan_event(session, sid, reason="retry 1")
    second = orphan_event(session, sid, reason="retry 2")
    hist = get(client, sid, "/history")
    assert hist["integrity"]["orphaned_reopen_event_ids"] == [first, second]
    assert hist["integrity"]["consistent"] is True
    assert summary(client, sid)["certification"]["active"] is True

    reopen(client, sid, "the real one")
    states = {e["event_id"]: e["state"] for e in get(client, sid, "/history")["events"] if e["event"] == "reopened"}
    assert states[first] == "superseded" and states[second] == "superseded"
    assert list(states.values()).count("effective") == 1
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    assert session.scalar(select(func.count(ReconciliationReopenEvent.id))) == 3  # nothing deleted


def test_orphaned_event_after_recertification_is_still_harmless(client, january, session):
    """Cycle 2: the orphan carries reopen_number 2; reopen_count stays the truth."""
    sid = january["sid"]
    reopen(client, sid, "cycle one")
    client.post(f"/api/v1/cash/reconciliations/{sid}/close")
    orphan = orphan_event(session, sid, number=2)
    assert get(client, sid)["reopen_count"] == 1
    assert summary(client, sid)["certification"]["version"] == 2
    reopened = reopen(client, sid, "cycle two")
    assert reopened["reopen_count"] == 2 and reopened["last_reopen_event_id"] != orphan
    client.post(f"/api/v1/cash/reconciliations/{sid}/close")
    assert get(client, sid, "/certification")["version"] == 3
    assert get(client, sid, "/history")["integrity"]["consistent"] is True


def test_stray_event_cannot_reference_anything_but_the_latest_certification(client, january, session):
    sid = january["sid"]
    reopen(client, sid, "r")
    client.post(f"/api/v1/cash/reconciliations/{sid}/close")  # version 2 is latest
    with pytest.raises(IntegrityError, match="latest certification"):
        session.execute(RAW_EVENT, {"s": sid, "c": cert_id(session, sid, 1), "n": 2, "reason": "old", "t": "2026-02-01 00:00:00+00:00"})
        session.commit()
    session.rollback()
    with pytest.raises(IntegrityError, match="next reopening"):
        session.execute(RAW_EVENT, {"s": sid, "c": cert_id(session, sid, 2), "n": 1, "reason": "stale", "t": "2026-02-01 00:00:00+00:00"})
        session.commit()
    session.rollback()


# --- Aborted transactions, rollback, retries ----------------------------------------------------


def test_aborted_reopening_leaves_no_trace(client, january, session, monkeypatch):
    sid = january["sid"]
    obj = session.get(ReconciliationSession, sid)

    def failing_update(*args, **kwargs):
        raise IntegrityError("UPDATE reconciliation_sessions", {}, Exception("simulated failure"))

    monkeypatch.setattr(reconciliation, "update", failing_update)
    with pytest.raises(CashConflict, match="simulated failure"):
        reconciliation.reopen_session(session, obj, ReopenRequest(reason="will abort"))
    monkeypatch.undo()

    assert session.scalar(select(func.count(ReconciliationReopenEvent.id))) == 0
    state = get(client, sid)
    assert state["status"] == "closed" and state["reopen_count"] == 0 and state["last_reopen_event_id"] is None
    assert summary(client, sid)["certification"]["active"] is True
    # The retry works and the history shows exactly one, effective, event.
    reconciliation.reopen_session(session, obj, ReopenRequest(reason="retry"))
    events = [e for e in get(client, sid, "/history")["events"] if e["event"] == "reopened"]
    assert [(e["state"], e["reason"]) for e in events] == [("effective", "retry")]


def test_database_rejection_during_reopen_rolls_back_the_event(client, january, session, monkeypatch):
    """If the trigger refuses the flip, the event written in the same transaction is gone too."""
    sid = january["sid"]
    obj = session.get(ReconciliationSession, sid)
    real_event = ReconciliationReopenEvent

    def wrong_numbered_event(**kwargs):  # simulate an application bug the trigger must catch
        kwargs["reopen_number"] += 1
        return real_event(**kwargs)

    monkeypatch.setattr(reconciliation, "ReconciliationReopenEvent", wrong_numbered_event)
    with pytest.raises(CashConflict, match="rejected by the database: .*next reopening"):
        reconciliation.reopen_session(session, obj, ReopenRequest(reason="stale"))
    monkeypatch.undo()
    assert session.scalar(select(func.count(ReconciliationReopenEvent.id))) == 0
    assert get(client, sid)["status"] == "closed"
    reopen(client, sid, "fine now")


def test_retries_are_idempotent_even_with_orphans_around(client, january, session):
    sid = january["sid"]
    orphan_event(session, sid)
    reopen(client, sid, "once")
    reopen(client, sid, "twice", expect=409)
    assert get(client, sid)["reopen_count"] == 1
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/recover").status_code == 409  # nothing to recover
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 409
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/recover").status_code == 409
    hist = get(client, sid, "/history")
    assert [e["version"] for e in hist["events"] if e["event"] == "certified"] == [1, 2]
    assert hist["integrity"]["consistent"] is True


# --- Half-done close: orphaned certification --------------------------------------------------


def _half_done_close(session, sid, monkeypatch):
    """Commit the certification row but make the status flip fail afterwards."""
    obj = session.get(ReconciliationSession, sid)
    real_flip = reconciliation._flip_closed

    def commit_then_fail(db, *args, **kwargs):
        db.commit()  # the certification row is now durable
        raise IntegrityError("UPDATE reconciliation_sessions", {}, Exception("crash after commit"))

    monkeypatch.setattr(reconciliation, "_flip_closed", commit_then_fail)
    with pytest.raises(CashConflict, match="crash after commit"):
        reconciliation.close_session(session, obj, CloseRequest())
    monkeypatch.setattr(reconciliation, "_flip_closed", real_flip)
    return obj


def test_matching_orphaned_certification_is_adopted_by_a_retry(client, january, session, monkeypatch):
    sid = january["sid"]
    reopen(client, sid, "r")
    _half_done_close(session, sid, monkeypatch)

    assert get(client, sid)["status"] == "open"
    integrity = get(client, sid, "/history")["integrity"]
    assert integrity["consistent"] is False and integrity["orphaned_certification_version"] == 2
    assert any("committed half-way" in p for p in integrity["problems"])
    assert summary(client, sid)["certification"] is None  # not active while open
    assert get(client, sid, "/certification")["active"] is False

    # Retrying the close adopts the identical evidence instead of writing a second version.
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    cert = get(client, sid, "/certification")
    assert cert["version"] == 2 and cert["active"] is True and cert["verified"] is True
    assert session.scalar(select(func.count(ReconciliationCertification.id))) == 2
    assert get(client, sid, "/history")["integrity"]["consistent"] is True


def test_mismatching_orphaned_certification_requires_recover(client, january, session, monkeypatch):
    sid = january["sid"]
    reopen(client, sid, "r")
    _half_done_close(session, sid, monkeypatch)
    # The state moves on before anyone retries: the orphan no longer matches.
    cash_in(client, "2026-01-20", january["ledger"], january["sales"], "5.00", "late receipt")

    resp = client.post(f"/api/v1/cash/reconciliations/{sid}/close")
    assert resp.status_code == 409 and "call recover" in resp.json()["detail"]
    assert get(client, sid)["status"] == "open"

    report = client.post(f"/api/v1/cash/reconciliations/{sid}/recover")
    assert report.status_code == 200, report.text
    assert report.json()["consistent"] is True and report.json()["status"] == "closed"
    cert = get(client, sid, "/certification")
    assert cert["version"] == 2 and cert["active"] is True and cert["verified"] is True
    # Its drift tells the truth about what changed since it was written.
    assert any(d["kind"] == "late_ledger_line" for d in cert["drift"])

    # Normal path again: reopen with a reason, certify version 3 chained to version 2.
    reopen(client, sid, "re-certify after recovery")
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    v3 = get(client, sid, "/certification")
    assert v3["version"] == 3 and v3["previous_hash"] == cert["content_hash"] and v3["drift"] == []
    assert [e["version"] for e in get(client, sid, "/history")["events"] if e["event"] == "certified"] == [1, 2, 3]


def test_raw_junk_certification_is_kept_flagged_and_recoverable(client, january, session):
    """A certification row forged for the open cycle: never deleted, always reported."""
    sid = january["sid"]
    reopened = reopen(client, sid, "r")
    session.execute(text(
        "INSERT INTO reconciliation_certifications (session_id, version, certified_at, content_hash, previous_hash, "
        "content, is_balanced, forced, difference, cleared_balance, ledger_balance, matches_count, reopen_event_id) "
        "VALUES (:s, 2, :t, 'deadbeef', NULL, '{}', :b, :f, 0, 0, 0, 0, :e)"),
        {"s": sid, "t": "2026-02-01 00:00:00+00:00", "b": True, "f": False, "e": reopened["last_reopen_event_id"]})
    session.commit()

    integrity = get(client, sid, "/history")["integrity"]
    assert integrity["consistent"] is False and integrity["orphaned_certification_version"] == 2
    assert any("version 2: content hash does not match" in p for p in integrity["problems"])
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 409
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/recover").status_code == 200

    cert = get(client, sid, "/certification")
    assert cert["version"] == 2 and cert["active"] is True and cert["verified"] is False
    reopen(client, sid, "replace the forged evidence with a real one")
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code == 200
    v3 = get(client, sid, "/certification")
    assert v3["verified"] is True and v3["previous_hash"] == "deadbeef"
    integrity = get(client, sid, "/history")["integrity"]
    assert integrity["certifications"] == 3  # the forged row is still part of the history
    assert any("version 2:" in p for p in integrity["problems"]) and integrity["consistent"] is False


# --- Concurrency with partial state present ---------------------------------------------------


def test_concurrent_reopen_with_orphan_present(engine, january, session):
    sid = january["sid"]
    orphan_event(session, sid)

    def reopener(db):
        return reconciliation.reopen_session(db, db.get(ReconciliationSession, sid), ReopenRequest(reason="race")).status

    outcomes = run_parallel(engine, [reopener, reopener])
    assert sorted(k for k, _ in outcomes) == ["error", "ok"], outcomes
    assert_conflict_or_busy(next(o for o in outcomes if o[0] == "error"))
    with Session(engine) as db:
        s = db.get(ReconciliationSession, sid)
        assert s.status == ReconciliationStatus.OPEN and s.reopen_count == 1
        events = db.scalars(select(ReconciliationReopenEvent).order_by(ReconciliationReopenEvent.id)).all()
        assert len(events) == 2 and s.last_reopen_event_id == events[1].id
        assert certification.integrity(db, sid)["consistent"] is True


def test_concurrent_recover_and_close_after_half_done_close(engine, january, session, monkeypatch):
    sid = january["sid"]
    with Session(engine, expire_on_commit=False) as db:
        reconciliation.reopen_session(db, db.get(ReconciliationSession, sid), ReopenRequest(reason="r"))
    _half_done_close(session, sid, monkeypatch)

    def recover(db):
        return reconciliation.recover_session(db, db.get(ReconciliationSession, sid))["status"]

    def close(db):
        return reconciliation.close_session(db, db.get(ReconciliationSession, sid), CloseRequest()).status

    outcomes = run_parallel(engine, [recover, close])
    assert sorted(k for k, _ in outcomes) == ["error", "ok"], outcomes
    with Session(engine) as db:
        assert db.get(ReconciliationSession, sid).status == ReconciliationStatus.CLOSED
        assert db.scalar(select(func.count(ReconciliationCertification.id))) == 2
        assert certification.integrity(db, sid)["consistent"] is True


# --- History and ledger are preserved through everything --------------------------------------


def test_evidence_and_posted_ledger_survive_every_path(client, january, session, monkeypatch):
    sid = january["sid"]
    ledger_before = ledger_snapshot(client)
    v1 = get(client, sid, "/certification")
    orphan_event(session, sid)
    reopen(client, sid, "a")
    client.post(f"/api/v1/cash/reconciliations/{sid}/close")
    reopen(client, sid, "b")
    _half_done_close(session, sid, monkeypatch)
    client.post(f"/api/v1/cash/reconciliations/{sid}/close")  # adopts version 3
    reopen(client, sid, "c")
    client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": False})

    certs = session.scalars(select(ReconciliationCertification).order_by(ReconciliationCertification.version)).all()
    assert [c.version for c in certs] == [1, 2, 3, 4]
    assert certs[0].content_hash == v1["content_hash"]
    for c in certs:
        assert certification.verify(session, c) == []
    for later, earlier in zip(certs[1:], certs):
        assert later.previous_hash == earlier.content_hash
    assert session.scalar(select(func.count(ReconciliationReopenEvent.id))) == 4  # 1 orphan + 3 effective
    assert get(client, sid, "/history")["integrity"]["consistent"] is True
    assert ledger_snapshot(client) == ledger_before
    assert all(e["status"] == "posted" for e in ledger_before)
    with pytest.raises(IntegrityError, match="cannot be modified"):
        session.execute(text("DELETE FROM journal_entries WHERE id = :id"), {"id": january["receipt"]["id"]}); session.commit()
    session.rollback()


# --- Migration round trip -------------------------------------------------------------------


def test_cashmgmt03_downgrade_and_upgrade(db_url):
    migrate(db_url)
    engine = make_engine(db_url)
    with Session(engine) as db:
        # A session with one completed reopening, as cashmgmt02 could have stored it.
        ledger = Account(code="1000", name="Bank", type=AccountType.ASSET)
        db.add(ledger); db.commit()
        ca = CashAccount(ledger_account_id=ledger.id, label="Bank"); db.add(ca); db.commit()
        s = ReconciliationSession(cash_account_id=ca.id, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
                                  opening_balance=Decimal("0"), closing_balance=Decimal("0"))
        db.add(s); db.commit()
        reconciliation.close_session(db, s, CloseRequest())
        reconciliation.reopen_session(db, s, ReopenRequest(reason="r"))
        reconciliation.close_session(db, s, CloseRequest())
        sid = s.id
    engine.dispose()

    command.downgrade(alembic_config(db_url), "cashmgmt02")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("reconciliation_reopen_events")}
        assert "reopen_number" not in cols
        assert "last_reopen_event_id" not in {c["name"] for c in inspect(conn).get_columns("reconciliation_sessions")}
        assert conn.execute(text("SELECT count(*) FROM reconciliation_certifications")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM reconciliation_reopen_events")).scalar() == 1
    engine.dispose()

    migrate(db_url)
    engine = make_engine(db_url)
    with Session(engine) as db:
        # Backfill reconstructed the links and the state machine still works.
        assert certification.integrity(db, sid)["consistent"] is True
        s = db.get(ReconciliationSession, sid)
        assert s.last_reopen_event_id is not None
        v2 = certification.latest_certification(db, sid)
        assert v2.version == 2 and v2.reopen_event_id == s.last_reopen_event_id
        reconciliation.reopen_session(db, s, ReopenRequest(reason="after migration"))
        reconciliation.close_session(db, s, CloseRequest())
        assert certification.latest_certification(db, sid).version == 3
        assert json.loads(certification.latest_certification(db, sid).content)["version"] == 3
    engine.dispose()
