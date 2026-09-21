"""The ``fmsreceivables01`` migration on a database that already holds AR data."""

from alembic import command
from sqlalchemy import inspect, text

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import alembic_config, migrate

NEW_TABLES = {"receivable_settings", "sales_invoice_lines", "customer_payments", "customer_adjustments", "invoice_allocations"}


def test_upgrade_keeps_existing_invoices_and_they_become_usable(db_url):
    migrate(db_url, "fmsmodules01")
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO customers (code, name, is_active) VALUES ('OLD', 'Legacy customer', true)"))
        conn.execute(text(
            "INSERT INTO sales_invoices (customer_id, invoice_number, invoice_date, due_date, total, status) "
            "VALUES (1, 'LEGACY-1', '2025-12-01', '2025-12-31', 95.25, 'draft')"))
    engine.dispose()

    migrate(db_url, "head")

    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert NEW_TABLES <= set(inspect(conn).get_table_names())
        row = conn.execute(text("SELECT subtotal, tax_total, total, amount_settled, status, journal_entry_id FROM sales_invoices")).one()
        assert [str(v) for v in row[:4]] == ["95.25", "0", "95.25", "0"] or [float(v) for v in row[:4]] == [95.25, 0, 95.25, 0]
        assert row.status == "draft" and row.journal_entry_id is None
    engine.dispose()

    # The legacy draft has no lines yet; once lines are added through the API it can be issued.
    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as client:
        sales = client.post("/api/v1/accounts", json={"code": "4000", "name": "Sales", "type": "revenue"}).json()["id"]
        ar = client.post("/api/v1/accounts", json={"code": "1200", "name": "AR", "type": "asset"}).json()["id"]
        tax = client.post("/api/v1/accounts", json={"code": "2200", "name": "Tax", "type": "liability"}).json()["id"]
        exp = client.post("/api/v1/accounts", json={"code": "6100", "name": "Bad debt", "type": "expense"}).json()["id"]
        assert client.put("/api/v1/receivables/settings", json={
            "receivable_account_id": ar, "tax_account_id": tax, "credit_account_id": sales, "write_off_account_id": exp}).status_code == 200
        legacy = client.get("/api/v1/receivables/invoices").json()[0]
        assert legacy["lines"] == [] and legacy["balance_due"] == "95.25"
        assert client.post(f"/api/v1/receivables/invoices/{legacy['id']}/issue").status_code == 422  # needs lines
        r = client.put(f"/api/v1/receivables/invoices/{legacy['id']}", json={
            "invoice_number": "LEGACY-1", "invoice_date": "2025-12-01", "due_date": "2025-12-31",
            "lines": [{"description": "Migrated", "quantity": "1", "unit_price": "95.25", "revenue_account_id": sales}]})
        assert r.status_code == 200, r.text
        assert client.post(f"/api/v1/receivables/invoices/{legacy['id']}/issue").json()["status"] == "issued"
    app.state.engine.dispose()


def test_downgrade_one_step_restores_baseline_schema(db_url):
    migrate(db_url)
    command.downgrade(alembic_config(db_url), "fmsmodules01")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert not NEW_TABLES & set(inspector.get_table_names())
        columns = {c["name"] for c in inspector.get_columns("sales_invoices")}
        assert columns == {"id", "customer_id", "invoice_number", "invoice_date", "due_date", "total", "status"}
        if engine.dialect.name == "postgresql":
            leftover = conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'fms\\_%' AND proname NOT LIKE 'fms\\_journal%'")).scalars().all()
            assert leftover == []
        else:
            triggers = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_sales%'")).scalars().all()
            assert triggers == []
    engine.dispose()
    migrate(db_url)  # and forward again
