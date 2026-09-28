"""Corner cases of balances, transfers, activity classification and the projection.

Debit on a cash ledger account = money in, credit = money out. Internal
transfers move money between our own accounts and must never inflate
consolidated flows. Activity is read from the counterpart lines.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.cash import planning
from app.modules.cash.models import PlannedCashFlow
from app.modules.cash.schemas import PlannedCashFlowIn
from app.modules.cash.services import snapshot_read
from tests.cash_helpers import account, cash_account, cash_in, cash_out, entry
from tests.test_cash_concurrency import run_parallel


@pytest.fixture
def world(client):
    w = {}
    for key, code, name, type_ in (
        ("bank", "1000", "Bank", "asset"), ("savings", "1010", "Savings", "asset"), ("petty", "1020", "Petty cash", "asset"),
        ("reserve", "1030", "Reserve", "asset"), ("ar", "1200", "Receivables", "asset"), ("loan", "2500", "Loan", "liability"),
        ("equity", "3000", "Share capital", "equity"), ("sales", "4000", "Sales", "revenue"),
        ("rent", "5000", "Rent", "expense"), ("fees", "5100", "Bank fees", "expense"),
    ):
        w[key] = account(client, code, name, type_)["id"]
    for key in ("bank", "savings", "petty", "reserve"):
        w["cash_" + key] = cash_account(client, w[key], key.capitalize())["id"]
    return w


def flow(client, start="2026-01-01", end="2026-01-31"):
    r = client.get("/api/v1/cash/reports/cash-flow", params={"start": start, "end": end})
    assert r.status_code == 200, r.text
    return r.json()


def balances(client, as_of=None):
    r = client.get("/api/v1/cash/reports/balances", params={"as_of": as_of} if as_of else {}).json()
    return {a["label"]: Decimal(a["balance"]) for a in r["accounts"]}, Decimal(r["consolidated_balance"])


# --- Debit / credit and transfers ---------------------------------------------------------------


def test_debit_is_inflow_credit_is_outflow_for_every_counterpart_type(client, world):
    w = world
    cash_in(client, "2026-01-02", w["bank"], w["equity"], "1000.00", "Capital")       # financing in
    cash_in(client, "2026-01-03", w["bank"], w["loan"], "500.00", "Loan")             # financing in
    cash_out(client, "2026-01-04", w["bank"], w["loan"], "100.00", "Loan repayment")  # financing out
    cash_in(client, "2026-01-05", w["bank"], w["sales"], "300.00", "Sale")            # operating in
    cash_out(client, "2026-01-06", w["bank"], w["rent"], "200.00", "Rent")            # operating out
    cash_out(client, "2026-01-07", w["bank"], w["ar"], "50.00", "Refund to customer")  # asset default: investing out
    report = flow(client)
    sections = {s["activity"]: s for s in report["activities"]}
    assert (sections["financing"]["inflows"], sections["financing"]["outflows"]) == ("1500.00", "100.00")
    assert (sections["operating"]["inflows"], sections["operating"]["outflows"]) == ("300.00", "200.00")
    assert (sections["investing"]["inflows"], sections["investing"]["outflows"]) == ("0.00", "50.00")
    assert report["net_change"] == "1450.00" and report["closing_balance"] == "1450.00" and report["ties_out"] is True
    assert balances(client)[1] == Decimal("1450.00")


def test_transfer_to_several_accounts_allocates_exactly_to_the_cent(client, world):
    w = world
    cash_in(client, "2026-01-02", w["bank"], w["sales"], "1000.00", "Sale")
    # 100.00 leaves Bank; three accounts receive a third each.
    entry(client, "2026-01-03", [
        {"account_id": w["savings"], "debit": "33.33"}, {"account_id": w["petty"], "debit": "33.33"},
        {"account_id": w["reserve"], "debit": "33.34"}, {"account_id": w["bank"], "credit": "100.00"}], "Split transfer")
    # 10.00 leaves Bank in three equal-weight legs that do not divide evenly in cents.
    entry(client, "2026-01-04", [
        {"account_id": w["savings"], "debit": "3.33"}, {"account_id": w["petty"], "debit": "3.33"},
        {"account_id": w["reserve"], "debit": "3.34"}, {"account_id": w["bank"], "credit": "10.00"}], "Small split")
    report = flow(client)
    accounts = {a["label"]: a for a in report["accounts"]}
    assert sum(Decimal(a["transfers_in"]) for a in accounts.values()) == Decimal("110.00")
    assert sum(Decimal(a["transfers_out"]) for a in accounts.values()) == Decimal("110.00")
    assert all(a["inflows"] == "0.00" for label, a in accounts.items() if label != "Bank")
    assert report["internal_transfers_total"] == "110.00"
    assert report["total_inflows"] == "1000.00" and report["total_outflows"] == "0.00" and report["ties_out"] is True
    per_account, total = balances(client)
    assert total == Decimal("1000.00") == sum(per_account.values())
    assert per_account == {"Bank": Decimal("890.00"), "Savings": Decimal("36.66"), "Petty": Decimal("36.66"), "Reserve": Decimal("36.68")}


def test_wash_entry_on_one_account_is_a_self_transfer_and_nets_to_zero(client, world):
    w = world
    cash_in(client, "2026-01-02", w["bank"], w["sales"], "100.00", "Sale")
    entry(client, "2026-01-03", [{"account_id": w["bank"], "debit": "25.00"}, {"account_id": w["bank"], "credit": "25.00"}], "Wash")
    report = flow(client)
    bank = next(a for a in report["accounts"] if a["label"] == "Bank")
    assert bank["transfers_in"] == bank["transfers_out"] == "25.00" and bank["inflows"] == "100.00"
    assert report["total_inflows"] == "100.00" and report["internal_transfers_total"] == "25.00"
    assert balances(client)[1] == Decimal("100.00") and report["ties_out"] is True


def test_mixed_entry_with_transfer_external_in_and_out(client, world):
    """Dr Bank 100 / Cr Savings 40 / Cr Sales 70 / Dr Fees 10: 40 is transfer, 70 in, 10 out."""
    w = world
    entry(client, "2026-01-03", [
        {"account_id": w["bank"], "debit": "100.00"}, {"account_id": w["fees"], "debit": "10.00"},
        {"account_id": w["savings"], "credit": "40.00"}, {"account_id": w["sales"], "credit": "70.00"}], "Mixed")
    report = flow(client)
    accounts = {a["label"]: a for a in report["accounts"]}
    assert (accounts["Bank"]["inflows"], accounts["Bank"]["transfers_in"]) == ("60.00", "40.00")
    assert (accounts["Savings"]["outflows"], accounts["Savings"]["transfers_out"]) == ("0.00", "40.00")
    operating = next(s for s in report["activities"] if s["activity"] == "operating")
    assert (operating["inflows"], operating["outflows"]) == ("70.00", "10.00")
    assert report["net_change"] == "60.00" and report["ties_out"] is True
    assert balances(client)[1] == Decimal("60.00")


def test_gross_counterpart_flows_may_exceed_cash_movement_but_net_always_ties(client, world):
    """Dr Bank 100 / Cr Sales 150 / Dr Rent 50: 150 in, 50 out by activity; cash moved by 100."""
    w = world
    entry(client, "2026-01-03", [
        {"account_id": w["bank"], "debit": "100.00"}, {"account_id": w["rent"], "debit": "50.00"},
        {"account_id": w["sales"], "credit": "150.00"}], "Netted settlement")
    report = flow(client)
    assert (report["total_inflows"], report["total_outflows"], report["net_change"]) == ("150.00", "50.00", "100.00")
    bank = next(a for a in report["accounts"] if a["label"] == "Bank")
    assert (bank["inflows"], bank["outflows"]) == ("100.00", "0.00")
    assert report["ties_out"] is True


def test_overdraft_balances_are_negative_and_consistent(client, world):
    w = world
    cash_out(client, "2026-01-02", w["bank"], w["rent"], "300.00", "Rent before funding")
    per_account, total = balances(client)
    assert per_account["Bank"] == Decimal("-300.00") and total == Decimal("-300.00")
    daily = client.get("/api/v1/cash/reports/daily-balances", params={"start": "2026-01-01", "end": "2026-01-03", "cash_account_id": w["cash_bank"]}).json()
    assert [d["closing_balance"] for d in daily["days"]] == ["0.00", "-300.00", "-300.00"]
    p = client.get("/api/v1/cash/projection", params={"as_of": "2026-01-03", "threshold": "0", "history_days": 0}).json()
    assert p["opening_balance"] == "-300.00" and len(p["alerts"]) == 30
    assert p["alerts"][0]["first_breach"] is True and "below the query threshold 0.00 by 300.00" in p["alerts"][0]["reason"]


def test_explicit_classification_changes_only_the_activity_not_the_totals(client, world):
    w = world
    cash_in(client, "2026-01-05", w["bank"], w["ar"], "250.00", "Customer pays")
    before = flow(client)
    assert next(s for s in before["activities"] if s["activity"] == "investing")["inflows"] == "250.00"
    client.put("/api/v1/cash/classifications", json={"account_id": w["ar"], "activity": "operating"})
    after = flow(client)
    assert next(s for s in after["activities"] if s["activity"] == "operating")["inflows"] == "250.00"
    assert next(s for s in after["activities"] if s["activity"] == "investing")["inflows"] == "0.00"
    assert (after["total_inflows"], after["net_change"], after["closing_balance"]) == (before["total_inflows"], before["net_change"], before["closing_balance"])
    line = next(l for s in after["activities"] for l in s["lines"] if l["account_code"] == "1200")
    assert line["classification_source"] == "explicit"


# --- Preservation of historical figures ----------------------------------------------------------


def test_dated_figures_are_stable_against_later_postings_and_explicit_about_backdated_ones(client, world):
    w = world
    cash_in(client, "2026-01-05", w["bank"], w["sales"], "100.00", "Sale")
    jan = balances(client, "2026-01-31")
    jan_flow = flow(client)
    # Later-dated postings never change January.
    cash_in(client, "2026-02-05", w["bank"], w["sales"], "999.00", "Feb sale")
    assert balances(client, "2026-01-31") == jan and flow(client) == jan_flow
    # A back-dated posting is a ledger fact: January changes, and the change is the exact amount.
    cash_out(client, "2026-01-20", w["bank"], w["rent"], "30.00", "Back-dated rent")
    assert balances(client, "2026-01-31")[1] == jan[1] - Decimal("30.00")
    assert Decimal(flow(client)["total_outflows"]) == Decimal(jan_flow["total_outflows"]) + Decimal("30.00")


def test_projection_reports_backdated_entries_in_its_history_window(client, world):
    w = world
    today = date.today()
    cash_in(client, (today - timedelta(days=10)).isoformat(), w["bank"], w["sales"], "100.00", "Recorded late")
    cash_in(client, today.isoformat(), w["bank"], w["sales"], "1.00", "Recorded on the day")
    cash_in(client, (today - timedelta(days=90)).isoformat(), w["bank"], w["sales"], "5.00", "Outside window")
    p = client.get("/api/v1/cash/projection", params={"as_of": today.isoformat(), "history_days": 30}).json()
    assert p["counted"]["backdated_posted_entries_in_history"] == 1
    assert any("recorded after their entry date" in x for x in p["warnings"])
    assert p["opening_balance"] == "106.00"  # still counted: the ledger is the truth
    without = client.get("/api/v1/cash/projection", params={"as_of": today.isoformat(), "history_days": 0}).json()
    assert without["counted"]["backdated_posted_entries_in_history"] == 0 and without["history"] == []


# --- Thresholds and alert reasons --------------------------------------------------------------


def test_threshold_precedence_query_then_account_then_sum(client, world):
    w = world
    client.patch(f"/api/v1/cash-accounts/{w['cash_bank']}", json={"minimum_balance": "100.00"})
    client.patch(f"/api/v1/cash-accounts/{w['cash_savings']}", json={"minimum_balance": "25.50"})
    consolidated = client.get("/api/v1/cash/projection", params={"as_of": "2026-01-31", "history_days": 0}).json()
    assert (consolidated["threshold"], consolidated["threshold_source"]) == ("125.50", "sum_of_cash_accounts")
    single = client.get("/api/v1/cash/projection", params={"as_of": "2026-01-31", "history_days": 0, "cash_account_id": w["cash_bank"]}).json()
    assert (single["threshold"], single["threshold_source"]) == ("100.00", "cash_account")
    unset = client.get("/api/v1/cash/projection", params={"as_of": "2026-01-31", "history_days": 0, "cash_account_id": w["cash_petty"]}).json()
    assert unset["threshold"] is None and unset["threshold_source"] is None
    override = client.get("/api/v1/cash/projection", params={"as_of": "2026-01-31", "history_days": 0, "cash_account_id": w["cash_petty"], "threshold": "7.77"}).json()
    assert (override["threshold"], override["threshold_source"]) == ("7.77", "query")
    assert client.get("/api/v1/cash/projection", params={"as_of": "2026-01-31", "threshold": "1.234"}).status_code == 422


def test_alert_reasons_name_the_drivers(client, world):
    w = world
    cash_in(client, "2026-02-01", w["bank"], w["sales"], "1000.00", "Opening funds")
    for day, amount, what in (("2026-03-03", "-600.00", "Payroll"), ("2026-03-05", "-300.00", "Rent"), ("2026-03-10", "-50.00", "Fee")):
        client.post("/api/v1/cash/planned-flows", json={"cash_account_id": w["cash_bank"], "expected_date": day, "amount": amount, "description": what})
    cash_out(client, "2026-03-07", w["bank"], w["rent"], "100.00", "Post-dated rent")
    p = client.get("/api/v1/cash/projection", params={"as_of": "2026-03-01", "threshold": "200.00", "history_days": 0}).json()
    first = p["alerts"][0]
    assert first["date"] == "2026-03-05" and first["first_breach"] is True and first["projected_balance"] == "100.00"
    assert "Rent (-300.00, planned cash flow)" in first["reason"] and "Payroll" in first["reason"]
    assert [i["description"] for i in first["items"]] == ["Rent"]
    assert [d["description"] for d in first["drivers"]] == ["Payroll", "Rent"]
    later = next(a for a in p["alerts"] if a["date"] == "2026-03-07")
    assert "Post-dated rent (-100.00, posted journal entry)" in later["reason"] and later["first_breach"] is False
    quiet = next(a for a in p["alerts"] if a["date"] == "2026-03-06")
    assert "no movement on this day" in quiet["reason"] and quiet["items"] == []
    assert [d["description"] for d in p["alerts"][-1]["drivers"]] == ["Payroll", "Rent", "Post-dated rent", "Fee"]
    assert p["lowest_projected_balance"] == "-50.00" and p["lowest_projected_date"] == "2026-03-10"


# --- Reads are consistent; duplicate and parallel writes; rollback --------------------------------


def test_reports_read_one_snapshot(engine):
    with Session(engine) as db:
        assert snapshot_read(db) is True
        if engine.dialect.name == "postgresql":
            assert db.execute(text("SHOW transaction_isolation")).scalar() == "repeatable read"
        else:
            assert db.connection().connection.dbapi_connection.in_transaction is True
        db.execute(text("SELECT 1"))
        assert snapshot_read(db) is True  # still the same snapshot transaction
        db.rollback()
    if engine.dialect.name == "postgresql":
        with Session(engine) as db:
            db.execute(text("SELECT 1"))  # an ordinary read-committed transaction is already running
            assert snapshot_read(db) is False  # best effort: reported, never an error
            assert db.execute(text("SHOW transaction_isolation")).scalar() == "read committed"
            db.rollback()


def test_parallel_identical_planned_flows_create_one_row(engine, client, world):
    w = world
    payload = PlannedCashFlowIn(cash_account_id=w["cash_bank"], expected_date=date(2026, 3, 1), amount=Decimal("-5.00"),
                                description="fee", source="bank", external_reference="FEE-1")
    outcomes = run_parallel(engine, [lambda db: planning.create_planned_flow(db, payload).id] * 3)
    ok = [v for k, v in outcomes if k == "ok"]
    assert len(ok) >= 1 and len(set(ok)) == 1, outcomes
    for k, v in outcomes:
        assert k == "ok" or "database is locked" in str(v), outcomes  # SQLite busy timeout only
    with Session(engine) as db:
        assert db.query(PlannedCashFlow).count() == 1
    different = PlannedCashFlowIn(cash_account_id=w["cash_bank"], expected_date=date(2026, 3, 2), amount=Decimal("-5.00"),
                                  description="fee", source="bank", external_reference="FEE-1")
    with Session(engine) as db, pytest.raises(Exception, match="different data"):
        planning.create_planned_flow(db, different)


def test_parallel_classification_upserts_converge(engine, world):
    w = world
    from app.modules.cash.schemas import ClassificationIn

    workers = [lambda db, a=a: planning.set_classification(db, ClassificationIn(account_id=w["ar"], activity=a)).account_id
               for a in ("operating", "investing", "operating")]
    outcomes = run_parallel(engine, workers)
    assert all(k == "ok" or "database is locked" in str(v) for k, v in outcomes), outcomes
    with Session(engine) as db:
        rows = db.execute(text("SELECT account_id, activity FROM cash_flow_classifications")).all()
        assert len(rows) == 1 and rows[0][1] in ("operating", "investing")


def test_failed_status_update_rolls_back_without_touching_the_plan(client, world):
    w = world
    flow_ = client.post("/api/v1/cash/planned-flows", json={"cash_account_id": w["cash_bank"], "expected_date": "2026-03-01",
                                                            "amount": "-5.00", "description": "fee"}).json()
    assert client.patch(f"/api/v1/cash/planned-flows/{flow_['id']}/status", json={"status": "realized", "journal_entry_id": 99999}).status_code == 422
    again = client.get(f"/api/v1/cash/planned-flows/{flow_['id']}").json()
    assert again["status"] == "planned" and again["journal_entry_id"] is None
    assert client.get("/api/v1/cash/projection", params={"as_of": "2026-02-28", "history_days": 0}).json()["counted"]["planned_cash_flows"] == 1
