"""Upgrading an existing installation: data created before revisions 0004/0005 survives."""

from decimal import Decimal

from alembic import command
from sqlalchemy import text

from app.db import make_engine
from tests.conftest import alembic_config, migrate

BEFORE = "a9ed0215b3c6"  # last revision of the previous milestone


def _seed_legacy_data(url):
    """A flat chart of accounts, posted history, a period and a template, as of revision 0003."""
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (code, name, type, is_active) VALUES "
                "('1000', 'Cash', 'asset', 1), ('4000', 'Sales', 'revenue', 1), "
                "('6000', 'Rent', 'expense', 0)"
            )
            if engine.dialect.name == "sqlite"
            else text(
                "INSERT INTO accounts (code, name, type, is_active) VALUES "
                "('1000', 'Cash', 'asset', true), ('4000', 'Sales', 'revenue', true), "
                "('6000', 'Rent', 'expense', false)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version) "
                "VALUES ('2025-11-10', 'Sale', 'draft', 'standard', 1), "
                "('2025-11-20', 'Adjustment', 'draft', 'adjusting', 2)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES "
                "(1, 1, 250.00, 0), (1, 2, 0, 250.00), (2, 1, 0, 10.00), (2, 2, 10.00, 0)"
            )
        )
        conn.execute(text("UPDATE journal_entries SET status = 'posted' WHERE id = 1"))  # pre-0006
        conn.execute(
            text(
                "INSERT INTO accounting_periods (name, start_date, end_date, status) VALUES "
                "('2025-11', '2025-11-01', '2025-11-30', 'open')"
            )
        )
    engine.dispose()


def _snapshot(url):
    engine = make_engine(url)
    with engine.connect() as conn:
        entries = conn.execute(
            text(
                "SELECT id, entry_date, status, entry_type, version FROM journal_entries "
                "ORDER BY id"
            )
        ).all()
        lines = conn.execute(
            text("SELECT entry_id, account_id, debit, credit FROM journal_lines ORDER BY id")
        ).all()
        accounts = conn.execute(
            text("SELECT code, name, type, is_active FROM accounts ORDER BY code")
        ).all()
    engine.dispose()
    return (
        [tuple(str(v) for v in row) for row in entries],
        [(e, a, Decimal(str(d)), Decimal(str(c))) for e, a, d, c in lines],
        [tuple(str(v) for v in row) for row in accounts],
    )


def test_upgrade_from_existing_data_preserves_history(db_url):
    migrate(db_url, BEFORE)
    _seed_legacy_data(db_url)
    before = _snapshot(db_url)

    migrate(db_url, "head")
    assert _snapshot(db_url) == before  # entries, lines and accounts untouched

    engine = make_engine(db_url)
    with engine.connect() as conn:
        accounts = conn.execute(
            text(
                "SELECT code, normal_balance, is_summary, parent_id, category FROM accounts "
                "ORDER BY code"
            )
        ).all()
        assert [(c, nb, bool(s), p, cat) for c, nb, s, p, cat in accounts] == [
            ("1000", "debit", False, None, None),
            ("4000", "credit", False, None, None),
            ("6000", "debit", False, None, None),
        ]
        metadata = conn.execute(
            text(
                "SELECT status, posted_at IS NOT NULL, posted_by, posting_source, "
                "length(correlation_id) >= 32 FROM journal_entries ORDER BY id"
            )
        ).all()
        assert [tuple(row) for row in metadata] == [
            ("posted", True, "legacy", "legacy", True),  # hex (SQLite) or uuid (PostgreSQL)
            ("draft", False, None, None, None),
        ]
        totals = conn.execute(
            text(
                "SELECT account_id, debit_cents, credit_cents FROM account_daily_totals ORDER BY 1"
            )
        ).all()
        assert [tuple(r) for r in totals] == [(1, 25000, 0), (2, 0, 25000)]
        sources = conn.execute(
            text(
                "SELECT source_type, source_id, operation_id, request_fingerprint "
                "FROM journal_entries"
            )
        ).all()
        assert all(row == (None, None, None, None) for row in sources)
        assert conn.execute(text("SELECT fiscal_year_id FROM accounting_periods")).scalar() is None
        # The legacy posted entry is still protected and still counted.
        import pytest
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE journal_lines SET debit = 1 WHERE entry_id = 1"))
    engine.dispose()

    # The application works on the migrated database: reports and reconciliation agree.
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as client:
        report = client.get("/api/v1/reports/trial-balance").json()
        rows = {r["code"]: r for r in report["rows"]}
        assert rows["1000"]["balance"] == "250.00" and rows["1000"]["normal_balance"] == "debit"
        assert rows["4000"]["balance"] == "-250.00" and report["is_balanced"] is True
        assert client.get("/api/v1/reconciliation").json()["ok"] is True
        # Legacy draft is still editable with its version, and postable.
        legacy_draft = client.get("/api/v1/journal-entries/2").json()
        assert legacy_draft["version"] == 2 and legacy_draft["entry_type"] == "adjusting"
        assert client.post("/api/v1/journal-entries/2/post").status_code == 200
        # New features work alongside legacy data.
        summary = client.post(
            "/api/v1/accounts",
            json={"code": "1", "name": "Assets", "type": "asset", "is_summary": True},
        ).json()
        # Account 1 has history: a plain move is refused, an effective-dated one works.
        assert (
            client.patch("/api/v1/accounts/1", json={"parent_id": summary["id"]}).status_code == 409
        )
        moved = client.post(
            "/api/v1/accounts/1/reclassify",
            json={
                "effective_from": "2026-01-01",
                "reason": "introduce hierarchy",
                "parent_id": summary["id"],
                "change_parent": True,
            },
        )
        assert moved.status_code == 200 and moved.json()["parent_id"] == summary["id"]
    app.state.engine.dispose()


