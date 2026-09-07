"""Hierarchical chart of accounts: structure rules, posting restrictions, safe changes."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.helpers import account, draft, post, trial_balance


@pytest.fixture
def chart(client):
    assets = account(client, "1", "Assets", "asset", is_summary=True, category="current_asset")
    current = account(
        client, "10", "Current assets", "asset", is_summary=True, parent_id=assets["id"]
    )
    cash = account(
        client, "1000", "Cash", "asset", parent_id=current["id"], category="current_asset"
    )
    bank = account(client, "1100", "Bank", "asset", parent_id=current["id"])
    revenue = account(client, "4", "Revenue", "revenue", is_summary=True)
    sales = account(client, "4000", "Sales", "revenue", parent_id=revenue["id"])
    return {
        "assets": assets,
        "current": current,
        "cash": cash,
        "bank": bank,
        "revenue": revenue,
        "sales": sales,
    }


def test_defaults_and_hierarchy_fields(client, chart):
    assert chart["cash"]["normal_balance"] == "debit"
    assert chart["sales"]["normal_balance"] == "credit"
    assert chart["cash"]["parent_id"] == chart["current"]["id"]
    assert chart["assets"]["is_summary"] is True and chart["cash"]["is_summary"] is False
    children = client.get(f"/api/v1/accounts?parent_id={chart['current']['id']}").json()
    assert [c["code"] for c in children] == ["1000", "1100"]
    descendants = client.get(f"/api/v1/accounts/{chart['assets']['id']}/descendants").json()
    assert [d["code"] for d in descendants] == ["10", "1000", "1100"]
    assert client.get("/api/v1/accounts/999/descendants").status_code == 404
    contra = account(
        client, "1200", "Allowance for doubtful accounts", "asset", normal_balance="credit"
    )
    assert contra["normal_balance"] == "credit"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"parent_id": "cash"}, "is not a summary account"),
        ({"parent_id": "revenue"}, "is of type revenue, not asset"),
        ({"category": "operating_expense"}, "belongs to expense accounts"),
        ({"parent_id": 999}, "Unknown parent account id"),
    ],
)
def test_invalid_structure_rejected(client, chart, body, fragment):
    if isinstance(body.get("parent_id"), str):
        body = {**body, "parent_id": chart[body["parent_id"]]["id"]}
    resp = client.post(
        "/api/v1/accounts", json={"code": "1999", "name": "X", "type": "asset", **body}
    )
    assert resp.status_code == 422, resp.text
    assert fragment in resp.json()["detail"]


def test_cycles_rejected(client, chart):
    # assets -> current -> cash; moving "assets" under "current" would create a cycle.
    resp = client.patch(
        f"/api/v1/accounts/{chart['assets']['id']}", json={"parent_id": chart["current"]["id"]}
    )
    assert resp.status_code == 422 and "cycle" in resp.json()["detail"]
    resp = client.patch(
        f"/api/v1/accounts/{chart['assets']['id']}", json={"parent_id": chart["assets"]["id"]}
    )
    assert resp.status_code == 422 and "cycle" in resp.json()["detail"]
    # Moving a subtree elsewhere is fine.
    other = account(
        client, "19", "Other assets", "asset", is_summary=True, parent_id=chart["assets"]["id"]
    )
    moved = client.patch(
        f"/api/v1/accounts/{chart['current']['id']}", json={"parent_id": other["id"]}
    )
    assert moved.status_code == 200 and moved.json()["parent_id"] == other["id"]
    assert [
        d["code"] for d in client.get(f"/api/v1/accounts/{other['id']}/descendants").json()
    ] == ["10", "1000", "1100"]


def test_summary_accounts_cannot_be_posted_to(client, chart):
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "x",
            "lines": [
                {"account_id": chart["assets"]["id"], "debit": "1.00"},
                {"account_id": chart["sales"]["id"], "credit": "1.00"},
            ],
        },
    )
    assert resp.status_code == 422  # database trigger, surfaced as 422 by the API
    assert "summary account" in resp.json()["detail"]

    # A draft on a posting account that later becomes... cannot become summary (history).
    entry = draft(
        client,
        [
            {"account_id": chart["cash"]["id"], "debit": "1.00"},
            {"account_id": chart["sales"]["id"], "credit": "1.00"},
        ],
    )
    refused = client.patch(f"/api/v1/accounts/{chart['cash']['id']}", json={"is_summary": True})
    assert refused.status_code == 409 and "has journal lines" in refused.json()["detail"]
    # Service-level posting check lists summary accounts explicitly (raw SQL can plant such a line).
    with Session(client.app.state.engine) as db:
        with pytest.raises(IntegrityError, match="summary account"):
            db.execute(
                text("UPDATE journal_lines SET account_id = :summary WHERE entry_id = :e"),
                {"summary": chart["assets"]["id"], "e": entry["id"]},
            )
        db.rollback()


def test_accounts_with_history_cannot_change_type_or_become_summary(client, chart):
    entry = draft(
        client,
        [
            {"account_id": chart["cash"]["id"], "debit": "1.00"},
            {"account_id": chart["sales"]["id"], "credit": "1.00"},
        ],
    )
    post(client, entry["id"])
    cash = chart["cash"]["id"]
    assert client.patch(f"/api/v1/accounts/{cash}", json={"type": "expense"}).status_code == 409
    assert client.patch(f"/api/v1/accounts/{cash}", json={"is_summary": True}).status_code == 409
    # Safe changes still work: rename, deactivate. Category and parent changes of an
    # account with history need an effective-dated reclassification; normal balance never changes.
    assert client.patch(f"/api/v1/accounts/{cash}", json={"name": "Petty cash"}).status_code == 200
    assert (
        client.patch(f"/api/v1/accounts/{cash}", json={"category": "other_asset"}).status_code
        == 409
    )
    assert (
        client.patch(f"/api/v1/accounts/{cash}", json={"normal_balance": "credit"}).status_code
        == 409
    )
    assert (
        client.patch(f"/api/v1/accounts/{cash}", json={"is_active": False}).json()["is_active"]
        is False
    )
    refused = client.patch(f"/api/v1/accounts/{cash}", json={"parent_id": chart["assets"]["id"]})
    assert refused.status_code == 409 and "reclassification" in refused.json()["detail"]
    moved = client.post(
        f"/api/v1/accounts/{cash}/reclassify",
        json={
            "effective_from": "2026-02-01",
            "reason": "restructure",
            "parent_id": chart["assets"]["id"],
            "change_parent": True,
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["parent_id"] == chart["assets"]["id"]
    # Database backstop for direct SQL.
    with Session(client.app.state.engine) as db:
        # Either guard may fire first (history, or parent/type consistency); both refuse.
        with pytest.raises(
            IntegrityError, match="journal lines cannot change|summary account of the same type"
        ):
            db.execute(text("UPDATE accounts SET type = 'expense' WHERE id = :id"), {"id": cash})
        db.rollback()
        with pytest.raises(IntegrityError, match="journal lines cannot change"):
            db.execute(
                text("UPDATE accounts SET is_summary = 1 WHERE id = :id")
                if db.bind.dialect.name == "sqlite"
                else text("UPDATE accounts SET is_summary = true WHERE id = :id"),
                {"id": cash},
            )
        db.rollback()
        with pytest.raises(  # reclassification-record guard or parent guard, both refuse
            IntegrityError, match="summary account of the same type|reclassification record"
        ):
            db.execute(
                text("UPDATE accounts SET parent_id = :p WHERE id = :id"),
                {"p": chart["revenue"]["id"], "id": cash},
            )
        db.rollback()


def test_summary_with_children_keeps_role_and_type(client, chart):
    current = chart["current"]["id"]
    assert (
        client.patch(f"/api/v1/accounts/{current}", json={"is_summary": False}).status_code == 409
    )
    assert (
        client.patch(f"/api/v1/accounts/{current}", json={"type": "liability"}).status_code == 409
    )
    # A childless, history-free account may freely change.
    lonely = account(client, "1500", "Prepaid", "asset")
    assert (
        client.patch(f"/api/v1/accounts/{lonely['id']}", json={"type": "expense"}).json()[
            "normal_balance"
        ]
        == "debit"
    )
    assert (
        client.patch(f"/api/v1/accounts/{lonely['id']}", json={"type": "liability"}).json()[
            "normal_balance"
        ]
        == "credit"
    )
    assert (
        client.patch(f"/api/v1/accounts/{lonely['id']}", json={"is_summary": True}).json()[
            "is_summary"
        ]
        is True
    )


def test_trial_balance_rolls_up_summary_accounts(client, chart):
    entry = draft(
        client,
        [
            {"account_id": chart["cash"]["id"], "debit": "30.00"},
            {"account_id": chart["bank"]["id"], "debit": "70.00"},
            {"account_id": chart["sales"]["id"], "credit": "100.00"},
        ],
    )
    post(client, entry["id"])
    report = trial_balance(client)
    rows = {r["code"]: r for r in report["rows"]}
    assert rows["1"]["is_summary"] and rows["1"]["debit"] == "100.00"
    assert rows["10"]["debit"] == "100.00" and rows["10"]["balance"] == "100.00"
    assert rows["4"]["credit"] == "100.00" and rows["4"]["normal_balance_amount"] == "100.00"
    assert (
        rows["4000"]["balance"] == "-100.00" and rows["4000"]["normal_balance_amount"] == "100.00"
    )
    assert (
        report["total_debit"] == report["total_credit"] == "100.00"
    )  # summaries not double counted
    assert report["is_balanced"] is True


def test_legacy_flat_accounts_keep_working(client):
    """Accounts created without any hierarchy fields behave exactly as before."""
    cash = account(client, "1000", "Cash", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    assert cash["parent_id"] is None and cash["is_summary"] is False and cash["category"] is None
    entry = draft(
        client,
        [
            {"account_id": cash["id"], "debit": "1.00"},
            {"account_id": sales["id"], "credit": "1.00"},
        ],
    )
    assert post(client, entry["id"])["status"] == "posted"
    assert client.patch(f"/api/v1/accounts/{cash['id']}", json={"bogus": 1}).status_code == 422
