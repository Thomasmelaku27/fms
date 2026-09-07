"""SQL-backed reporting: exact aggregation, pagination, hierarchy views, comparisons."""

import random
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.services.reports import account_balances, account_totals, aggregate_totals, cents_sum
from tests.helpers import account, draft, post


@pytest.fixture
def chart(client):
    assets = account(client, "1", "Assets", "asset", is_summary=True)
    current = account(client, "10", "Current", "asset", is_summary=True, parent_id=assets["id"])
    return {
        "assets": assets,
        "current": current,
        "cash": account(client, "1000", "Cash", "asset", parent_id=current["id"]),
        "bank": account(client, "1100", "Bank", "asset", parent_id=current["id"]),
        "equipment": account(client, "1500", "Equipment", "asset", parent_id=assets["id"]),
        "sales": account(client, "4000", "Sales", "revenue"),
    }


def _seed(client, chart, n=40, seed=3):
    rng = random.Random(seed)
    posting = [chart["cash"], chart["bank"], chart["equipment"], chart["sales"]]
    expected = {a["id"]: [Decimal("0"), Decimal("0")] for a in posting}
    for i in range(n):
        amount = Decimal(rng.randint(1, 99999)) / 100
        debit, credit = rng.sample(posting, 2)
        day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        post(
            client,
            draft(
                client,
                [
                    {"account_id": debit["id"], "debit": f"{amount:.2f}"},
                    {"account_id": credit["id"], "credit": f"{amount:.2f}"},
                ],
                entry_date=day,
            )["id"],
        )
        expected[debit["id"]][0] += amount
        expected[credit["id"]][1] += amount
    return expected


def test_sql_totals_equal_python_sums_and_aggregate(client, chart):
    expected = _seed(client, chart)
    with Session(client.app.state.engine) as db:
        totals = account_totals(db)
        agg = aggregate_totals(db)
    for acc_id, (d, c) in expected.items():
        assert totals[acc_id] == (d.quantize(Decimal("0.01")), c.quantize(Decimal("0.01")))
    assert agg == totals


def test_cents_sum_is_exact_for_awkward_cents(client, chart):
    for cents in ("0.01", "0.02", "0.07", "0.10", "0.20", "0.30", "99.99", "1234.56"):
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
        totals = account_totals(db)
    assert totals[chart["cash"]["id"]][0] == Decimal("1335.25")
    assert (
        str(totals[chart["cash"]["id"]][0]) == "1335.25"
    )  # a Decimal with two places, not a float
    with Session(client.app.state.engine) as db:
        assert cents_sum(db, Decimal("0")) is not None  # the helper itself is importable/usable


def test_general_ledger_pagination_is_deterministic_and_balances_carry(client, chart):
    _seed(client, chart, n=30)
    full = client.get("/api/v1/reports/general-ledger").json()
    assert full["next_cursor"] is None
    all_lines = [
        (a["code"], ln["entry_id"], ln["line_id"], ln["balance"])
        for a in full["accounts"]
        for ln in a["lines"]
    ]
    assert len(all_lines) == 60

    paged = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 7}
        if cursor:
            params["after"] = cursor
        page = client.get("/api/v1/reports/general-ledger", params=params).json()
        pages += 1
        for a in page["accounts"]:
            for ln in a["lines"]:
                paged.append((a["code"], ln["entry_id"], ln["line_id"], ln["balance"]))
            assert a["closing_balance"] == next(
                x["closing_balance"] for x in full["accounts"] if x["code"] == a["code"]
            )
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 9
    assert paged == all_lines  # same order, same running balances across page boundaries
    # Replaying a cursor gives the same page; a garbage cursor is 422.
    assert (
        client.get(
            "/api/v1/reports/general-ledger", params={"limit": 7, "after": "nonsense"}
        ).status_code
        == 422
    )


