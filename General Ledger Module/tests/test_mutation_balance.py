"""Mutation test: what happens if the posting service's balance check is disabled?

The check lives in app.services.posting.balance_problem. This test switches it
off for one request and asserts two things:

1. the assertion that test_posting.py::test_post_unbalanced_entry makes about the
   service diagnostic FAILS under the mutation (i.e. the suite would catch it);
2. the database trigger still refuses the unbalanced posting, so even a
   regression in the service cannot produce an unbalanced posted entry.
"""

import pytest

from app.services import posting


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


@pytest.fixture
def unbalanced(client):
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")
    resp = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Unbalanced",
            "lines": [
                {"account_id": cash["id"], "debit": "100.00"},
                {"account_id": sales["id"], "credit": "60.00"},
            ],
        },
    )
    return resp.json()


def _assert_service_reports_imbalance(resp):
    """The assertion test_post_unbalanced_entry relies on."""
    assert resp.status_code == 422
    assert "debits (100.00) do not equal credits (60.00)" in resp.json()["detail"]


def test_balance_check_is_live(client, unbalanced):
    resp = client.post(f"/api/v1/journal-entries/{unbalanced['id']}/post")
    _assert_service_reports_imbalance(resp)


def test_disabled_balance_check_is_detected_and_backstopped(client, unbalanced, monkeypatch):
    monkeypatch.setattr(posting, "balance_problem", lambda entry: None)  # the mutation

    resp = client.post(f"/api/v1/journal-entries/{unbalanced['id']}/post")

    with pytest.raises(AssertionError):  # 1. the regular test would fail
        _assert_service_reports_imbalance(resp)
    # 2. the database backstop still holds: no unbalanced entry was posted
    assert resp.status_code == 422
    assert "must have lines that balance" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{unbalanced['id']}").json()["status"] == "draft"
    tb = client.get("/api/v1/reports/trial-balance").json()
    assert tb["total_debit"] == tb["total_credit"] == "0.00"
