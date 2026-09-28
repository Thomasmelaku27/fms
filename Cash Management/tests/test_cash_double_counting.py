"""Nothing is counted twice: internal transfers, future posted entries and planned flows.

* consolidated balance == sum of account balances; a transfer moves money
  between accounts but leaves the consolidated figure and the consolidated
  net flow untouched;
* a planned flow and the posted entry that realises it are the same money:
  only the posted entry counts once the link exists (or the plan is realized);
* a future-dated posted transfer contributes 0 to the consolidated projection
  and +/- to each account's own projection.
"""

from decimal import Decimal

import pytest

from tests.cash_helpers import account, cash_account, cash_in, cash_out, entry

AS_OF = "2026-03-01"


@pytest.fixture
def world(client):
    bank = account(client, "1000", "Bank", "asset")
    savings = account(client, "1010", "Savings", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    fees = account(client, "5100", "Bank fees", "expense")
    w = {"bank": bank["id"], "savings": savings["id"], "sales": sales["id"], "fees": fees["id"]}
    w["cash_bank"] = cash_account(client, bank["id"], "Bank")["id"]
    w["cash_savings"] = cash_account(client, savings["id"], "Savings")["id"]
    cash_in(client, "2026-02-10", w["bank"], w["sales"], "1000.00", "Sale")
    return w


def transfer(client, w, day, amount, fee=None):
    lines = [{"account_id": w["savings"], "debit": str(Decimal(amount) - Decimal(fee or 0))},
             {"account_id": w["bank"], "credit": str(amount)}]
    if fee:
        lines.insert(1, {"account_id": w["fees"], "debit": str(fee)})
    return entry(client, day, lines, "Transfer")


def balances(client, as_of=None):
    r = client.get("/api/v1/cash/reports/balances", params={"as_of": as_of} if as_of else {}).json()
    return {a["label"]: Decimal(a["balance"]) for a in r["accounts"]}, Decimal(r["consolidated_balance"])


def projection(client, **params):
    return client.get("/api/v1/cash/projection", params={"as_of": AS_OF, "history_days": 0, **params}).json()


def test_transfers_change_accounts_but_not_the_consolidated_balance(client, world):
    per_account, total = balances(client)
    assert per_account == {"Bank": Decimal("1000.00"), "Savings": Decimal("0.00")} and total == Decimal("1000.00")

    transfer(client, world, "2026-02-15", "400.00")
    per_account, total = balances(client)
    assert per_account == {"Bank": Decimal("600.00"), "Savings": Decimal("400.00")}
    assert total == sum(per_account.values()) == Decimal("1000.00")

    transfer(client, world, "2026-02-16", "100.00", fee="1.00")  # only the fee leaves the company
    per_account, total = balances(client)
    assert per_account == {"Bank": Decimal("500.00"), "Savings": Decimal("499.00")}
    assert total == Decimal("999.00")

    report = client.get("/api/v1/cash/reports/cash-flow", params={"start": "2026-02-01", "end": "2026-02-28"}).json()
    assert report["internal_transfers_total"] == "499.00"
    assert report["total_inflows"] == "1000.00" and report["total_outflows"] == "1.00"
    assert Decimal(report["net_change"]) == total - Decimal("0.00")
    daily = client.get("/api/v1/cash/reports/daily-balances", params={"start": "2026-02-15", "end": "2026-02-16"}).json()
    for d in daily["days"]:
        assert d["transfers_in"] == d["transfers_out"]  # consolidated: transfers cancel out per day
    assert daily["closing_balance"] == "999.00"


def test_future_posted_transfer_is_neutral_in_consolidated_projection(client, world):
    transfer(client, world, "2026-03-10", "300.00", fee="2.00")
    consolidated = projection(client)
    day = next(d for d in consolidated["days"] if d["date"] == "2026-03-10")
    assert day["posted_net"] == "-2.00" and day["net"] == "-2.00"  # only the fee
    assert sum(Decimal(i["amount"]) for i in day["items"]) == Decimal("-2.00")
    assert consolidated["closing_projected_balance"] == "998.00"

    bank = projection(client, cash_account_id=world["cash_bank"])
    savings = projection(client, cash_account_id=world["cash_savings"])
    assert next(d for d in bank["days"] if d["date"] == "2026-03-10")["posted_net"] == "-300.00"
    assert next(d for d in savings["days"] if d["date"] == "2026-03-10")["posted_net"] == "298.00"
    assert Decimal(bank["closing_projected_balance"]) + Decimal(savings["closing_projected_balance"]) == Decimal(consolidated["closing_projected_balance"])


def test_planned_flow_and_its_posted_entry_count_once(client, world):
    w = world
    plan = client.post("/api/v1/cash/planned-flows", json={"cash_account_id": w["cash_bank"], "expected_date": "2026-03-05",
                                                           "amount": "-100.00", "description": "Rent"}).json()
    assert projection(client)["closing_projected_balance"] == "900.00"

    posted = cash_out(client, "2026-03-05", w["bank"], w["fees"], "100.00", "Rent paid")
    assert projection(client)["closing_projected_balance"] == "800.00"  # plan + entry: not yet linked -> both counted

    # Linking the plan to the posted entry (even without changing its status) removes the double count.
    resp = client.patch(f"/api/v1/cash/planned-flows/{plan['id']}/status", json={"status": "planned", "journal_entry_id": posted["id"]})
    assert resp.status_code == 200 and resp.json()["status"] == "planned"
    p = projection(client)
    assert p["closing_projected_balance"] == "900.00"
    assert p["excluded"]["planned_cash_flows_linked_to_posted_entries"] == 1
    assert any("linked to posted journal entries" in x for x in p["warnings"])
    day = next(d for d in p["days"] if d["date"] == "2026-03-05")
    assert [i["kind"] for i in day["items"]] == ["posted_journal_entry"]

    # Marking it realized is the clean way; the result is the same.
    client.patch(f"/api/v1/cash/planned-flows/{plan['id']}/status", json={"status": "realized", "journal_entry_id": posted["id"]})
    p = projection(client)
    assert p["closing_projected_balance"] == "900.00" and p["excluded"]["planned_cash_flows_linked_to_posted_entries"] == 0
    assert p["excluded"]["realized_or_cancelled_planned_cash_flows"] == 1


def test_planned_flows_on_several_accounts_are_counted_once_in_consolidation(client, world):
    w = world
    for cash_id, amount in ((w["cash_bank"], "-50.00"), (w["cash_savings"], "20.00")):
        client.post("/api/v1/cash/planned-flows", json={"cash_account_id": cash_id, "expected_date": "2026-03-07",
                                                        "amount": amount, "description": "plan"})
    consolidated = projection(client)
    day = next(d for d in consolidated["days"] if d["date"] == "2026-03-07")
    assert day["planned_inflows"] == "20.00" and day["planned_outflows"] == "50.00" and len(day["items"]) == 2
    assert consolidated["closing_projected_balance"] == "970.00"
    assert consolidated["counted"]["planned_cash_flows"] == 2
    bank = projection(client, cash_account_id=w["cash_bank"])
    savings = projection(client, cash_account_id=w["cash_savings"])
    assert bank["counted"]["planned_cash_flows"] == 1 and savings["counted"]["planned_cash_flows"] == 1
    assert Decimal(bank["closing_projected_balance"]) + Decimal(savings["closing_projected_balance"]) == Decimal("970.00")


def test_draft_and_bank_only_movements_never_enter_balances_or_projection(client, world):
    w = world
    entry(client, "2026-02-20", [{"account_id": w["bank"], "debit": "5000.00"}, {"account_id": w["sales"], "credit": "5000.00"}], "draft", post=False)
    client.post("/api/v1/cash/bank-statements", json={
        "cash_account_id": w["cash_bank"], "statement_reference": "S", "period_start": "2026-02-01", "period_end": "2026-02-28",
        "opening_balance": "0", "closing_balance": "7000.00",
        "transactions": [{"transaction_date": "2026-02-21", "amount": "7000.00", "description": "not in the ledger"}]})
    _, total = balances(client)
    assert total == Decimal("1000.00")
    p = projection(client)
    assert p["opening_balance"] == "1000.00" and p["closing_projected_balance"] == "1000.00"
    assert p["excluded"]["draft_journal_entries_in_horizon"] == 1 and p["excluded"]["unmatched_bank_transactions"] == 1
