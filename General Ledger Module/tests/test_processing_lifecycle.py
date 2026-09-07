"""Journal-processing lifecycle hardening: ordering, partial batches, stale drafts,
transaction ownership, retries and multi-connection races with fault injection."""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.services import hooks
from app.services import journal_processing as jp
from app.services.actor import PostingContext
from tests.helpers import (  # noqa: F401
    Gate,
    Worker,
    account,
    balances,
    clear_hooks,
    failing_checkpoint,
)

URL = "/api/v1/journal-processing/entries"
BATCH_URL = "/api/v1/journal-processing/batches"


@pytest.fixture
def chart(client):
    return {
        "ar": account(client, "1200", "Receivables", "asset"),
        "cash": account(client, "1000", "Cash", "asset"),
        "sales": account(client, "4000", "Sales", "revenue"),
    }


def _req(
    chart, source_id="INV-1", operation_id="post", amount="100.00", post=True, day="2026-01-15"
):
    return jp.JournalRequest(
        source_type="ar_invoice", source_id=source_id, operation_id=operation_id,
        entry_date=date.fromisoformat(day), description=f"{source_id}/{operation_id}",
        post=post,
        lines=(
            jp.JournalRequestLine(chart["ar"]["id"], amount),
            jp.JournalRequestLine(chart["sales"]["id"], 0, amount),
        ),
    )  # fmt: skip


def _json(chart, **kw):
    r = _req(chart, **kw)
    return {
        "source_type": r.source_type, "source_id": r.source_id, "operation_id": r.operation_id,
        "entry_date": r.entry_date.isoformat(), "description": r.description, "post": r.post,
        "lines": [
            {"account_id": ln.account_id, "debit": str(ln.debit), "credit": str(ln.credit)}
            for ln in r.lines
        ],
    }  # fmt: skip


def test_operations_on_one_document_are_distinct_events_in_any_order(client, chart):
    """A void arriving before its post is still a distinct, valid event (the GL does not
    know document semantics); both are recorded once, retries of each are no-ops."""
    void = _json(chart, operation_id="void")
    void["lines"] = list(reversed(void["lines"]))  # the void mirrors the invoice
    void["lines"][0]["debit"], void["lines"][0]["credit"] = "100.00", "0.00"
    void["lines"][1]["debit"], void["lines"][1]["credit"] = "0.00", "100.00"
    assert client.post(URL, json=void).json()["created"] is True
    assert client.post(URL, json=_json(chart)).json()["created"] is True
    assert client.post(URL, json=void).json()["created"] is False
    assert client.post(URL, json=_json(chart)).json()["created"] is False
    assert len(client.get("/api/v1/journal-entries").json()) == 2
    assert balances(client)["1200"] == "0.00"


def test_partial_batch_failure_keeps_nothing(client, chart):
    good = _json(chart, source_id="A")
    bad = _json(chart, source_id="B")
    bad["lines"][0]["debit"] = "101.00"
    later = _json(chart, source_id="C")
    assert client.post(BATCH_URL, json={"entries": [good, bad, later]}).status_code == 422
    assert client.get("/api/v1/journal-entries").json() == []
    # The keys stay free: a corrected batch succeeds, and a retry of it is a no-op.
    bad["lines"][0]["debit"] = "100.00"
    assert [
        r["created"]
        for r in client.post(BATCH_URL, json={"entries": [good, bad, later]}).json()["results"]
    ] == [True, True, True]
    assert [
        r["created"]
        for r in client.post(BATCH_URL, json={"entries": [good, bad, later]}).json()["results"]
    ] == [False, False, False]


