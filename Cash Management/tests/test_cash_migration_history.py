"""cashmgmt03 applied to databases with history created under cashmgmt02.

Every scenario is built with the cashmgmt02 schema *and its triggers active*
(so it is a state a real cashmgmt02 database can be in), then upgraded:

* nothing is rewritten or discarded - every pre-existing column of every row
  is byte-for-byte the same afterwards;
* the new links are filled only where they are unambiguous;
* irregular history (orphaned events, double certifications) is preserved,
  reported as anomalies, and the session stays fully operational;
* broken references refuse the upgrade before any DDL;
* a failure in the middle of the upgrade leaves the database exactly at
  cashmgmt02 (SQLite included), and the upgrade can simply be retried.
"""

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.db import make_engine
from app.modules.cash import certification, reconciliation
from app.modules.cash.certification import FIGURE_KEYS, SCHEMA, canonical_json, content_hash
from app.modules.cash.errors import CashConflict
from app.modules.cash.models import ReconciliationSession, ReconciliationStatus
from app.modules.cash.schemas import CloseRequest, ReopenRequest
from tests.conftest import alembic_config, migrate
from tests.test_cash_concurrency import assert_conflict_or_busy, run_parallel

TABLES = ("reconciliation_sessions", "reconciliation_certifications", "reconciliation_reopen_events")
NEW_COLUMNS = {
    "reconciliation_sessions": {"last_reopen_event_id"},
    "reconciliation_certifications": {"reopen_event_id"},
    "reconciliation_reopen_events": {"reopen_number"},
}


def valid_content(sid: int, version: int, previous_hash: str | None) -> tuple[str, str]:
    """A minimal but genuine certification document (so verification passes)."""
    figures = {k: ("0.00" if "total" in k or "balance" in k or k == "difference" else 0) for k in FIGURE_KEYS}
    figures["is_balanced"] = True
    doc = {
        "schema": SCHEMA, "version": version, "previous_hash": previous_hash,
        "certified_at": datetime(2026, 2, version, tzinfo=timezone.utc).isoformat(), "forced": False, "notes": None,
        "session": {"id": sid, "cash_account_id": 1, "ledger_account_id": 1, "statement_id": None,
                    "period_start": "2026-01-01", "period_end": "2026-01-31",
                    "opening_balance": "0.00", "closing_balance": "0.00"},
        "figures": figures, "matches": [], "bank_transactions": [], "ledger_lines": [], "discrepancies": [],
    }
    canonical = canonical_json(doc)
    return canonical, content_hash(canonical)


class Cashmgmt02World:
    """Raw-SQL driver for a database at cashmgmt02 (triggers of that revision apply)."""

    def __init__(self, conn):
        self.conn = conn
        conn.execute(text("INSERT INTO accounts (code, name, type, is_active) VALUES ('1000', 'Bank', 'asset', :t)"), {"t": True})
        conn.execute(text("INSERT INTO cash_accounts (ledger_account_id, label) VALUES (1, 'Bank')"))
        self.hashes: dict[tuple[int, int], str] = {}

    def session(self, sid, month):
        self.conn.execute(text(
            "INSERT INTO reconciliation_sessions (id, cash_account_id, period_start, period_end, opening_balance, "
            "closing_balance, status, reopen_count) VALUES (:id, 1, :start, :end, 0, 0, 'open', 0)"),
            {"id": sid, "start": f"2026-{month:02d}-01", "end": f"2026-{month:02d}-28"})

    def close(self, sid, version):
        previous = self.hashes.get((sid, version - 1))
        content, digest = valid_content(sid, version, previous)
        self.hashes[(sid, version)] = digest
        self.conn.execute(text(
            "INSERT INTO reconciliation_certifications (session_id, version, certified_at, content_hash, previous_hash, "
            "content, is_balanced, forced, difference, cleared_balance, ledger_balance, matches_count) "
            "VALUES (:s, :v, :t, :h, :p, :c, :b, :f, 0, 0, 0, 0)"),
            {"s": sid, "v": version, "t": datetime(2026, 2, version, tzinfo=timezone.utc).isoformat(), "h": digest, "p": previous,
             "c": content, "b": True, "f": False})
        self.conn.execute(text("UPDATE reconciliation_sessions SET status = 'closed' WHERE id = :s"), {"s": sid})

    def event(self, sid, version, reason):
        cert_id = self.conn.execute(text(
            "SELECT id FROM reconciliation_certifications WHERE session_id = :s AND version = :v"),
            {"s": sid, "v": version}).scalar()
        self.conn.execute(text(
            "INSERT INTO reconciliation_reopen_events (session_id, certification_id, reason, reopened_at) "
            "VALUES (:s, :c, :r, :t)"), {"s": sid, "c": cert_id, "r": reason, "t": datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat()})
        return self.conn.execute(text("SELECT MAX(id) FROM reconciliation_reopen_events")).scalar()

    def reopen(self, sid, version, reason="reopen"):
        """The complete cashmgmt02 reopening: event + status flip + counter."""
        self.event(sid, version, reason)
        self.conn.execute(text(
            "UPDATE reconciliation_sessions SET status = 'open', reopen_count = reopen_count + 1 WHERE id = :s"), {"s": sid})

    def raw_flip_open(self, sid):
        """What cashmgmt02 wrongly allowed once an orphaned event existed."""
        self.conn.execute(text("UPDATE reconciliation_sessions SET status = 'open' WHERE id = :s"), {"s": sid})


