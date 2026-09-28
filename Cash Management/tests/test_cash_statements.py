"""Bank statement import: idempotent, duplicate-free, never touches the ledger."""

import pytest

from tests.cash_helpers import (
    account,
    cash_account,
    cash_in,
    import_statement,
    ledger_snapshot,
    statement_payload,
)


@pytest.fixture
def setup(client):
    ledger = account(client, "1000", "Bank", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    ca = cash_account(client, ledger["id"])
    return {"ledger": ledger, "sales": sales, "cash": ca}


JAN = [
    {"date": "2026-01-05", "amount": "100.00", "description": "Customer A", "reference": "TX1"},
    {"date": "2026-01-10", "amount": "-40.50", "description": "Supplier B", "reference": "TX2"},
    {"date": "2026-01-20", "amount": "-9.99", "description": "Bank fee"},
]


def test_import_keeps_reference_date_amount_description(client, setup):
    payload = statement_payload(setup["cash"]["id"], "2026-01", "2026-01-01", "2026-01-31", "0", JAN)
    result = import_statement(client, payload)

    assert result["created"] is True
    assert result["imported_count"] == 3
    assert result["skipped_duplicates"] == []
    st = result["statement"]
    assert st["statement_reference"] == "2026-01"
    assert st["opening_balance"] == "0.00" and st["closing_balance"] == "49.51"

    txns = result["transactions"]
    assert [t["line_no"] for t in txns] == [1, 2, 3]
    assert [(t["reference"], t["transaction_date"], t["amount"], t["description"]) for t in txns] == [
        ("TX1", "2026-01-05", "100.00", "Customer A"),
        ("TX2", "2026-01-10", "-40.50", "Supplier B"),
        (None, "2026-01-20", "-9.99", "Bank fee"),
    ]
    assert all(t["status"] == "unmatched" and t["matched_amount"] == "0.00" for t in txns)

    detail = client.get(f"/api/v1/cash/bank-statements/{st['id']}").json()
    assert len(detail["transactions"]) == 3
    assert [s["id"] for s in client.get("/api/v1/cash/bank-statements").json()] == [st["id"]]


def test_importing_same_statement_twice_creates_nothing(client, setup):
    payload = statement_payload(setup["cash"]["id"], "2026-01", "2026-01-01", "2026-01-31", "0", JAN)
    first = import_statement(client, payload)
    second = import_statement(client, payload, expect=200)

    assert second["created"] is False
    assert second["imported_count"] == 0
    assert second["statement"]["id"] == first["statement"]["id"]
    assert len(client.get("/api/v1/cash/bank-statements").json()) == 1
    assert len(client.get("/api/v1/cash/bank-transactions").json()) == 3


def test_same_reference_with_different_content_is_rejected(client, setup):
    payload = statement_payload(setup["cash"]["id"], "2026-01", "2026-01-01", "2026-01-31", "0", JAN)
    import_statement(client, payload)

    changed = statement_payload(
        setup["cash"]["id"], "2026-01", "2026-01-01", "2026-01-31", "0", JAN[:2]
    )
    resp = client.post("/api/v1/cash/bank-statements", json=changed)
    assert resp.status_code == 409
    assert "different content" in resp.json()["detail"]
    assert len(client.get("/api/v1/cash/bank-transactions").json()) == 3


def test_overlapping_statement_skips_transactions_already_imported(client, setup):
    cash_id = setup["cash"]["id"]
    import_statement(
        client, statement_payload(cash_id, "W1", "2026-01-01", "2026-01-10", "0", JAN[:2])
    )
    # A monthly statement repeating the weekly lines plus one new line.
    result = import_statement(
        client, statement_payload(cash_id, "M1", "2026-01-01", "2026-01-31", "0", JAN)
    )
    assert result["created"] is True
    assert result["imported_count"] == 1
    assert [d["reference"] for d in result["skipped_duplicates"]] == ["TX1", "TX2"]
    assert all("existing_transaction_id" in d for d in result["skipped_duplicates"])
    assert [t["description"] for t in result["transactions"]] == ["Bank fee"]
    assert len(client.get("/api/v1/cash/bank-transactions").json()) == 3


def test_identical_lines_within_one_statement_are_both_kept(client, setup):
    twice = [
        {"date": "2026-01-05", "amount": "-3.00", "description": "Coffee"},
        {"date": "2026-01-05", "amount": "-3.00", "description": "Coffee"},
    ]
    result = import_statement(
        client, statement_payload(setup["cash"]["id"], "S", "2026-01-01", "2026-01-31", "10", twice)
    )
    assert result["imported_count"] == 2
    # ...and still deduplicated when the same two lines come again in another statement.
    again = import_statement(
        client,
        statement_payload(setup["cash"]["id"], "S2", "2026-01-01", "2026-01-31", "10", twice),
    )
    assert again["imported_count"] == 0 and len(again["skipped_duplicates"]) == 2


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda p: p.update(closing_balance="1.00"), "does not foot"),
        (lambda p: p["transactions"][0].update(transaction_date="2025-12-31"), "outside the statement period"),
        (lambda p: p["transactions"].append({"transaction_date": "2026-01-05", "amount": "0", "description": "info"}), "zero amount"),
        (lambda p: p.update(period_end="2025-12-01"), "period_end precedes"),
    ],
)
def test_invalid_statements_are_rejected_without_side_effects(client, setup, mutate, fragment):
    payload = statement_payload(setup["cash"]["id"], "BAD", "2026-01-01", "2026-01-31", "0", JAN)
    mutate(payload)
    resp = client.post("/api/v1/cash/bank-statements", json=payload)
    assert resp.status_code == 422, resp.text
    assert fragment in resp.json()["detail"]
    assert client.get("/api/v1/cash/bank-statements").json() == []
    assert client.get("/api/v1/cash/bank-transactions").json() == []


