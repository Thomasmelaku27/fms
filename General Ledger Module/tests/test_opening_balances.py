"""Opening-balance initialisation through the journal-processing contract."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.helpers import account, balances


@pytest.fixture
def chart(client):
    return {
        "cash": account(client, "1000", "Cash", "asset"),
        "receivables": account(client, "1200", "Receivables", "asset"),
        "payables": account(client, "2000", "Payables", "liability"),
        "equity": account(client, "3000", "Share capital", "equity"),
        "obe": account(client, "3999", "Opening balance equity", "equity"),
        "sales": account(client, "4000", "Sales", "revenue"),
    }


def _apply(client, chart, import_id="IMPORT-2026", as_of="2025-12-31", **overrides):
    body = {
        "import_id": import_id,
        "as_of": as_of,
        "balances": [
            {"account_id": chart["cash"]["id"], "debit": "5000.00"},
            {"account_id": chart["receivables"]["id"], "debit": "1500.00"},
            {"account_id": chart["payables"]["id"], "credit": "2500.00"},
            {"account_id": chart["equity"]["id"], "credit": "4000.00"},
        ],
        **overrides,
    }
    return client.post("/api/v1/opening-balances", json=body)


def test_apply_opening_balances(client, chart):
    resp = _apply(client, chart)
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert result["created"] is True and result["posted_now"] is True
    entry = result["entry"]
    assert entry["entry_type"] == "opening" and entry["status"] == "posted"
    assert entry["entry_date"] == "2025-12-31"
    assert (entry["source_type"], entry["source_id"], entry["operation_id"]) == (
        "opening_balance",
        "IMPORT-2026",
        "apply",
    )
    assert balances(client) == {
        "1000": "5000.00",
        "1200": "1500.00",
        "2000": "-2500.00",
        "3000": "-4000.00",
        "3999": "0.00",
        "4000": "0.00",
        "__total_debit__": "6500.00",
        "__total_credit__": "6500.00",
    }
    # Carried forward into the new year as opening balances.
    gl = client.get(
        f"/api/v1/reports/general-ledger?account_id={chart['cash']['id']}&from=2026-01-01"
    ).json()
    assert gl["accounts"][0]["opening_balance"] == "5000.00"


def test_same_import_cannot_be_applied_twice(client, chart):
    first = _apply(client, chart).json()
    snapshot = balances(client)
    for _ in range(2):
        again = _apply(client, chart)
        assert again.status_code == 201
        assert again.json()["created"] is False
        assert again.json()["entry"]["id"] == first["entry"]["id"]
    assert balances(client) == snapshot
    assert len(client.get("/api/v1/journal-entries").json()) == 1

    # Same import id, different balances: conflict, nothing written.
    changed = _apply(
        client,
        chart,
        balances=[
            {"account_id": chart["cash"]["id"], "debit": "5001.00"},
            {"account_id": chart["equity"]["id"], "credit": "5001.00"},
        ],
    )
    assert changed.status_code == 409
    assert "different accounting data" in changed.json()["detail"]
    assert balances(client) == snapshot


def test_unbalanced_import_needs_balancing_account(client, chart):
    unbalanced = [
        {"account_id": chart["cash"]["id"], "debit": "100.00"},
        {"account_id": chart["payables"]["id"], "credit": "30.00"},
    ]
    resp = _apply(client, chart, balances=unbalanced)
    assert resp.status_code == 422 and "provide a balancing account" in resp.json()["detail"]
    assert client.get("/api/v1/journal-entries").json() == []

    resp = _apply(client, chart, balances=unbalanced, balancing_account_id=chart["obe"]["id"])
    assert resp.status_code == 201, resp.text
    lines = {
        line["account_id"]: (line["debit"], line["credit"], line["memo"])
        for line in resp.json()["entry"]["lines"]
    }
    assert lines[chart["obe"]["id"]] == ("0.00", "70.00", "opening balance difference")
    assert balances(client)["3999"] == "-70.00"


@pytest.mark.parametrize(
    ("balances_body", "fragment"),
    [
        (
            [{"account_id": 1, "debit": "1.00"}, {"account_id": 1, "credit": "1.00"}],
            "appears twice",
        ),
        ([{"account_id": 1, "debit": "1.00", "credit": "1.00"}], "both a debit and a credit"),
        ([{"account_id": 1}], "all opening balances are zero"),
        ([{"account_id": 1, "debit": "-1.00"}], "greater than or equal"),
        ([{"account_id": 1, "debit": "1.001"}], "decimal"),
        (
            [{"account_id": 999, "debit": "1.00"}, {"account_id": 1, "credit": "1.00"}],
            "Unknown account ids",
        ),
    ],
)
def test_invalid_imports_rejected(client, chart, balances_body, fragment):
    resp = _apply(client, chart, balances=balances_body)
    assert resp.status_code == 422, resp.text
    assert fragment in resp.text


def test_opening_entry_is_a_protected_system_entry(client, chart):
    entry = _apply(client, chart).json()["entry"]
    resp = client.post(
        f"/api/v1/journal-entries/{entry['id']}/reverse", json={"reversal_date": "2026-01-01"}
    )
    assert resp.status_code == 409 and "cannot be reversed" in resp.json()["detail"]
    assert client.delete(f"/api/v1/journal-entries/{entry['id']}").status_code == 409
    with Session(client.app.state.engine) as db:
        with pytest.raises(IntegrityError, match="cannot be reversed"):
            db.execute(
                text(
                    "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                    "version, reverses_entry_id) VALUES ('2026-01-01', 'x', 'draft', 'reversal', "
                    "1, :id)"
                ),
                {"id": entry["id"]},
            )
        db.rollback()
    # Ordinary journals cannot masquerade as opening balances.
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-01",
            "description": "x",
            "entry_type": "opening",
            "lines": [{"account_id": chart["cash"]["id"]}],
        },
    )
    assert resp.status_code == 422


def test_opening_balances_respect_period_rules(client, chart):
    period = client.post(
        "/api/v1/periods",
        json={"name": "2025-12", "start_date": "2025-12-01", "end_date": "2025-12-31"},
    ).json()
    client.post(f"/api/v1/periods/{period['id']}/close")
    resp = _apply(client, chart)
    assert resp.status_code == 422 and "is closed" in resp.json()["detail"]
    assert client.get("/api/v1/journal-entries").json() == []
    assert _apply(client, chart, as_of="2026-01-01").status_code == 201
