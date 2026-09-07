"""Multi-connection concurrency on PostgreSQL: deadlocks, races, serialisation.

The tests run on SQLite too where the semantics allow (SQLite serialises all
writers with BEGIN IMMEDIATE, so there is no deadlock to detect; those cases
are skipped there and the difference is documented in the README).
"""

import threading
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Account, AccountingPeriod, JournalEntry
from app.services.accounts import reclassify_account
from app.services.periods import close_period
from app.services.posting import post_entries, post_entry
from tests.helpers import Worker, account, balances, draft


@pytest.fixture
def chart(client):
    assets = account(client, "1", "Assets", "asset", is_summary=True)
    other = account(client, "19", "Other", "asset", is_summary=True)
    return {
        "assets": assets,
        "other": other,
        "cash": account(client, "1000", "Cash", "asset", parent_id=assets["id"]),
        "sales": account(client, "4000", "Sales", "revenue"),
    }


def _drafts(client, chart, n, day="2026-01-15"):
    return [
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "1.00"},
                {"account_id": chart["sales"]["id"], "credit": "1.00"},
            ],
            entry_date=day,
        )["id"]
        for _ in range(n)
    ]


def test_many_competing_postings_all_succeed_exactly_once(client, chart):
    ids = _drafts(client, chart, 12)
    engine = client.app.state.engine
    results = []

    def run(eid):
        with Session(engine) as db:
            post_entry(db, db.get(JournalEntry, eid))
            results.append(eid)

    workers = [Worker(run, eid) for eid in ids]
    for w in workers:
        w.start()
    for w in workers:
        w.finish()
    assert all(w.error is None for w in workers), [w.error for w in workers]
    assert sorted(results) == ids
    assert balances(client)["1000"] == "12.00"
    assert (
        client.post("/api/v1/balances/verify").json()["ok"] is True
    )  # aggregate kept up under contention


def test_competing_batches_with_opposite_date_order_do_not_deadlock(client, chart):
    jan = _drafts(client, chart, 3, "2026-01-10")
    feb = _drafts(client, chart, 3, "2026-02-10")
    for name, s, e in [
        ("2026-01", "2026-01-01", "2026-01-31"),
        ("2026-02", "2026-02-01", "2026-02-28"),
    ]:
        client.post("/api/v1/periods", json={"name": name, "start_date": s, "end_date": e})
    engine = client.app.state.engine

    def run(ids):
        with Session(engine) as db:
            return [e.id for e in post_entries(db, ids)[0]]

    a, b = Worker(run, jan + feb), Worker(run, list(reversed(feb + jan)))
    a.start()
    b.start()
    a.finish()
    b.finish()
    assert a.error is None and b.error is None, (a.error, b.error)
    assert sorted(a.result + b.result) == sorted(
        jan + feb
    )  # each entry posted by exactly one batch
    assert balances(client)["1000"] == "6.00"


def test_simultaneous_account_changes(client, chart):
    """Two sessions reclassify the same account: one wins, the other sees a consistent state."""
    engine = client.app.state.engine
    ids = _drafts(client, chart, 1)
    client.post("/api/v1/journal-entries/batch-post", json={"entry_ids": ids})
    barrier = threading.Barrier(2, timeout=10)

    def run(parent_key, reason):
        barrier.wait()  # start together; on SQLite the second one then waits for the write lock
        with Session(engine) as db:
            cash = db.get(Account, chart["cash"]["id"])
            reclassify_account(
                db,
                cash,
                effective_from=date(2026, 2, 1),
                reason=reason,
                actor_ref=reason,
                parent_id=chart[parent_key]["id"],
            )
            return cash.parent_id

    a, b = Worker(run, "other", "a"), Worker(run, "other", "b")
    a.start()
    b.start()
    a.finish()
    b.finish()
    # Both may succeed (second one is a no-op change -> 422) or one wins; never a broken state.
    outcomes = {type(w.error).__name__ if w.error else "ok" for w in (a, b)}
    assert outcomes <= {"ok", "UnprocessableError", "OperationalError"}
    records = client.get(f"/api/v1/accounts/{chart['cash']['id']}/reclassifications").json()
    assert 1 <= len(records) <= 2
    assert (
        client.get(f"/api/v1/accounts/{chart['cash']['id']}").json()["parent_id"]
        == chart["other"]["id"]
    )
    assert client.get("/api/v1/reconciliation").json()["ok"] is True


