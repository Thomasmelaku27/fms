"""Trial balance and General Ledger detail: posted entries only, Decimal arithmetic."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.services.reports import general_ledger, trial_balance


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _entry(client, entry_date, lines, description="Entry", post=True):
    resp = client.post(
        "/api/v1/journal-entries",
        json={"entry_date": entry_date, "description": description, "lines": lines},
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    if post:
        posted = client.post(f"/api/v1/journal-entries/{entry['id']}/post")
        assert posted.status_code == 200, posted.text
        return posted.json()
    return entry


@pytest.fixture
def ledger(client):
    """Accounts + a small history including a draft and a reversal."""
    cash = _account(client, "1000", "Cash", "asset")
    bank = _account(client, "1100", "Bank", "asset")
    sales = _account(client, "4000", "Sales", "revenue")
    rent = _account(client, "6000", "Rent", "expense")
    d = lambda acc, amt: {"account_id": acc["id"], "debit": amt}  # noqa: E731
    c = lambda acc, amt: {"account_id": acc["id"], "credit": amt}  # noqa: E731

    sale = _entry(
        client, "2026-01-10", [d(cash, "0.10"), d(cash, "0.20"), c(sales, "0.30")], "Sale"
    )
    rent_paid = _entry(client, "2026-01-20", [d(rent, "500.00"), c(bank, "500.00")], "Rent")
    feb_sale = _entry(client, "2026-02-05", [d(bank, "1000.00"), c(sales, "1000.00")], "Feb sale")
    draft = _entry(
        client, "2026-01-15", [d(cash, "999.00"), c(sales, "999.00")], "Draft", post=False
    )
    reversal = client.post(
        f"/api/v1/journal-entries/{rent_paid['id']}/reverse", json={"entry_date": "2026-01-25"}
    ).json()
    return {
        "cash": cash, "bank": bank, "sales": sales, "rent": rent,
        "sale": sale, "rent_paid": rent_paid, "feb_sale": feb_sale,
        "draft": draft, "reversal": reversal,
    }  # fmt: skip


def _rows(report):
    return {row["code"]: (row["debit"], row["credit"], row["balance"]) for row in report["rows"]}


def test_trial_balance_all_time(client, ledger):
    resp = client.get("/api/v1/reports/trial-balance")
    assert resp.status_code == 200
    report = resp.json()
    assert report["is_balanced"] is True
    assert report["total_debit"] == report["total_credit"] == "2000.30"
    assert _rows(report) == {
        "1000": ("0.30", "0.00", "0.30"),
        "1100": (
            "1500.00",
            "500.00",
            "1000.00",
        ),  # rent reversal re-debits bank; draft's 999 absent
        "4000": ("0.00", "1000.30", "-1000.30"),
        "6000": ("500.00", "500.00", "0.00"),  # rent and its reversal net to zero
    }
    assert report["date_from"] is None and report["date_to"] is None


def test_trial_balance_date_filters(client, ledger):
    january = client.get("/api/v1/reports/trial-balance?from=2026-01-01&to=2026-01-31").json()
    assert january["is_balanced"] is True
    assert january["total_debit"] == "1000.30"
    assert _rows(january)["1100"] == ("500.00", "500.00", "0.00")
    assert _rows(january)["6000"] == ("500.00", "500.00", "0.00")

    before_reversal = client.get("/api/v1/reports/trial-balance?to=2026-01-24").json()
    assert before_reversal["is_balanced"] is True
    assert _rows(before_reversal)["6000"] == ("500.00", "0.00", "500.00")

    february = client.get("/api/v1/reports/trial-balance?from=2026-02-01").json()
    assert february["total_debit"] == "1000.00"
    assert _rows(february)["1000"] == ("0.00", "0.00", "0.00")  # all accounts listed

    nothing = client.get("/api/v1/reports/trial-balance?from=2027-01-01").json()
    assert nothing["total_debit"] == nothing["total_credit"] == "0.00"
    assert nothing["is_balanced"] is True

    assert (
        client.get("/api/v1/reports/trial-balance?from=2026-02-01&to=2026-01-01").status_code == 422
    )
    assert client.get("/api/v1/reports/trial-balance?from=yesterday").status_code == 422


def test_trial_balance_ignores_drafts_entirely(client):
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")
    _entry(
        client,
        "2026-01-10",
        [{"account_id": cash["id"], "debit": "1.00"}, {"account_id": sales["id"]}],
        post=False,
    )
    report = client.get("/api/v1/reports/trial-balance").json()
    assert report["total_debit"] == report["total_credit"] == "0.00"
    assert all(row["debit"] == "0.00" and row["credit"] == "0.00" for row in report["rows"])


def test_general_ledger_detail(client, ledger):
    bank = ledger["bank"]
    resp = client.get(f"/api/v1/reports/general-ledger?account_id={bank['id']}")
    assert resp.status_code == 200
    report = resp.json()
    assert [a["code"] for a in report["accounts"]] == ["1100"]
    section = report["accounts"][0]
    assert section["opening_balance"] == "0.00"
    assert [
        (
            line["entry_date"],
            line["debit"],
            line["credit"],
            line["balance"],
            line["reverses_entry_id"],
        )
        for line in section["lines"]
    ] == [
        ("2026-01-20", "0.00", "500.00", "-500.00", None),
        ("2026-01-25", "500.00", "0.00", "0.00", ledger["rent_paid"]["id"]),
        ("2026-02-05", "1000.00", "0.00", "1000.00", None),
    ]
    assert section["lines"][0]["reversed_by_entry_id"] == ledger["reversal"]["id"]
    assert section["lines"][0]["description"] == "Rent"
    assert (section["total_debit"], section["total_credit"], section["closing_balance"]) == (
        "1500.00",
        "500.00",
        "1000.00",
    )


def test_general_ledger_opening_balance_and_filters(client, ledger):
    bank = ledger["bank"]
    resp = client.get(
        f"/api/v1/reports/general-ledger?account_id={bank['id']}&from=2026-02-01&to=2026-02-28"
    )
    section = resp.json()["accounts"][0]
    assert section["opening_balance"] == "0.00"  # -500 + 500 before February
    assert len(section["lines"]) == 1
    assert section["closing_balance"] == "1000.00"

    resp = client.get(f"/api/v1/reports/general-ledger?account_id={bank['id']}&from=2026-01-21")
    section = resp.json()["accounts"][0]
    assert section["opening_balance"] == "-500.00"
    assert [line["balance"] for line in section["lines"]] == ["0.00", "1000.00"]
    assert section["closing_balance"] == "1000.00"

    everything = client.get("/api/v1/reports/general-ledger").json()
    assert [a["code"] for a in everything["accounts"]] == ["1000", "1100", "4000", "6000"]
    cash = everything["accounts"][0]
    assert [line["balance"] for line in cash["lines"]] == ["0.10", "0.30"]  # exact cents
    assert all(
        "Draft" != line["description"] for acc in everything["accounts"] for line in acc["lines"]
    )

    assert client.get("/api/v1/reports/general-ledger?account_id=999").status_code == 404
    assert (
        client.get("/api/v1/reports/general-ledger?from=2026-02-01&to=2026-01-01").status_code
        == 422
    )


def test_services_compute_in_decimal(client, ledger):
    with Session(client.app.state.engine) as db:
        tb = trial_balance(db, date(2026, 1, 1), date(2026, 1, 31))
        assert isinstance(tb.total_debit, Decimal)
        assert tb.total_debit == Decimal("1000.30")
        assert all(isinstance(row.balance, Decimal) for row in tb.rows)
        assert tb.is_balanced

        gl = general_ledger(db, ledger["cash"]["id"])
        section = gl.accounts[0]
        assert isinstance(section.closing_balance, Decimal)
        assert section.closing_balance == Decimal("0.30")
        assert [line.balance for line in section.lines] == [Decimal("0.10"), Decimal("0.30")]
