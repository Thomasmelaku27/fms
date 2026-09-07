"""Draft editing/deletion, posted-entry protection via the API, reversals, PATCH account."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.services.journal import EntryAlreadyReversed, EntryNotPosted, delete_draft, reverse_entry
from app.services.posting import EntryAlreadyPosted


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _payload(cash, sales, debit="250.00", credit="250.00", description="Cash sale", **extra):
    return {
        "entry_date": "2026-01-15",
        "description": description,
        "lines": [
            {"account_id": cash["id"], "debit": debit},
            {"account_id": sales["id"], "credit": credit},
        ],
        **extra,
    }


def _update(client, entry, payload):
    """PUT with the optimistic-locking version taken from the entry as last seen."""
    return client.put(
        f"/api/v1/journal-entries/{entry['id']}", json={**payload, "version": entry["version"]}
    )


@pytest.fixture
def accounts(client):
    return _account(client, "1000", "Cash", "asset"), _account(client, "4000", "Sales", "revenue")


@pytest.fixture
def draft(client, accounts):
    cash, sales = accounts
    resp = client.post("/api/v1/journal-entries", json=_payload(cash, sales))
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
def posted(client, draft):
    resp = client.post(f"/api/v1/journal-entries/{draft['id']}/post")
    assert resp.status_code == 200
    return resp.json()


# --- Draft editing ---------------------------------------------------------------


def test_update_draft_replaces_header_and_lines(client, accounts, draft):
    cash, sales = accounts
    bank = _account(client, "1100", "Bank", "asset")
    resp = _update(
        client,
        draft,
        {
            "entry_date": "2026-01-20",
            "description": "Bank sale",
            "reference": "INV-7",
            "lines": [
                {"account_id": bank["id"], "debit": "100.00", "memo": "deposit"},
                {"account_id": sales["id"], "credit": "60.00"},
                {"account_id": sales["id"]},  # amount still to be decided
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == draft["version"] + 1
    assert (body["entry_date"], body["description"], body["reference"]) == (
        "2026-01-20",
        "Bank sale",
        "INV-7",
    )
    assert [(line["account_id"], line["debit"], line["credit"]) for line in body["lines"]] == [
        (bank["id"], "100.00", "0.00"),
        (sales["id"], "0.00", "60.00"),
        (sales["id"], "0.00", "0.00"),
    ]
    assert body["status"] == "draft"

    fetched = client.get(f"/api/v1/journal-entries/{draft['id']}").json()
    assert fetched == body
    # Old lines are gone, not orphaned.
    assert {line["id"] for line in draft["lines"]}.isdisjoint(line["id"] for line in body["lines"])


def test_update_draft_with_unknown_account(client, draft):
    payload = {
        "entry_date": "2026-01-20",
        "description": "x",
        "lines": [{"account_id": 999, "debit": "1.00"}],
    }
    resp = _update(client, draft, payload)
    assert resp.status_code == 422
    assert "Unknown account ids: [999]" in resp.json()["detail"]


def test_update_draft_with_negative_amount(client, accounts, draft):
    cash, sales = accounts
    resp = _update(client, draft, _payload(cash, sales, debit="-1.00"))
    assert resp.status_code == 422


def test_update_then_post_updated_lines(client, accounts, draft):
    cash, sales = accounts
    assert _update(client, draft, _payload(cash, sales, "80.00", "80.00")).status_code == 200
    resp = client.post(f"/api/v1/journal-entries/{draft['id']}/post")
    assert resp.status_code == 200
    assert [line["debit"] for line in resp.json()["lines"]] == ["80.00", "0.00"]


def test_delete_draft(client, draft):
    assert client.delete(f"/api/v1/journal-entries/{draft['id']}").status_code == 204
    assert client.get(f"/api/v1/journal-entries/{draft['id']}").status_code == 404
    assert client.delete(f"/api/v1/journal-entries/{draft['id']}").status_code == 404


def test_delete_draft_removes_its_lines(client, draft):
    client.delete(f"/api/v1/journal-entries/{draft['id']}")
    with Session(client.app.state.engine) as db:
        count = db.execute(
            text("SELECT COUNT(*) FROM journal_lines WHERE entry_id = :id"), {"id": draft["id"]}
        ).scalar_one()
    assert count == 0


def test_update_and_delete_missing_entry(client, accounts):
    cash, sales = accounts
    payload = {**_payload(cash, sales), "version": 1}
    assert client.put("/api/v1/journal-entries/999", json=payload).status_code == 404
    assert client.delete("/api/v1/journal-entries/999").status_code == 404


# --- Posted entries are protected ---------------------------------------------------


def test_modifying_posted_entry_is_conflict(client, accounts, posted):
    cash, sales = accounts
    entry_id = posted["id"]

    resp = _update(client, posted, _payload(cash, sales, "1.00", "1.00"))
    assert resp.status_code == 409
    assert "already posted" in resp.json()["detail"]

    resp = client.delete(f"/api/v1/journal-entries/{entry_id}")
    assert resp.status_code == 409

    # Repeating the requests keeps answering 409 and nothing changed.
    assert _update(client, posted, _payload(cash, sales)).status_code == 409
    assert client.delete(f"/api/v1/journal-entries/{entry_id}").status_code == 409
    assert client.get(f"/api/v1/journal-entries/{entry_id}").json() == posted


# --- Reversals --------------------------------------------------------------------------


def test_reverse_posted_entry(client, accounts, posted):
    cash, sales = accounts
    resp = client.post(
        f"/api/v1/journal-entries/{posted['id']}/reverse",
        json={"reversal_date": "2026-01-31", "description": "Customer returned goods"},
    )
    assert resp.status_code == 201, resp.text
    reversal = resp.json()
    assert reversal["status"] == "posted"
    assert reversal["reverses_entry_id"] == posted["id"]
    assert reversal["reversed_by_entry_id"] is None
    assert reversal["entry_date"] == "2026-01-31"
    assert reversal["description"] == "Customer returned goods"
    assert [(line["account_id"], line["debit"], line["credit"]) for line in reversal["lines"]] == [
        (cash["id"], "0.00", "250.00"),
        (sales["id"], "250.00", "0.00"),
    ]

    # Audit history: the original is untouched, still posted, and linked both ways.
    original = client.get(f"/api/v1/journal-entries/{posted['id']}").json()
    assert original["status"] == "posted"
    assert original["lines"] == posted["lines"]
    assert original["reversed_by_entry_id"] == reversal["id"]
    assert original["reverses_entry_id"] is None
    ids = [entry["id"] for entry in client.get("/api/v1/journal-entries").json()]
    assert ids == [posted["id"], reversal["id"]]


def test_reverse_defaults(client, posted):
    resp = client.post(f"/api/v1/journal-entries/{posted['id']}/reverse")
    assert resp.status_code == 201
    assert resp.json()["entry_date"] == date.today().isoformat()
    assert resp.json()["description"] == f"Reversal of journal entry {posted['id']}: Cash sale"
    assert resp.json()["reference"] == posted["reference"]


def test_reverse_twice_is_conflict(client, posted):
    first = client.post(f"/api/v1/journal-entries/{posted['id']}/reverse")
    assert first.status_code == 201

    second = client.post(f"/api/v1/journal-entries/{posted['id']}/reverse")
    assert second.status_code == 409
    assert f"already reversed by entry {first.json()['id']}" in second.json()["detail"]
    assert len(client.get("/api/v1/journal-entries").json()) == 2


def test_reverse_draft_is_conflict(client, draft):
    resp = client.post(f"/api/v1/journal-entries/{draft['id']}/reverse")
    assert resp.status_code == 409
    assert "is not posted" in resp.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{draft['id']}").json()["status"] == "draft"


def test_reverse_missing_entry(client):
    assert client.post("/api/v1/journal-entries/999/reverse").status_code == 404


def test_reversal_is_itself_protected_and_reversible(client, posted):
    reversal = client.post(f"/api/v1/journal-entries/{posted['id']}/reverse").json()

    assert client.delete(f"/api/v1/journal-entries/{reversal['id']}").status_code == 409
    assert client.post(f"/api/v1/journal-entries/{reversal['id']}/post").status_code == 409

    # Reversing the reversal re-books the original amounts; the chain is preserved.
    again = client.post(f"/api/v1/journal-entries/{reversal['id']}/reverse")
    assert again.status_code == 201
    assert again.json()["reverses_entry_id"] == reversal["id"]
    assert again.json()["lines"][0]["debit"] == "250.00"


def test_reversal_with_inactive_account_is_rejected(client, accounts, posted):
    cash, _ = accounts
    client.post(f"/api/v1/accounts/{cash['id']}/deactivate")

    resp = client.post(f"/api/v1/journal-entries/{posted['id']}/reverse")
    assert resp.status_code == 422
    assert "inactive accounts: 1000" in resp.json()["detail"]
    # Nothing was left behind and the original is still reversible.
    assert len(client.get("/api/v1/journal-entries").json()) == 1
    assert (
        client.get(f"/api/v1/journal-entries/{posted['id']}").json()["reversed_by_entry_id"] is None
    )


def test_reverse_service_errors(client, accounts, draft):
    with Session(client.app.state.engine, expire_on_commit=False) as db:
        entry = db.get(JournalEntry, draft["id"])
        with pytest.raises(EntryNotPosted):
            reverse_entry(db, entry)
        db.rollback()  # release the SQLite write lock before the API request below

        client.post(f"/api/v1/journal-entries/{draft['id']}/post")
        db.expire_all()
        reversal = reverse_entry(db, entry, reversal_date=date(2026, 1, 31))
        assert reversal.reverses_entry_id == entry.id
        assert reversal.lines[0].credit == Decimal("250.00")

        with pytest.raises(EntryAlreadyReversed):
            reverse_entry(db, entry)
        with pytest.raises(EntryAlreadyPosted):  # the posted reversal is protected too
            delete_draft(db, reversal)


# --- PATCH /accounts/{id} ------------------------------------------------------------------


def test_patch_account_is_active(client):
    account = client.post(
        "/api/v1/accounts", json={"code": "1000", "name": "Cash", "type": "asset"}
    ).json()

    resp = client.patch(f"/api/v1/accounts/{account['id']}", json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Existing action endpoints keep working alongside PATCH.
    assert client.post(f"/api/v1/accounts/{account['id']}/activate").json()["is_active"] is True
    assert (
        client.patch(f"/api/v1/accounts/{account['id']}", json={"name": "Petty cash"}).json()[
            "name"
        ]
        == "Petty cash"
    )
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["is_active"] is True

    assert client.patch("/api/v1/accounts/999", json={"is_active": False}).status_code == 404
    assert client.patch(f"/api/v1/accounts/{account['id']}", json={"name": ""}).status_code == 422