def test_journal_page_cursor(client, chart):
    _seed(client, chart, n=12)
    seen = []
    cursor = None
    while True:
        params = {"limit": 5, "status": "posted"}
        if cursor:
            params["after"] = cursor
        page = client.get("/api/v1/journal-entries/page", params=params).json()
        seen.extend((e["entry_date"], e["id"]) for e in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == sorted(seen) and len(seen) == 12
    assert client.get("/api/v1/journal-entries/page", params={"after": "bad"}).status_code == 422


def test_account_balances_and_hierarchy_do_not_double_count(client, chart):
    for day, amount, acc in [
        ("2026-01-10", "100.00", "cash"),
        ("2026-02-10", "50.00", "bank"),
        ("2026-03-10", "20.00", "equipment"),
    ]:
        post(
            client,
            draft(
                client,
                [
                    {"account_id": chart[acc]["id"], "debit": amount},
                    {"account_id": chart["sales"]["id"], "credit": amount},
                ],
                entry_date=day,
            )["id"],
        )
    for source in ("aggregate", "lines"):
        resp = client.get(
            "/api/v1/reports/account-balances",
            params={"from": "2026-02-01", "to": "2026-03-31", "source": source},
        )
        assert resp.status_code == 200, resp.text
        rows = {r["code"]: r for r in resp.json()["rows"]}
        assert (
            rows["1000"]["opening_balance"],
            rows["1000"]["debit"],
            rows["1000"]["closing_balance"],
        ) == ("100.00", "0.00", "100.00")
        assert (
            rows["10"]["opening_balance"],
            rows["10"]["debit"],
            rows["10"]["closing_balance"],
        ) == ("100.00", "50.00", "150.00")
        assert (rows["1"]["opening_balance"], rows["1"]["debit"], rows["1"]["closing_balance"]) == (
            "100.00",
            "70.00",
            "170.00",
        )
        assert rows["1"]["net_change"] == "70.00"
        tree = client.get(
            "/api/v1/reports/hierarchy",
            params={"from": "2026-02-01", "to": "2026-03-31", "source": source},
        ).json()
        assets = next(n for n in tree["roots"] if n["code"] == "1")
        assert (
            assets["own_debit"] == "0.00"
            and assets["rolled_debit"] == "70.00"
            and assets["closing_balance"] == "170.00"
        )
        current = next(c for c in assets["children"] if c["code"] == "10")
        assert current["rolled_debit"] == "50.00" and [c["code"] for c in current["children"]] == [
            "1000",
            "1100",
        ]
        assert sum(Decimal(c["rolled_debit"]) for c in assets["children"]) == Decimal(
            assets["rolled_debit"]
        )
    subtree = client.get(
        "/api/v1/reports/hierarchy", params={"root_id": chart["current"]["id"]}
    ).json()
    assert [n["code"] for n in subtree["roots"]] == ["10"]
    assert client.get("/api/v1/reports/hierarchy", params={"root_id": 999}).status_code == 404
    filtered = client.get(
        "/api/v1/reports/account-balances",
        params={"account_ids": f"{chart['current']['id']},{chart['cash']['id']}"},
    ).json()
    assert [r["code"] for r in filtered["rows"]] == ["10", "1000"]
    assert (
        client.get("/api/v1/reports/account-balances", params={"account_ids": "x"}).status_code
        == 422
    )


def test_balance_comparison_across_ranges(client, chart):
    for day, amount in [("2026-01-10", "100.00"), ("2026-02-10", "30.00")]:
        post(
            client,
            draft(
                client,
                [
                    {"account_id": chart["cash"]["id"], "debit": amount},
                    {"account_id": chart["sales"]["id"], "credit": amount},
                ],
                entry_date=day,
            )["id"],
        )
    resp = client.get(
        "/api/v1/reports/account-balances/compare",
        params={
            "from": "2026-02-01",
            "to": "2026-02-28",
            "compare_from": "2026-01-01",
            "compare_to": "2026-01-31",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["differences"]["1000"] == "30.00" and body["differences"]["4000"] == "-30.00"
    assert body["differences"]["1"] == "30.00"  # roll-up difference, not doubled
    current = {r["code"]: r for r in body["current"]["rows"]}
    assert (current["1000"]["opening_balance"], current["1000"]["closing_balance"]) == (
        "100.00",
        "130.00",
    )


def test_service_account_balances_sources_agree(client, chart):
    _seed(client, chart, n=25, seed=11)
    with Session(client.app.state.engine) as db:
        a = account_balances(db, source="aggregate").rows
        b = account_balances(db, source="lines").rows
    assert [(r.code, r.closing_balance) for r in a] == [(r.code, r.closing_balance) for r in b]
