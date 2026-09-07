"""generate-due: atomic batch mode, failures on the 2nd/3rd template, duplicate-free retries."""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import JournalEntry, RecurringTemplate
from app.services.recurring import generate, generate_due
from tests.helpers import account, clear_hooks, failing_checkpoint  # noqa: F401

UNTIL = date(2026, 3, 31)


@pytest.fixture
def templates(client):
    rent = account(client, "6000", "Rent", "expense")
    bank = account(client, "1100", "Bank", "asset")
    created = []
    for name in ("alpha", "beta", "gamma"):
        resp = client.post(
            "/api/v1/recurring-templates",
            json={
                "name": name,
                "description": f"{name} rent",
                "frequency": "monthly",
                "start_date": "2026-01-31",
                "lines": [
                    {"account_id": rent["id"], "debit": "100.00"},
                    {"account_id": bank["id"], "credit": "100.00"},
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        created.append(resp.json())
    return created


def _state(db, template_ids):
    """(drafts per template, cursor per template) straight from the database."""
    drafts = {
        tid: db.scalars(
            select_ids := text(
                "SELECT scheduled_for FROM journal_entries WHERE recurring_template_id = :t "
                "ORDER BY scheduled_for"
            ),
            {"t": tid},
        ).all()
        for tid in template_ids
    }
    del select_ids
    cursors = {
        tid: str(
            db.execute(
                text("SELECT next_run_date FROM recurring_templates WHERE id = :t"), {"t": tid}
            ).scalar_one()
        )
        for tid in template_ids
    }
    return drafts, cursors


def test_atomic_generate_due_commits_everything_together(client, templates):
    resp = client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-03-31"})
    assert resp.status_code == 201
    created = resp.json()
    assert len(created) == 9
    with Session(client.app.state.engine) as db:
        drafts, cursors = _state(db, [t["id"] for t in templates])
    assert all(len(d) == 3 for d in drafts.values())
    assert set(cursors.values()) == {"2026-04-30"}
    # Repeating is a no-op.
    assert (
        client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-03-31"}).json()
        == []
    )


@pytest.mark.parametrize("failing", ["beta", "gamma"])
def test_flush_failure_on_later_template_rolls_back_all(client, templates, failing):
    """A unique-slot violation while flushing the 2nd/3rd template undoes the whole batch."""
    tid = {t["name"]: t["id"] for t in templates}[failing]
    with Session(client.app.state.engine) as db:
        db.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version, recurring_template_id, scheduled_for) VALUES ('2026-02-28', 'blocker', "
                "'draft', 'standard', 1, :t, '2026-02-28')"
            ),
            {"t": tid},
        )
        db.commit()

    resp = client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-03-31"})
    assert resp.status_code == 422
    assert "constraint" in resp.json()["detail"].lower()

    with Session(client.app.state.engine) as db:
        drafts, cursors = _state(db, [t["id"] for t in templates])
    for t in templates:
        if t["name"] == failing:
            assert [str(d) for d in drafts[t["id"]]] == ["2026-02-28"]  # only the blocker
        else:
            assert drafts[t["id"]] == []  # earlier templates were rolled back too
    assert set(cursors.values()) == {"2026-01-31"}  # no cursor moved

    # Non-atomic mode, for contrast: templates before the failing one keep their drafts.
    resp = client.post(
        "/api/v1/recurring-templates/generate-due", json={"until": "2026-03-31", "atomic": False}
    )
    assert resp.status_code == 422
    with Session(client.app.state.engine) as db:
        drafts, cursors = _state(db, [t["id"] for t in templates])
    names = [t["name"] for t in templates]
    for t in templates:
        if names.index(t["name"]) < names.index(failing):
            assert len(drafts[t["id"]]) == 3 and cursors[t["id"]] == "2026-04-30"
        elif t["name"] == failing:
            assert [str(d) for d in drafts[t["id"]]] == ["2026-02-28"]
            assert cursors[t["id"]] == "2026-01-31"


@pytest.mark.parametrize("failing", ["beta", "gamma"])
def test_injected_flush_failure_then_retry_has_no_duplicates(client, templates, failing):
    with failing_checkpoint(f"recurring.flush:{failing}"):
        with pytest.raises(RuntimeError, match="injected failure"):
            with Session(client.app.state.engine) as db:
                generate_due(db, UNTIL)
    with Session(client.app.state.engine) as db:
        drafts, cursors = _state(db, [t["id"] for t in templates])
    assert all(d == [] for d in drafts.values())
    assert set(cursors.values()) == {"2026-01-31"}

    # Retry after the fault is cleared: exactly one draft per slot, nothing doubled.
    resp = client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-03-31"})
    assert resp.status_code == 201 and len(resp.json()) == 9
    with Session(client.app.state.engine) as db:
        drafts, _ = _state(db, [t["id"] for t in templates])
        total = db.execute(text("SELECT COUNT(*) FROM journal_entries")).scalar_one()
    assert total == 9
    assert all(
        [str(d) for d in ds] == ["2026-01-31", "2026-02-28", "2026-03-31"] for ds in drafts.values()
    )


def test_commit_failure_rolls_back_all_templates(client, templates):
    with failing_checkpoint("recurring.batch_before_commit"):
        with pytest.raises(RuntimeError, match="injected failure"):
            with Session(client.app.state.engine) as db:
                generate_due(db, UNTIL)
    with Session(client.app.state.engine) as db:
        drafts, cursors = _state(db, [t["id"] for t in templates])
    assert all(d == [] for d in drafts.values())
    assert set(cursors.values()) == {"2026-01-31"}

    resp = client.post("/api/v1/recurring-templates/generate-due", json={"until": "2026-03-31"})
    assert resp.status_code == 201 and len(resp.json()) == 9


def test_single_template_generate_owns_or_borrows_the_transaction(client, templates):
    engine = client.app.state.engine
    alpha = templates[0]
    with Session(engine) as db:
        template = db.get(RecurringTemplate, alpha["id"])
        drafts = generate(db, template, UNTIL, commit=False)  # caller owns the transaction
        assert len(drafts) == 3
        db.rollback()  # caller decides: discard
    with Session(engine) as db:
        assert db.execute(text("SELECT COUNT(*) FROM journal_entries")).scalar_one() == 0
        assert db.get(RecurringTemplate, alpha["id"]).next_run_date == date(2026, 1, 31)

    with Session(engine) as db:
        template = db.get(RecurringTemplate, alpha["id"])
        generate(db, template, UNTIL)  # commit=True: committed by the service
    with Session(engine) as db:
        assert db.execute(text("SELECT COUNT(*) FROM journal_entries")).scalar_one() == 3
        assert db.get(RecurringTemplate, alpha["id"]).next_run_date == date(2026, 4, 30)


def test_generate_unique_slot_backstop_is_reported_as_integrity_error(client, templates):
    engine = client.app.state.engine
    alpha = templates[0]
    with Session(engine) as db:
        db.execute(
            text(
                "INSERT INTO journal_entries (entry_date, description, status, entry_type, "
                "version, recurring_template_id, scheduled_for) VALUES ('2026-01-31', 'blocker', "
                "'draft', 'standard', 1, :t, '2026-01-31')"
            ),
            {"t": alpha["id"]},
        )
        db.commit()
    with Session(engine) as db:
        with pytest.raises(IntegrityError):
            generate(db, db.get(RecurringTemplate, alpha["id"]), UNTIL)
    with Session(engine) as db:
        assert db.scalars(select_all := text("SELECT description FROM journal_entries")).all() == [
            "blocker"
        ]
        del select_all
        assert db.get(JournalEntry, 1).description == "blocker"