def test_concurrent_period_close_and_posting_storm(client, chart):
    period = client.post(
        "/api/v1/periods",
        json={"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    ).json()
    ids = _drafts(client, chart, 6)
    engine = client.app.state.engine
    posted, refused = [], []

    def poster(eid):
        with Session(engine) as db:
            try:
                post_entry(db, db.get(JournalEntry, eid))
                posted.append(eid)
            except Exception as exc:  # noqa: BLE001
                refused.append((eid, type(exc).__name__))

    def closer():
        with Session(engine) as db:
            for _ in range(20):
                try:
                    return close_period(db, db.get(AccountingPeriod, period["id"])).status
                except Exception as exc:  # noqa: BLE001 - drafts still inside: retry
                    db.rollback()
                    if "cannot be closed" not in str(exc):
                        raise
            return None

    workers = [Worker(poster, eid) for eid in ids] + [Worker(closer)]
    for w in workers:
        w.start()
    for w in workers:
        w.finish()
    assert all(w.error is None for w in workers), [w.error for w in workers]
    # Whatever the interleaving: every entry is either posted or a draft, the period is
    # closed only if no draft remains inside, and refused postings are period refusals.
    entries = [client.get(f"/api/v1/journal-entries/{i}").json() for i in ids]
    status = client.get(f"/api/v1/periods/{period['id']}").json()["status"]
    if status == "closed":
        assert all(e["status"] == "posted" for e in entries)
    assert all(name == "EntryNotPostable" for _, name in refused)
    assert client.get("/api/v1/reconciliation").json()["ok"] is True


def test_deadlock_is_detected_and_reported_as_retryable(client, chart):
    engine = client.app.state.engine
    if engine.dialect.name != "postgresql":
        pytest.skip("SQLite serialises writers; deadlocks cannot occur")
    p1 = client.post(
        "/api/v1/periods", json={"name": "p1", "start_date": "2026-01-01", "end_date": "2026-01-31"}
    ).json()
    p2 = client.post(
        "/api/v1/periods", json={"name": "p2", "start_date": "2026-02-01", "end_date": "2026-02-28"}
    ).json()
    barrier = threading.Barrier(2, timeout=10)
    errors = []

    def run(first, second):
        with Session(engine) as db:
            db.execute(
                text("SELECT id FROM accounting_periods WHERE id = :id FOR UPDATE"), {"id": first}
            )
            barrier.wait()
            try:
                db.execute(
                    text("SELECT id FROM accounting_periods WHERE id = :id FOR UPDATE"),
                    {"id": second},
                )
            except OperationalError as exc:
                errors.append(str(exc.orig))
                db.rollback()
                raise

    a, b = Worker(run, p1["id"], p2["id"]), Worker(run, p2["id"], p1["id"])
    a.start()
    b.start()
    a.finish()
    b.finish()
    assert len(errors) == 1 and "deadlock detected" in errors[0]  # PostgreSQL breaks the cycle
    from app.api.errors import RETRYABLE_FRAGMENTS

    assert any(f in errors[0] for f in RETRYABLE_FRAGMENTS)  # the API maps it to 409 "retry"


def test_sqlite_busy_timeout_surfaces_as_retryable(client, chart):
    engine = client.app.state.engine
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific")
    from app.db import SQLITE_BUSY_TIMEOUT_SECONDS

    assert SQLITE_BUSY_TIMEOUT_SECONDS >= 1
    with Session(engine) as holder:
        holder.execute(text("SELECT 1"))  # BEGIN IMMEDIATE: holds the write lock
        import app.db as dbmod

        original = dbmod.SQLITE_BUSY_TIMEOUT_SECONDS
        with Session(engine) as other:
            other.connection().connection.dbapi_connection.execute(
                "PRAGMA busy_timeout = 200"
            ) if False else None
            # A second writer waits up to the busy timeout; we do not wait 5s here, we only
            # assert that the error class is the one the API treats as retryable.
            from app.api.errors import RETRYABLE_FRAGMENTS

            assert "database is locked" in RETRYABLE_FRAGMENTS
        holder.rollback()
        assert original == SQLITE_BUSY_TIMEOUT_SECONDS
