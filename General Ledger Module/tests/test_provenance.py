"""Draft edits preserve provenance; reclassification is explicit and limited."""

import pytest
from sqlalchemy.orm import Session

from app.models import EntryType, JournalEntry
from app.services.journal import NotReclassifiable, ProvenanceChange, reclassify_draft
from tests.helpers import account, draft, post


@pytest.fixture
def accounts(client):
    return account(client, "1000", "Cash", "asset"), account(client, "4000", "Sales", "revenue")


def _put(client, entry, **changes):
    body = {
        "entry_date": entry["entry_date"],
        "description": entry["description"],
        "reference": entry["reference"],
        "version": entry["version"],
        "lines": [
            {"account_id": line["account_id"], "debit": line["debit"], "credit": line["credit"]}
            for line in entry["lines"]
        ],
        **changes,
    }
    return client.put(f"/api/v1/journal-entries/{entry['id']}", json=body)


def _recurring_draft(client, accounts):
    cash, sales = accounts
    template = client.post(
        "/api/v1/recurring-templates",
        json={
            "name": "Monthly",
            "description": "Monthly",
            "frequency": "monthly",
            "start_date": "2026-01-31",
            "lines": [
                {"account_id": cash["id"], "debit": "10.00"},
                {"account_id": sales["id"], "credit": "10.00"},
            ],
        },
    ).json()
    return client.post(
        f"/api/v1/recurring-templates/{template['id']}/generate", json={"until": "2026-01-31"}
    ).json()[0]


def _sourced_draft(client, accounts):
    cash, sales = accounts
    resp = client.post(
        "/api/v1/journal-processing/entries",
        json={
            "source_type": "ap_invoice",
            "source_id": "INV-1",
            "operation_id": "post",
            "entry_date": "2026-01-15",
            "description": "Invoice",
            "post": False,
            "lines": [
                {"account_id": cash["id"], "debit": "10.00"},
                {"account_id": sales["id"], "credit": "10.00"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["entry"]


def test_editing_recurring_draft_keeps_its_origin(client, accounts):
    cash, sales = accounts
    entry = _recurring_draft(client, accounts)
    edited = _put(
        client,
        entry,
        description="Reviewed",
        lines=[
            {"account_id": cash["id"], "debit": "12.00"},
            {"account_id": sales["id"], "credit": "12.00"},
        ],
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["description"] == "Reviewed" and body["lines"][0]["debit"] == "12.00"
    assert body["entry_type"] == "recurring"
    assert body["recurring_template_id"] == entry["recurring_template_id"]
    assert body["scheduled_for"] == entry["scheduled_for"]

    # Naming the same type is fine; naming another type is refused, not silently applied.
    assert _put(client, body, entry_type="recurring").status_code == 200
    body = client.get(f"/api/v1/journal-entries/{entry['id']}").json()
    refused = _put(client, body, entry_type="standard")
    assert refused.status_code == 422
    assert "Use the reclassify operation" in refused.json()["detail"]
    assert client.get(f"/api/v1/journal-entries/{entry['id']}").json()["entry_type"] == "recurring"


def test_editing_sourced_draft_keeps_source_reference(client, accounts):
    entry = _sourced_draft(client, accounts)
    assert (entry["source_type"], entry["source_id"], entry["operation_id"]) == (
        "ap_invoice",
        "INV-1",
        "post",
    )
    edited = _put(client, entry, description="Touched by a user").json()
    assert (edited["source_type"], edited["source_id"], edited["operation_id"]) == (
        "ap_invoice",
        "INV-1",
        "post",
    )
    posted = post(client, entry["id"])
    assert posted["source_id"] == "INV-1"
    with Session(client.app.state.engine) as db:
        row = db.get(JournalEntry, entry["id"])
        assert row.request_fingerprint is not None  # fingerprint survives edit and posting


def test_reclassification_rules(client, accounts):
    cash, sales = accounts
    standard = draft(
        client,
        [
            {"account_id": cash["id"], "debit": "5.00"},
            {"account_id": sales["id"], "credit": "5.00"},
        ],
    )
    url = f"/api/v1/journal-entries/{standard['id']}/reclassify"
    stale = client.post(url, json={"entry_type": "adjusting", "version": 99})
    assert stale.status_code == 409
    ok = client.post(url, json={"entry_type": "adjusting", "version": standard["version"]})
    assert ok.status_code == 200 and ok.json()["entry_type"] == "adjusting"
    assert ok.json()["version"] == standard["version"] + 1
    back = client.post(url, json={"entry_type": "standard", "version": ok.json()["version"]})
    assert back.status_code == 200 and back.json()["entry_type"] == "standard"
    assert client.post(url, json={"entry_type": "reversal", "version": 3}).status_code == 422
    assert client.post(url, json={"entry_type": "recurring", "version": 3}).status_code == 422

    # System-created drafts keep their classification.
    recurring = _recurring_draft(client, accounts)
    refused = client.post(
        f"/api/v1/journal-entries/{recurring['id']}/reclassify",
        json={"entry_type": "adjusting", "version": recurring["version"]},
    )
    assert refused.status_code == 422
    assert "created by the system" in refused.json()["detail"]
    sourced = _sourced_draft(client, accounts)
    assert (
        client.post(
            f"/api/v1/journal-entries/{sourced['id']}/reclassify",
            json={"entry_type": "adjusting", "version": sourced["version"]},
        ).status_code
        == 422
    )

    # Posted entries never change.
    post(client, standard["id"])
    posted = client.post(url, json={"entry_type": "adjusting", "version": 3})
    assert posted.status_code == 409


def test_reclassify_service_level(client, accounts):
    cash, sales = accounts
    entry = draft(
        client,
        [
            {"account_id": cash["id"], "debit": "5.00"},
            {"account_id": sales["id"], "credit": "5.00"},
        ],
    )
    with Session(client.app.state.engine, expire_on_commit=False) as db:
        row = db.get(JournalEntry, entry["id"])
        reclassify_draft(db, row, EntryType.ADJUSTING, row.version)
        assert row.entry_type == EntryType.ADJUSTING
        with pytest.raises(NotReclassifiable):
            reclassify_draft(db, row, EntryType.REVERSAL, row.version)
        row.source_type, row.source_id, row.operation_id = "x", "y", "z"
        with pytest.raises(NotReclassifiable, match="created by the system"):
            reclassify_draft(db, row, EntryType.STANDARD, row.version)
        db.rollback()


def test_put_payload_cannot_carry_system_fields(client, accounts):
    cash, sales = accounts
    entry = draft(
        client,
        [
            {"account_id": cash["id"], "debit": "5.00"},
            {"account_id": sales["id"], "credit": "5.00"},
        ],
    )
    for field, value in [
        ("recurring_template_id", 1),
        ("scheduled_for", "2026-01-31"),
        ("reverses_entry_id", 1),
        ("source_type", "ap"),
        ("status", "posted"),
    ]:
        resp = _put(client, entry, **{field: value})
        assert resp.status_code == 422, field
        assert "Extra inputs are not permitted" in resp.text


def test_provenance_change_error_type():
    entry = JournalEntry(id=7, entry_type=EntryType.RECURRING)
    err = ProvenanceChange(entry, EntryType.STANDARD)
    assert err.status_code == 422 and "recurring" in str(err)