def test_downgrade_after_upgrade_keeps_legacy_data(db_url):
    migrate(db_url, BEFORE)
    _seed_legacy_data(db_url)
    before = _snapshot(db_url)
    migrate(db_url, "head")
    command.downgrade(alembic_config(db_url), BEFORE)
    assert _snapshot(db_url) == before
    engine = make_engine(db_url)
    with engine.connect() as conn:
        columns = (
            {row[1] for row in conn.execute(text("PRAGMA table_info(accounts)"))}
            if engine.dialect.name == "sqlite"
            else {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'accounts'"
                    )
                )
            }
        )
        assert "parent_id" not in columns and "normal_balance" not in columns
    engine.dispose()
    migrate(db_url, "head")  # and forward again
    assert _snapshot(db_url) == before


PRE_0006 = "78f70521def3"


def _seed_two_years(url):
    """A pre-0006 installation: two fiscal years, one closed with a closing entry."""
    from fastapi.testclient import TestClient

    from app.config import Settings

    # The current application cannot run against the old schema (new columns), so the
    # history is seeded with SQL in the order the old application would have done it.
    engine = make_engine(url)
    sqlite = engine.dialect.name == "sqlite"
    t, f = ("1", "0") if sqlite else ("true", "false")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (code, name, type, is_active, normal_balance, is_summary) "
                f"VALUES ('1000', 'Cash', 'asset', {t}, 'debit', {f}), "
                f"('3900', 'RE', 'equity', {t}, 'credit', {f}), "
                f"('4000', 'Sales', 'revenue', {t}, 'credit', {f})"
            )
        )
        conn.execute(
            text(
                "INSERT INTO fiscal_years (name, start_date, end_date, status, "
                "retained_earnings_account_id, closed_at) VALUES "
                "('FY2025', '2025-01-01', '2025-12-31', 'open', 2, NULL), "
                "('FY2026', '2026-01-01', '2026-12-31', 'open', 2, NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO accounting_periods (name, start_date, end_date, status, "
                "fiscal_year_id) VALUES ('2025', '2025-01-01', '2025-12-31', 'open', 1), "
                "('2026-H1', '2026-01-01', '2026-06-30', 'open', 2)"
            )
        )
        # 1: 2025 sale (posted), 2: 2026 sale (posted), 3: 2026 draft, then the period
        # closes and 4: the 2025 closing entry is posted into it (pre-0006 rule).
        for day, kind in [
            ("2025-03-03", "standard"),
            ("2026-02-02", "standard"),
            ("2026-03-03", "adjusting"),
        ]:
            conn.execute(
                text(
                    "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                    f"version) VALUES ('{day}', '{kind}', 'draft', '{kind}', 1)"
                )
            )
        conn.execute(
            text(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES "
                "(1, 1, 500.00, 0), (1, 3, 0, 500.00), (2, 1, 80.00, 0), (2, 3, 0, 80.00), "
                "(3, 1, 1.00, 0), (3, 3, 0, 1.00)"
            )
        )
        conn.execute(text("UPDATE journal_entries SET status = 'posted' WHERE id IN (1, 2)"))
        conn.execute(
            text(
                "UPDATE accounting_periods SET status = 'closed', closed_at = "
                "CURRENT_TIMESTAMP WHERE id = 1"
            )
        )
        conn.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version) VALUES ('2025-12-31', 'closing', 'draft', 'closing', 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES "
                "(4, 3, 500.00, 0), (4, 2, 0, 500.00)"
            )
        )
        conn.execute(text("UPDATE journal_entries SET status = 'posted' WHERE id = 4"))
        conn.execute(
            text(
                "UPDATE fiscal_years SET status = 'closed', closing_entry_id = 4, "
                "closed_at = CURRENT_TIMESTAMP WHERE id = 1"
            )
        )
    engine.dispose()
    return TestClient, Settings


