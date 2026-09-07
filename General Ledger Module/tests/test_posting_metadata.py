"""Posting metadata: written by the posting transition, immutable, consistent."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.helpers import account, draft, post


@pytest.fixture
def chart(client):
    return account(client, "1000", "Cash", "asset"), account(client, "4000", "Sales", "revenue")


def _lines(chart, amount="5.00"):
    cash, sales = chart
    return [
        {"account_id": cash["id"], "debit": amount},
        {"account_id": sales["id"], "credit": amount},
    ]


def test_metadata_set_by_each_posting_path(client, chart):
    headers = {"X-Actor-Ref": "alice", "X-Correlation-Id": "req-1"}
    d = draft(client, _lines(chart))
    assert d["posted_at"] is None and d["posted_by"] is None and d["correlation_id"] is None
    api = client.post(f"/api/v1/journal-entries/{d['id']}/post", headers=headers).json()
    assert (api["posted_by"], api["posting_source"], api["correlation_id"]) == (
        "alice",
        "api",
        "req-1",
    )
    assert api["posted_at"] is not None

    ids = [draft(client, _lines(chart))["id"] for _ in range(2)]
    client.post(
        "/api/v1/journal-entries/batch-post",
        json={"entry_ids": ids},
        headers={"X-Actor-Ref": "bob"},
    )
    batch = [client.get(f"/api/v1/journal-entries/{i}").json() for i in ids]
    assert {e["posting_source"] for e in batch} == {"batch"} and {
        e["posted_by"] for e in batch
    } == {"bob"}
    assert len({e["correlation_id"] for e in batch}) == 1  # one id for the whole batch

    reversal = client.post(
        f"/api/v1/journal-entries/{d['id']}/reverse",
        json={"reversal_date": "2026-01-20"},
        headers={"X-Actor-Ref": "carol"},
    ).json()
    assert (reversal["posted_by"], reversal["posting_source"]) == ("carol", "reversal")

    opening = client.post(
        "/api/v1/opening-balances",
        json={
            "import_id": "I1",
            "as_of": "2025-12-31",
            "balances": [
                {"account_id": chart[0]["id"], "debit": "1.00"},
                {"account_id": chart[1]["id"], "credit": "1.00"},
            ],
        },
    ).json()["entry"]
    assert opening["posting_source"] == "opening_balance" and opening["posted_by"] == "anonymous"


def test_metadata_is_immutable_and_consistent(client, chart):
    entry = post(client, draft(client, _lines(chart))["id"])
    with Session(client.app.state.engine) as db:
        for column, value in [
            ("posted_at", "'2000-01-01 00:00:00'"),
            ("posted_by", "'mallory'"),
            ("posting_source", "'api'"),
            ("correlation_id", "'x'"),
        ]:
            with pytest.raises(IntegrityError, match="posted journal entry cannot be modified"):
                db.execute(
                    text(f"UPDATE journal_entries SET {column} = {value} WHERE id = :id"),
                    {"id": entry["id"]},
                )
            db.rollback()
        # A draft cannot carry metadata, a posted row cannot lack it (CHECK).
        d = draft(client, _lines(chart))
        with pytest.raises(IntegrityError, match="posting_metadata"):
            db.execute(
                text("UPDATE journal_entries SET posted_by = 'x' WHERE id = :id"), {"id": d["id"]}
            )
        db.rollback()
        with pytest.raises(IntegrityError, match="posting_metadata"):
            db.execute(
                text("UPDATE journal_entries SET status = 'posted' WHERE id = :id"), {"id": d["id"]}
            )
        db.rollback()


def test_reconciliation_reports_metadata_anomalies(client, chart):
    from tests.test_reconciliation import triggers_disabled

    entry = post(client, draft(client, _lines(chart))["id"])
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text(
                "UPDATE journal_entries SET posting_source = 'processing', "
                "posted_at = '2000-01-01 00:00:00' WHERE id = :id"
            ),
            {"id": entry["id"]},
        )
        db.commit()
    report = client.get("/api/v1/reconciliation").json()
    messages = [
        f["message"]
        for c in report["checks"]
        if c["name"] == "posting_metadata"
        for f in c["findings"]
    ]
    assert any("posted before it was created" in m for m in messages)
    assert any("no source reference" in m for m in messages)
    assert report["ok"] is False
