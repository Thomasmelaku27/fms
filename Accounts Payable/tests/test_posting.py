"""Posting journal entries: the service and POST /journal-entries/{id}/post."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import Account, AccountType, EntryStatus, JournalEntry, JournalLine
from app.services.posting import EntryAlreadyPosted, EntryNotPostable, post_entry, posting_problems


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _entry(client, lines, description="Entry"):
    resp = client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-01-15", "description": description, "lines": lines},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _post(client, entry_id):
    return client.post(f"/api/v1/journal-entries/{entry_id}/post")


@pytest.fixture
def accounts(client):
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")
    return cash, sales


# --- API -------------------------------------------------------------------------


def test_post_balanced_entry(client, accounts):
    cash, sales = accounts
    entry = _entry(
        client,
        [
            {"account_id": cash["id"], "debit": "250.00"},
            {"account_id": sales["id"], "credit": "150.00"},
            {"account_id": sales["id"], "credit": "100.00"},
        ],
    )
    assert entry["status"] == "draft"

    resp = _post(client, entry["id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "posted"
    assert [line["debit"] for line in body["lines"]] == ["250.00", "0.00", "0.00"]
    assert client.get(f"/api/v1/journal-entries/{entry['id']}").json()["status"] == "posted"


def test_post_unknown_entry(client):
    assert _post(client, 999).status_code == 404


def test_post_already_posted_entry_is_conflict(client, accounts):
    cash, sales = accounts
    entry = _entry(
        client,
        [
            {"account_id": cash["id"], "debit": "10.00"},
            {"account_id": sales["id"], "credit": "10.00"},
        ],
    )
    assert _post(client, entry["id"]).status_code == 200

    resp = _post(client, entry["id"])
    assert resp.status_code == 409
    assert "already posted" in resp.json()["detail"]


def test_post_unbalanced_entry(client, accounts):
    cash, sales = accounts
    entry = _entry(
        client,
        [
            {"account_id": cash["id"], "debit": "100.00"},
            {"account_id": sales["id"], "credit": "60.00"},
        ],
    )

    resp = _post(client, entry["id"])
    assert resp.status_code == 422
    assert "debits (100.00) do not equal credits (60.00)" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{entry['id']}").json()["status"] == "draft"


def test_post_entry_with_line_without_amount(client, accounts):
    cash, sales = accounts
    entry = _entry(
        client,
        [
            {"account_id": cash["id"], "debit": "100.00"},
            {"account_id": sales["id"]},
            {"account_id": sales["id"], "credit": "100.00"},
        ],
    )

    resp = _post(client, entry["id"])
    assert resp.status_code == 422
    assert "line 2 has no amount" in resp.json()["detail"]


def test_post_entry_with_single_line(client, accounts):
    cash, _ = accounts
    entry = _entry(client, [{"account_id": cash["id"], "debit": "100.00"}])

    resp = _post(client, entry["id"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "at least 2 lines are required" in detail
    assert "debits (100.00) do not equal credits (0.00)" in detail


def test_post_entry_with_inactive_account(client, accounts):
    cash, sales = accounts
    entry = _entry(
        client,
        [
            {"account_id": cash["id"], "debit": "10.00"},
            {"account_id": sales["id"], "credit": "10.00"},
        ],
    )
    assert client.post(f"/api/v1/accounts/{sales['id']}/deactivate").status_code == 200

    resp = _post(client, entry["id"])
    assert resp.status_code == 422
    assert "inactive accounts: 4000" in resp.json()["detail"]

    # Reactivating the account makes the same draft postable.
    client.post(f"/api/v1/accounts/{sales['id']}/activate")
    assert _post(client, entry["id"]).status_code == 200


def test_post_reports_every_problem_at_once(client, accounts):
    cash, sales = accounts
    client.post(f"/api/v1/accounts/{cash['id']}/deactivate")
    entry = _entry(
        client, [{"account_id": cash["id"], "debit": "100.00"}, {"account_id": sales["id"]}]
    )

    detail = _post(client, entry["id"]).json()["detail"]
    assert "line 2 has no amount" in detail
    assert "debits (100.00) do not equal credits (0.00)" in detail
    assert "inactive accounts: 1000" in detail


# --- Service (reusable outside the API) -------------------------------------------


@pytest.fixture
def db(client) -> Session:
    with client.app.state.session_factory() as session:
        yield session


def _draft(db, lines):
    entry = JournalEntry(entry_date=date(2026, 1, 15), description="svc", lines=lines)
    db.add(entry)
    db.commit()
    return entry


def test_service_posts_and_reports_problems(db):
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    sales = Account(code="4000", name="Sales", type=AccountType.REVENUE)
    db.add_all([cash, sales])
    db.commit()

    incomplete = _draft(db, [JournalLine(account_id=cash.id, debit=Decimal("5.00"))])
    assert posting_problems(db, incomplete) == [
        "at least 2 lines are required",
        "debits (5.00) do not equal credits (0.00)",
    ]
    with pytest.raises(EntryNotPostable) as exc_info:
        post_entry(db, incomplete)
    assert exc_info.value.problems == posting_problems(db, incomplete)
    assert db.get(JournalEntry, incomplete.id).status == EntryStatus.DRAFT

    good = _draft(
        db,
        [
            JournalLine(account_id=cash.id, debit=Decimal("5.00")),
            JournalLine(account_id=sales.id, credit=Decimal("5.00")),
        ],
    )
    assert posting_problems(db, good) == []
    assert post_entry(db, good).status == EntryStatus.POSTED
    with pytest.raises(EntryAlreadyPosted):
        post_entry(db, good)


def test_service_detects_concurrent_posting(db):
    """If another session posts first, the conditional update loses and reports a conflict."""
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    db.add(cash)
    db.commit()
    entry = _draft(
        db,
        [
            JournalLine(account_id=cash.id, debit=Decimal("5.00")),
            JournalLine(account_id=cash.id, credit=Decimal("5.00")),
        ],
    )

    with Session(db.get_bind()) as other:
        post_entry(other, other.get(JournalEntry, entry.id))

    with pytest.raises(EntryAlreadyPosted):  # stale in-memory status is "draft"
        post_entry(db, entry)