def build_history(url) -> dict:
    """Sessions 1..6 in the states a cashmgmt02 database can contain."""
    migrate(url, "cashmgmt02")
    engine = make_engine(url)
    with engine.begin() as conn:
        w = Cashmgmt02World(conn)
        w.session(1, 1); w.close(1, 1)                                   # closed once
        w.session(2, 2); w.close(2, 1); w.reopen(2, 1, "fix")              # reopened, open
        w.session(3, 3); w.close(3, 1); w.reopen(3, 1, "fix"); w.close(3, 2)  # re-certified, closed
        w.session(4, 4); w.close(4, 1); w.event(4, 1, "orphan")            # the reported bug: orphan, still closed
        w.session(5, 5); w.close(5, 1); w.event(5, 1, "orphan"); w.raw_flip_open(5); w.close(5, 2)  # double certification
        w.session(6, 6)                                                   # never certified
        for sid, status, count in conn.execute(text("SELECT id, status, reopen_count FROM reconciliation_sessions ORDER BY id")):
            assert (status, count) == {1: ("closed", 0), 2: ("open", 1), 3: ("closed", 1), 4: ("closed", 0),
                                       5: ("closed", 0), 6: ("open", 0)}[sid]
    engine.dispose()
    return snapshot(url)


def snapshot(url) -> dict:
    engine = make_engine(url)
    data = {}
    with engine.connect() as conn:
        for table in TABLES:
            rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings().all()
            data[table] = [dict(r) for r in rows]
        data["version"] = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        data["columns"] = {t: {c["name"] for c in inspect(conn).get_columns(t)} for t in TABLES}
    engine.dispose()
    return data