def test_sub_cent_amounts_rejected_by_validation(client, setup):
    payload = statement_payload(setup["cash"]["id"], "X", "2026-01-01", "2026-01-31", "0", [])
    payload["transactions"] = [
        {"transaction_date": "2026-01-05", "amount": "0.001", "description": "tiny"}
    ]
    assert client.post("/api/v1/cash/bank-statements", json=payload).status_code == 422


def test_unknown_cash_account(client):
    payload = statement_payload(999, "X", "2026-01-01", "2026-01-31", "0", [])
    assert client.post("/api/v1/cash/bank-statements", json=payload).status_code == 404


def test_import_never_posts_or_changes_balances(client, setup):
    cash_in(client, "2026-01-02", setup["ledger"]["id"], setup["sales"]["id"], "10.00")
    before = ledger_snapshot(client)
    balance_before = client.get(f"/api/v1/cash-accounts/{setup['cash']['id']}/balance").json()

    import_statement(
        client, statement_payload(setup["cash"]["id"], "2026-01", "2026-01-01", "2026-01-31", "0", JAN)
    )

    assert ledger_snapshot(client) == before
    after = client.get(f"/api/v1/cash-accounts/{setup['cash']['id']}/balance").json()
    assert after["balance"] == balance_before["balance"] == "10.00"
    # Unmatched bank transactions are not cash: the balances report still says 10.00.
    assert client.get("/api/v1/cash/reports/balances").json()["consolidated_balance"] == "10.00"


def test_transaction_listing_filters(client, setup):
    cash_id = setup["cash"]["id"]
    import_statement(client, statement_payload(cash_id, "2026-01", "2026-01-01", "2026-01-31", "0", JAN))
    rows = client.get(
        "/api/v1/cash/bank-transactions",
        params={"cash_account_id": cash_id, "date_from": "2026-01-10", "date_to": "2026-01-31"},
    ).json()
    assert [r["reference"] for r in rows] == ["TX2", None]
    assert client.get("/api/v1/cash/bank-transactions", params={"status": "matched"}).json() == []
    assert client.get("/api/v1/cash/bank-transactions", params={"status": "bogus"}).status_code == 422


def test_delete_statement_removes_its_transactions(client, setup):
    cash_id = setup["cash"]["id"]
    st = import_statement(client, statement_payload(cash_id, "2026-01", "2026-01-01", "2026-01-31", "0", JAN))
    assert client.delete(f"/api/v1/cash/bank-statements/{st['statement']['id']}").status_code == 204
    assert client.get("/api/v1/cash/bank-transactions").json() == []
    assert client.get(f"/api/v1/cash/bank-statements/{st['statement']['id']}").status_code == 404
    # ...and the same statement can be imported again afterwards.
    assert import_statement(client, statement_payload(cash_id, "2026-01", "2026-01-01", "2026-01-31", "0", JAN))["created"]
