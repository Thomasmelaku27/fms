"""Period closing process: month-end validation, reopen, permanent lock, concurrency.

Locked periods are checked at the API (409), service and database level (raw
SQL UPDATE/DELETE rejected by triggers).
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountingPeriod,
    AccountType,
    JournalEntry,
    JournalLine,
    PeriodStatus,
)
from app.services.periods import (
    PeriodNotClosable,
    PeriodStateConflict,
    close_period,
    close_problems,
    lock_period,
    reopen_period,
)
from app.services.posting import posting_problems
from tests.helpers import POST_SQL

JAN = {"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"}
FEB = {"name": "2026-02", "start_date": "2026-02-01", "end_date": "2026-02-28"}
MAR = {"name": "2026-03", "start_date": "2026-03-01", "end_date": "2026-03-31"}


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _period(client, body):
    resp = client.post("/api/v1/periods", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _entry(client, cash, sales, entry_date, post=True, amount="100.00"):
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": entry_date,
            "description": "Sale",
            "lines": [
                {"account_id": cash["id"], "debit": amount},
                {"account_id": sales["id"], "credit": amount},
            ],
        },
    )
    assert resp.status_code == 201
    entry = resp.json()
    if post:
        assert client.post(f"/api/v1/journal-entries/{entry['id']}/post").status_code == 200
    return entry


def _status(client, period):
    return client.get(f"/api/v1/periods/{period['id']}").json()["status"]


@pytest.fixture
def accounts(client):
    return _account(client, "1000", "Cash", "asset"), _account(client, "4000", "Sales", "revenue")


@pytest.fixture
def january(client):
    return _period(client, JAN)


# --- Lifecycle --------------------------------------------------------------------------


def test_lock_lifecycle(client, january):
    url = f"/api/v1/periods/{january['id']}"

    blocked = client.post(f"{url}/lock")
    assert blocked.status_code == 409
    assert "must be closed before it can be locked" in blocked.json()["detail"]

    assert client.post(f"{url}/close").status_code == 200
    locked = client.post(f"{url}/lock")
    assert locked.status_code == 200
    assert locked.json()["status"] == "locked"
    assert locked.json()["locked_at"] is not None
    assert locked.json()["closed_at"] is not None

    # Final state: every transition, repeated or not, is refused.
    for _ in range(2):
        assert client.post(f"{url}/lock").status_code == 409
        assert client.post(f"{url}/reopen").status_code == 409
        assert client.post(f"{url}/close").status_code == 409
    assert "locked" in client.post(f"{url}/reopen").json()["detail"]
    assert _status(client, january) == "locked"
    assert client.post("/api/v1/periods/999/lock").status_code == 404


def test_reopen_after_close_then_lock_again_requires_close(client, january):
    url = f"/api/v1/periods/{january['id']}"
    client.post(f"{url}/close")
    assert client.post(f"{url}/reopen").status_code == 200
    assert client.post(f"{url}/lock").status_code == 409
    assert client.post(f"{url}/close").status_code == 200
    assert client.post(f"{url}/lock").status_code == 200


# --- Month-end validation ------------------------------------------------------------


def test_close_requires_earlier_periods_closed(client, january):
    february = _period(client, FEB)
    check = client.get(f"/api/v1/periods/{february['id']}/close-check").json()
    assert check == {
        "period_id": february["id"],
        "can_close": False,
        "problems": ["earlier periods are still open: 2026-01"],
    }
    resp = client.post(f"/api/v1/periods/{february['id']}/close")
    assert resp.status_code == 409
    assert "earlier periods are still open: 2026-01" in resp.json()["detail"]

    assert client.post(f"/api/v1/periods/{january['id']}/close").status_code == 200
    assert client.get(f"/api/v1/periods/{february['id']}/close-check").json()["can_close"] is True
    assert client.post(f"/api/v1/periods/{february['id']}/close").status_code == 200


def test_close_check_reports_all_problems(client, accounts, january):
    cash, sales = accounts
    february = _period(client, FEB)
    draft = _entry(client, cash, sales, "2026-02-10", post=False)
    _entry(client, cash, sales, "2026-02-11")  # posted: fine

    check = client.get(f"/api/v1/periods/{february['id']}/close-check").json()
    assert check["can_close"] is False
    assert check["problems"] == [
        f"draft journal entries dated in the period: [{draft['id']}]",
        "earlier periods are still open: 2026-01",
    ]
    assert client.post(f"/api/v1/periods/{february['id']}/close").status_code == 409
    assert _status(client, february) == "open"
    assert client.get("/api/v1/periods/999/close-check").status_code == 404


def test_failed_close_leaves_no_trace(client, accounts, january):
    cash, sales = accounts
    _entry(client, cash, sales, "2026-01-10", post=False)
    before = client.get(f"/api/v1/periods/{january['id']}").json()
    assert client.post(f"/api/v1/periods/{january['id']}/close").status_code == 409
    after = client.get(f"/api/v1/periods/{january['id']}").json()
    assert after == before
    assert after["closed_at"] is None


# --- Posting / reversing into closed or locked periods ----------------------------------


def test_locked_period_blocks_posting_and_reversals(client, accounts, january):
    cash, sales = accounts
    posted = _entry(client, cash, sales, "2026-01-15")
    _period(client, FEB)
    client.post(f"/api/v1/periods/{january['id']}/close")
    client.post(f"/api/v1/periods/{january['id']}/lock")

    late = _entry(client, cash, sales, "2026-01-20", post=False)
    resp = client.post(f"/api/v1/journal-entries/{late['id']}/post")
    assert resp.status_code == 422
    assert "accounting period '2026-01' is locked" in resp.json()["detail"]

    resp = client.post(
        f"/api/v1/journal-entries/{posted['id']}/reverse", json={"reversal_date": "2026-01-31"}
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/v1/journal-entries/{posted['id']}/reverse", json={"reversal_date": "2026-02-01"}
    )
    assert resp.status_code == 201


# --- Service level and concurrency ------------------------------------------------------


def _seed(session):
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add(cash)
    period = AccountingPeriod(
        name="2026-01", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    session.add(period)
    session.commit()
    return cash, period


def test_service_transitions(session):
    cash, period = _seed(session)
    with pytest.raises(PeriodStateConflict):
        lock_period(session, period)
    with pytest.raises(PeriodStateConflict):
        reopen_period(session, period)

    draft = JournalEntry(
        entry_date=date(2026, 1, 5),
        description="pending",
        lines=[JournalLine(account_id=cash.id, debit=Decimal("1.00"))],
    )
    session.add(draft)
    session.commit()
    assert close_problems(session, period) == [
        f"draft journal entries dated in the period: [{draft.id}]"
    ]
    with pytest.raises(PeriodNotClosable) as exc_info:
        close_period(session, period)
    assert exc_info.value.problems == close_problems(session, period)
    assert period.status == PeriodStatus.OPEN

    session.delete(draft)
    session.commit()
    assert close_period(session, period).status == PeriodStatus.CLOSED
    assert lock_period(session, period).status == PeriodStatus.LOCKED
    assert period.locked_at is not None
    with pytest.raises(PeriodStateConflict):
        reopen_period(session, period)
    with pytest.raises(PeriodStateConflict):
        close_period(session, period)

    entry = JournalEntry(
        entry_date=date(2026, 1, 5),
        description="x",
        lines=[
            JournalLine(account_id=cash.id, debit=Decimal("1.00")),
            JournalLine(account_id=cash.id, credit=Decimal("1.00")),
        ],
    )
    session.add(entry)
    session.commit()
    assert posting_problems(session, entry) == ["accounting period '2026-01' is locked"]


def test_concurrent_close_only_one_wins(session, engine):
    _, period = _seed(session)
    session.commit()  # no open transaction: the other session must not wait on us
    with Session(engine, expire_on_commit=False) as other:
        other_period = other.get(AccountingPeriod, period.id)
        close_period(other, other_period)  # first caller wins

    # This session still believes the period is open; the locked re-read says otherwise.
    assert period.status == PeriodStatus.OPEN
    with pytest.raises(PeriodStateConflict, match="already closed"):
        close_period(session, period)
    session.rollback()
    session.refresh(period)
    assert period.status == PeriodStatus.CLOSED


def test_concurrent_lock_only_one_wins(session, engine):
    _, period = _seed(session)
    close_period(session, period)
    session.commit()  # release the transaction opened by refresh()
    with Session(engine, expire_on_commit=False) as other:
        lock_period(other, other.get(AccountingPeriod, period.id))
    with pytest.raises(PeriodStateConflict, match="already locked"):
        lock_period(session, period)
    with pytest.raises(PeriodStateConflict):
        reopen_period(session, period)


# --- Database level: locked periods are immutable even for direct SQL ---------------------


def _locked_period(session):
    session.execute(
        text(
            "INSERT INTO accounting_periods (name, start_date, end_date, status) "
            "VALUES ('2025-12', '2025-12-01', '2025-12-31', 'closed')"
        )
    )
    session.execute(text("UPDATE accounting_periods SET status = 'locked' WHERE name = '2025-12'"))
    session.commit()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE accounting_periods SET status = 'open' WHERE name = '2025-12'",
        "UPDATE accounting_periods SET status = 'closed' WHERE name = '2025-12'",
        "UPDATE accounting_periods SET end_date = '2026-01-15' WHERE name = '2025-12'",
        "UPDATE accounting_periods SET name = 'renamed' WHERE name = '2025-12'",
        "DELETE FROM accounting_periods WHERE name = '2025-12'",
    ],
)
def test_trigger_protects_locked_period(session, sql):
    _locked_period(session)
    with pytest.raises(IntegrityError, match="locked accounting period cannot be modified"):
        session.execute(text(sql))
    session.rollback()
    row = session.execute(
        text("SELECT status, end_date FROM accounting_periods WHERE name = '2025-12'")
    ).one()
    assert str(row.status) == "locked" and str(row.end_date) == "2025-12-31"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO accounting_periods (name, start_date, end_date, status) "
        "VALUES ('x', '2030-01-01', '2030-01-31', 'locked')",
        "UPDATE accounting_periods SET status = 'locked' WHERE name = '2026-01'",
    ],
)
def test_trigger_only_locks_closed_periods(session, sql):
    _seed(session)  # 2026-01, open
    with pytest.raises(IntegrityError, match="only be locked when it is closed"):
        session.execute(text(sql))
    session.rollback()


def test_trigger_blocks_posting_into_locked_period(session):
    _locked_period(session)
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    session.add(cash)
    session.commit()
    entry = JournalEntry(
        entry_date=date(2025, 12, 15),
        description="x",
        lines=[
            JournalLine(account_id=cash.id, debit=Decimal("1.00")),
            JournalLine(account_id=cash.id, credit=Decimal("1.00")),
        ],
    )
    session.add(entry)
    session.commit()
    with pytest.raises(IntegrityError, match="closed accounting period"):
        session.execute(text(POST_SQL), {"id": entry.id})
    session.rollback()
