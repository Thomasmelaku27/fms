"""Optimistic locking of draft edits (version on PUT, If-Match on DELETE)."""

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.models import JournalEntry
from app.schemas.journal import JournalEntryUpdate
from app.services.journal import VersionConflict, update_draft


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _body(cash, description="Draft", **extra):
    return {
        "entry_date": "2026-01-15",
        "description": description,
        "lines": [{"account_id": cash["id"], "debit": "1.00"}],
        **extra,
    }


@pytest.fixture
def cash(client):
    return _account(client, "1000", "Cash", "asset")


@pytest.fixture
def draft(client, cash):
    resp = client.post("/api/v1/journal-entries", json=_body(cash))
    assert resp.status_code == 201
    assert resp.json()["version"] == 1
    return resp.json()


def test_version_increments_and_stale_update_is_rejected(client, cash, draft):
    url = f"/api/v1/journal-entries/{draft['id']}"
    first = client.put(url, json=_body(cash, "Edit 1", version=1))
    assert first.status_code == 200
    assert first.json()["version"] == 2

    # A second editor still holding version 1 loses.
    stale = client.put(url, json=_body(cash, "Edit 2 (stale)", version=1))
    assert stale.status_code == 409
    assert "is at version 2, not 1" in stale.json()["detail"]
    assert client.get(url).json()["description"] == "Edit 1"

    # Repeating the stale request keeps failing; reloading and retrying works.
    assert client.put(url, json=_body(cash, "Edit 2 (stale)", version=1)).status_code == 409
    retry = client.put(url, json=_body(cash, "Edit 2", version=2))
    assert retry.status_code == 200 and retry.json()["version"] == 3


def test_version_is_required_and_validated(client, cash, draft):
    url = f"/api/v1/journal-entries/{draft['id']}"
    assert client.put(url, json=_body(cash)).status_code == 422
    assert client.put(url, json=_body(cash, version=0)).status_code == 422
    assert client.put(url, json=_body(cash, version="one")).status_code == 422
    assert client.put(url, json=_body(cash, version=99)).status_code == 409


def test_posting_does_not_bump_version_but_edits_are_blocked(client, cash, draft):
    sales = _account(client, "4000", "Sales", "revenue")
    url = f"/api/v1/journal-entries/{draft['id']}"
    body = _body(cash, version=1)
    body["lines"].append({"account_id": sales["id"], "credit": "1.00"})
    assert client.put(url, json=body).status_code == 200
    posted = client.post(f"{url}/post").json()
    assert posted["version"] == 2
    body["version"] = 2
    assert client.put(url, json=body).status_code == 409  # posted, not a version problem


def test_delete_with_if_match(client, cash, draft):
    url = f"/api/v1/journal-entries/{draft['id']}"
    client.put(url, json=_body(cash, "Edited", version=1))  # now version 2

    stale = client.delete(url, headers={"If-Match": '"1"'})
    assert stale.status_code == 409
    assert client.get(url).status_code == 200

    assert client.delete(url, headers={"If-Match": "abc"}).status_code == 422
    assert client.delete(url, headers={"If-Match": "2"}).status_code == 204
    assert client.get(url).status_code == 404


def test_delete_without_if_match_still_works(client, draft):
    assert client.delete(f"/api/v1/journal-entries/{draft['id']}").status_code == 204


def test_concurrent_edit_between_check_and_write(client, cash, draft):
    """Two sessions load version 1; the second write hits the ORM version check."""
    engine = client.app.state.engine
    payload = JournalEntryUpdate.model_validate(_body(cash, "Mine", version=1))

    with Session(engine, expire_on_commit=False) as a, Session(engine, expire_on_commit=False) as b:
        entry_b = b.get(JournalEntry, draft["id"])
        b.commit()  # b keeps its stale in-memory copy but holds no database lock
        entry_a = a.get(JournalEntry, draft["id"])
        assert entry_a.version == entry_b.version == 1

        update_draft(a, entry_a, payload)  # wins: version 2
        assert entry_a.version == 2
        a.commit()  # release the transaction opened by the service's refresh()

        # Session b passed the explicit check (it still believes version 1) but the
        # UPDATE ... WHERE version = 1 matches nothing -> StaleDataError -> 409.
        with pytest.raises(VersionConflict, match="is at version 2, not 1"):
            update_draft(b, entry_b, payload.model_copy(update={"description": "Theirs"}))
        b.refresh(entry_b)
        assert entry_b.description == "Mine"

    assert client.get(f"/api/v1/journal-entries/{draft['id']}").json()["version"] == 2


def test_stale_data_error_is_what_the_orm_raises(client, cash, draft):
    """Sanity check of the mechanism the service relies on (version_id_col).

    Every UPDATE is emitted as ``WHERE version = <value loaded>``; the services
    bump the counter explicitly, so a writer holding an old version matches no row.
    """
    engine = client.app.state.engine
    with Session(engine, expire_on_commit=False) as a, Session(engine, expire_on_commit=False) as b:
        entry_b = b.get(JournalEntry, draft["id"])
        b.commit()  # release the lock; entry_b stays loaded at version 1
        entry_a = a.get(JournalEntry, draft["id"])
        entry_a.description = "A"
        entry_a.version += 1
        a.commit()
        entry_b.description = "B"
        entry_b.version += 1
        with pytest.raises(StaleDataError):
            b.commit()
        b.rollback()
