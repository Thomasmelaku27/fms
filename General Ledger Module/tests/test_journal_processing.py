"""The journal-processing contract used by future AP / AR / cash modules (simulated callers)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import EntryType, JournalEntry
from app.services import journal_processing as jp
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
        "receivables": account(client, "1200", "Receivables", "asset"),
        "cash": account(client, "1000", "Cash", "asset"),
        "sales": account(client, "4000", "Sales", "revenue"),
        "tax": account(client, "2100", "Sales tax payable", "liability"),
    }


def _invoice(chart, source_id="INV-1", operation_id="post", amount="120.00", **overrides):
    """What an AR module would send when it posts an invoice."""
    net = (Decimal(amount) / Decimal("1.2")).quantize(Decimal("0.01"))
    tax = Decimal(amount) - net
    return {
        "source_type": "ar_invoice",
        "source_id": source_id,
        "operation_id": operation_id,
        "entry_date": "2026-01-15",
        "description": f"Invoice {source_id}",
        "reference": source_id,
        "lines": [
            {"account_id": chart["receivables"]["id"], "debit": amount},
            {"account_id": chart["sales"]["id"], "credit": str(net)},
            {"account_id": chart["tax"]["id"], "credit": str(tax)},
        ],
        **overrides,
    }


# --- Single requests -------------------------------------------------------------------


def test_process_creates_and_posts(client, chart):
    resp = client.post(URL, json=_invoice(chart))
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["created"] is True and result["posted_now"] is True
    entry = result["entry"]
    assert entry["status"] == "posted" and entry["entry_type"] == "standard"
    assert (entry["source_type"], entry["source_id"], entry["operation_id"]) == (
        "ar_invoice",
        "INV-1",
        "post",
    )
    assert balances(client)["1200"] == "120.00" and balances(client)["4000"] == "-100.00"


def test_identical_retry_returns_existing_result(client, chart):
    first = client.post(URL, json=_invoice(chart)).json()
    snapshot = balances(client)
    for _ in range(3):
        again = client.post(URL, json=_invoice(chart)).json()
        assert again["created"] is False and again["posted_now"] is False
        assert again["entry"]["id"] == first["entry"]["id"]
    assert balances(client) == snapshot
    assert len(client.get("/api/v1/journal-entries").json()) == 1


def test_non_material_differences_are_still_a_retry(client, chart):
    first = client.post(URL, json=_invoice(chart)).json()
    reworded = _invoice(chart, description="Invoice INV-1 (resent)", reference="INV-1/A")
    reworded["lines"][0]["memo"] = "customer copy"
    reworded["lines"].reverse()  # line order is not material either
    again = client.post(URL, json=reworded).json()
    assert again["created"] is False and again["entry"]["id"] == first["entry"]["id"]
    assert again["entry"]["description"] == "Invoice INV-1"  # the original wording is kept


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "121.00"},
        {"entry_date": "2026-01-16"},
        {"entry_type": "adjusting"},
    ],
)
def test_material_difference_is_a_conflict(client, chart, change):
    first = client.post(URL, json=_invoice(chart)).json()
    snapshot = balances(client)
    conflicting = _invoice(chart, **change)
    resp = client.post(URL, json=conflicting)
    assert resp.status_code == 409, resp.text
    assert f"already used by journal entry {first['entry']['id']}" in resp.json()["detail"]
    assert balances(client) == snapshot
    assert client.get(f"/api/v1/journal-entries/{first['entry']['id']}").json() == first["entry"]


def test_fingerprint_fields():
    """Exactly: entry_date, entry_type and the multiset of (account, debit, credit) - not post."""
    debit_line = jp.JournalRequestLine(1, Decimal("5"), Decimal("0"))
    credit_line = jp.JournalRequestLine(2, Decimal("0"), Decimal("5"))
    base = jp.JournalRequest(
        source_type="s",
        source_id="1",
        operation_id="o",
        entry_date=date(2026, 1, 1),
        description="d",
        lines=(debit_line, credit_line),
    )
    same = [
        jp.JournalRequest(**{**base.__dict__, "description": "other"}),
        jp.JournalRequest(**{**base.__dict__, "reference": "REF"}),
        jp.JournalRequest(**{**base.__dict__, "lines": tuple(reversed(base.lines))}),
        jp.JournalRequest(
            **{
                **base.__dict__,
                "lines": (
                    jp.JournalRequestLine(1, Decimal("5.00"), Decimal("0.00"), memo="m"),
                    base.lines[1],
                ),
            }
        ),
        jp.JournalRequest(
            **{**base.__dict__, "source_id": "2"}
        ),  # identifiers are the key, not the fingerprint
    ]
    same.append(jp.JournalRequest(**{**base.__dict__, "post": False}))  # workflow, not content
    different = [
        jp.JournalRequest(**{**base.__dict__, "entry_date": date(2026, 1, 2)}),
        jp.JournalRequest(**{**base.__dict__, "entry_type": EntryType.ADJUSTING}),
        jp.JournalRequest(
            **{
                **base.__dict__,
                "lines": (jp.JournalRequestLine(1, Decimal("5.01"), Decimal("0")), base.lines[1]),
            }
        ),
        jp.JournalRequest(
            **{
                **base.__dict__,
                "lines": (jp.JournalRequestLine(3, Decimal("5"), Decimal("0")), base.lines[1]),
            }
        ),
    ]
    assert all(r.fingerprint() == base.fingerprint() for r in same)
    assert all(r.fingerprint() != base.fingerprint() for r in different)
    assert len(base.fingerprint()) == 64


def test_draft_then_post_on_retry(client, chart):
    """post=True on an existing draft is the idempotent draft-to-post transition."""
    first = client.post(URL, json=_invoice(chart, post=False)).json()
    assert first["entry"]["status"] == "draft" and first["posted_now"] is False
    snapshot = balances(client)
    again = client.post(URL, json=_invoice(chart, post=False)).json()
    assert again["created"] is False and again["posted_now"] is False  # still a draft

    posted = client.post(URL, json=_invoice(chart, post=True)).json()
    assert posted["created"] is False and posted["posted_now"] is True
    assert posted["entry"]["id"] == first["entry"]["id"]
    assert posted["entry"]["status"] == "posted"
    assert posted["entry"]["posting_source"] == "processing"
    assert balances(client) != snapshot

    # Any later retry, with either flag, returns the posted entry unchanged.
    for flag in (True, False):
        retry = client.post(URL, json=_invoice(chart, post=flag)).json()
        assert (retry["created"], retry["posted_now"]) == (False, False)
        assert retry["entry"]["status"] == "posted"
    assert len(client.get("/api/v1/journal-entries").json()) == 1


def test_edited_draft_cannot_be_posted_by_source_retry(client, chart):
    first = client.post(URL, json=_invoice(chart, post=False)).json()["entry"]
    edited = client.put(
        f"/api/v1/journal-entries/{first['id']}",
        json={
            "entry_date": first["entry_date"],
            "description": "edited by a user",
            "version": first["version"],
            "lines": [
                {"account_id": chart["receivables"]["id"], "debit": "120.00"},
                {"account_id": chart["sales"]["id"], "credit": "120.00"},
            ],
        },
    )
    assert edited.status_code == 200
    resp = client.post(URL, json=_invoice(chart, post=True))
    assert resp.status_code == 409
    assert "was edited in the General Ledger" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{first['id']}").json()["status"] == "draft"
    # The GL itself can still post it, after which the source's retries are no-ops.
    assert client.post(f"/api/v1/journal-entries/{first['id']}/post").status_code == 200
    assert client.post(URL, json=_invoice(chart, post=True)).json()["posted_now"] is False


def test_validation_and_posting_problems(client, chart):
    bad = _invoice(chart, amount="120.00")
    bad["lines"][0]["debit"] = "130.00"
    resp = client.post(URL, json=bad)
    assert resp.status_code == 422 and "do not equal" in resp.json()["detail"]
    assert client.get("/api/v1/journal-entries").json() == []  # nothing stored, not even a draft
    unknown = _invoice(chart)
    unknown["lines"][0]["account_id"] = 999
    assert client.post(URL, json=_invoice(chart, entry_type="reversal")).status_code == 422
    assert client.post(URL, json=unknown).status_code == 422
    assert client.post(URL, json={**_invoice(chart), "bogus": 1}).status_code == 422
    # After a failure the same key is still free: the corrected request succeeds.
    assert client.post(URL, json=_invoice(chart)).json()["created"] is True


def test_commit_failure_leaves_nothing_and_retry_creates_once(client, chart):
    with failing_checkpoint("journal_processing.before_commit"):
        with Session(client.app.state.engine) as db:
            with pytest.raises(RuntimeError, match="injected"):
                jp.process_journal(db, _request(chart))
    assert client.get("/api/v1/journal-entries").json() == []
    assert client.post(URL, json=_invoice(chart)).json()["created"] is True
    assert client.post(URL, json=_invoice(chart)).json()["created"] is False
    assert len(client.get("/api/v1/journal-entries").json()) == 1


def _request(chart, **overrides):
    body = _invoice(chart, **overrides)
    return jp.JournalRequest(
        source_type=body["source_type"],
        source_id=body["source_id"],
        operation_id=body["operation_id"],
        entry_date=date.fromisoformat(body["entry_date"]),
        description=body["description"],
        reference=body.get("reference"),
        post=body.get("post", True),
        lines=tuple(
            jp.JournalRequestLine(
                line["account_id"],
                Decimal(line.get("debit", "0")),
                Decimal(line.get("credit", "0")),
            )
            for line in body["lines"]
        ),
    )


def test_concurrent_identical_requests_create_once(client, chart):
    """Two sessions race on the same source reference; both get the same entry."""
    engine = client.app.state.engine
    gate = Gate("journal_processing.before_commit")

    def run():
        with Session(engine, expire_on_commit=False) as db:
            result = jp.process_journal(db, _request(chart))
            return result.entry.id, result.created

    first = Worker(run)
    first.start()
    gate.wait_reached()
    from app.services import hooks

    hooks.clear()  # the second runner must not pause
    second = Worker(run)
    second.start()
    second.join(0.4)
    assert second.is_alive()  # blocked on the unique key (PostgreSQL) / write lock (SQLite)
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None and second.error is None, (first.error, second.error)
    assert first.result[1] is True
    assert second.result == (first.result[0], False)
    assert len(client.get("/api/v1/journal-entries").json()) == 1


def test_concurrent_conflicting_requests(client, chart):
    engine = client.app.state.engine
    gate = Gate("journal_processing.before_commit")

    def run(amount):
        with Session(engine, expire_on_commit=False) as db:
            return jp.process_journal(db, _request(chart, amount=amount)).entry.id

    first = Worker(run, "120.00")
    first.start()
    gate.wait_reached()
    from app.services import hooks

    hooks.clear()
    second = Worker(run, "240.00")
    second.start()
    second.join(0.4)
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None
    assert isinstance(second.error, jp.SourceConflict)
    assert balances(client)["1200"] == "120.00"


# --- Batches ---------------------------------------------------------------------------


def test_batch_is_atomic(client, chart):
    good = _invoice(chart, source_id="INV-1")
    bad = _invoice(chart, source_id="INV-2")
    bad["lines"][0]["debit"] = "999.00"
    resp = client.post(BATCH_URL, json={"entries": [good, bad, _invoice(chart, source_id="INV-3")]})
    assert resp.status_code == 422
    assert "INV-2" not in resp.text or "entry" in resp.text
    assert client.get("/api/v1/journal-entries").json() == []  # INV-1 was not kept

    resp = client.post(BATCH_URL, json={"entries": [good, _invoice(chart, source_id="INV-3")]})
    assert resp.status_code == 200
    assert [r["created"] for r in resp.json()["results"]] == [True, True]
    assert balances(client)["1200"] == "240.00"


def test_overlapping_batches(client, chart):
    client.post(
        BATCH_URL,
        json={"entries": [_invoice(chart, source_id="INV-1"), _invoice(chart, source_id="INV-2")]},
    )
    snapshot = balances(client)
    # Overlap with identical data: reused; new ones created.
    resp = client.post(
        BATCH_URL,
        json={"entries": [_invoice(chart, source_id="INV-2"), _invoice(chart, source_id="INV-3")]},
    )
    assert resp.status_code == 200
    assert [r["created"] for r in resp.json()["results"]] == [False, True]
    assert balances(client)["1200"] == "360.00"
    # Overlap with conflicting data: the whole batch is refused, INV-4 is not created.
    conflict = _invoice(chart, source_id="INV-1", amount="1.00")
    resp = client.post(BATCH_URL, json={"entries": [conflict, _invoice(chart, source_id="INV-4")]})
    assert resp.status_code == 409
    assert balances(client)["1200"] == "360.00"
    assert [e["source_id"] for e in client.get("/api/v1/journal-entries").json()] == [
        "INV-1",
        "INV-2",
        "INV-3",
    ]
    # A batch that repeats a key is malformed.
    resp = client.post(
        BATCH_URL,
        json={"entries": [_invoice(chart, source_id="INV-9"), _invoice(chart, source_id="INV-9")]},
    )
    assert resp.status_code == 422 and balances(client)["1200"] == "360.00"
    assert balances(client) != snapshot


def test_concurrent_identical_batches(client, chart):
    engine = client.app.state.engine
    requests = [_request(chart, source_id=f"INV-{i}") for i in range(1, 6)]
    gate = Gate("journal_processing.before_commit")

    def run():
        with Session(engine, expire_on_commit=False) as db:
            return [(r.entry.id, r.created) for r in jp.process_batch(db, requests).results]

    first = Worker(run)
    first.start()
    gate.wait_reached()
    from app.services import hooks

    hooks.clear()
    second = Worker(run)
    second.start()
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None and second.error is None, (first.error, second.error)
    assert [c for _, c in first.result] == [True] * 5
    assert [c for _, c in second.result] == [False] * 5
    assert [i for i, _ in first.result] == [i for i, _ in second.result]
    assert len(client.get("/api/v1/journal-entries").json()) == 5
    assert balances(client)["1200"] == "600.00"


def test_provenance_survives_posting_and_is_immutable(client, chart):
    entry = client.post(URL, json=_invoice(chart)).json()["entry"]
    with Session(client.app.state.engine) as db:
        row = db.get(JournalEntry, entry["id"])
        fingerprint = row.request_fingerprint
        assert fingerprint == _request(chart).fingerprint()
        with pytest.raises(Exception, match="posted journal entry cannot be modified"):
            db.execute(
                text("UPDATE journal_entries SET source_id = 'INV-X' WHERE id = :id"),
                {"id": entry["id"]},
            )
        db.rollback()
        with pytest.raises(Exception):  # partial references are refused by the CHECK constraint
            db.execute(
                text(
                    "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                    "version, source_type) VALUES ('2026-01-01', 'x', 'draft', 'standard', 1, "
                    "'ar_invoice')"
                )
            )
        db.rollback()
