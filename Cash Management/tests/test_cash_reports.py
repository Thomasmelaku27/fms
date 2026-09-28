"""Cash reports: dated balances, daily balances and cash flow by activity.

All figures come from posted entries only; debits to a cash ledger account are
inflows, credits are outflows; internal transfers are excluded from the
consolidated view.
"""

from decimal import Decimal

import pytest

from tests.cash_helpers import account, cash_account, cash_in, cash_out, entry


@pytest.fixture
def world(client):
    bank = account(client, "1000", "Bank", "asset")
    savings = account(client, "1010", "Savings", "asset")
    receivables = account(client, "1200", "Receivables", "asset")
    equipment = account(client, "1500", "Equipment", "asset")
    loan = account(client, "2500", "Bank loan", "liability")
    sales = account(client, "4000", "Sales", "revenue")
    rent = account(client, "5000", "Rent", "expense")
    fees = account(client, "5100", "Bank fees", "expense")
    return {
        "bank": bank["id"],
        "savings": savings["id"],
        "receivables": receivables["id"],
        "equipment": equipment["id"],
        "loan": loan["id"],
        "sales": sales["id"],
        "rent": rent["id"],
        "fees": fees["id"],
        "cash_bank": cash_account(client, bank["id"], "Bank")["id"],
        "cash_savings": cash_account(client, savings["id"], "Savings")["id"],
    }


@pytest.fixture
def activity(client, world):
    w = world
    cash_in(client, "2026-01-05", w["bank"], w["sales"], "1000.00", "Cash sale")
    cash_out(client, "2026-01-06", w["bank"], w["rent"], "300.00", "Rent")
    cash_in(client, "2026-01-07", w["bank"], w["loan"], "5000.00", "Loan drawdown")
    cash_out(client, "2026-01-08", w["bank"], w["equipment"], "2000.00", "Buy laptop")
    cash_in(client, "2026-01-09", w["bank"], w["receivables"], "250.00", "Customer settles invoice")
    # Internal transfer: bank -> savings.
    entry(
        client, "2026-01-10",
        [{"account_id": w["savings"], "debit": "1500.00"}, {"account_id": w["bank"], "credit": "1500.00"}],
        "Move to savings",
    )
    # Transfer with a fee: savings -> bank 99.00 and 1.00 fee, 100.00 leaves savings.
    entry(
        client, "2026-01-11",
        [
            {"account_id": w["bank"], "debit": "99.00"},
            {"account_id": w["fees"], "debit": "1.00"},
            {"account_id": w["savings"], "credit": "100.00"},
        ],
        "Transfer back with fee",
    )
    # A draft must never count.
    entry(
        client, "2026-01-12",
        [{"account_id": w["bank"], "debit": "999.00"}, {"account_id": w["sales"], "credit": "999.00"}],
        "Draft sale", post=False,
    )
    # Outside the period (February).
    cash_in(client, "2026-02-01", w["bank"], w["sales"], "10.00", "Feb sale")
    return w


# --- Dated balances ------------------------------------------------------------------


def test_balances_report_is_dated_and_posted_only(client, activity):
    w = activity
    report = client.get("/api/v1/cash/reports/balances", params={"as_of": "2026-01-31"}).json()
    by_id = {r["cash_account_id"]: r for r in report["accounts"]}
    # Bank: +1000 -300 +5000 -2000 +250 -1500 +99 = 2549 ; Savings: +1500 -100 = 1400
    assert by_id[w["cash_bank"]]["balance"] == "2549.00"
    assert by_id[w["cash_savings"]]["balance"] == "1400.00"
    assert by_id[w["cash_bank"]]["ledger_account_code"] == "1000"
    assert report["consolidated_balance"] == "3949.00"
    assert "posted" in report["basis"]

    early = client.get("/api/v1/cash/reports/balances", params={"as_of": "2026-01-06"}).json()
    assert early["consolidated_balance"] == "700.00"
    latest = client.get("/api/v1/cash/reports/balances").json()
    assert latest["consolidated_balance"] == "3959.00"  # + Feb sale, draft still excluded

    per_account = client.get(f"/api/v1/cash-accounts/{w['cash_bank']}/balance", params={"as_of": "2026-01-05"}).json()
    assert per_account["balance"] == "1000.00" and per_account["as_of"] == "2026-01-05"


def test_balances_report_with_no_activity(client, world):
    report = client.get("/api/v1/cash/reports/balances").json()
    assert report["consolidated_balance"] == "0.00"
    assert [r["balance"] for r in report["accounts"]] == ["0.00", "0.00"]


# --- Daily balances -----------------------------------------------------------------------


def test_daily_balances_running_total_and_transfers(client, activity):
    w = activity
    report = client.get(
        "/api/v1/cash/reports/daily-balances",
        params={"start": "2026-01-09", "end": "2026-01-12", "cash_account_id": w["cash_bank"]},
    ).json()
    assert report["opening_balance"] == "3700.00"  # bank balance at 2026-01-08
    days = {d["date"]: d for d in report["days"]}
    assert days["2026-01-09"]["inflows"] == "250.00" and days["2026-01-09"]["closing_balance"] == "3950.00"
    assert days["2026-01-10"]["transfers_out"] == "1500.00" and days["2026-01-10"]["outflows"] == "0.00"
    assert days["2026-01-10"]["closing_balance"] == "2450.00"
    assert days["2026-01-11"]["transfers_in"] == "99.00" and days["2026-01-11"]["closing_balance"] == "2549.00"
    assert days["2026-01-12"]["net"] == "0.00"  # draft ignored
    assert report["closing_balance"] == "2549.00"

    consolidated = client.get(
        "/api/v1/cash/reports/daily-balances", params={"start": "2026-01-10", "end": "2026-01-11"}
    ).json()
    cd = {d["date"]: d for d in consolidated["days"]}
    # Pure transfer: in and out cancel; fee day: 1.00 external outflow only.
    assert cd["2026-01-10"]["net"] == "0.00" and cd["2026-01-10"]["transfers_in"] == "1500.00"
    assert cd["2026-01-11"]["outflows"] == "1.00" and cd["2026-01-11"]["net"] == "-1.00"
    assert consolidated["closing_balance"] == "3949.00"


