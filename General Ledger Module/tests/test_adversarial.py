"""Adversarial direct-SQL attempts against the critical invariants (both backends)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.helpers import POST_SQL, account, balances, draft, post


@pytest.fixture
def world(client):
    chart = {
        "cash": account(client, "1000", "Cash", "asset"),
        "sales": account(client, "4000", "Sales", "revenue"),
        "retained": account(client, "3900", "RE", "equity"),
        "summary": account(client, "1", "Assets", "asset", is_summary=True),
    }
    year = client.post(
        "/api/v1/fiscal-years",
        json={
            "name": "FY2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "retained_earnings_account_id": chart["retained"]["id"],
            "generate_monthly_periods": True,
        },
    ).json()
    posted = post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "100.00"},
                {"account_id": chart["sales"]["id"], "credit": "100.00"},
            ],
            entry_date="2026-01-15",
        )["id"],
    )
    periods = client.get("/api/v1/periods").json()
    client.post(f"/api/v1/periods/{periods[0]['id']}/close")
    client.post(f"/api/v1/periods/{periods[0]['id']}/lock")
    later_draft = draft(
        client,
        [
            {"account_id": chart["cash"]["id"], "debit": "1.00"},
            {"account_id": chart["sales"]["id"], "credit": "1.00"},
        ],
        entry_date="2026-01-20",
    )
    return {**chart, "year": year, "posted": posted, "periods": periods, "later_draft": later_draft}


ATTACKS = [
    ("post into a locked period", POST_SQL, "later_draft", "closed accounting period"),
    (
        "reopen a locked period",
        "UPDATE accounting_periods SET status = 'open' WHERE id = :id",
        "period0",
        "locked accounting period",
    ),
    (
        "re-date a locked period",
        "UPDATE accounting_periods SET end_date = '2026-01-10' WHERE id = :id",
        "period0",
        "locked accounting period",
    ),
    (
        "delete a posted entry",
        "DELETE FROM journal_entries WHERE id = :id",
        "posted",
        "posted journal entry cannot be",
    ),
    (
        "change a posted amount",
        "UPDATE journal_lines SET debit = 1 WHERE entry_id = :id AND debit > 0",
        "posted",
        "posted journal entry cannot be modified",
    ),
    (
        "move a posted line to a summary account",
        "UPDATE journal_lines SET account_id = :summary WHERE entry_id = :id AND debit > 0",
        "posted",
        "posted journal entry cannot be modified|summary account",
    ),
    (
        "alter posting timestamp",
        "UPDATE journal_entries SET posted_at = '1999-01-01 00:00:00' WHERE id = :id",
        "posted",
        "posted journal entry cannot be modified",
    ),
    (
        "alter actor",
        "UPDATE journal_entries SET posted_by = 'mallory' WHERE id = :id",
        "posted",
        "posted journal entry cannot be modified",
    ),
    (
        "strip a source reference partially",
        "UPDATE journal_entries SET source_type = 'ap', source_id = NULL, operation_id = NULL "
        "WHERE id = :id",
        "later_draft",
        "source_complete|posted journal entry",
    ),
    (
        "make a summary account postable with children",
        "UPDATE accounts SET is_summary = 0 WHERE id = :summary_with_child",
        "summary_with_child",
        "keeps its role and type|journal lines",
    ),
    (
        "give the summary account a cycle",
        "UPDATE accounts SET parent_id = :child WHERE id = :summary",
        "cycle",
        "cycle|summary account of the same type",
    ),
    (
        "close a period with drafts in it",
        "UPDATE accounting_periods SET status = 'closed' WHERE id = :id",
        "period_with_draft",
        "draft journal entries|earlier period",
    ),
    (
        "close the year by hand",
        "UPDATE fiscal_years SET status = 'closed' WHERE id = :id",
        "year",
        "closed or locked|closing record",
    ),
]


@pytest.mark.parametrize(
    ("name", "sql", "target", "fragment"), ATTACKS, ids=[a[0] for a in ATTACKS]
)
def test_direct_sql_attack_is_refused(client, world, name, sql, target, fragment):
    child = account(client, "1500", "Equipment", "asset", parent_id=world["summary"]["id"])
    feb_draft = draft(
        client, [{"account_id": world["cash"]["id"], "debit": "1.00"}], entry_date="2026-02-05"
    )
    params = {
        "id": {
            "later_draft": world["later_draft"]["id"],
            "period0": world["periods"][0]["id"],
            "posted": world["posted"]["id"],
            "summary_with_child": world["summary"]["id"],
            "cycle": world["summary"]["id"],
            "period_with_draft": world["periods"][1]["id"],
            "year": world["year"]["id"],
        }[target],
        "summary": world["summary"]["id"],
        "summary_with_child": world["summary"]["id"],
        "child": child["id"],
    }
    with Session(client.app.state.engine) as db:
        if db.get_bind().dialect.name == "postgresql":
            sql = sql.replace("is_summary = 0", "is_summary = false")
        before = balances(client)
        with pytest.raises(IntegrityError, match=fragment):
            db.execute(text(sql), params)
        db.rollback()
    assert balances(client) == before
    assert client.get("/api/v1/reconciliation").json()["ok"] is True
    assert client.get(f"/api/v1/journal-entries/{feb_draft['id']}").json()["status"] == "draft"


def test_forged_closing_and_duplicate_source_are_refused(client, world):
    with Session(client.app.state.engine) as db:
        db.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version) VALUES ('2026-12-31', 'forged', 'draft', 'closing', 1)"
            )
        )
        forged = db.execute(text("SELECT max(id) FROM journal_entries")).scalar_one()
        db.execute(
            text(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
                "VALUES (:e, :a, 100, 0), (:e, :b, 0, 100)"
            ),
            {"e": forged, "a": world["sales"]["id"], "b": world["retained"]["id"]},
        )
        with pytest.raises(IntegrityError, match="in-progress fiscal year closing"):
            db.execute(text(POST_SQL), {"id": forged})
        db.rollback()
        client.post(
            "/api/v1/journal-processing/entries",
            json={
                "source_type": "ap",
                "source_id": "1",
                "operation_id": "post",
                "entry_date": "2026-03-03",
                "description": "x",
                "lines": [
                    {"account_id": world["cash"]["id"], "debit": "1.00"},
                    {"account_id": world["sales"]["id"], "credit": "1.00"},
                ],
            },
        )
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                    "version, source_type, source_id, operation_id, request_fingerprint) VALUES "
                    "('2026-03-03', 'dup', 'draft', 'standard', 1, 'ap', '1', 'post', 'x')"
                )
            )
        db.rollback()


def test_tampering_with_triggers_off_is_caught_by_reconciliation(client, world):
    """If protections are removed (superuser), the reconciliation report still finds the damage."""
    from tests.test_reconciliation import triggers_disabled

    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE journal_lines SET debit = 150 WHERE entry_id = :id AND debit > 0"),
            {"id": world["posted"]["id"]},
        )
        db.execute(
            text("UPDATE journal_entries SET posted_at = '1999-01-01 00:00:00' WHERE id = :id"),
            {"id": world["posted"]["id"]},
        )
        db.execute(text("UPDATE account_daily_totals SET credit_cents = 1 WHERE credit_cents > 0"))
        db.commit()
    report = client.get("/api/v1/reconciliation").json()
    failing = {c["name"] for c in report["checks"] if c["findings"] and c["severity"] == "error"}
    assert {"unbalanced_posted_entries", "posting_metadata", "aggregate_balances"} <= failing
    assert report["ok"] is False