@contextmanager
def migration_warnings():
    """Collect warnings of the migration logger (alembic reconfigures root logging itself)."""
    records: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("alembic.runtime.migration")
    handler = Collector(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def migrate_without_logging_reconfiguration(url: str) -> None:
    """``alembic upgrade head`` but without env.py's fileConfig (it would drop our handler)."""
    cfg = alembic_config(url)
    cfg.config_file_name = None  # script_location was already read from the ini file
    command.upgrade(cfg, "head")


def old_columns_only(data: dict) -> dict:
    return {
        t: [{k: v for k, v in row.items() if k not in NEW_COLUMNS[t]} for row in data[t]] for t in TABLES
    }


# --- Upgrade on historical data ---------------------------------------------------------------


def test_upgrade_preserves_every_row_and_maps_links_only_when_unambiguous(db_url):
    before = build_history(db_url)
    assert before["version"] == "cashmgmt02"
    with migration_warnings() as messages:
        migrate_without_logging_reconfiguration(db_url)
    after = snapshot(db_url)

    assert after["version"] == "cashmgmt03"
    assert old_columns_only(after) == old_columns_only(before)  # nothing rewritten, nothing lost
    for table in TABLES:
        assert NEW_COLUMNS[table] <= after["columns"][table]

    sessions = {r["id"]: r for r in after["reconciliation_sessions"]}
    certs = {(r["session_id"], r["version"]): r for r in after["reconciliation_certifications"]}
    events = {r["id"]: r for r in after["reconciliation_reopen_events"]}
    by_session_events = {}
    for e in events.values():
        by_session_events.setdefault(e["session_id"], []).append(e)

    # reopen_number = version of the certification the event references
    assert all(e["reopen_number"] == certs[(e["session_id"], 1)]["version"] for e in events.values())
    # last_reopen_event_id only for completed reopenings
    assert sessions[1]["last_reopen_event_id"] is None
    assert sessions[2]["last_reopen_event_id"] == by_session_events[2][0]["id"]
    assert sessions[3]["last_reopen_event_id"] == by_session_events[3][0]["id"]
    assert sessions[4]["last_reopen_event_id"] is None  # orphan never completed anything
    assert sessions[5]["last_reopen_event_id"] is None  # reopen_count stayed 0 under cashmgmt02
    assert sessions[6]["last_reopen_event_id"] is None
    # certifications link to the event that preceded them
    assert certs[(3, 2)]["reopen_event_id"] == by_session_events[3][0]["id"]
    assert certs[(5, 2)]["reopen_event_id"] == by_session_events[5][0]["id"]
    assert all(certs[(s, 1)]["reopen_event_id"] is None for s in (1, 2, 3, 4, 5))

    # The operator was told about the irregular sessions during the upgrade.
    warned = "\n".join(messages)
    assert "session 4: 1 reopen event(s) but reopen_count = 0" in warned
    assert "session 5: 1 reopen event(s) but reopen_count = 0" in warned
    assert "session 3" not in warned and "session 2" not in warned

    engine = make_engine(db_url)
    with Session(engine) as db:
        reports = {sid: certification.integrity(db, sid) for sid in range(1, 7)}
    engine.dispose()
    for sid in range(1, 7):
        assert reports[sid]["consistent"] is True, (sid, reports[sid]["problems"])
    assert reports[1]["active_certification_version"] == 1 and reports[1]["anomalies"] == []
    assert reports[2]["active_certification_version"] is None and reports[2]["anomalies"] == []
    assert reports[3]["active_certification_version"] == 2 and reports[3]["anomalies"] == []
    assert reports[4]["active_certification_version"] == 1  # the orphan hides nothing
    assert reports[4]["orphaned_reopen_event_ids"] == [by_session_events[4][0]["id"]] and reports[4]["anomalies"] == []
    assert reports[5]["active_certification_version"] == 2
    assert any("reopen_count" in a for a in reports[5]["anomalies"])
    assert reports[6]["certifications"] == 0 and reports[6]["anomalies"] == []


def test_migrated_sessions_remain_fully_operational(db_url):
    build_history(db_url)
    migrate(db_url)
    engine = make_engine(db_url)
    with Session(engine, expire_on_commit=False) as db:
        def s(sid):
            return db.get(ReconciliationSession, sid)

        # 1: closed once -> reopen with a reason, certify version 2 chained to version 1
        reconciliation.reopen_session(db, s(1), ReopenRequest(reason="after migration"))
        reconciliation.close_session(db, s(1), CloseRequest())
        v2 = certification.latest_certification(db, 1)
        assert v2.version == 2 and v2.previous_hash == certification.latest_certification(db, 1).previous_hash
        assert v2.reopen_event_id == s(1).last_reopen_event_id
        # 2: open after a cashmgmt02 reopening -> closes as version 2 linked to that old event
        reconciliation.close_session(db, s(2), CloseRequest())
        assert certification.latest_certification(db, 2).reopen_event_id == s(2).last_reopen_event_id
        # 3: re-certified under cashmgmt02 -> reopening number 2
        reconciliation.reopen_session(db, s(3), ReopenRequest(reason="again"))
        assert s(3).reopen_count == 2
        # 4: orphaned event -> legitimate reopening succeeds, orphan is superseded, certify again
        reconciliation.reopen_session(db, s(4), ReopenRequest(reason="legitimate"))
        events = [e for e in certification.history(db, 4) if e["event"] == "reopened"]
        assert [(e["reason"], e["state"]) for e in events] == [("orphan", "superseded"), ("legitimate", "effective")]
        reconciliation.close_session(db, s(4), CloseRequest())
        # 5: double certification -> reopen (number 1 supersedes version 2), certify version 3
        reconciliation.reopen_session(db, s(5), ReopenRequest(reason="unstick"))
        reconciliation.close_session(db, s(5), CloseRequest())
        assert certification.latest_certification(db, 5).version == 3
        # 6: never certified -> closes as version 1
        reconciliation.close_session(db, s(6), CloseRequest())
        assert certification.latest_certification(db, 6).version == 1
        for sid in range(1, 7):
            report = certification.integrity(db, sid)
            assert report["consistent"] is True, (sid, report["problems"])
        # History of session 5 keeps the irregularity visible, nothing was cleaned up.
        assert any("reopen_count" in a for a in certification.integrity(db, 5)["anomalies"])
        assert [e["version"] for e in certification.history(db, 5) if e["event"] == "certified"] == [1, 2, 3]
        assert all(certification.verify(db, c) == [] for c in db.query(certification.ReconciliationCertification))
    engine.dispose()


def test_concurrent_reopen_of_migrated_orphan_session(db_url):
    build_history(db_url)
    migrate(db_url)
    engine = make_engine(db_url)

    def reopener(db):
        return reconciliation.reopen_session(db, db.get(ReconciliationSession, 4), ReopenRequest(reason="race")).status

    outcomes = run_parallel(engine, [reopener, reopener])
    assert sorted(k for k, _ in outcomes) == ["error", "ok"], outcomes
    assert_conflict_or_busy(next(o for o in outcomes if o[0] == "error"))
    with Session(engine) as db:
        s = db.get(ReconciliationSession, 4)
        assert s.status == ReconciliationStatus.OPEN and s.reopen_count == 1
        assert certification.integrity(db, 4)["reopen_events"] == 2  # orphan + the winner's
        assert certification.integrity(db, 4)["consistent"] is True
    engine.dispose()


# --- Refusals and atomicity --------------------------------------------------------------------


def test_upgrade_refuses_broken_references_before_any_change(db_url):
    if not db_url.startswith("sqlite"):
        pytest.skip("PostgreSQL enforces the foreign key; the state cannot exist there")
    before = build_history(db_url)
    engine = make_engine(db_url)
    with engine.begin() as conn:
        # cashmgmt02's own trigger refuses this; simulate someone who bypassed it.
        conn.execute(text("DROP TRIGGER trg_recon_reopen_insert_cert"))
        conn.execute(text(
            "INSERT INTO reconciliation_reopen_events (session_id, certification_id, reason, reopened_at) "
            "VALUES (1, 9999, 'dangling', '2026-03-01 00:00:00+00:00')"))
    engine.dispose()
    before = snapshot(db_url)

    with pytest.raises(RuntimeError, match="refused, nothing was changed.*missing certification"):
        migrate(db_url)
    assert snapshot(db_url) == before  # still cashmgmt02, every row intact, no new columns
    assert before["version"] == "cashmgmt02"


def test_failed_upgrade_leaves_cashmgmt02_intact_and_can_be_retried(db_url):
    """A conflicting object makes the upgrade fail half-way; nothing of it may persist."""
    build_history(db_url)
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX ix_reconciliation_reopen_events_session_id_reopen_number "
            "ON reconciliation_reopen_events (session_id)"))
    engine.dispose()
    before = snapshot(db_url)

    with pytest.raises(Exception, match="already exists|exists"):
        migrate(db_url)

    after_failure = snapshot(db_url)
    assert after_failure == before
    assert after_failure["version"] == "cashmgmt02"
    assert "reopen_number" not in after_failure["columns"]["reconciliation_reopen_events"]
    engine = make_engine(db_url)
    with engine.connect() as conn:
        uniques = {u["name"] for u in inspect(conn).get_unique_constraints("reconciliation_reopen_events")}
        assert "uq_reconciliation_reopen_events_certification_id" in uniques
    # cashmgmt02 rules are still in force
    with pytest.raises(Exception, match="only be recorded for a closed|latest certification"), engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO reconciliation_reopen_events (session_id, certification_id, reason, reopened_at) "
            "VALUES (6, 1, 'x', '2026-03-01 00:00:00+00:00')"))
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_reconciliation_reopen_events_session_id_reopen_number"))
    engine.dispose()

    migrate(db_url)  # the retry succeeds
    retried = snapshot(db_url)
    assert retried["version"] == "cashmgmt03"
    assert old_columns_only(retried) == old_columns_only(before)
    engine = make_engine(db_url)
    with Session(engine) as db:
        assert all(certification.integrity(db, sid)["consistent"] for sid in range(1, 7))
    engine.dispose()


