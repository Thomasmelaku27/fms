"""Short-term liquidity projection and planned cash flows.

The projection is read-only: posted balance + future-dated posted entries +
planned flows, itemised per day, with threshold alerts. Drafts, bank
transactions and realized/cancelled plans are never counted.
"""

import pytest

from tests.cash_helpers import (
    account,
    cash_account,
    cash_in,
    cash_out,
    entry,
    import_statement,
    ledger_snapshot,
    statement_payload,
)

AS_OF = "2026-03-01"


@pytest.fixture
def world(client):
    bank = account(client, "1000", "Bank", "asset")
    savings = account(client, "1010", "Savings", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    rent = account(client, "5000", "Rent", "expense")
    w = {
        "bank": bank["id"],
        "savings": savings["id"],
        "sales": sales["id"],
        "rent": rent["id"],
        "cash_bank": cash_account(client, bank["id"], "Bank", minimum_balance="500.00")["id"],
        "cash_savings": cash_account(client, savings["id"], "Savings")["id"],
    }
    cash_in(client, "2026-02-10", w["bank"], w["sales"], "1000.00", "Sale")
    cash_in(client, "2026-02-20", w["savings"], w["sales"], "200.00", "Sale to savings")
    return w


def plan(client, cash_account_id, expected_date, amount, description="plan", expect=201, **extra):
    resp = client.post(
        "/api/v1/cash/planned-flows",
        json={"cash_account_id": cash_account_id, "expected_date": expected_date, "amount": str(amount), "description": description, **extra},
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


def projection(client, **params):
    resp = client.get("/api/v1/cash/projection", params={"as_of": AS_OF, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_projection_shape_and_opening_balance(client, world):
    p = projection(client)
    assert (p["horizon_start"], p["horizon_end"], p["horizon_days"]) == ("2026-03-02", "2026-03-31", 30)
    assert len(p["days"]) == 30
    assert p["opening_balance"] == "1200.00"
    assert p["closing_projected_balance"] == "1200.00"
    assert p["threshold"] == "500.00" and p["threshold_source"] == "sum_of_cash_accounts"
    assert p["alerts"] == []
    assert {k: p["counted"][k] for k in ("posted_journal_entries", "planned_cash_flows", "overdue_planned_cash_flows")} == {
        "posted_journal_entries": 0, "planned_cash_flows": 0, "overdue_planned_cash_flows": 0}
    assert len(p["history"]) == 30 and p["history"][-1]["closing_balance"] == "1200.00"
    assert p["history"][0]["date"] == "2026-01-31"
    assert "posted" in p["basis"] and "forecast" in p["basis"]

    single = projection(client, cash_account_id=world["cash_bank"])
    assert single["opening_balance"] == "1000.00" and single["threshold_source"] == "cash_account"
    no_limit = projection(client, cash_account_id=world["cash_savings"])
    assert no_limit["threshold"] is None and any("no threshold" in w for w in no_limit["warnings"])


def test_planned_flows_and_alerts_are_explainable(client, world):
    w = world
    rent = plan(client, w["cash_bank"], "2026-03-05", "-800.00", "Rent March", activity="operating")
    plan(client, w["cash_bank"], "2026-03-20", "300.00", "Customer payment expected")
    p = projection(client, cash_account_id=w["cash_bank"])

    days = {d["date"]: d for d in p["days"]}
    assert days["2026-03-05"]["planned_outflows"] == "800.00" and days["2026-03-05"]["projected_balance"] == "200.00"
    assert days["2026-03-05"]["items"] == [
        {"kind": "planned_cash_flow", "id": rent["id"], "cash_account_id": w["cash_bank"], "description": "Rent March", "amount": "-800.00", "overdue": False}
    ]
    assert days["2026-03-05"]["below_threshold"] is True
    assert days["2026-03-20"]["planned_inflows"] == "300.00" and days["2026-03-20"]["projected_balance"] == "500.00"
    assert days["2026-03-20"]["below_threshold"] is False  # exactly at the threshold is fine
    assert p["lowest_projected_balance"] == "200.00" and p["lowest_projected_date"] == "2026-03-05"
    assert p["closing_projected_balance"] == "500.00"
    assert len(p["alerts"]) == 15  # 03-05 .. 03-19
    first = p["alerts"][0]
    assert {k: first[k] for k in ("date", "projected_balance", "threshold", "shortfall", "first_breach")} == {
        "date": "2026-03-05", "projected_balance": "200.00", "threshold": "500.00", "shortfall": "300.00", "first_breach": True}
    assert "Rent March" in first["reason"] and p["alerts"][1]["first_breach"] is False
    assert p["counted"]["planned_cash_flows"] == 2

    # Threshold override from the query string.
    strict = projection(client, cash_account_id=w["cash_bank"], threshold="600.00")
    assert strict["threshold_source"] == "query" and len(strict["alerts"]) == 27  # 03-05 .. 03-31
    relaxed = projection(client, cash_account_id=w["cash_bank"], threshold="0")
    assert relaxed["alerts"] == []


def test_future_dated_posted_entries_count_but_drafts_do_not(client, world):
    w = world
    posted = cash_out(client, "2026-03-10", w["bank"], w["rent"], "100.00", "Post-dated rent")
    entry(client, "2026-03-11", [{"account_id": w["bank"], "debit": "5000.00"}, {"account_id": w["sales"], "credit": "5000.00"}], "Draft", post=False)
    p = projection(client, cash_account_id=w["cash_bank"], history_days=0)

    days = {d["date"]: d for d in p["days"]}
    assert days["2026-03-10"]["posted_net"] == "-100.00"
    assert days["2026-03-10"]["items"] == [
        {"kind": "posted_journal_entry", "id": posted["id"], "cash_account_id": w["cash_bank"], "description": "Post-dated rent", "amount": "-100.00", "overdue": False}
    ]
    assert days["2026-03-11"]["net"] == "0.00" and days["2026-03-11"]["items"] == []
    assert p["closing_projected_balance"] == "900.00"
    assert p["counted"]["posted_journal_entries"] == 1
    assert p["excluded"]["draft_journal_entries_in_horizon"] == 1
    assert any("draft" in w_ for w_ in p["warnings"])
    assert p["history"] == []


def test_internal_transfers_do_not_move_consolidated_projection(client, world):
    w = world
    entry(client, "2026-03-03", [{"account_id": w["savings"], "debit": "400.00"}, {"account_id": w["bank"], "credit": "400.00"}], "Sweep")
    consolidated = projection(client)
    day = next(d for d in consolidated["days"] if d["date"] == "2026-03-03")
    assert day["net"] == "0.00" and day["items"] == []
    assert consolidated["closing_projected_balance"] == "1200.00"

    bank_only = projection(client, cash_account_id=w["cash_bank"])
    day = next(d for d in bank_only["days"] if d["date"] == "2026-03-03")
    assert day["posted_net"] == "-400.00" and bank_only["closing_projected_balance"] == "600.00"


def test_overdue_planned_flows_are_carried_and_flagged(client, world):
    w = world
    plan(client, w["cash_bank"], "2026-02-25", "-50.00", "Late invoice")
    p = projection(client, cash_account_id=w["cash_bank"])
    first = p["days"][0]
    assert first["date"] == "2026-03-02" and first["planned_outflows"] == "50.00"
    assert first["items"][0]["overdue"] is True
    assert p["counted"]["overdue_planned_cash_flows"] == 1


def test_realized_and_cancelled_plans_are_excluded(client, world):
    w = world
    a = plan(client, w["cash_bank"], "2026-03-05", "-800.00", "Rent March")
    b = plan(client, w["cash_bank"], "2026-03-06", "-10.00", "Subscription")
    posted = cash_out(client, "2026-03-05", w["bank"], w["rent"], "800.00", "Rent March actual")

    assert client.patch(f"/api/v1/cash/planned-flows/{a['id']}/status", json={"status": "realized", "journal_entry_id": posted["id"]}).json()["status"] == "realized"
    assert client.patch(f"/api/v1/cash/planned-flows/{b['id']}/status", json={"status": "cancelled"}).json()["status"] == "cancelled"

    p = projection(client, cash_account_id=w["cash_bank"])
    day = next(d for d in p["days"] if d["date"] == "2026-03-05")
    # Only the posted entry counts now; the plan is not double counted.
    assert day["posted_net"] == "-800.00" and day["planned_outflows"] == "0.00"
    assert p["excluded"]["realized_or_cancelled_planned_cash_flows"] == 2
    assert [f["status"] for f in client.get("/api/v1/cash/planned-flows", params={"status": "planned"}).json()] == []

    draft = entry(client, "2026-03-06", [{"account_id": w["bank"], "credit": "10.00"}, {"account_id": w["rent"], "debit": "10.00"}], "d", post=False)
    resp = client.patch(f"/api/v1/cash/planned-flows/{b['id']}/status", json={"status": "realized", "journal_entry_id": draft["id"]})
    assert resp.status_code == 422 and "posted" in resp.json()["detail"]


def test_planned_flow_validation_and_idempotency(client, world):
    w = world
    plan(client, w["cash_bank"], "2026-03-05", "0", expect=422)
    plan(client, 999, "2026-03-05", "1.00", expect=404)
    first = plan(client, w["cash_bank"], "2026-03-05", "-120.00", "Bill 42", source="payables", external_reference="BILL-42")
    again = plan(client, w["cash_bank"], "2026-03-05", "-120.00", "Bill 42", source="payables", external_reference="BILL-42", expect=200)
    assert again["id"] == first["id"]
    plan(client, w["cash_bank"], "2026-03-06", "-120.00", "Bill 42", source="payables", external_reference="BILL-42", expect=409)
    assert len(client.get("/api/v1/cash/planned-flows").json()) == 1
    assert client.get(f"/api/v1/cash/planned-flows/{first['id']}").json()["external_reference"] == "BILL-42"
    assert client.delete(f"/api/v1/cash/planned-flows/{first['id']}").status_code == 204
    assert client.get(f"/api/v1/cash/planned-flows/{first['id']}").status_code == 404


def test_projection_changes_nothing(client, world):
    w = world
    plan(client, w["cash_bank"], "2026-03-05", "-800.00", "Rent March")
    import_statement(
        client,
        statement_payload(w["cash_bank"], "FEB", "2026-02-01", "2026-02-28", "0", [{"date": "2026-02-10", "amount": "1000.00", "description": "Sale"}]),
    )
    before = ledger_snapshot(client)
    balances_before = client.get("/api/v1/cash/reports/balances").json()

    p = projection(client)
    assert p["excluded"]["unmatched_bank_transactions"] == 1
    assert any("not (fully) reconciled" in w_ for w_ in p["warnings"])
    projection(client, cash_account_id=w["cash_bank"], threshold="99999")

    assert ledger_snapshot(client) == before
    assert client.get("/api/v1/cash/reports/balances").json() == balances_before
    assert client.get("/api/v1/cash/planned-flows").json()[0]["status"] == "planned"


def test_cash_account_threshold_can_be_changed_and_cleared(client, world):
    w = world
    resp = client.patch(f"/api/v1/cash-accounts/{w['cash_bank']}", json={"minimum_balance": "750.50", "label": "Main"})
    assert resp.status_code == 200 and resp.json()["minimum_balance"] == "750.50" and resp.json()["label"] == "Main"
    assert projection(client, cash_account_id=w["cash_bank"])["threshold"] == "750.50"
    assert client.patch(f"/api/v1/cash-accounts/{w['cash_bank']}", json={"clear_minimum_balance": True}).json()["minimum_balance"] is None
    assert client.get(f"/api/v1/cash-accounts/{w['cash_bank']}").json()["minimum_balance"] is None
    assert client.patch("/api/v1/cash-accounts/999", json={"label": "x"}).status_code == 404
    assert client.get("/api/v1/cash/projection", params={"as_of": AS_OF, "horizon_days": 0}).status_code == 422
