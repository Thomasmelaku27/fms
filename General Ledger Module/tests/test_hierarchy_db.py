"""Account hierarchy integrity enforced by the database, plus effective-dated reclassification."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.helpers import account, draft, post


@pytest.fixture
def chart(client):
    a = account(client, "1", "Assets", "asset", is_summary=True)
    b = account(client, "10", "Current", "asset", is_summary=True, parent_id=a["id"])
    c = account(client, "100", "Cash group", "asset", is_summary=True, parent_id=b["id"])
    cash = account(client, "1000", "Cash", "asset", parent_id=c["id"], category="current_asset")
    sales = account(client, "4000", "Sales", "revenue")
    return {"a": a, "b": b, "c": c, "cash": cash, "sales": sales}


def _sql(client):
    return Session(client.app.state.engine)


@pytest.mark.parametrize("target", ["b", "c"])  # indirect cycles of different depth
def test_indirect_cycle_rejected_by_trigger(client, chart, target):
    with _sql(client) as db:
        with pytest.raises(IntegrityError, match="cannot contain a cycle"):
            db.execute(
                text("UPDATE accounts SET parent_id = :p WHERE id = :id"),
                {"p": chart[target]["id"], "id": chart["a"]["id"]},
            )
        db.rollback()
        with pytest.raises(IntegrityError, match="not_own_parent|cycle"):
            db.execute(
                text("UPDATE accounts SET parent_id = :id WHERE id = :id"), {"id": chart["b"]["id"]}
            )
        db.rollback()
        # Inserting a new summary whose parent is one of its own future descendants is impossible
        # by construction, but inserting under a valid summary works.
        db.execute(
            text(
                "INSERT INTO accounts (code, name, type, normal_balance, is_summary, parent_id, "
                "is_active) VALUES ('101', 'Petty', 'asset', 'debit', 0, :p, 1)"
                if db.get_bind().dialect.name == "sqlite"
                else "INSERT INTO accounts (code, name, type, normal_balance, is_summary, "
                "parent_id, is_active) VALUES ('101', 'Petty', 'asset', 'debit', false, :p, true)"
            ),
            {"p": chart["c"]["id"]},
        )
        db.commit()
    assert client.get("/api/v1/reconciliation").json()["ok"] is True


def test_category_type_consistency_is_a_check_constraint(client, chart):
    with _sql(client) as db:
        with pytest.raises(IntegrityError, match="category_type"):
            db.execute(
                text("UPDATE accounts SET category = 'operating_expense' WHERE id = :id"),
                {"id": chart["sales"]["id"]},
            )
        db.rollback()
        db.execute(
            text("UPDATE accounts SET category = 'operating_revenue' WHERE id = :id"),
            {"id": chart["sales"]["id"]},
        )
        db.commit()
        with pytest.raises(IntegrityError, match="category_type"):
            db.execute(
                text("UPDATE accounts SET type = 'expense' WHERE id = :id"),
                {"id": chart["sales"]["id"]},
            )  # the type moves away from the category's type (no parent guard in the way)
        db.rollback()


def test_history_protected_changes_at_database_level(client, chart):
    entry = draft(
        client,
        [
            {"account_id": chart["cash"]["id"], "debit": "5.00"},
            {"account_id": chart["sales"]["id"], "credit": "5.00"},
        ],
    )
    post(client, entry["id"])
    cash = chart["cash"]["id"]
    with _sql(client) as db:
        for sql, fragment in [
            ("UPDATE accounts SET normal_balance = 'credit' WHERE id = :id", "normal balance"),
            ("UPDATE accounts SET parent_id = :p WHERE id = :id", "reclassification record"),
            (
                "UPDATE accounts SET category = 'other_asset' WHERE id = :id",
                "reclassification record",
            ),
            (
                "UPDATE accounts SET is_summary = 1 WHERE id = :id"
                if db.get_bind().dialect.name == "sqlite"
                else "UPDATE accounts SET is_summary = true WHERE id = :id",
                "journal lines cannot change",
            ),
        ]:
            with pytest.raises(IntegrityError, match=fragment):
                db.execute(text(sql), {"id": cash, "p": chart["b"]["id"]})
            db.rollback()
        # Summary with children: role and type are frozen.
        with pytest.raises(IntegrityError, match="keeps its role and type"):
            db.execute(
                text(
                    "UPDATE accounts SET is_summary = 0 WHERE id = :id"
                    if db.get_bind().dialect.name == "sqlite"
                    else "UPDATE accounts SET is_summary = false WHERE id = :id"
                ),
                {"id": chart["c"]["id"]},
            )
        db.rollback()
        # With a matching reclassification record the same UPDATE is accepted (traceable change).
        db.execute(
            text(
                "INSERT INTO account_reclassifications (account_id, old_parent_id, new_parent_id, "
                "old_category, new_category, effective_from, reason, actor_ref) VALUES "
                "(:id, :old, :new, 'current_asset', 'current_asset', '2026-06-01', 'manual', 'dba')"
            ),
            {"id": cash, "old": chart["c"]["id"], "new": chart["b"]["id"]},
        )
        db.execute(
            text("UPDATE accounts SET parent_id = :p WHERE id = :id"),
            {"id": cash, "p": chart["b"]["id"]},
        )
        db.commit()
    assert client.get(f"/api/v1/accounts/{cash}").json()["parent_id"] == chart["b"]["id"]


def test_reclassification_api_records_and_reports_impact(client, chart):
    cash = chart["cash"]["id"]
    for day, amount in [("2026-01-10", "100.00"), ("2026-03-10", "40.00")]:
        post(
            client,
            draft(
                client,
                [
                    {"account_id": cash, "debit": amount},
                    {"account_id": chart["sales"]["id"], "credit": amount},
                ],
                entry_date=day,
            )["id"],
        )

    refused = client.post(
        f"/api/v1/accounts/{cash}/reclassify", json={"effective_from": "2026-02-01", "reason": "x"}
    )
    assert refused.status_code == 422  # nothing to change
    resp = client.post(
        f"/api/v1/accounts/{cash}/reclassify",
        json={
            "effective_from": "2026-02-01",
            "reason": "moved to current assets",
            "parent_id": chart["b"]["id"],
            "change_parent": True,
            "category": "other_asset",
            "change_category": True,
        },
        headers={"X-Actor-Ref": "controller"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_id"] == chart["b"]["id"] and resp.json()["category"] == "other_asset"

    records = client.get(f"/api/v1/accounts/{cash}/reclassifications").json()
    assert len(records) == 1
    assert (
        records[0]["old_parent_id"] == chart["c"]["id"]
        and records[0]["new_parent_id"] == chart["b"]["id"]
    )
    assert records[0]["actor_ref"] == "controller" and records[0]["effective_from"] == "2026-02-01"
    impact = client.get(f"/api/v1/accounts/{cash}/reclassifications/impact").json()
    assert impact[0]["balance_before_effective_date"] == "100.00"  # moved from group "100" to "10"
    assert impact[0]["activity_since_effective_date"] == "40.00"
    # Reports use the current structure: "100" no longer rolls up the cash history.
    rows = {r["code"]: r for r in client.get("/api/v1/reports/trial-balance").json()["rows"]}
    assert (
        rows["100"]["debit"] == "0.00"
        and rows["10"]["debit"] == "140.00"
        and rows["1"]["debit"] == "140.00"
    )
    # Invalid targets are refused and recorded nowhere.
    bad = client.post(
        f"/api/v1/accounts/{cash}/reclassify",
        json={
            "effective_from": "2026-02-01",
            "reason": "x",
            "parent_id": chart["sales"]["id"],
            "change_parent": True,
        },
    )
    assert bad.status_code == 422
    assert len(client.get(f"/api/v1/accounts/{cash}/reclassifications").json()) == 1


def test_reconciliation_detects_hierarchy_damage(client, chart):
    from tests.test_reconciliation import triggers_disabled

    with triggers_disabled(client.app.state.engine) as db:
        db.execute(
            text("UPDATE accounts SET parent_id = :p WHERE id = :id"),
            {"p": chart["c"]["id"], "id": chart["a"]["id"]},
        )  # cycle
        db.execute(
            text("UPDATE accounts SET parent_id = :p WHERE id = :id"),
            {"p": chart["cash"]["id"], "id": chart["sales"]["id"]},
        )  # under a posting account of another type
        db.commit()
    report = client.get("/api/v1/reconciliation").json()
    messages = [
        f["message"]
        for c in report["checks"]
        if c["name"] == "hierarchy_integrity"
        for f in c["findings"]
    ]
    assert any("hierarchy cycle" in m for m in messages)
    assert any("not a summary account of type revenue" in m for m in messages)
    assert any("posting account 1000 has child accounts" in m for m in messages)
    assert report["ok"] is False