def test_downgrade_refuses_when_cashmgmt02_cannot_represent_the_history(db_url):
    build_history(db_url)
    migrate(db_url)
    engine = make_engine(db_url)
    with Session(engine, expire_on_commit=False) as db:
        reconciliation.reopen_session(db, db.get(ReconciliationSession, 4), ReopenRequest(reason="legit"))
    engine.dispose()
    before = snapshot(db_url)

    with pytest.raises(RuntimeError, match="downgrade to cashmgmt02 refused, nothing was changed"):
        command.downgrade(alembic_config(db_url), "cashmgmt02")
    assert snapshot(db_url) == before
    assert before["version"] == "cashmgmt03"

    # Still operational afterwards.
    engine = make_engine(db_url)
    with Session(engine, expire_on_commit=False) as db:
        reconciliation.close_session(db, db.get(ReconciliationSession, 4), CloseRequest())
        assert certification.integrity(db, 4)["consistent"] is True
    engine.dispose()


def test_downgrade_and_upgrade_round_trip_keeps_history(db_url):
    """Regular history (one event per certification) survives cashmgmt03 -> cashmgmt02 -> cashmgmt03."""
    build_history(db_url)
    migrate(db_url)
    with_links = snapshot(db_url)
    command.downgrade(alembic_config(db_url), "cashmgmt02")
    at_02 = snapshot(db_url)
    assert at_02["version"] == "cashmgmt02"
    assert old_columns_only(at_02) == old_columns_only(with_links)
    migrate(db_url)
    again = snapshot(db_url)
    assert again == with_links  # deterministic backfill


def test_half_done_close_after_migration_is_recoverable(db_url, monkeypatch):
    build_history(db_url)
    migrate(db_url)
    engine = make_engine(db_url)
    with Session(engine, expire_on_commit=False) as db:
        obj = db.get(ReconciliationSession, 2)  # open after a cashmgmt02 reopening

        def commit_then_fail(inner_db, *args, **kwargs):
            inner_db.commit()
            raise __import__("sqlalchemy.exc", fromlist=["IntegrityError"]).IntegrityError("x", {}, Exception("crash"))

        monkeypatch.setattr(reconciliation, "_flip_closed", commit_then_fail)
        with pytest.raises(CashConflict, match="crash"):
            reconciliation.close_session(db, obj, CloseRequest())
        monkeypatch.undo()
        report = certification.integrity(db, 2)
        assert report["consistent"] is False and report["orphaned_certification_version"] == 2
        reconciliation.close_session(db, obj, CloseRequest())  # adopts the identical evidence
        assert certification.integrity(db, 2)["consistent"] is True
        assert certification.integrity(db, 2)["certifications"] == 2
        assert json.loads(certification.latest_certification(db, 2).content)["version"] == 2
    engine.dispose()
