"""Recurring templates: schedules, duplicate-free generation, review and posting."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Frequency, RecurringTemplate
from app.services.recurring import add_months, generate, occurrences_between


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


@pytest.fixture
def accounts(client):
    return _account(client, "6000", "Rent", "expense"), _account(client, "1100", "Bank", "asset")


def _template_body(rent, bank, **overrides):
    return {
        "name": "Office rent",
        "description": "Monthly office rent",
        "reference": "LEASE-1",
        "frequency": "monthly",
        "start_date": "2026-01-31",
        "lines": [
            {"account_id": rent["id"], "debit": "1200.00", "memo": "rent"},
            {"account_id": bank["id"], "credit": "1200.00"},
        ],
        **overrides,
    }


@pytest.fixture
def template(client, accounts):
    rent, bank = accounts
    resp = client.post("/api/v1/recurring-templates", json=_template_body(rent, bank))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _generate(client, template, until):
    return client.post(
        f"/api/v1/recurring-templates/{template['id']}/generate", json={"until": until}
    )


# --- Schedule arithmetic --------------------------------------------------------------


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert add_months(date(2026, 1, 31), 12) == date(2027, 1, 31)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [
        (Frequency.WEEKLY, ["2026-01-31", "2026-02-07", "2026-02-14"]),
        (Frequency.MONTHLY, ["2026-01-31", "2026-02-28", "2026-03-31"]),
        (Frequency.QUARTERLY, ["2026-01-31", "2026-04-30", "2026-07-31"]),
        (Frequency.YEARLY, ["2026-01-31", "2027-01-31", "2028-01-31"]),
    ],
)
def test_occurrences_are_anchored_on_start_date(frequency, expected):
    template = RecurringTemplate(
        frequency=frequency, start_date=date(2026, 1, 31), end_date=None, next_run_date=None
    )
    first, last = date(2026, 1, 1), date.fromisoformat(expected[-1])
    assert [d.isoformat() for d in occurrences_between(template, first, last)] == expected


# --- Templates -------------------------------------------------------------------


def test_create_and_read_template(client, template):
    assert template["next_run_date"] == "2026-01-31"
    assert template["is_active"] is True
    assert [line["debit"] for line in template["lines"]] == ["1200.00", "0.00"]
    assert client.get(f"/api/v1/recurring-templates/{template['id']}").json() == template
    assert [t["name"] for t in client.get("/api/v1/recurring-templates").json()] == ["Office rent"]
    assert client.get("/api/v1/recurring-templates/999").status_code == 404


def test_template_validation(client, accounts, template):
    rent, bank = accounts
    assert (
        client.post("/api/v1/recurring-templates", json=_template_body(rent, bank)).status_code
        == 409
    )
    bad_dates = _template_body(rent, bank, name="x", end_date="2025-12-31")
    assert client.post("/api/v1/recurring-templates", json=bad_dates).status_code == 422
    unknown = _template_body(rent, bank, name="y", lines=[{"account_id": 999, "debit": "1.00"}])
    assert client.post("/api/v1/recurring-templates", json=unknown).status_code == 422
    negative = _template_body(
        rent, bank, name="z", lines=[{"account_id": rent["id"], "debit": "-1"}]
    )
    assert client.post("/api/v1/recurring-templates", json=negative).status_code == 422


def test_patch_template(client, template):
    url = f"/api/v1/recurring-templates/{template['id']}"
    assert client.patch(url, json={"is_active": False}).json()["is_active"] is False
    assert client.patch(url, json={"end_date": "2026-06-30"}).json()["end_date"] == "2026-06-30"
    assert client.patch(url, json={"end_date": "2025-01-01"}).status_code == 422
    assert client.patch(url, json={"frequency": "weekly"}).status_code == 422  # not editable
    assert (
        client.patch("/api/v1/recurring-templates/999", json={"is_active": True}).status_code == 404
    )


# --- Generation ----------------------------------------------------------------------


def test_generate_creates_reviewable_drafts_without_duplicates(client, accounts, template):
    rent, bank = accounts
    resp = _generate(client, template, "2026-03-31")
    assert resp.status_code == 201, resp.text
    drafts = resp.json()
    assert [d["entry_date"] for d in drafts] == ["2026-01-31", "2026-02-28", "2026-03-31"]
    assert all(d["status"] == "draft" and d["entry_type"] == "recurring" for d in drafts)
    assert all(d["recurring_template_id"] == template["id"] for d in drafts)
    assert [d["scheduled_for"] for d in drafts] == [d["entry_date"] for d in drafts]
    assert drafts[0]["description"] == "Monthly office rent"
    assert drafts[0]["reference"] == "LEASE-1"
    assert [
        (line["account_id"], line["debit"], line["credit"], line["memo"])
        for line in drafts[0]["lines"]
    ] == [
        (rent["id"], "1200.00", "0.00", "rent"),
        (bank["id"], "0.00", "1200.00", None),
    ]
    assert (
        client.get(f"/api/v1/recurring-templates/{template['id']}").json()["next_run_date"]
        == "2026-04-30"
    )

    # Repeating the request (same or earlier horizon) creates nothing new.
    assert _generate(client, template, "2026-03-31").json() == []
    assert _generate(client, template, "2026-02-01").json() == []
    listed = client.get(f"/api/v1/journal-entries?recurring_template_id={template['id']}").json()
    assert len(listed) == 3

    # Extending the horizon only adds the new occurrences.
    more = _generate(client, template, "2026-05-01").json()
    assert [d["entry_date"] for d in more] == ["2026-04-30"]


def test_generated_drafts_are_reviewed_like_any_draft(client, accounts, template):
    rent, bank = accounts
    draft = _generate(client, template, "2026-01-31").json()[0]
    url = f"/api/v1/journal-entries/{draft['id']}"

    edited = client.put(
        url,
        json={
            "entry_date": "2026-01-30",
            "description": "January rent (early)",
            "version": draft["version"],
            "lines": [
                {"account_id": rent["id"], "debit": "1250.00"},
                {"account_id": bank["id"], "credit": "1250.00"},
            ],
        },
    )
    assert edited.status_code == 200
    assert edited.json()["scheduled_for"] == "2026-01-31"  # slot is kept for duplicate protection
    assert edited.json()["entry_type"] == "recurring"  # provenance survives the edit
    assert edited.json()["recurring_template_id"] == template["id"]
    posted = client.post(f"{url}/post")
    assert posted.status_code == 200 and posted.json()["status"] == "posted"

    # Re-running generation does not recreate January even though the draft changed.
    assert _generate(client, template, "2026-01-31").json() == []


def test_deleted_draft_is_not_regenerated(client, template):
    draft = _generate(client, template, "2026-01-31").json()[0]
    assert client.delete(f"/api/v1/journal-entries/{draft['id']}").status_code == 204
    assert _generate(client, template, "2026-01-31").json() == []
    assert _generate(client, template, "2026-02-28").json()[0]["entry_date"] == "2026-02-28"


def test_end_date_and_inactive_template(client, accounts, template):
    url = f"/api/v1/recurring-templates/{template['id']}"
    client.patch(url, json={"end_date": "2026-02-28"})
    drafts = _generate(client, template, "2026-12-31").json()
    assert [d["entry_date"] for d in drafts] == ["2026-01-31", "2026-02-28"]
    assert client.get(url).json()["next_run_date"] == "2026-03-31"  # beyond end_date: nothing more

    rent, bank = accounts
    other = client.post(
        "/api/v1/recurring-templates", json=_template_body(rent, bank, name="Paused")
    ).json()
    client.patch(f"/api/v1/recurring-templates/{other['id']}", json={"is_active": False})
    assert _generate(client, other, "2026-12-31").json() == []
    assert (
        client.get(f"/api/v1/recurring-templates/{other['id']}").json()["next_run_date"]
        == "2026-01-31"
    )


def test_generate_due_runs_every_active_template(client, accounts, template):
    rent, bank = accounts
    weekly = client.post(
        "/api/v1/recurring-templates",
        json=_template_body(rent, bank, name="Weekly", frequency="weekly", start_date="2026-02-01"),
    ).json()
    paused = client.post(
        "/api/v1/recurring-templates", json=_template_body(rent, bank, name="Paused")
    ).json()
    client.patch(f"/api/v1/recurring-templates/{paused['id']}", json={"is_active": False})
    future = client.post(
        "/api/v1/recurring-templates",
        json=_template_body(rent, bank, name="Future", start_date="2027-01-01"),
    ).json()

    resp = client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-02-15"})
    assert resp.status_code == 201
    created = resp.json()
    assert sorted((d["recurring_template_id"], d["entry_date"]) for d in created) == sorted(
        [
            (template["id"], "2026-01-31"),
            (weekly["id"], "2026-02-01"),
            (weekly["id"], "2026-02-08"),
            (weekly["id"], "2026-02-15"),
        ]
    )
    assert (
        client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-02-15"}).json()
        == []
    )
    assert (
        client.get(f"/api/v1/recurring-templates/{future['id']}").json()["next_run_date"]
        == "2027-01-01"
    )
    assert (
        client.post("/api/v1/recurring-templates/generate-due", json={"till": "x"}).status_code
        == 422
    )


def test_generation_failure_rolls_back_completely(client, template):
    """If any draft in a batch cannot be stored, no draft is stored and the cursor stays."""
    # Occupy the February slot behind the service's back so the batch Jan..Mar hits
    # the unique (template, scheduled_for) constraint on its second draft.
    with Session(client.app.state.engine) as db:
        db.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version, recurring_template_id, scheduled_for) VALUES ('2026-02-28', 'blocker', "
                "'draft', 'standard', 1, :template, '2026-02-28')"
            ),
            {"template": template["id"]},
        )
        db.commit()

    resp = _generate(client, template, "2026-03-31")
    assert resp.status_code == 422
    assert "constraint" in resp.json()["detail"].lower()

    entries = client.get("/api/v1/journal-entries").json()
    assert [e["description"] for e in entries] == ["blocker"]  # no January, no March
    assert (
        client.get(f"/api/v1/recurring-templates/{template['id']}").json()["next_run_date"]
        == "2026-01-31"
    )


def test_unique_slot_enforced_by_database(client, template):
    draft = _generate(client, template, "2026-01-31").json()[0]
    with Session(client.app.state.engine) as db:
        with pytest.raises(IntegrityError, match="(?i)unique"):
            db.execute(
                text(
                    "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                    "version, recurring_template_id, scheduled_for) VALUES ('2026-01-31', 'dup', "
                    "'draft', 'recurring', 1, :template, :slot)"
                ),
                {"template": template["id"], "slot": draft["scheduled_for"]},
            )
        db.rollback()


def test_service_generate_returns_decimal_lines(client, template):
    with Session(client.app.state.engine, expire_on_commit=False) as db:
        created = generate(db, db.get(RecurringTemplate, template["id"]), date(2026, 1, 31))
        assert len(created) == 1
        assert created[0].lines[0].debit == Decimal("1200.00")
        assert isinstance(created[0].lines[0].debit, Decimal)
