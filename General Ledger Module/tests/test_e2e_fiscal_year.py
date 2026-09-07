"""End-to-end: a full fiscal year, through the API, with failures and retries on the way.

Opening balances -> ordinary postings (incl. an AR module via the processing
contract) -> recurring rent -> adjusting accrual + its reversal -> period closes
-> year-end close -> carry-forward into the next year -> reconciliation.
At every failure point the totals are shown to be unchanged, and every retry is
shown to be a no-op.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import FiscalYear
from app.services.fiscal_years import close_year
from tests.helpers import balances, clear_hooks, failing_checkpoint  # noqa: F401

API = "/api/v1"


def _ok(resp, status=200):
    assert resp.status_code == status, f"{resp.request.method} {resp.url}: {resp.text}"
    return resp.json() if resp.content else None


def test_full_fiscal_year_lifecycle(client):
    # --- Chart of accounts with a hierarchy ---------------------------------------------
    acc = {}

    def add(code, name, type_, **extra):
        acc[code] = _ok(
            client.post(
                f"{API}/accounts", json={"code": code, "name": name, "type": type_, **extra}
            ),
            201,
        )
        return acc[code]

    add("1", "Assets", "asset", is_summary=True)
    add("1000", "Cash", "asset", parent_id=acc["1"]["id"], category="current_asset")
    add("1200", "Receivables", "asset", parent_id=acc["1"]["id"], category="current_asset")
    add("2000", "Payables", "liability", category="current_liability")
    add("2100", "Accrued liabilities", "liability", category="current_liability")
    add("3000", "Share capital", "equity", category="equity")
    add("3900", "Retained earnings", "equity", category="retained_earnings")
    add("4000", "Sales", "revenue", category="operating_revenue")
    add("6000", "Rent", "expense", category="operating_expense")
    add("6100", "Utilities", "expense", category="operating_expense")

    year = _ok(
        client.post(
            f"{API}/fiscal-years",
            json={
                "name": "FY2026",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "retained_earnings_account_id": acc["3900"]["id"],
                "generate_monthly_periods": True,
            },
        ),
        201,
    )
    periods = {p["name"]: p for p in client.get(f"{API}/periods").json()}
    assert len(periods) == 12

    # --- 1. Opening balances from the legacy system (applied once, retried twice)
    opening = {
        "import_id": "LEGACY-2025",
        "as_of": "2026-01-01",
        "balances": [
            {"account_id": acc["1000"]["id"], "debit": "10000.00"},
            {"account_id": acc["1200"]["id"], "debit": "2500.00"},
            {"account_id": acc["2000"]["id"], "credit": "1500.00"},
            {"account_id": acc["3000"]["id"], "credit": "9000.00"},
            {"account_id": acc["3900"]["id"], "credit": "2000.00"},
        ],
    }
    first = _ok(client.post(f"{API}/opening-balances", json=opening), 201)
    assert first["created"] and first["entry"]["entry_type"] == "opening"
    for _ in range(2):
        assert _ok(client.post(f"{API}/opening-balances", json=opening), 201)["created"] is False
    after_opening = balances(client)
    assert after_opening["1000"] == "10000.00" and after_opening["3900"] == "-2000.00"
    assert after_opening["__total_debit__"] == after_opening["__total_credit__"] == "12500.00"
    assert len(client.get(f"{API}/journal-entries").json()) == 1

    # --- 2. Ordinary business: an AR module posts invoices through the contract
    def invoice(number, amount, day):
        return {
            "source_type": "ar_invoice",
            "source_id": number,
            "operation_id": "post",
            "entry_date": day,
            "description": f"Invoice {number}",
            "lines": [
                {"account_id": acc["1200"]["id"], "debit": amount},
                {"account_id": acc["4000"]["id"], "credit": amount},
            ],
        }

    batch = {
        "entries": [
            invoice("INV-1", "1200.00", "2026-01-20"),
            invoice("INV-2", "800.00", "2026-02-14"),
        ]
    }
    _ok(client.post(f"{API}/journal-processing/batches", json=batch))
    # The AR module retries the whole batch after a timeout: nothing changes.
    retry = _ok(client.post(f"{API}/journal-processing/batches", json=batch))
    assert [r["created"] for r in retry["results"]] == [False, False]
    # A bug in the caller reuses INV-2 with a different amount: refused, ledger untouched.
    snapshot = balances(client)
    assert (
        client.post(
            f"{API}/journal-processing/entries", json=invoice("INV-2", "900.00", "2026-02-14")
        ).status_code
        == 409
    )
    assert balances(client) == snapshot

    # Customer pays INV-1 (cash receipt entered by a user).
    receipt = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-02-01",
                "description": "Receipt INV-1",
                "lines": [
                    {"account_id": acc["1000"]["id"], "debit": "1200.00"},
                    {"account_id": acc["1200"]["id"], "credit": "1200.00"},
                ],
            },
        ),
        201,
    )
    _ok(client.post(f"{API}/journal-entries/{receipt['id']}/post"))

    # --- 3. Recurring rent, generated quarterly by a scheduler
    template = _ok(
        client.post(
            f"{API}/recurring-templates",
            json={
                "name": "Office rent",
                "description": "Office rent",
                "frequency": "monthly",
                "start_date": "2026-01-31",
                "lines": [
                    {"account_id": acc["6000"]["id"], "debit": "500.00"},
                    {"account_id": acc["1000"]["id"], "credit": "500.00"},
                ],
            },
        ),
        201,
    )
    rent_drafts = _ok(
        client.post(f"{API}/recurring-templates/generate-due", json={"until": "2026-03-31"}), 201
    )
    assert len(rent_drafts) == 3
    assert (
        _ok(
            client.post(f"{API}/recurring-templates/generate-due", json={"until": "2026-03-31"}),
            201,
        )
        == []
    )
    # Reviewed and posted as a batch; the batch is retried by an impatient user.
    rent_ids = [d["id"] for d in rent_drafts]
    assert _ok(client.post(f"{API}/journal-entries/batch-post", json={"entry_ids": rent_ids}))[
        "posted_now"
    ] == sorted(rent_ids)
    assert (
        _ok(client.post(f"{API}/journal-entries/batch-post", json={"entry_ids": rent_ids}))[
            "posted_now"
        ]
        == []
    )

    # --- 4. Month-end: adjusting accrual for March utilities, reversed on April 1
    accrual = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-03-31",
                "description": "Accrued utilities",
                "entry_type": "adjusting",
                "lines": [
                    {"account_id": acc["6100"]["id"], "debit": "130.00"},
                    {"account_id": acc["2100"]["id"], "credit": "130.00"},
                ],
            },
        ),
        201,
    )
    _ok(client.post(f"{API}/journal-entries/{accrual['id']}/post"))
    reversal = _ok(
        client.post(
            f"{API}/journal-entries/{accrual['id']}/reverse", json={"reversal_date": "2026-04-01"}
        ),
        201,
    )
    assert reversal["entry_type"] == "reversal"
    assert (
        client.post(
            f"{API}/journal-entries/{accrual['id']}/reverse", json={"reversal_date": "2026-04-01"}
        ).status_code
        == 409
    )
    # The real utilities bill arrives in April.
    bill = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-04-10",
                "description": "Utilities bill",
                "lines": [
                    {"account_id": acc["6100"]["id"], "debit": "125.00"},
                    {"account_id": acc["1000"]["id"], "credit": "125.00"},
                ],
            },
        ),
        201,
    )
    _ok(client.post(f"{API}/journal-entries/{bill['id']}/post"))

    # Generate the rest of the year's rent and post it.
    more_rent = _ok(
        client.post(f"{API}/recurring-templates/generate-due", json={"until": "2026-12-31"}), 201
    )
    assert len(more_rent) == 9
    _ok(
        client.post(
            f"{API}/journal-entries/batch-post", json={"entry_ids": [d["id"] for d in more_rent]}
        )
    )

    # --- 5. Close the periods in order; a stray draft blocks December until resolved
    stray = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-12-15",
                "description": "?",
                "lines": [{"account_id": acc["1000"]["id"]}],
            },
        ),
        201,
    )
    for name in sorted(periods):
        if name == "2026-12":
            check = _ok(client.get(f"{API}/periods/{periods[name]['id']}/close-check"))
            assert check["can_close"] is False and str(stray["id"]) in check["problems"][0]
            assert client.post(f"{API}/periods/{periods[name]['id']}/close").status_code == 409
            _ok(client.delete(f"{API}/journal-entries/{stray['id']}"), 204)
        _ok(client.post(f"{API}/periods/{periods[name]['id']}/close"))
    assert (
        client.post(f"{API}/periods/{periods['2026-06']['id']}/close").status_code == 409
    )  # already

    # --- 6. Year-end close: preview, a failed attempt, the real close, a repeated request
    pre_close = balances(client)
    expected_net = (
        Decimal("2000.00") - Decimal("6000.00") - Decimal("125.00")
    )  # sales - rent - utilities
    preview = _ok(client.get(f"{API}/fiscal-years/{year['id']}/closing-balances"))
    assert Decimal(preview["net_income"]) == expected_net  # a loss of 4125.00
    assert {line["code"] for line in preview["lines"]} == {"4000", "6000", "6100"}
    assert (
        pre_close["4000"] == "-2000.00"
        and pre_close["6000"] == "6000.00"
        and pre_close["6100"] == "125.00"
    )

    with failing_checkpoint("fiscal_years.close_before_commit"):
        with Session(client.app.state.engine) as db:
            try:
                close_year(db, db.get(FiscalYear, year["id"]))
            except RuntimeError:
                pass
    assert balances(client) == pre_close
    assert _ok(client.get(f"{API}/fiscal-years/{year['id']}"))["status"] == "open"

    closed = _ok(client.post(f"{API}/fiscal-years/{year['id']}/close"))
    assert closed["status"] == "closed"
    assert client.post(f"{API}/fiscal-years/{year['id']}/close").status_code == 409
    post_close = balances(client)
    assert post_close["4000"] == post_close["6000"] == post_close["6100"] == "0.00"
    assert (
        Decimal(post_close["3900"]) == Decimal("-2000.00") - expected_net
    )  # -2000 + 4125 = 2125 debit
    assert post_close["1000"] == pre_close["1000"] and post_close["1200"] == pre_close["1200"]
    assert post_close["2000"] == pre_close["2000"] and post_close["3000"] == pre_close["3000"]
    assert post_close["__total_debit__"] == post_close["__total_credit__"]
    closing_entry = _ok(client.get(f"{API}/journal-entries/{closed['closing_entry_id']}"))
    assert closing_entry["entry_type"] == "closing" and closing_entry["entry_date"] == "2026-12-31"
    assert client.post(f"{API}/journal-entries/{closing_entry['id']}/reverse").status_code == 409

    # Lock the year's periods for good.
    for period in periods.values():
        _ok(client.post(f"{API}/periods/{period['id']}/lock"))
    assert client.post(f"{API}/periods/{periods['2026-03']['id']}/reopen").status_code == 409

    # --- 7. Carry-forward into FY2027
    year2 = _ok(
        client.post(
            f"{API}/fiscal-years",
            json={
                "name": "FY2027",
                "start_date": "2027-01-01",
                "end_date": "2027-12-31",
                "retained_earnings_account_id": acc["3900"]["id"],
                "generate_monthly_periods": True,
            },
        ),
        201,
    )
    opening_2027 = _ok(client.get(f"{API}/reports/general-ledger?from=2027-01-01&to=2027-12-31"))
    opening_by_code = {a["code"]: a["opening_balance"] for a in opening_2027["accounts"]}
    assert opening_by_code["1000"] == post_close["1000"]  # balance-sheet accounts carry forward
    assert opening_by_code["3900"] == post_close["3900"]
    assert (
        opening_by_code["4000"] == "0.00" and opening_by_code["6000"] == "0.00"
    )  # temporary accounts start at zero

    # January 2027 activity: the rent template simply continues.
    jan_rent = _ok(
        client.post(
            f"{API}/recurring-templates/{template['id']}/generate", json={"until": "2027-01-31"}
        ),
        201,
    )
    assert [d["entry_date"] for d in jan_rent] == ["2027-01-31"]
    _ok(client.post(f"{API}/journal-entries/{jan_rent[0]['id']}/post"))
    tb_2027 = _ok(client.get(f"{API}/reports/trial-balance?from=2027-01-01&to=2027-12-31"))
    assert tb_2027["is_balanced"] and tb_2027["total_debit"] == "500.00"
    assert _ok(client.get(f"{API}/fiscal-years/{year2['id']}/close-check"))["can_close"] is False

    # --- 8. Everything reconciles; the history is complete and classified
    recon = _ok(client.get(f"{API}/reconciliation"))
    assert recon["ok"] is True and recon["error_count"] == 0, recon["checks"]
    by_type = {}
    for entry in client.get(f"{API}/journal-entries?status=posted").json():
        by_type[entry["entry_type"]] = by_type.get(entry["entry_type"], 0) + 1
    assert by_type == {
        "opening": 1,
        "standard": 4,
        "recurring": 13,
        "adjusting": 1,
        "reversal": 1,
        "closing": 1,
    }
    all_time = balances(client)
    assert all_time["__total_debit__"] == all_time["__total_credit__"]
