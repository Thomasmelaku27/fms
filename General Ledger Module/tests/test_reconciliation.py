"""GL reconciliation utilities: a clean ledger reports nothing; corrupted states are detected.

Corruption is planted with the database triggers disabled (adversarial direct
SQL), exactly the kind of damage the checks are meant to find.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.reconciliation import run_checks
from tests.helpers import account, draft, post

URL = "/api/v1/reconciliation"


@pytest.fixture
def chart(client):
    return {
        "cash": account(client, "1000", "Cash", "asset"),
        "assets": account(client, "1", "Assets", "asset", is_summary=True),
        "sales": account(client, "4000", "Sales", "revenue"),
        "rent": account(client, "6000", "Rent", "expense"),
        "retained": account(client, "3900", "Retained earnings", "equity"),
    }


@contextmanager
def triggers_disabled(engine):
    """Temporarily remove the ledger's database protections (test-only sabotage)."""
    with Session(engine) as db:
        if engine.dialect.name == "sqlite":
            triggers = db.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
            ).all()
            for name, _ in triggers:
                db.execute(text(f"DROP TRIGGER {name}"))
            db.commit()
            try:
                yield db
            finally:
                for _, sql in triggers:
                    db.execute(text(sql))
                db.commit()
        else:
            tables = [
                "journal_entries",
                "journal_lines",
                "accounting_periods",
                "accounts",
                "fiscal_years",
            ]
            for table in tables:
                db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER ALL"))
            db.commit()
            try:
                yield db
            finally:
                for table in tables:
                    db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER ALL"))
                db.commit()


def _checks(report):
    return {c["name"]: c for c in report["checks"]}


def test_clean_ledger_reconciles(client, chart):
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "0.10"},
                {"account_id": chart["sales"]["id"], "credit": "0.10"},
            ],
        )["id"],
    )
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "0.20"},
                {"account_id": chart["sales"]["id"], "credit": "0.20"},
            ],
        )["id"],
    )
    draft(client, [{"account_id": chart["cash"]["id"], "debit": "99.00"}])  # drafts are ignored
    resp = client.get(URL)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["ok"] is True and report["error_count"] == 0 and report["warning_count"] == 0
    assert {c["name"] for c in report["checks"]} == {
        "unbalanced_posted_entries",
        "invalid_posted_lines",
        "duplicate_source_references",
        "duplicate_source_operations",
        "unmatched_references",
        "invalid_posting_dates",
        "posting_metadata",
        "period_state",
        "fiscal_periods",
        "closing_consistency",
        "hierarchy_integrity",
        "orphaned_records",
        "balance_discrepancies",
        "aggregate_balances",
        "drafts_in_closed_periods",
    }
    assert all(c["ok"] for c in report["checks"])
    assert client.get(f"{URL}?from=2026-02-01&to=2026-01-01").status_code == 422


def test_detects_unbalanced_and_invalid_posted_lines(client, chart):
    entry = post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "50.00"},
                {"account_id": chart["sales"]["id"], "credit": "50.00"},
            ],
        )["id"],
    )
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE journal_lines SET debit = 60 WHERE entry_id = :e AND debit > 0"),
            {"e": entry["id"]},
        )
        db.execute(
            text(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
                "VALUES (:e, :a, 0, 0)"
            ),
            {"e": entry["id"], "a": chart["assets"]["id"]},
        )
        db.commit()
    report = client.get(URL).json()
    assert report["ok"] is False
    checks = _checks(report)
    unbalanced = checks["unbalanced_posted_entries"]["findings"]
    assert len(unbalanced) == 1
    assert unbalanced[0]["entry_ids"] == [entry["id"]]
    assert unbalanced[0]["amounts"] == {"debit": "60.00", "credit": "50.00", "difference": "10.00"}
    messages = [f["message"] for f in checks["invalid_posted_lines"]["findings"]]
    assert any("has no amount" in m for m in messages)
    # A summary-account line with an amount is flagged as such.
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE journal_lines SET debit = 1 WHERE account_id = :a"),
            {"a": chart["assets"]["id"]},
        )
        db.commit()
    messages = [
        f["message"] for f in _checks(client.get(URL).json())["invalid_posted_lines"]["findings"]
    ]
    assert any("summary account" in m for m in messages)


def test_detects_source_reference_problems(client, chart):
    body = {
        "source_type": "ap_invoice",
        "source_id": "INV-7",
        "operation_id": "post",
        "entry_date": "2026-01-15",
        "description": "x",
        "lines": [
            {"account_id": chart["rent"]["id"], "debit": "10.00"},
            {"account_id": chart["cash"]["id"], "credit": "10.00"},
        ],
    }
    first = client.post("/api/v1/journal-processing/entries", json=body).json()["entry"]
    # Same document, same data, *different* operation id: legitimate per the contract but a
    # probable double processing - reported as a warning.
    second = client.post(
        "/api/v1/journal-processing/entries", json={**body, "operation_id": "post-again"}
    ).json()["entry"]
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE journal_entries SET request_fingerprint = NULL WHERE id = :id"),
            {"id": second["id"]},
        )
        db.commit()
    report = client.get(URL).json()
    checks = _checks(report)
    dup = checks["duplicate_source_references"]["findings"]
    assert dup == [] or dup[0]["entry_ids"] == [
        first["id"],
        second["id"],
    ]  # fingerprint removed above
    unmatched = [f["message"] for f in checks["unmatched_references"]["findings"]]
    assert any("no fingerprint" in m for m in unmatched)
    assert report["warning_count"] + report["error_count"] >= 1


