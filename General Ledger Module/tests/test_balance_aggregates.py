"""Trigger-maintained daily totals: maintenance, verification, repair, drift detection."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AccountDailyTotal
from app.services.balances import compare, rebuild_daily_totals, verify_daily_totals
from tests.helpers import account, draft, post


@pytest.fixture
def chart(client):
    return account(client, "1000", "Cash", "asset"), account(client, "4000", "Sales", "revenue")


def _totals(client):
    with Session(client.app.state.engine) as db:
        return {
            (r.account_id, r.entry_date.isoformat()): (r.debit_cents, r.credit_cents)
            for r in db.scalars(select(AccountDailyTotal))
        }


def test_totals_follow_every_posting_path(client, chart):
    cash, sales = chart
    lines = [
        {"account_id": cash["id"], "debit": "10.50"},
        {"account_id": sales["id"], "credit": "10.50"},
    ]
    d = draft(client, lines, entry_date="2026-01-10")
    assert _totals(client) == {}  # drafts do not count
    post(client, d["id"])
    assert _totals(client) == {
        (cash["id"], "2026-01-10"): (1050, 0),
        (sales["id"], "2026-01-10"): (0, 1050),
    }
    post(client, draft(client, lines, entry_date="2026-01-10")["id"])  # same day accumulates
    assert _totals(client)[(cash["id"], "2026-01-10")] == (2100, 0)
    client.post(f"/api/v1/journal-entries/{d['id']}/reverse", json={"reversal_date": "2026-01-11"})
    assert _totals(client)[(cash["id"], "2026-01-11")] == (0, 1050)
    client.post(
        "/api/v1/journal-processing/entries",
        json={
            "source_type": "s",
            "source_id": "1",
            "operation_id": "p",
            "entry_date": "2026-01-12",
            "description": "x",
            "lines": lines,
        },
    )
    assert _totals(client)[(sales["id"], "2026-01-12")] == (0, 1050)
    assert client.post("/api/v1/balances/verify").json()["ok"] is True


def test_verify_detects_drift_and_rebuild_repairs_it(client, chart):
    cash, sales = chart
    post(
        client,
        draft(
            client,
            [
                {"account_id": cash["id"], "debit": "10.00"},
                {"account_id": sales["id"], "credit": "10.00"},
            ],
            entry_date="2026-01-10",
        )["id"],
    )
    with Session(client.app.state.engine) as db:  # simulate aggregate corruption / drift
        db.execute(
            text("UPDATE account_daily_totals SET debit_cents = 999 WHERE account_id = :a"),
            {"a": cash["id"]},
        )
        db.execute(
            text(
                "INSERT INTO account_daily_totals (account_id, entry_date, debit_cents, "
                "credit_cents) VALUES (:a, '2026-05-05', 1, 0)"
            ),
            {"a": sales["id"]},
        )
        db.commit()
    verify = client.post("/api/v1/balances/verify", headers={"X-Actor-Ref": "auditor"}).json()
    assert verify["ok"] is False and len(verify["discrepancies"]) == 2
    drift = next(d for d in verify["discrepancies"] if d["account_id"] == cash["id"])
    assert (drift["ledger_debit_cents"], drift["aggregate_debit_cents"]) == (1000, 999)
    # Reports from the aggregate are now wrong, reports from the lines are right; the
    # reconciliation report says so.
    agg = {
        r["code"]: r["closing_balance"]
        for r in client.get("/api/v1/reports/account-balances?source=aggregate").json()["rows"]
    }
    lines = {
        r["code"]: r["closing_balance"]
        for r in client.get("/api/v1/reports/account-balances?source=lines").json()["rows"]
    }
    assert agg["1000"] == "9.99" and lines["1000"] == "10.00"
    recon = client.get("/api/v1/reconciliation").json()
    assert next(c for c in recon["checks"] if c["name"] == "aggregate_balances")["findings"]
    assert next(c for c in recon["checks"] if c["name"] == "orphaned_records")["findings"]

    rebuild = client.post("/api/v1/balances/rebuild", headers={"X-Actor-Ref": "auditor"}).json()
    assert (
        rebuild["kind"] == "rebuild"
        and rebuild["ok"] is True
        and len(rebuild["discrepancies"]) == 2
    )
    assert client.post("/api/v1/balances/verify").json()["ok"] is True
    assert (
        client.get("/api/v1/reports/account-balances?source=aggregate").json()["rows"][0][
            "closing_balance"
        ]
        == "10.00"
    )
    runs = client.get("/api/v1/balances/verification-runs").json()
    assert [(r["kind"], r["ok"], r["discrepancy_count"], r["actor_ref"]) for r in runs] == [
        ("verify", True, 0, "anonymous"),
        ("rebuild", True, 2, "auditor"),
        ("verify", False, 2, "auditor"),
    ]
    # Journals were never touched.
    assert client.get("/api/v1/reconciliation").json()["ok"] is True


def test_rebuild_failure_rolls_back(client, chart):
    from tests.helpers import failing_checkpoint

    cash, sales = chart
    post(
        client,
        draft(
            client,
            [
                {"account_id": cash["id"], "debit": "10.00"},
                {"account_id": sales["id"], "credit": "10.00"},
            ],
        )["id"],
    )
    with Session(client.app.state.engine) as db:
        db.execute(
            text("UPDATE account_daily_totals SET debit_cents = 5 WHERE account_id = :a"),
            {"a": cash["id"]},
        )
        db.commit()
    with failing_checkpoint("balances.rebuild_before_commit"):
        with Session(client.app.state.engine) as db:
            with pytest.raises(RuntimeError):
                rebuild_daily_totals(db)
    with Session(client.app.state.engine) as db:
        assert not compare(db).ok  # still drifted: the failed rebuild changed nothing
        assert db.execute(text("SELECT COUNT(*) FROM balance_verification_runs")).scalar_one() == 0
        result = rebuild_daily_totals(db, actor_ref="ops")
        assert result.ok and compare(db).ok
        verify_daily_totals(db)
        assert db.execute(text("SELECT COUNT(*) FROM balance_verification_runs")).scalar_one() == 2
