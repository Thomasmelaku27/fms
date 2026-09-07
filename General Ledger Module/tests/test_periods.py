"""Accounting periods: lifecycle, overlap rules and closed-period protection.

The closed-period rule is checked at three levels: API (422 from the posting
service), service (posting_problems) and database (trigger, exercised with raw
SQL that bypasses the application entirely).
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, AccountType, EntryStatus, JournalEntry, JournalLine, PeriodStatus
from app.services.periods import closed_period_for, create_period
from app.services.posting import posting_problems
from tests.helpers import POST_SQL

JAN = {"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"}
FEB = {"name": "2026-02", "start_date": "2026-02-01", "end_date": "2026-02-28"}


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _set_status(client, period_id, status):
    with Session(client.app.state.engine) as db:
        db.execute(
            text("UPDATE accounting_periods SET status = :status WHERE id = :id"),
            {"status": status, "id": period_id},
        )
        db.commit()


def _balanced_draft(client, cash, sales, entry_date="2026-01-15"):
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": entry_date,
            "description": "Sale",
            "lines": [
                {"account_id": cash["id"], "debit": "100.00"},
                {"account_id": sales["id"], "credit": "100.00"},
            ],
        },
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
def accounts(client):
    return _account(client, "1000", "Cash", "asset"), _account(client, "4000", "Sales", "revenue")


@pytest.fixture
def january(client):
    resp = client.post("/api/v1/periods", json=JAN)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Lifecycle ---------------------------------------------------------------------


def test_create_list_get_period(client, january):
    assert january["status"] == "open"
    assert january["closed_at"] is None
    client.post("/api/v1/periods", json=FEB)
    names = [p["name"] for p in client.get("/api/v1/periods").json()]
    assert names == ["2026-01", "2026-02"]
    assert client.get(f"/api/v1/periods/{january['id']}").json() == january
    assert client.get("/api/v1/periods/999").status_code == 404


def test_close_and_reopen_period(client, january):
    closed = client.post(f"/api/v1/periods/{january['id']}/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_at"] is not None

    again = client.post(f"/api/v1/periods/{january['id']}/close")
    assert again.status_code == 409
    assert "already closed" in again.json()["detail"]

    reopened = client.post(f"/api/v1/periods/{january['id']}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closed_at"] is None
    assert client.post(f"/api/v1/periods/{january['id']}/reopen").status_code == 409

    assert client.post("/api/v1/periods/999/close").status_code == 404
    assert client.post("/api/v1/periods/999/reopen").status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Overlap start", "start_date": "2026-01-31", "end_date": "2026-02-15"},
        {"name": "Overlap end", "start_date": "2025-12-15", "end_date": "2026-01-01"},
        {"name": "Inside", "start_date": "2026-01-10", "end_date": "2026-01-20"},
        {"name": "Around", "start_date": "2025-12-01", "end_date": "2026-03-01"},
    ],
)
def test_overlapping_period_rejected(client, january, payload):
    resp = client.post("/api/v1/periods", json=payload)
    assert resp.status_code == 409
    assert "overlaps '2026-01'" in resp.json()["detail"]
    assert len(client.get("/api/v1/periods").json()) == 1


def test_adjacent_period_accepted(client, january):
    assert client.post("/api/v1/periods", json=FEB).status_code == 201


def test_duplicate_name_rejected(client, january):
    resp = client.post("/api/v1/periods", json={**FEB, "name": "2026-01"})
    assert resp.status_code == 409


def test_invalid_date_range_rejected(client):
    resp = client.post(
        "/api/v1/periods",
        json={"name": "Backwards", "start_date": "2026-01-31", "end_date": "2026-01-01"},
    )
    assert resp.status_code == 422


# --- Closed periods block posting and reversals ----------------------------------------


def test_posting_in_closed_period_rejected(client, accounts, january):
    cash, sales = accounts
    assert client.post(f"/api/v1/periods/{january['id']}/close").status_code == 200
    # Drafts may still be created inside a closed period; they just cannot be posted there.
    draft = _balanced_draft(client, cash, sales, "2026-01-15")

    resp = client.post(f"/api/v1/journal-entries/{draft['id']}/post")
    assert resp.status_code == 422
    assert "accounting period '2026-01' is closed" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{draft['id']}").json()["status"] == "draft"

    # Drafts can still be edited (e.g. re-dated into an open month) and then posted.
    payload = {
        "entry_date": "2026-02-01",
        "description": "Sale",
        "version": draft["version"],
        "lines": [
            {"account_id": cash["id"], "debit": "100.00"},
            {"account_id": sales["id"], "credit": "100.00"},
        ],
    }
    assert client.put(f"/api/v1/journal-entries/{draft['id']}", json=payload).status_code == 200
    assert client.post(f"/api/v1/journal-entries/{draft['id']}/post").status_code == 200

    # Reopening the period allows posting inside it again.
    other = _balanced_draft(client, cash, sales, "2026-01-20")
    client.post(f"/api/v1/periods/{january['id']}/reopen")
    assert client.post(f"/api/v1/journal-entries/{other['id']}/post").status_code == 200


def test_posting_outside_any_period_is_allowed(client, accounts, january):
    cash, sales = accounts
    client.post(f"/api/v1/periods/{january['id']}/close")
    draft = _balanced_draft(client, cash, sales, "2025-06-30")
    assert client.post(f"/api/v1/journal-entries/{draft['id']}/post").status_code == 200


def test_reversal_dated_in_closed_period_rejected(client, accounts, january):
    cash, sales = accounts
    draft = _balanced_draft(client, cash, sales, "2026-01-15")
    posted = client.post(f"/api/v1/journal-entries/{draft['id']}/post").json()
    client.post(f"/api/v1/periods/{january['id']}/close")

    blocked = client.post(
        f"/api/v1/journal-entries/{posted['id']}/reverse", json={"entry_date": "2026-01-31"}
    )
    assert blocked.status_code == 422
    assert "accounting period '2026-01' is closed" in blocked.json()["detail"]
    assert len(client.get("/api/v1/journal-entries").json()) == 1  # no half-made reversal

    # The standard correction for a closed period: reverse into the next open one.
    ok = client.post(
        f"/api/v1/journal-entries/{posted['id']}/reverse", json={"entry_date": "2026-02-01"}
    )
    assert ok.status_code == 201
    assert ok.json()["status"] == "posted"


def test_closing_period_with_drafts_inside_is_blocked(client, accounts, january):
    """Month-end validation: pending drafts must be resolved before closing."""
    cash, sales = accounts
    draft = _balanced_draft(client, cash, sales, "2026-01-15")

    check = client.get(f"/api/v1/periods/{january['id']}/close-check").json()
    assert check["can_close"] is False
    assert check["problems"] == [f"draft journal entries dated in the period: [{draft['id']}]"]

    resp = client.post(f"/api/v1/periods/{january['id']}/close")
    assert resp.status_code == 409
    assert f"draft journal entries dated in the period: [{draft['id']}]" in resp.json()["detail"]
    assert client.get(f"/api/v1/periods/{january['id']}").json()["status"] == "open"

    assert client.delete(f"/api/v1/journal-entries/{draft['id']}").status_code == 204
    assert client.get(f"/api/v1/periods/{january['id']}/close-check").json()["can_close"] is True
    assert client.post(f"/api/v1/periods/{january['id']}/close").status_code == 200


# --- Service level ---------------------------------------------------------------------------


def test_service_reports_closed_period(session):
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add(cash)
    session.commit()
    period = create_period(session, "2026-01", date(2026, 1, 1), date(2026, 1, 31))
    entry = JournalEntry(
        entry_date=date(2026, 1, 15),
        description="x",
        lines=[
            JournalLine(account_id=cash.id, debit=Decimal("1.00")),
            JournalLine(account_id=cash.id, credit=Decimal("1.00")),
        ],
    )
    session.add(entry)
    session.commit()

    assert closed_period_for(session, date(2026, 1, 15)) is None
    assert posting_problems(session, entry) == []

    session.delete(entry)  # month-end rule (also a trigger): no drafts inside when closing
    session.commit()
    period.status = PeriodStatus.CLOSED
    session.commit()
    session.add(
        entry := JournalEntry(
            entry_date=date(2026, 1, 15),
            description="x",
            lines=[
                JournalLine(account_id=cash.id, debit=Decimal("1.00")),
                JournalLine(account_id=cash.id, credit=Decimal("1.00")),
            ],
        )
    )
    session.commit()
    assert closed_period_for(session, date(2026, 1, 15)).id == period.id
    assert closed_period_for(session, date(2026, 2, 1)) is None
    assert posting_problems(session, entry) == ["accounting period '2026-01' is closed"]


# --- Database level (bypassing the application) ----------------------------------------------


def _raw_setup(session):
    session.execute(
        text(
            "INSERT INTO accounting_periods (name, start_date, end_date, status) "
            "VALUES ('2026-01', '2026-01-01', '2026-01-31', 'closed')"
        )
    )
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add(cash)
    session.commit()
    entry = JournalEntry(
        entry_date=date(2026, 1, 15),
        description="x",
        lines=[
            JournalLine(account_id=cash.id, debit=Decimal("1.00")),
            JournalLine(account_id=cash.id, credit=Decimal("1.00")),
        ],
    )
    session.add(entry)
    session.commit()
    return entry


def test_trigger_blocks_posting_in_closed_period(session):
    entry = _raw_setup(session)
    with pytest.raises(IntegrityError, match="closed accounting period"):
        session.execute(text(POST_SQL), {"id": entry.id})
    session.rollback()
    assert session.get(JournalEntry, entry.id).status == EntryStatus.DRAFT

    # Same entry, re-dated outside the closed period, posts fine.
    entry.entry_date = date(2026, 2, 1)
    session.commit()
    session.execute(text(POST_SQL), {"id": entry.id})
    session.commit()
    session.expire_all()
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED


def test_trigger_blocks_overlapping_periods(session):
    _raw_setup(session)
    with pytest.raises(IntegrityError, match="cannot overlap"):
        session.execute(
            text(
                "INSERT INTO accounting_periods (name, start_date, end_date, status) "
                "VALUES ('Overlap', '2026-01-31', '2026-02-28', 'open')"
            )
        )
    session.rollback()

    session.execute(
        text(
            "INSERT INTO accounting_periods (name, start_date, end_date, status) "
            "VALUES ('2026-02', '2026-02-01', '2026-02-28', 'open')"
        )
    )
    session.commit()
    with pytest.raises(IntegrityError, match="cannot overlap"):
        session.execute(
            text("UPDATE accounting_periods SET start_date = '2026-01-31' WHERE name = '2026-02'")
        )
    session.rollback()


def test_trigger_rejects_reversal_of_unposted_entry(session):
    entry = _raw_setup(session)
    with pytest.raises(IntegrityError, match="only a posted journal entry can be reversed"):
        session.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, reverses_entry_id) "
                "VALUES ('2026-02-01', 'bogus reversal', 'draft', :id)"
            ),
            {"id": entry.id},
        )
    session.rollback()


def test_unique_reversal_link(session, engine):
    entry = _raw_setup(session)
    entry.entry_date = date(2026, 2, 1)
    session.commit()
    session.execute(text(POST_SQL), {"id": entry.id})
    session.commit()
    insert = text(
        "INSERT INTO journal_entries (entry_date, description, status, reverses_entry_id) "
        "VALUES ('2026-02-02', 'reversal', 'draft', :id)"
    )
    session.execute(insert, {"id": entry.id})
    session.commit()
    with pytest.raises(IntegrityError, match="(?i)unique"):
        session.execute(insert, {"id": entry.id})
    session.rollback()
