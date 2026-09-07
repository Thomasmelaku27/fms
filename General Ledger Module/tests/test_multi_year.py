"""Three fiscal years of history, compared at many cutoff dates.

Opening balances, ordinary journals, accruals with reversals, recurring rent,
period closes, year-end closes and a post-close correction; trial balances,
retained earnings, carried-forward balances, hierarchical totals and the
reconciliation report are checked at each cutoff.
"""

from decimal import Decimal

import pytest

from tests.helpers import account, balances, draft, post

API = "/api/v1"


def _ok(resp, status=200):
    assert resp.status_code == status, f"{resp.request.method} {resp.url}: {resp.text}"
    return resp.json() if resp.content else None


@pytest.fixture
def world(client):
    acc = {}
    acc["assets"] = account(client, "1", "Assets", "asset", is_summary=True)
    acc["cash"] = account(client, "1000", "Cash", "asset", parent_id=acc["assets"]["id"])
    acc["ar"] = account(client, "1200", "Receivables", "asset", parent_id=acc["assets"]["id"])
    acc["accrued"] = account(client, "2100", "Accrued", "liability")
    acc["equity"] = account(client, "3000", "Capital", "equity")
    acc["re"] = account(client, "3900", "Retained earnings", "equity", category="retained_earnings")
    acc["sales"] = account(client, "4000", "Sales", "revenue")
    acc["rent"] = account(client, "6000", "Rent", "expense")
    acc["util"] = account(client, "6100", "Utilities", "expense")
    years = {}
    for y in (2025, 2026, 2027):
        years[y] = _ok(
            client.post(
                f"{API}/fiscal-years",
                json={
                    "name": f"FY{y}",
                    "start_date": f"{y}-01-01",
                    "end_date": f"{y}-12-31",
                    "retained_earnings_account_id": acc["re"]["id"],
                    "generate_monthly_periods": True,
                },
            ),
            201,
        )
    return acc, years


def _post(client, acc, day, debit, credit, amount, entry_type="standard"):
    """Post a two-line entry; returns the entry id."""
    return post(
        client,
        draft(
            client,
            [
                {"account_id": acc[debit]["id"], "debit": amount},
                {"account_id": acc[credit]["id"], "credit": amount},
            ],
            entry_date=day,
            entry_type=entry_type,
        )["id"],
    )["id"]


def _close_year(client, year):
    for p in client.get(f"{API}/periods").json():
        if p["fiscal_year_id"] == year["id"] and p["status"] == "open":
            _ok(client.post(f"{API}/periods/{p['id']}/close"))
    return _ok(client.post(f"{API}/fiscal-years/{year['id']}/close"))


def _tb(client, to=None, frm=None):
    params = {}
    if frm:
        params["from"] = frm
    if to:
        params["to"] = to
    report = client.get(f"{API}/reports/trial-balance", params=params).json()
    assert report["is_balanced"], report
    return {r["code"]: Decimal(r["balance"]) for r in report["rows"]}