def test_mixed_posted_and_draft_batch(client, chart):
    """Posting requests for a batch where some events already exist: posted ones are
    returned, drafts are posted now, new ones created - all in one transaction."""
    posted = client.post(URL, json=_json(chart, source_id="P")).json()["entry"]
    drafted = client.post(URL, json=_json(chart, source_id="D", post=False)).json()["entry"]
    resp = client.post(
        BATCH_URL,
        json={
            "entries": [
                _json(chart, source_id="P"),
                _json(chart, source_id="D"),
                _json(chart, source_id="N"),
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [(r["created"], r["posted_now"]) for r in results] == [
        (False, False),
        (False, True),
        (True, True),
    ]
    assert [r["entry"]["status"] for r in results] == ["posted"] * 3
    assert results[0]["entry"]["id"] == posted["id"] and results[1]["entry"]["id"] == drafted["id"]
    # One poison pill rolls back even the draft-to-post transition of D2.
    drafted2 = client.post(URL, json=_json(chart, source_id="D2", post=False)).json()["entry"]
    poison = _json(chart, source_id="X")
    poison["lines"][0]["debit"] = "1.00"
    assert (
        client.post(BATCH_URL, json={"entries": [_json(chart, source_id="D2"), poison]}).status_code
        == 422
    )
    assert client.get(f"/api/v1/journal-entries/{drafted2['id']}").json()["status"] == "draft"


def test_stale_draft_version_after_user_edit(client, chart):
    drafted = client.post(URL, json=_json(chart, source_id="S", post=False)).json()["entry"]
    client.put(
        f"/api/v1/journal-entries/{drafted['id']}",
        json={
            "entry_date": "2026-01-15",
            "description": "edited",
            "version": drafted["version"],
            "lines": [
                {"account_id": chart["ar"]["id"], "debit": "99.00"},
                {"account_id": chart["sales"]["id"], "credit": "99.00"},
            ],
        },
    )
    resp = client.post(URL, json=_json(chart, source_id="S"))
    assert resp.status_code == 409 and "edited in the General Ledger" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{drafted['id']}").json()["status"] == "draft"
    # A header-only edit (description) does not change the content: the transition still works.
    other = client.post(URL, json=_json(chart, source_id="T", post=False)).json()["entry"]
    client.put(
        f"/api/v1/journal-entries/{other['id']}",
        json={
            "entry_date": "2026-01-15",
            "description": "reworded",
            "version": other["version"],
            "lines": [
                {"account_id": chart["ar"]["id"], "debit": "100.00"},
                {"account_id": chart["sales"]["id"], "credit": "100.00"},
            ],
        },
    )
    assert client.post(URL, json=_json(chart, source_id="T")).json()["posted_now"] is True


def test_batch_with_commit_false_never_commits_callers_work(client, chart):
    engine = client.app.state.engine
    with Session(engine) as db:
        db.add(JournalEntry(entry_date=date(2026, 1, 1), description="caller's pending draft"))
        db.flush()
        result = jp.process_batch(db, [_req(chart, source_id="C1")], commit=False)
        assert result.results[0].created is True
        db.rollback()  # the caller decides: nothing is kept, neither its draft nor the batch
    assert client.get("/api/v1/journal-entries").json() == []

    with Session(engine) as db:
        db.add(JournalEntry(entry_date=date(2026, 1, 1), description="caller's pending draft"))
        db.flush()
        with pytest.raises(jp.UnprocessableError):
            jp.process_batch(db, [_req(chart, source_id="C2", amount="1.005")], commit=False)
        db.commit()  # the caller's own work survives the batch's failure
    assert [e["description"] for e in client.get("/api/v1/journal-entries").json()] == [
        "caller's pending draft"
    ]


def test_retry_after_commit_fault_creates_exactly_once(client, chart):
    with failing_checkpoint("journal_processing.before_commit"):
        with Session(client.app.state.engine) as db:
            with pytest.raises(RuntimeError):
                jp.process_journal(db, _req(chart, source_id="F"))
    assert client.get("/api/v1/journal-entries").json() == []
    first = client.post(URL, json=_json(chart, source_id="F")).json()
    second = client.post(URL, json=_json(chart, source_id="F")).json()
    assert (
        first["created"] and not second["created"] and first["entry"]["id"] == second["entry"]["id"]
    )


def test_lost_insert_race_is_retried_and_converges(client, chart):
    """Two connections race on one key; the loser's retry path is exercised (hook observed)."""
    engine = client.app.state.engine
    gate = Gate("journal_processing.before_commit")
    retried = []
    hooks.install("journal_processing.retry", lambda: retried.append(True))

    def run():
        with Session(engine, expire_on_commit=False) as db:
            r = jp.process_journal(
                db, _req(chart, source_id="R"), ctx=PostingContext("worker", "processing")
            )
            return r.entry.id, r.created

    first = Worker(run)
    first.start()
    gate.wait_reached()
    hooks._hooks.pop("journal_processing.before_commit")  # only the first waits
    second = Worker(run)
    second.start()
    second.join(0.4)
    assert second.is_alive()
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None and second.error is None, (first.error, second.error)
    assert first.result[1] is True and second.result == (first.result[0], False)
    if engine.dialect.name == "postgresql":
        assert retried, "the loser should have gone through the retry path"
    assert len(client.get("/api/v1/journal-entries").json()) == 1


def test_flush_fault_on_second_request_of_batch(client, chart):
    """An integrity failure while flushing the 2nd request undoes the 1st; a retry recovers."""
    with Session(client.app.state.engine) as db:
        db.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version, source_type, source_id, operation_id, request_fingerprint) VALUES "
                "('2026-01-15', 'planted', 'draft', 'standard', 1, 'ar_invoice', 'B', 'post', "
                "'bogus')"
            )
        )
        db.commit()
    resp = client.post(
        BATCH_URL, json={"entries": [_json(chart, source_id="A"), _json(chart, source_id="B")]}
    )
    assert resp.status_code == 409  # B exists with a different fingerprint: conflict, A not kept
    assert [e["source_id"] for e in client.get("/api/v1/journal-entries").json()] == ["B"]
    with Session(client.app.state.engine) as db:
        db.execute(text("DELETE FROM journal_entries WHERE source_id = 'B'"))
        db.commit()
    assert [
        r["created"]
        for r in client.post(
            BATCH_URL, json={"entries": [_json(chart, source_id="A"), _json(chart, source_id="B")]}
        ).json()["results"]
    ] == [True, True]


def test_posting_metadata_from_processing(client, chart):
    resp = client.post(
        URL,
        json=_json(chart),
        headers={"X-Actor-Ref": "ar-service", "X-Correlation-Id": "corr-123"},
    )
    entry = resp.json()["entry"]
    assert entry["posted_by"] == "ar-service" and entry["posting_source"] == "processing"
    assert entry["correlation_id"] == "corr-123" and entry["posted_at"] is not None