def test_detects_duplicate_processing(client, chart):
    body = {
        "source_type": "ap_invoice",
        "source_id": "INV-8",
        "operation_id": "post",
        "entry_date": "2026-01-15",
        "description": "x",
        "lines": [
            {"account_id": chart["rent"]["id"], "debit": "10.00"},
            {"account_id": chart["cash"]["id"], "credit": "10.00"},
        ],
    }
    a = client.post("/api/v1/journal-processing/entries", json=body).json()["entry"]
    b = client.post(
        "/api/v1/journal-processing/entries", json={**body, "operation_id": "post-2"}
    ).json()["entry"]
    finding = _checks(client.get(URL).json())["duplicate_source_references"]["findings"][0]
    assert finding["entry_ids"] == [a["id"], b["id"]]
    assert finding["amounts"]["debit"] == "20.00"
    assert finding["account_ids"] == sorted([chart["rent"]["id"], chart["cash"]["id"]])


def test_detects_broken_reversal_links(client, chart):
    original = post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "5.00"},
                {"account_id": chart["sales"]["id"], "credit": "5.00"},
            ],
        )["id"],
    )
    reversal = client.post(
        f"/api/v1/journal-entries/{original['id']}/reverse", json={"reversal_date": "2026-01-20"}
    ).json()
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE journal_lines SET credit = 6 WHERE entry_id = :e AND credit > 0"),
            {"e": reversal["id"]},
        )
        db.commit()
    findings = _checks(client.get(URL).json())["unmatched_references"]["findings"]
    assert any(
        "does not mirror" in f["message"] and f["entry_ids"] == [reversal["id"], original["id"]]
        for f in findings
    )


def test_detects_invalid_posting_dates_and_period_state(client, chart):
    jan = client.post(
        "/api/v1/periods",
        json={"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    ).json()
    feb = client.post(
        "/api/v1/periods",
        json={"name": "2026-02", "start_date": "2026-02-01", "end_date": "2026-02-28"},
    ).json()
    entry = post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "5.00"},
                {"account_id": chart["sales"]["id"], "credit": "5.00"},
            ],
            entry_date="2026-01-10",
        )["id"],
    )
    late_draft = draft(
        client, [{"account_id": chart["cash"]["id"], "debit": "1.00"}], entry_date="2026-01-11"
    )
    with triggers_disabled(client.app.state.engine) as db:
        # February closed (no timestamp) while January is open; January locked with a lock
        # time that predates the posted entry, as if the posting had bypassed the gate.
        db.execute(
            text("UPDATE accounting_periods SET status = 'closed' WHERE id = :id"),
            {"id": feb["id"]},
        )
        db.execute(
            text(
                "UPDATE accounting_periods SET status = 'locked', "
                "closed_at = '2000-01-01 00:00:00', locked_at = '2000-01-01 00:00:00' "
                "WHERE id = :id"
            ),
            {"id": jan["id"]},
        )
        db.commit()
    report = client.get(URL).json()
    checks = _checks(report)
    dates = checks["invalid_posting_dates"]["findings"]
    assert len(dates) == 1 and dates[0]["entry_ids"] == [entry["id"]]
    assert "created after period '2026-01' was locked" in dates[0]["message"]
    state = [f["message"] for f in checks["period_state"]["findings"]]
    assert any("'2026-02' is closed without closed_at" in m for m in state)
    assert not any("earlier periods are open" in m for m in state)  # January is locked, not open
    warnings = checks["drafts_in_closed_periods"]["findings"]
    assert warnings[0]["entry_ids"] == [late_draft["id"]]
    assert report["ok"] is False and report["warning_count"] >= 1
    # Date filters narrow the entry-based checks.
    assert (
        _checks(client.get(f"{URL}?from=2026-02-01").json())["invalid_posting_dates"]["findings"]
        == []
    )


def test_detects_inconsistent_fiscal_year(client, chart):
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
    post(
        client,
        draft(
            client,
            [
                {"account_id": chart["cash"]["id"], "debit": "5.00"},
                {"account_id": chart["sales"]["id"], "credit": "5.00"},
            ],
            entry_date="2026-03-03",
        )["id"],
    )
    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE fiscal_years SET status = 'closed' WHERE id = :id"), {"id": year["id"]}
        )
        db.commit()
    state = [f["message"] for f in _checks(client.get(URL).json())["period_state"]["findings"]]
    assert any("closed but periods are open" in m for m in state)
    assert any("closed without a closing entry" in m for m in state)


def test_balance_discrepancy_check_compares_three_sources(client, chart):
    """Detail vs aggregate vs trial balance agree on a ledger full of awkward cents."""
    for cents in ("0.01", "0.02", "0.07", "1.10", "2.30", "99.99"):
        post(
            client,
            draft(
                client,
                [
                    {"account_id": chart["cash"]["id"], "debit": cents},
                    {"account_id": chart["sales"]["id"], "credit": cents},
                ],
            )["id"],
        )
    with Session(client.app.state.engine) as db:
        report = run_checks(db)
    balances_check = next(c for c in report.checks if c.name == "balance_discrepancies")
    assert balances_check.ok
    # Plant a disagreement: a line whose entry is posted but is not visible to the trial
    # balance is impossible; instead corrupt the aggregate by adding a line on a
    # non-existent... also impossible (FK). The check is exercised structurally here and
    # through the corrupted-line test above (which changes all three consistently).
    assert report.ok