def test_three_year_history(client, world):
    acc, years = world
    D = Decimal

    # FY2025: opening balances, sales, rent, an accrual reversed in 2026.
    _ok(
        client.post(
            f"{API}/opening-balances",
            json={
                "import_id": "OB-2025",
                "as_of": "2025-01-01",
                "balances": [
                    {"account_id": acc["cash"]["id"], "debit": "1000.00"},
                    {"account_id": acc["equity"]["id"], "credit": "1000.00"},
                ],
            },
        ),
        201,
    )
    _post(client, acc, "2025-03-01", "ar", "sales", "500.00")
    _post(client, acc, "2025-04-01", "cash", "ar", "300.00")
    _post(client, acc, "2025-06-30", "rent", "cash", "120.00")
    accrual_2025 = _post(
        client, acc, "2025-12-31", "util", "accrued", "40.00", entry_type="adjusting"
    )
    template = _ok(
        client.post(
            f"{API}/recurring-templates",
            json={
                "name": "Rent",
                "description": "Rent",
                "frequency": "monthly",
                "start_date": "2026-01-31",
                "lines": [
                    {"account_id": acc["rent"]["id"], "debit": "120.00"},
                    {"account_id": acc["cash"]["id"], "credit": "120.00"},
                ],
            },
        ),
        201,
    )

    cut_2025 = _tb(client, to="2025-12-31")
    assert cut_2025["1000"] == D("1180.00") and cut_2025["1200"] == D("200.00")
    assert (
        cut_2025["4000"] == D("-500.00")
        and cut_2025["6000"] == D("120.00")
        and cut_2025["6100"] == D("40.00")
    )
    assert cut_2025["1"] == D("1380.00")  # hierarchy: cash + receivables
    closed_2025 = _close_year(client, years[2025])
    assert client.get(f"{API}/fiscal-years/{years[2025]['id']}/closing/verify").json()["ok"] is True
    net_2025 = D("500.00") - D("120.00") - D("40.00")  # 340
    after_2025 = _tb(client, to="2025-12-31")
    assert (
        after_2025["3900"] == -net_2025
        and after_2025["4000"] == D("0.00")
        and after_2025["6000"] == D("0.00")
    )
    assert after_2025["1"] == cut_2025["1"]  # balance-sheet side untouched by the close
    assert _tb(client, to="2025-12-30")["4000"] == D(
        "-500.00"
    )  # the day before the close still shows activity

    # FY2026: the accrual reverses, recurring rent all year, a sale, and an attempted
    # correction of 2025 done the right way (in 2026 against retained earnings).
    _ok(
        client.post(
            f"{API}/journal-entries/{accrual_2025}/reverse", json={"reversal_date": "2026-01-01"}
        ),
        201,
    )
    _post(client, acc, "2026-01-05", "util", "cash", "40.00")  # the real bill
    rent_drafts = _ok(
        client.post(
            f"{API}/recurring-templates/{template['id']}/generate", json={"until": "2026-12-31"}
        ),
        201,
    )
    _ok(
        client.post(
            f"{API}/journal-entries/batch-post", json={"entry_ids": [d["id"] for d in rent_drafts]}
        )
    )
    _post(client, acc, "2026-07-15", "ar", "sales", "900.00")
    assert client.post(f"{API}/journal-entries/{accrual_2025}/post").status_code == 409
    prior_period_fix = _post(
        client, acc, "2026-02-01", "re", "cash", "10.00", entry_type="adjusting"
    )  # prior-period adjustment
    assert (
        client.get(f"{API}/journal-entries/{prior_period_fix}").json()["entry_type"] == "adjusting"
    )

    opening_2026 = {
        a["code"]: Decimal(a["opening_balance"])
        for a in client.get(f"{API}/reports/general-ledger", params={"from": "2026-01-01"}).json()[
            "accounts"
        ]
    }
    assert opening_2026["1000"] == after_2025["1000"] and opening_2026["3900"] == after_2025["3900"]
    assert opening_2026["4000"] == D("0.00") and opening_2026["6000"] == D("0.00")
    mid_2026 = _tb(client, frm="2026-01-01", to="2026-06-30")
    assert mid_2026["6000"] == D("720.00") and mid_2026["6100"] == D(
        "0.00"
    )  # accrual reversed, bill booked
    closed_2026 = _close_year(client, years[2026])
    net_2026 = D("900.00") - D("1440.00") - D("0.00")  # loss of 540
    after_2026 = _tb(client, to="2026-12-31")
    assert after_2026["3900"] == -(net_2025 + net_2026) + D("10.00")
    assert after_2026["2100"] == D("0.00")
    # Year-over-year comparison via the balances endpoint.
    cmp = client.get(
        f"{API}/reports/account-balances/compare",
        params={
            "from": "2026-01-01",
            "to": "2026-12-31",
            "compare_from": "2025-01-01",
            "compare_to": "2025-12-31",
        },
    ).json()
    assert Decimal(cmp["differences"]["1000"]) == after_2026["1000"] - after_2025["1000"]

    # FY2027: carried forward, still open; cutoffs inside the year.
    _post(client, acc, "2027-02-02", "cash", "ar", "1100.00")
    opening_2027 = {
        r["code"]: Decimal(r["opening_balance"])
        for r in client.get(
            f"{API}/reports/account-balances", params={"from": "2027-01-01", "to": "2027-12-31"}
        ).json()["rows"]
    }
    assert opening_2027["1000"] == after_2026["1000"] and opening_2027["1"] == after_2026["1"]
    assert opening_2027["4000"] == D("0.00")
    q1 = _tb(client, frm="2027-01-01", to="2027-03-31")
    assert q1["1000"] == D("1100.00") and q1["1200"] == D("-1100.00")
    assert (
        client.get(f"{API}/fiscal-years/{years[2027]['id']}/close-check").json()["can_close"]
        is False
    )

    # Everything reconciles, closing attestations verify, history is classified.
    recon = client.get(f"{API}/reconciliation").json()
    assert recon["ok"], [c for c in recon["checks"] if c["findings"]]
    for y in (2025, 2026):
        assert (
            client.get(f"{API}/fiscal-years/{years[y]['id']}/closing/verify").json()["ok"] is True
        )
    kinds = {}
    for e in client.get(f"{API}/journal-entries?status=posted").json():
        kinds[e["entry_type"]] = kinds.get(e["entry_type"], 0) + 1
    assert kinds == {
        "opening": 1,
        "standard": 6,
        "adjusting": 2,
        "reversal": 1,
        "recurring": 12,
        "closing": 2,
    }
    assert closed_2025["closing_entry_id"] != closed_2026["closing_entry_id"]
    all_time = balances(client)
    assert all_time["__total_debit__"] == all_time["__total_credit__"]
