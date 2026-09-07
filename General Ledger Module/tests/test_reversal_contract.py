"""POST /journal-entries/{id}/reverse request contract."""

from datetime import date

import pytest


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


@pytest.fixture
def posted(client):
    cash = _account(client, "1000", "Cash", "asset")
    sales = _account(client, "4000", "Sales", "revenue")
    entry = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "Sale",
            "lines": [
                {"account_id": cash["id"], "debit": "10.00"},
                {"account_id": sales["id"], "credit": "10.00"},
            ],
        },
    ).json()
    return client.post(f"/api/v1/journal-entries/{entry['id']}/post").json()


def _reverse(client, entry, body=None):
    if body is None:
        return client.post(f"/api/v1/journal-entries/{entry['id']}/reverse")
    return client.post(f"/api/v1/journal-entries/{entry['id']}/reverse", json=body)


def test_reversal_date_is_used_exactly(client, posted):
    resp = _reverse(client, posted, {"reversal_date": "2026-03-31"})
    assert resp.status_code == 201
    assert resp.json()["entry_date"] == "2026-03-31"


def test_reversal_date_same_day_as_original_is_allowed(client, posted):
    resp = _reverse(client, posted, {"reversal_date": "2026-01-15"})
    assert resp.status_code == 201
    assert resp.json()["entry_date"] == "2026-01-15"


def test_omitted_date_defaults_to_today(client, posted):
    resp = _reverse(client, posted)
    assert resp.status_code == 201
    assert resp.json()["entry_date"] == date.today().isoformat()


def test_legacy_entry_date_alias_still_works(client, posted):
    resp = _reverse(client, posted, {"entry_date": "2026-02-01"})
    assert resp.status_code == 201
    assert resp.json()["entry_date"] == "2026-02-01"


def test_matching_alias_and_reversal_date_accepted(client, posted):
    resp = _reverse(client, posted, {"reversal_date": "2026-02-01", "entry_date": "2026-02-01"})
    assert resp.status_code == 201


def test_conflicting_dates_rejected(client, posted):
    resp = _reverse(client, posted, {"reversal_date": "2026-02-01", "entry_date": "2026-02-02"})
    assert resp.status_code == 422
    assert "conflict" in resp.text
    assert (
        client.get(f"/api/v1/journal-entries/{posted['id']}").json()["reversed_by_entry_id"] is None
    )


@pytest.mark.parametrize(
    "body",
    [
        {"date": "2026-02-01"},  # unknown field: previously silently ignored
        {"reversal_date": "2026-02-01", "reversalDate": "2026-02-01"},
        {"reversal_date": "2026-02-01", "lines": []},
    ],
)
def test_unknown_fields_rejected(client, posted, body):
    resp = _reverse(client, posted, body)
    assert resp.status_code == 422
    assert "Extra inputs are not permitted" in resp.text
    assert len(client.get("/api/v1/journal-entries").json()) == 1


@pytest.mark.parametrize("value", ["2026-02-30", "02/01/2026", "tomorrow", "", 20260201, None])
def test_invalid_dates_rejected(client, posted, value):
    resp = _reverse(client, posted, {"reversal_date": value})
    if value is None:  # explicit null == omitted
        assert resp.status_code == 201
    else:
        assert resp.status_code == 422, resp.text


def test_reversal_before_original_rejected(client, posted):
    resp = _reverse(client, posted, {"reversal_date": "2026-01-14"})
    assert resp.status_code == 422
    assert "before the entry date 2026-01-15" in resp.json()["detail"]
    assert (
        client.get(f"/api/v1/journal-entries/{posted['id']}").json()["reversed_by_entry_id"] is None
    )


def test_description_validation(client, posted):
    assert _reverse(client, posted, {"description": ""}).status_code == 422
    assert _reverse(client, posted, {"description": "x" * 501}).status_code == 422
    ok = _reverse(client, posted, {"description": "Corrected"})
    assert ok.status_code == 201 and ok.json()["description"] == "Corrected"
