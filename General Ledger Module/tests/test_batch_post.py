"""Atomic batch posting of drafts."""

import pytest
from sqlalchemy.orm import Session

from app.services.posting import BatchNotPostable, post_entries
from tests.helpers import (  # noqa: F401
    Gate,
    Worker,
    account,
    balances,
    clear_hooks,
    draft,
    failing_checkpoint,
)

URL = "/api/v1/journal-entries/batch-post"


@pytest.fixture
def chart(client):
    return account(client, "1000", "Cash", "asset"), account(client, "4000", "Sales", "revenue")


def _drafts(client, chart, amounts, day="2026-01-15"):
    cash, sales = chart
    return [
        draft(
            client,
            [{"account_id": cash["id"], "debit": a}, {"account_id": sales["id"], "credit": a}],
            entry_date=day,
        )["id"]
        for a in amounts
    ]


def test_batch_posts_all_or_nothing(client, chart):
    ids = _drafts(client, chart, ["10.00", "20.00", "30.00"])
    cash, sales = chart
    bad = draft(
        client,
        [
            {"account_id": cash["id"], "debit": "5.00"},
            {"account_id": sales["id"], "credit": "4.00"},
        ],
    )["id"]
    resp = client.post(URL, json={"entry_ids": ids + [bad]})
    assert resp.status_code == 422
    assert f"entry {bad}: debits (5.00) do not equal credits (4.00)" in resp.json()["detail"]
    assert all(e["status"] == "draft" for e in client.get("/api/v1/journal-entries").json())
    assert balances(client)["__total_debit__"] == "0.00"

    resp = client.post(URL, json={"entry_ids": ids})
    assert resp.status_code == 200
    assert resp.json() == {"posted_now": sorted(ids), "already_posted": []}
    assert balances(client)["1000"] == "60.00"


def test_repeated_and_overlapping_batches(client, chart):
    ids = _drafts(client, chart, ["10.00", "20.00"])
    client.post(URL, json={"entry_ids": ids})
    snapshot = balances(client)
    # Retry after success: idempotent no-op.
    resp = client.post(URL, json={"entry_ids": list(reversed(ids)) + [ids[0]]})
    assert resp.status_code == 200 and resp.json() == {"posted_now": [], "already_posted": ids}
    assert balances(client) == snapshot
    # Overlap with an unposted draft: refused, the draft stays a draft.
    extra = _drafts(client, chart, ["5.00"])[0]
    resp = client.post(URL, json={"entry_ids": ids + [extra]})
    assert resp.status_code == 409 and "overlaps" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{extra}").json()["status"] == "draft"
    assert balances(client) == snapshot
    assert client.post(URL, json={"entry_ids": [extra, 999]}).status_code == 422
    assert client.post(URL, json={"entry_ids": []}).status_code == 422


def test_batch_respects_period_rules_for_every_entry(client, chart):
    period = client.post(
        "/api/v1/periods",
        json={"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    ).json()
    feb = _drafts(client, chart, ["1.00"], day="2026-02-10")
    client.post(f"/api/v1/periods/{period['id']}/close")
    jan = _drafts(client, chart, ["2.00"], day="2026-01-10")  # created after the close
    resp = client.post(URL, json={"entry_ids": feb + jan})
    assert resp.status_code == 422 and "is closed" in resp.json()["detail"]
    assert balances(client)["__total_debit__"] == "0.00"


def test_commit_failure_posts_nothing(client, chart):
    ids = _drafts(client, chart, ["10.00", "20.00"])
    with failing_checkpoint("posting.batch_before_commit"):
        with Session(client.app.state.engine) as db:
            with pytest.raises(RuntimeError, match="injected"):
                post_entries(db, ids)
    assert all(e["status"] == "draft" for e in client.get("/api/v1/journal-entries").json())
    assert client.post(URL, json={"entry_ids": ids}).json()["posted_now"] == ids


def test_concurrent_identical_batches_one_posts(client, chart):
    ids = _drafts(client, chart, ["10.00", "20.00", "30.00"])
    engine = client.app.state.engine
    gate = Gate("posting.batch_before_commit")

    def run():
        with Session(engine) as db:
            posted, already = post_entries(db, ids)
            return [e.id for e in posted], [e.id for e in already]

    first = Worker(run)
    first.start()
    gate.wait_reached()
    from app.services import hooks

    hooks.clear()
    second = Worker(run)
    second.start()
    second.join(0.4)
    assert second.is_alive()  # blocked on the row locks / write lock
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None and second.error is None, (first.error, second.error)
    assert first.result == (ids, [])
    assert second.result == ([], ids)
    assert balances(client)["1000"] == "60.00"


def test_service_error_shape(client, chart):
    cash, sales = chart
    bad = draft(client, [{"account_id": cash["id"], "debit": "5.00"}])["id"]
    with Session(client.app.state.engine) as db:
        with pytest.raises(BatchNotPostable) as exc_info:
            post_entries(db, [bad])
    assert list(exc_info.value.problems) == [bad]
    assert "at least 2 lines are required" in exc_info.value.problems[bad]
