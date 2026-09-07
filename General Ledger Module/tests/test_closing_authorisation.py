"""Year-end closing: authorised exception, attestation record, verification, recovery."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import FiscalYear
from app.services.fiscal_years import close_year, verify_closing
from tests.helpers import (  # noqa: F401
    POST_SQL,
    account,
    balances,
    clear_hooks,
    draft,
    failing_checkpoint,
    post,
)


@pytest.fixture
def year(client):
    chart = {
        "cash": account(client, "1000", "Cash", "asset"),
        "retained": account(client, "3900", "Retained earnings", "equity"),
        "sales": account(client, "4000", "Sales", "revenue"),
    }
    y = client.post(
        "/api/v1/fiscal-years",
        json={
            "name": "FY2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "retained_earnings_account_id": chart["retained"]["id"],
            "generate_monthly_periods": True,
        },
    ).json()
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "300.00"},
                {"account_id": chart["sales"]["id"], "credit": "300.00"},
            ],
            entry_date="2026-05-05",
        )["id"],
    )
    for p in client.get("/api/v1/periods").json():
        assert client.post(f"/api/v1/periods/{p['id']}/close").status_code == 200
    return {"year": y, **chart}


def _insert_in_progress_record(db, year_id):
    false = "0" if db.get_bind().dialect.name == "sqlite" else "false"
    db.execute(
        text(
            "INSERT INTO fiscal_year_closings (fiscal_year_id, closing_entry_id, status, "
            "covered_period_ids, net_income, total_debit, total_credit, ledger_hash, "
            "reconciliation_ok, reconciliation_summary, actor_ref, correlation_id) VALUES "
            f"(:y, NULL, 'in_progress', '[]', 0, 0, 0, 'x', {false}, '{{}}', 'dba', 'c')"
        ),
        {"y": year_id},
    )


def _forge_closing_entry(db, year, day="2026-12-31"):
    db.execute(
        text(
            "INSERT INTO journal_entries (entry_date, description, status, entry_type, version) "
            "VALUES (:d, 'forged close', 'draft', 'closing', 1)"
        ),
        {"d": day},
    )
    entry_id = db.execute(text("SELECT max(id) FROM journal_entries")).scalar_one()
    db.execute(
        text(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
            "VALUES (:e, :a, 300, 0), (:e, :b, 0, 300)"
        ),
        {"e": entry_id, "a": year["sales"]["id"], "b": year["retained"]["id"]},
    )
    return entry_id


def test_direct_sql_cannot_post_closing_entries_into_closed_periods(client, year):
    with Session(client.app.state.engine) as db:
        forged = _forge_closing_entry(db, year)
        with pytest.raises(IntegrityError, match="in-progress fiscal year closing"):
            db.execute(text(POST_SQL), {"id": forged})
        db.rollback()
        # Even with a closing record, only the entry the record names, while the year is open.
        forged = _forge_closing_entry(db, year)
        _insert_in_progress_record(db, year["year"]["id"])
        with pytest.raises(IntegrityError, match="in-progress fiscal year closing"):
            db.execute(text(POST_SQL), {"id": forged})
        db.rollback()
    # A closing-type entry can never be posted into an *open* period without a record either.
    with Session(client.app.state.engine) as db:
        forged = _forge_closing_entry(db, year, day="2027-01-15")
        with pytest.raises(IntegrityError, match="in-progress fiscal year closing"):
            db.execute(text(POST_SQL), {"id": forged})
        db.rollback()
    assert client.get("/api/v1/journal-entries?entry_type=closing").json() == []


def test_api_cannot_create_closing_entries(client, year):
    for url, body in [
        (
            "/api/v1/journal-entries",
            {
                "entry_date": "2026-12-31",
                "description": "x",
                "entry_type": "closing",
                "lines": [{"account_id": year["cash"]["id"]}],
            },
        ),
        (
            "/api/v1/journal-processing/entries",
            {
                "source_type": "s",
                "source_id": "1",
                "operation_id": "o",
                "entry_date": "2026-12-31",
                "description": "x",
                "entry_type": "closing",
                "lines": [{"account_id": year["cash"]["id"], "debit": "1.00"}],
            },
        ),
    ]:
        assert client.post(url, json=body).status_code == 422


def test_authorised_close_produces_attestation(client, year):
    before = balances(client)
    resp = client.post(
        f"/api/v1/fiscal-years/{year['year']['id']}/close",
        headers={"X-Actor-Ref": "cfo", "X-Correlation-Id": "close-2026"},
    )
    assert resp.status_code == 200, resp.text
    record = client.get(f"/api/v1/fiscal-years/{year['year']['id']}/closing").json()
    assert record["status"] == "completed" and record["actor_ref"] == "cfo"
    assert record["correlation_id"] == "close-2026" and record["completed_at"] is not None
    assert record["net_income"] == "300.00" and record["total_debit"] == "300.00"
    assert json.loads(record["covered_period_ids"]) == [
        p["id"] for p in client.get("/api/v1/periods").json()
    ]
    assert (
        record["reconciliation_ok"] is True
        and json.loads(record["reconciliation_summary"])["error_count"] == 0
    )
    assert len(record["ledger_hash"]) == 64
    entry = client.get(f"/api/v1/journal-entries/{record['closing_entry_id']}").json()
    assert entry["posted_by"] == "cfo" and entry["posting_source"] == "year_end_close"
    assert entry["correlation_id"] == "close-2026"

    verification = client.get(f"/api/v1/fiscal-years/{year['year']['id']}/closing/verify").json()
    assert (
        verification["ok"] is True
        and verification["recorded_hash"] == verification["recomputed_hash"]
    )
    assert verification["recorded_net_income"] == verification["recomputed_net_income"] == "300.00"
    after = balances(client)
    assert (
        after["4000"] == "0.00" and after["3900"] == "-300.00" and after["1000"] == before["1000"]
    )

    # The attestation is immutable, via API (no endpoint) and via SQL.
    with Session(client.app.state.engine) as db:
        for sql in [
            "UPDATE fiscal_year_closings SET net_income = 1 WHERE id = :id",
            "UPDATE fiscal_year_closings SET status = 'in_progress' WHERE id = :id",
            "DELETE FROM fiscal_year_closings WHERE id = :id",
        ]:
            with pytest.raises(IntegrityError, match="completed fiscal year closing record"):
                db.execute(text(sql), {"id": record["id"]})
            db.rollback()


def test_verification_detects_tampered_history(client, year):
    from tests.test_reconciliation import triggers_disabled

    client.post(f"/api/v1/fiscal-years/{year['year']['id']}/close")
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text(
                "UPDATE journal_lines SET debit = 301, credit = 0 WHERE debit = 300 AND credit = 0"
            )
        )
        db.commit()
    verification = client.get(f"/api/v1/fiscal-years/{year['year']['id']}/closing/verify").json()
    assert verification["ok"] is False
    assert any("history of the year changed" in p for p in verification["problems"])
    report = client.get("/api/v1/reconciliation").json()
    closing = next(c for c in report["checks"] if c["name"] == "closing_consistency")
    assert (
        closing["findings"] and "history of the year changed" in closing["findings"][0]["message"]
    )


def test_interrupted_close_leaves_no_trace_and_reruns(client, year):
    with failing_checkpoint("fiscal_years.close_before_commit"):
        with Session(client.app.state.engine) as db:
            with pytest.raises(RuntimeError):
                close_year(db, db.get(FiscalYear, year["year"]["id"]))
    assert client.get(f"/api/v1/fiscal-years/{year['year']['id']}/closing").status_code == 404
    assert client.get("/api/v1/journal-entries?entry_type=closing").json() == []
    assert (
        client.get(f"/api/v1/fiscal-years/{year['year']['id']}/close-check").json()["can_close"]
        is True
    )
    assert client.get("/api/v1/reconciliation").json()["ok"] is True
    assert client.post(f"/api/v1/fiscal-years/{year['year']['id']}/close").status_code == 200
    assert len(client.get("/api/v1/journal-entries?entry_type=closing").json()) == 1


def test_leftover_in_progress_record_is_reported_and_blocks_nothing_else(client, year):
    with Session(client.app.state.engine) as db:
        _insert_in_progress_record(db, year["year"]["id"])
        db.commit()
    report = client.get("/api/v1/reconciliation").json()
    orphans = next(c for c in report["checks"] if c["name"] == "orphaned_records")
    assert "still in progress" in orphans["findings"][0]["message"]
    # The documented recovery: delete the leftover, re-run the close.
    with Session(client.app.state.engine) as db:
        db.execute(text("DELETE FROM fiscal_year_closings WHERE status = 'in_progress'"))
        db.commit()
    assert client.post(f"/api/v1/fiscal-years/{year['year']['id']}/close").status_code == 200


def test_close_refused_when_reconciliation_fails(client, year):
    from tests.test_reconciliation import triggers_disabled

    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE journal_lines SET debit = 299 WHERE debit = 300")
        )  # unbalanced history
        db.commit()
    resp = client.post(f"/api/v1/fiscal-years/{year['year']['id']}/close")
    assert resp.status_code == 409
    assert "does not balance" in resp.json()["detail"] or "reconciliation" in resp.json()["detail"]
    assert client.get(f"/api/v1/fiscal-years/{year['year']['id']}").json()["status"] == "open"
    assert client.get("/api/v1/journal-entries?entry_type=closing").json() == []


def test_verify_closing_service_without_record(client, year):
    with Session(client.app.state.engine) as db:
        result = verify_closing(db, db.get(FiscalYear, year["year"]["id"]))
    assert result.ok is False and "no closing record" in result.problems[0]
