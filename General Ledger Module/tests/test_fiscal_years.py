"""Fiscal years and the controlled year-end close."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import FiscalYear
from app.services.fiscal_years import FiscalYearStateConflict, close_year
from tests.helpers import (  # noqa: F401
    account,
    balances,
    clear_hooks,
    draft,
    failing_checkpoint,
    post,
)

YEAR = {"name": "FY2026", "start_date": "2026-01-01", "end_date": "2026-12-31"}


@pytest.fixture
def chart(client):
    return {
        "cash": account(client, "1000", "Cash", "asset"),
        "equity": account(client, "3000", "Share capital", "equity"),
        "retained": account(
            client, "3900", "Retained earnings", "equity", category="retained_earnings"
        ),
        "sales": account(client, "4000", "Sales", "revenue"),
        "other_income": account(client, "4900", "Other income", "revenue"),
        "rent": account(client, "6000", "Rent", "expense"),
        "summary_equity": account(client, "3", "Equity", "equity", is_summary=True),
    }


@pytest.fixture
def year(client, chart):
    resp = client.post(
        "/api/v1/fiscal-years",
        json={
            **YEAR,
            "retained_earnings_account_id": chart["retained"]["id"],
            "generate_monthly_periods": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activity(client, chart):
    """Sales 1000 + other income 50, rent 300 -> net income 750; cash 750."""
    for lines, day in [
        (
            [
                {"account_id": chart["cash"]["id"], "debit": "1000.00"},
                {"account_id": chart["sales"]["id"], "credit": "1000.00"},
            ],
            "2026-03-10",
        ),
        (
            [
                {"account_id": chart["cash"]["id"], "debit": "50.00"},
                {"account_id": chart["other_income"]["id"], "credit": "50.00"},
            ],
            "2026-06-15",
        ),
        (
            [
                {"account_id": chart["rent"]["id"], "debit": "300.00"},
                {"account_id": chart["cash"]["id"], "credit": "300.00"},
            ],
            "2026-11-30",
        ),
    ]:
        post(client, draft(client, lines, entry_date=day)["id"])


def _close_all_periods(client, year):
    periods = client.get("/api/v1/periods").json()
    for period in periods:
        if period["fiscal_year_id"] == year["id"]:
            resp = client.post(f"/api/v1/periods/{period['id']}/close")
            assert resp.status_code == 200, resp.text
    return periods


def test_create_year_with_monthly_periods(client, chart, year):
    assert year["status"] == "open" and year["closing_entry_id"] is None
    periods = [p for p in client.get("/api/v1/periods").json() if p["fiscal_year_id"] == year["id"]]
    assert len(periods) == 12
    assert (periods[0]["start_date"], periods[0]["end_date"]) == ("2026-01-01", "2026-01-31")
    assert (periods[1]["start_date"], periods[1]["end_date"]) == ("2026-02-01", "2026-02-28")
    assert (periods[-1]["start_date"], periods[-1]["end_date"]) == ("2026-12-01", "2026-12-31")
    assert [y["name"] for y in client.get("/api/v1/fiscal-years").json()] == ["FY2026"]
    assert client.get(f"/api/v1/fiscal-years/{year['id']}").json() == year
    assert client.get("/api/v1/fiscal-years/999").status_code == 404


def test_year_validation(client, chart, year):
    base = {"retained_earnings_account_id": chart["retained"]["id"]}
    overlap = client.post(
        "/api/v1/fiscal-years",
        json={"name": "X", "start_date": "2026-12-31", "end_date": "2027-12-30", **base},
    )
    assert overlap.status_code == 409
    assert (
        client.post(
            "/api/v1/fiscal-years",
            json={
                **YEAR,
                "name": "Y",
                "start_date": "2027-01-01",
                "end_date": "2027-12-31",
                **base,
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/fiscal-years",
            json={"name": "Z", "start_date": "2028-12-31", "end_date": "2028-01-01", **base},
        ).status_code
        == 422
    )
    wrong = client.post(
        "/api/v1/fiscal-years",
        json={
            "name": "W",
            "start_date": "2029-01-01",
            "end_date": "2029-12-31",
            "retained_earnings_account_id": chart["sales"]["id"],
        },
    )
    assert wrong.status_code == 422 and "equity account" in wrong.json()["detail"]
    summary = client.post(
        "/api/v1/fiscal-years",
        json={
            "name": "V",
            "start_date": "2030-01-01",
            "end_date": "2030-12-31",
            "retained_earnings_account_id": chart["summary_equity"]["id"],
        },
    )
    assert summary.status_code == 422
    # Periods must lie inside their year.
    outside = client.post(
        "/api/v1/periods",
        json={
            "name": "bad",
            "start_date": "2031-01-01",
            "end_date": "2031-01-31",
            "fiscal_year_id": year["id"],
        },
    )
    assert outside.status_code == 422
    assert (
        client.post(
            "/api/v1/periods",
            json={
                "name": "bad",
                "start_date": "2031-01-01",
                "end_date": "2031-01-31",
                "fiscal_year_id": 999,
            },
        ).status_code
        == 404
    )


def test_close_check_lists_every_problem(client, chart, year):
    _activity(client, chart)
    pending = draft(
        client, [{"account_id": chart["cash"]["id"], "debit": "1.00"}], entry_date="2026-07-07"
    )
    client.patch(f"/api/v1/accounts/{chart['retained']['id']}", json={"is_active": False})

    check = client.get(f"/api/v1/fiscal-years/{year['id']}/close-check").json()
    assert check["can_close"] is False
    problems = check["problems"]
    assert any(p.startswith("periods still open: 2026-01, 2026-02") for p in problems)
    assert f"draft journal entries dated in the year: [{pending['id']}]" in problems
    assert "retained earnings account 3900 is inactive" in problems
    resp = client.post(f"/api/v1/fiscal-years/{year['id']}/close")
    assert resp.status_code == 409
    assert client.get(f"/api/v1/fiscal-years/{year['id']}").json()["status"] == "open"
    assert client.get("/api/v1/journal-entries?entry_type=closing").json() == []


def test_close_check_detects_period_gaps_and_missing_retained_earnings(client, chart):
    year = client.post("/api/v1/fiscal-years", json=YEAR).json()  # no periods, no RE account
    check = client.get(f"/api/v1/fiscal-years/{year['id']}/close-check").json()
    assert "the fiscal year has no accounting periods" in check["problems"]
    assert "no retained earnings account configured" in check["problems"]
    client.post(
        "/api/v1/periods",
        json={
            "name": "H1",
            "start_date": "2026-01-01",
            "end_date": "2026-06-30",
            "fiscal_year_id": year["id"],
        },
    )
    check = client.get(f"/api/v1/fiscal-years/{year['id']}/close-check").json()
    assert "dates not covered by any period: 2026-07-01 to 2026-12-31" in check["problems"]


def test_year_end_close_posts_balanced_closing_entry(client, chart, year):
    _activity(client, chart)
    before = balances(client)
    assert (
        before["4000"] == "-1000.00" and before["6000"] == "300.00" and before["1000"] == "750.00"
    )
    _close_all_periods(client, year)

    preview = client.get(f"/api/v1/fiscal-years/{year['id']}/closing-balances").json()
    assert preview["net_income"] == "750.00"
    assert {
        line["code"]: (line["balance"], line["debit"], line["credit"]) for line in preview["lines"]
    } == {
        "4000": ("-1000.00", "1000.00", "0.00"),
        "4900": ("-50.00", "50.00", "0.00"),
        "6000": ("300.00", "0.00", "300.00"),
    }
    assert (
        preview["total_debit"] == "1050.00" and preview["total_credit"] == "300.00"
    )  # + 750 to RE

    resp = client.post(f"/api/v1/fiscal-years/{year['id']}/close")
    assert resp.status_code == 200, resp.text
    closed = resp.json()
    assert closed["status"] == "closed" and closed["closed_at"] is not None
    entry = client.get(f"/api/v1/journal-entries/{closed['closing_entry_id']}").json()
    assert entry["entry_type"] == "closing" and entry["status"] == "posted"
    assert entry["entry_date"] == "2026-12-31" and entry["reference"] == "FY2026"
    lines = {line["account_id"]: (line["debit"], line["credit"]) for line in entry["lines"]}
    assert lines[chart["sales"]["id"]] == ("1000.00", "0.00")
    assert lines[chart["other_income"]["id"]] == ("50.00", "0.00")
    assert lines[chart["rent"]["id"]] == ("0.00", "300.00")
    assert lines[chart["retained"]["id"]] == ("0.00", "750.00")

    # Accounting outcome: temporary accounts are zero, retained earnings holds the profit,
    # balance-sheet accounts carry forward, and the books still balance.
    after = balances(client)
    assert after["4000"] == after["4900"] == after["6000"] == "0.00"
    assert after["3900"] == "-750.00" and after["1000"] == "750.00"
    assert after["__total_debit__"] == after["__total_credit__"]
    # Activity view of the year is unchanged by the close (the closing entry is dated inside
    # the year, so the year's gross totals grow, but the pre-close figures are intact).
    pre_close = {
        r["code"]: r["balance"]
        for r in client.get("/api/v1/reports/trial-balance?to=2026-12-30").json()["rows"]
    }
    assert pre_close["4000"] == "-1000.00"
    # Opening balance of the next year for cash is the carried-forward 750.
    gl = client.get(
        f"/api/v1/reports/general-ledger?account_id={chart['cash']['id']}&from=2027-01-01"
    ).json()
    assert gl["accounts"][0]["opening_balance"] == "750.00"
    gl = client.get(
        f"/api/v1/reports/general-ledger?account_id={chart['sales']['id']}&from=2027-01-01"
    ).json()
    assert gl["accounts"][0]["opening_balance"] == "0.00"


def test_repeating_a_completed_close_does_not_duplicate(client, chart, year):
    _activity(client, chart)
    _close_all_periods(client, year)
    first = client.post(f"/api/v1/fiscal-years/{year['id']}/close").json()
    snapshot = balances(client)
    for _ in range(3):
        again = client.post(f"/api/v1/fiscal-years/{year['id']}/close")
        assert again.status_code == 409
        assert f"closing entry {first['closing_entry_id']}" in again.json()["detail"]
    assert len(client.get("/api/v1/journal-entries?entry_type=closing").json()) == 1
    assert balances(client) == snapshot


def test_failed_close_leaves_financial_state_unchanged(client, chart, year):
    _activity(client, chart)
    _close_all_periods(client, year)
    snapshot = balances(client)
    entries_before = client.get("/api/v1/journal-entries").json()

    with failing_checkpoint("fiscal_years.close_before_commit"):
        with Session(client.app.state.engine) as db:
            with pytest.raises(RuntimeError, match="injected"):
                close_year(db, db.get(FiscalYear, year["id"]))

    assert client.get(f"/api/v1/fiscal-years/{year['id']}").json()["status"] == "open"
    assert (
        client.get("/api/v1/journal-entries").json() == entries_before
    )  # no closing entry, no lines
    assert balances(client) == snapshot
    # The retry succeeds exactly once.
    assert client.post(f"/api/v1/fiscal-years/{year['id']}/close").status_code == 200
    assert len(client.get("/api/v1/journal-entries?entry_type=closing").json()) == 1


def test_net_loss_and_zero_result(client, chart, year):
    # Loss: rent 300, no revenue.
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["rent"]["id"], "debit": "300.00"},
                {"account_id": chart["cash"]["id"], "credit": "300.00"},
            ],
            entry_date="2026-02-02",
        )["id"],
    )
    _close_all_periods(client, year)
    preview = client.get(f"/api/v1/fiscal-years/{year['id']}/closing-balances").json()
    assert preview["net_income"] == "-300.00"
    closed = client.post(f"/api/v1/fiscal-years/{year['id']}/close").json()
    entry = client.get(f"/api/v1/journal-entries/{closed['closing_entry_id']}").json()
    retained = next(
        line for line in entry["lines"] if line["account_id"] == chart["retained"]["id"]
    )
    assert (retained["debit"], retained["credit"]) == ("300.00", "0.00")
    assert retained["memo"] == "net loss for the year"
    assert balances(client)["3900"] == "300.00"  # debit balance: accumulated loss

    # Zero result (revenue == expense) in the next year: closing entry without an RE line.
    year2 = client.post(
        "/api/v1/fiscal-years",
        json={
            "name": "FY2027",
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            "retained_earnings_account_id": chart["retained"]["id"],
            "generate_monthly_periods": True,
        },
    ).json()
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "80.00"},
                {"account_id": chart["sales"]["id"], "credit": "80.00"},
            ],
            entry_date="2027-05-05",
        )["id"],
    )
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["rent"]["id"], "debit": "80.00"},
                {"account_id": chart["cash"]["id"], "credit": "80.00"},
            ],
            entry_date="2027-05-06",
        )["id"],
    )
    _close_all_periods(client, year2)
    closed2 = client.post(f"/api/v1/fiscal-years/{year2['id']}/close").json()
    entry2 = client.get(f"/api/v1/journal-entries/{closed2['closing_entry_id']}").json()
    assert {line["account_id"] for line in entry2["lines"]} == {
        chart["sales"]["id"],
        chart["rent"]["id"],
    }

    # No activity at all: the year closes without an entry.
    year3 = client.post(
        "/api/v1/fiscal-years",
        json={
            "name": "FY2028",
            "start_date": "2028-01-01",
            "end_date": "2028-12-31",
            "retained_earnings_account_id": chart["retained"]["id"],
            "generate_monthly_periods": True,
        },
    ).json()
    _close_all_periods(client, year3)
    assert (
        client.post(f"/api/v1/fiscal-years/{year3['id']}/close").json()["closing_entry_id"] is None
    )


def test_closed_year_is_final(client, chart, year):
    _activity(client, chart)
    periods = _close_all_periods(client, year)
    closed = client.post(f"/api/v1/fiscal-years/{year['id']}/close").json()
    december = next(p for p in periods if p["name"] == "2026-12")

    # Periods of a closed year cannot be reopened (API and trigger); they can be locked.
    assert client.post(f"/api/v1/periods/{december['id']}/reopen").status_code == 409
    with Session(client.app.state.engine) as db:
        with pytest.raises(IntegrityError, match="closed fiscal year cannot be reopened"):
            db.execute(
                text("UPDATE accounting_periods SET status = 'open' WHERE id = :id"),
                {"id": december["id"]},
            )
        db.rollback()
        with pytest.raises(IntegrityError, match="closed fiscal year cannot be modified"):
            db.execute(
                text("UPDATE fiscal_years SET status = 'open' WHERE id = :id"), {"id": year["id"]}
            )
        db.rollback()
        with pytest.raises(IntegrityError, match="closed fiscal year cannot be modified"):
            db.execute(text("DELETE FROM fiscal_years WHERE id = :id"), {"id": year["id"]})
        db.rollback()
    assert client.post(f"/api/v1/periods/{december['id']}/lock").status_code == 200

    # The closing entry is a system entry: no reversal, no edits.
    resp = client.post(
        f"/api/v1/journal-entries/{closed['closing_entry_id']}/reverse",
        json={"reversal_date": "2027-01-01"},
    )
    assert resp.status_code == 409 and "cannot be reversed" in resp.json()["detail"]
    with Session(client.app.state.engine) as db:
        with pytest.raises(IntegrityError, match="cannot be reversed"):
            db.execute(
                text(
                    "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                    "version, reverses_entry_id) VALUES ('2027-01-01', 'x', 'draft', 'reversal', "
                    "1, :id)"
                ),
                {"id": closed["closing_entry_id"]},
            )
        db.rollback()


def test_close_requires_year_end_period_not_locked(client, chart, year):
    _activity(client, chart)
    periods = _close_all_periods(client, year)
    december = next(p for p in periods if p["name"] == "2026-12")
    client.post(f"/api/v1/periods/{december['id']}/lock")
    check = client.get(f"/api/v1/fiscal-years/{year['id']}/close-check").json()
    assert any("containing the year end is locked" in p for p in check["problems"])
    assert client.post(f"/api/v1/fiscal-years/{year['id']}/close").status_code == 409


def test_direct_sql_cannot_close_year_with_open_periods(client, chart, year):
    with Session(client.app.state.engine) as db:
        with pytest.raises(  # either guard may fire first
            IntegrityError,
            match="all of its periods are closed or locked|through a fiscal year closing record",
        ):
            db.execute(
                text("UPDATE fiscal_years SET status = 'closed' WHERE id = :id"), {"id": year["id"]}
            )
        db.rollback()


def test_concurrent_year_end_closes_one_wins(client, chart, year):
    from tests.helpers import Gate, Worker

    _activity(client, chart)
    _close_all_periods(client, year)
    engine = client.app.state.engine
    gate = Gate("fiscal_years.close_before_commit")

    def closing():
        with Session(engine) as db:
            return close_year(db, db.get(FiscalYear, year["id"])).closing_entry_id

    first, second = Worker(closing), Worker(closing)
    first.start()
    gate.wait_reached()
    second.start()
    second.join(0.4)
    assert second.is_alive()  # blocked on the year row / write lock
    gate.release.set()
    first.finish()
    second.finish()
    assert first.error is None and first.result is not None
    assert isinstance(second.error, FiscalYearStateConflict)
    with Session(engine) as db:
        closing_entries = db.scalars(
            text("SELECT id FROM journal_entries WHERE entry_type = 'closing'")
        ).all()
    assert closing_entries == [first.result]