def test_daily_balances_rejects_reversed_period(client, world):
    resp = client.get("/api/v1/cash/reports/daily-balances", params={"start": "2026-02-01", "end": "2026-01-01"})
    assert resp.status_code == 422


# --- Cash flow by activity ----------------------------------------------------------------


def test_cash_flow_report_classifies_and_excludes_internal_transfers(client, activity):
    w = activity
    # Receivables would default to "investing" (asset); classify explicitly as operating.
    assert client.put("/api/v1/cash/classifications", json={"account_id": w["receivables"], "activity": "operating"}).status_code == 200
    report = client.get("/api/v1/cash/reports/cash-flow", params={"start": "2026-01-01", "end": "2026-01-31"}).json()

    sections = {s["activity"]: s for s in report["activities"]}
    operating = sections["operating"]
    assert operating["inflows"] == "1250.00" and operating["outflows"] == "301.00" and operating["net"] == "949.00"
    by_code = {l["account_code"]: l for l in operating["lines"]}
    assert by_code["4000"]["inflows"] == "1000.00" and by_code["4000"]["classification_source"] == "default_by_type"
    assert by_code["1200"]["inflows"] == "250.00" and by_code["1200"]["classification_source"] == "explicit"
    assert by_code["5000"]["outflows"] == "300.00"
    assert by_code["5100"]["outflows"] == "1.00"  # fee part of the transfer entry
    assert sections["investing"]["outflows"] == "2000.00" and sections["investing"]["inflows"] == "0.00"
    assert sections["financing"]["inflows"] == "5000.00"

    assert report["total_inflows"] == "6250.00" and report["total_outflows"] == "2301.00"
    assert report["net_change"] == "3949.00"
    assert report["opening_balance"] == "0.00" and report["closing_balance"] == "3949.00"
    assert Decimal(report["opening_balance"]) + Decimal(report["net_change"]) == Decimal(report["closing_balance"])

    # Transfers are listed but never part of the totals above.
    assert report["internal_transfers_total"] == "1599.00"
    assert [(t["description"], t["amount"]) for t in report["internal_transfers"]] == [
        ("Move to savings", "1500.00"),
        ("Transfer back with fee", "99.00"),
    ]
    accounts = {a["label"]: a for a in report["accounts"]}
    assert accounts["Bank"]["transfers_out"] == "1500.00" and accounts["Bank"]["transfers_in"] == "99.00"
    assert accounts["Bank"]["inflows"] == "6250.00" and accounts["Bank"]["outflows"] == "2300.00"
    assert accounts["Bank"]["closing_balance"] == "2549.00"
    assert accounts["Savings"]["transfers_in"] == "1500.00" and accounts["Savings"]["transfers_out"] == "99.00"
    assert accounts["Savings"]["outflows"] == "1.00"  # the fee left the savings account
    assert accounts["Savings"]["closing_balance"] == "1400.00"


def test_cash_flow_report_ignores_drafts_and_respects_period(client, activity):
    feb = client.get("/api/v1/cash/reports/cash-flow", params={"start": "2026-02-01", "end": "2026-02-28"}).json()
    assert feb["opening_balance"] == "3949.00" and feb["closing_balance"] == "3959.00"
    assert feb["total_inflows"] == "10.00" and feb["internal_transfers"] == []
    march = client.get("/api/v1/cash/reports/cash-flow", params={"start": "2026-03-01", "end": "2026-03-31"}).json()
    assert march["total_inflows"] == "0.00" and march["opening_balance"] == march["closing_balance"] == "3959.00"


def test_classification_crud(client, world):
    w = world
    assert client.get("/api/v1/cash/classifications").json() == []
    created = client.put("/api/v1/cash/classifications", json={"account_id": w["loan"], "activity": "operating"}).json()
    updated = client.put("/api/v1/cash/classifications", json={"account_id": w["loan"], "activity": "financing"}).json()
    assert created["id"] == updated["id"] and updated["activity"] == "financing"
    assert client.put("/api/v1/cash/classifications", json={"account_id": 999, "activity": "operating"}).status_code == 422
    assert client.put("/api/v1/cash/classifications", json={"account_id": w["loan"], "activity": "bogus"}).status_code == 422
    assert client.delete(f"/api/v1/cash/classifications/{w['loan']}").status_code == 204
    assert client.delete(f"/api/v1/cash/classifications/{w['loan']}").status_code == 404


def test_sums_are_exact_decimals(client, world):
    """0.10 + 0.20 style sums must be exact: no float anywhere in the pipeline."""
    w = world
    for _ in range(10):
        cash_in(client, "2026-01-05", w["bank"], w["sales"], "0.10", "tiny")
    cash_out(client, "2026-01-05", w["bank"], w["rent"], "0.30", "tiny out")
    report = client.get("/api/v1/cash/reports/cash-flow", params={"start": "2026-01-01", "end": "2026-01-31"}).json()
    assert report["total_inflows"] == "1.00" and report["total_outflows"] == "0.30" and report["net_change"] == "0.70"
    assert client.get("/api/v1/cash/reports/balances").json()["consolidated_balance"] == "0.70"