def test_upgrade_from_pre_0006_with_two_fiscal_years(db_url):
    migrate(db_url, PRE_0006)
    TestClient, Settings = _seed_two_years(db_url)
    before = _snapshot(db_url)
    migrate(db_url, "head")
    assert _snapshot(db_url) == before  # no journal row changed

    engine = make_engine(db_url)
    with engine.connect() as conn:
        meta = conn.execute(
            text(
                "SELECT id, status, posted_by, posting_source, posted_at IS NOT NULL, "
                "correlation_id IS NOT NULL FROM journal_entries ORDER BY id"
            )
        ).all()
        assert [tuple(r) for r in meta] == [
            (1, "posted", "legacy", "legacy", True, True),
            (2, "posted", "legacy", "legacy", True, True),
            (3, "draft", None, None, False, False),
            (4, "posted", "legacy", "legacy", True, True),
        ]
        closings = conn.execute(
            text(
                "SELECT fiscal_year_id, closing_entry_id, status, ledger_hash, actor_ref "
                "FROM fiscal_year_closings"
            )
        ).all()
        assert [tuple(r) for r in closings] == [(1, 4, "completed", "legacy-backfill", "migration")]
        totals = conn.execute(
            text(
                "SELECT account_id, entry_date, debit_cents, credit_cents "
                "FROM account_daily_totals ORDER BY entry_date, account_id"
            )
        ).all()
        assert [(a, str(d), dc, cc) for a, d, dc, cc in totals] == [
            (1, "2025-03-03", 50000, 0),
            (3, "2025-03-03", 0, 50000),
            (2, "2025-12-31", 0, 50000),
            (3, "2025-12-31", 50000, 0),
            (1, "2026-02-02", 8000, 0),
            (3, "2026-02-02", 0, 8000),
        ]
        # The backfilled closing record is immutable like any completed record.
        import pytest
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            conn.execute(text("DELETE FROM fiscal_year_closings"))
    engine.dispose()

    from app.main import create_app

    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as client:
        recon = client.get("/api/v1/reconciliation").json()
        assert recon["ok"], [c for c in recon["checks"] if c["findings"]]
        verify = client.get("/api/v1/fiscal-years/1/closing/verify").json()
        assert verify["ok"] is True and verify["recorded_hash"] == "legacy-backfill"
        tb = {
            r["code"]: r["balance"]
            for r in client.get("/api/v1/reports/trial-balance").json()["rows"]
        }
        assert tb == {"1000": "580.00", "3900": "-500.00", "4000": "-80.00"}
        assert client.post("/api/v1/balances/verify").json()["ok"] is True
        # The legacy draft can still be posted, now with full metadata.
        resp = client.post("/api/v1/journal-entries/3/post", headers={"X-Actor-Ref": "alice"})
        assert resp.status_code == 200, resp.text
        posted = resp.json()
        assert posted["posted_by"] == "alice" and posted["posting_source"] == "api"
        # The open year can still be closed the new way.
        period = [p for p in client.get("/api/v1/periods").json() if p["name"] == "2026-H1"][0]
        client.post(
            "/api/v1/periods",
            json={
                "name": "2026-H2",
                "start_date": "2026-07-01",
                "end_date": "2026-12-31",
                "fiscal_year_id": 2,
            },
        )
        for p in client.get("/api/v1/periods").json():
            if p["status"] == "open":
                assert client.post(f"/api/v1/periods/{p['id']}/close").status_code == 200, p
        assert client.post("/api/v1/fiscal-years/2/close").status_code == 200
        assert client.get("/api/v1/fiscal-years/2/closing").json()["status"] == "completed"
        del period
    app.state.engine.dispose()


def test_downgrade_0006_keeps_journals_but_drops_traceability(db_url):
    migrate(db_url, PRE_0006)
    _seed_two_years(db_url)
    before = _snapshot(db_url)
    migrate(db_url, "head")
    command.downgrade(alembic_config(db_url), PRE_0006)
    assert _snapshot(db_url) == before
    engine = make_engine(db_url)
    with engine.connect() as conn:
        from sqlalchemy import inspect

        tables = set(inspect(conn).get_table_names())
        assert (
            not {"fiscal_year_closings", "account_daily_totals", "account_reclassifications"}
            & tables
        )
        columns = {c["name"] for c in inspect(conn).get_columns("journal_entries")}
        assert "posted_at" not in columns  # traceability data is gone: documented as irreversible
        assert (
            conn.execute(text("SELECT closing_entry_id FROM fiscal_years WHERE id = 1")).scalar()
            == 4
        )
    engine.dispose()
    migrate(db_url, "head")  # re-upgrading backfills again from the journals
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM fiscal_year_closings")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM account_daily_totals")).scalar() == 6
    engine.dispose()
