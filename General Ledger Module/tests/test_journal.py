from datetime import date
from decimal import Decimal

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import JournalEntry, JournalLine


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def test_create_draft_entry(client):
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")

    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Cash sale",
            "lines": [
                {"account_id": cash["id"], "debit": "250.00"},
                {"account_id": sales["id"], "credit": "250.00"},
            ],
        },
    )
    assert resp.status_code == 201
    entry = resp.json()
    assert entry["status"] == "draft"
    assert len(entry["lines"]) == 2

    fetched = client.get(f"/api/v1/journal-entries/{entry['id']}").json()
    assert fetched["lines"][0]["debit"] == "250.00"


def test_unbalanced_or_incomplete_draft_accepted(client):
    """Balance is only enforced at posting time; drafts may be work in progress."""
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")

    single_line = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Started",
            "lines": [{"account_id": cash["id"], "debit": "250.00"}],
        },
    )
    assert single_line.status_code == 201
    assert single_line.json()["status"] == "draft"

    unbalanced = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Work in progress",
            "lines": [
                {"account_id": cash["id"], "debit": "250.00"},
                {"account_id": sales["id"], "credit": "100.00"},
            ],
        },
    )
    assert unbalanced.status_code == 201
    assert unbalanced.json()["status"] == "draft"


def test_unknown_account_rejected(client):
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Bad",
            "lines": [{"account_id": 42, "debit": "10.00"}],
        },
    )
    assert resp.status_code == 422


def test_negative_amount_rejected(client):
    cash = _account(client, "1000", "Cash", "asset")
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Bad",
            "lines": [{"account_id": cash["id"], "debit": "-5.00"}],
        },
    )
    assert resp.status_code == 422


def test_line_without_amount_accepted_in_draft(client):
    """A draft may be saved before every line has an amount."""
    cash = _account(client, "1000", "Cash", "asset")
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Not finished yet",
            "lines": [{"account_id": cash["id"]}],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["lines"][0] == {
        "id": resp.json()["lines"][0]["id"],
        "account_id": cash["id"],
        "debit": "0.00",
        "credit": "0.00",
        "memo": None,
    }


def test_line_with_both_sides_accepted_in_draft_but_blocks_posting(client):
    """While preparing a draft a line may carry both sides; posting rejects it."""
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Undecided",
            "lines": [
                {"account_id": cash["id"], "debit": "5.00", "credit": "5.00"},
                {"account_id": sales["id"], "credit": "5.00"},
                {"account_id": cash["id"], "debit": "5.00"},
            ],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["lines"][0]["debit"] == "5.00"
    assert resp.json()["lines"][0]["credit"] == "5.00"

    posted = client.post(f"/api/v1/journal-entries/{resp.json()['id']}/post")
    assert posted.status_code == 422
    assert "line 1 has both a debit and a credit" in posted.json()["detail"]


def test_database_constraint_violation_is_422_not_500(client):
    """If bad data slips past application validation, the DB backstop still yields 422.

    Simulates an application bug by writing an invalid line through the ORM from
    inside a request, bypassing the Pydantic checks of the real endpoint.
    """
    cash = _account(client, "1000", "Cash", "asset")

    @client.app.post("/_test/buggy-write")
    def buggy_write(db: Session = Depends(get_db)) -> None:
        entry = JournalEntry(
            entry_date=date(2026, 1, 15),
            description="bug",
            lines=[JournalLine(account_id=cash["id"], debit=Decimal("1.00"))],
        )
        db.add(entry)
        db.flush()
        # Bypass the ORM money validation on purpose (a raw write): the CHECK catches it.
        db.execute(
            text("UPDATE journal_lines SET debit = -1 WHERE entry_id = :id"), {"id": entry.id}
        )
        db.commit()

    @client.app.post("/_test/buggy-orm-write")
    def buggy_orm_write(db: Session = Depends(get_db)) -> None:
        JournalLine(account_id=cash["id"], debit=Decimal("-1.00"))  # ORM boundary refuses

    resp = client.post("/_test/buggy-write")
    assert resp.status_code == 422
    assert "ck_journal_lines_non_negative" in resp.json()["detail"]
    resp = client.post("/_test/buggy-orm-write")
    assert resp.status_code == 422 and "must not be negative" in resp.json()["detail"]
    assert client.get("/api/v1/journal-entries").json() == []
