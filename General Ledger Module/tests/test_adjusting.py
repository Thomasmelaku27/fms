"""Adjusting entries: traceable through entry_type and subject to the normal rules."""


def _account(client, code, name, type_):
    return client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_}).json()


def _adjusting(client, expense, accrued, entry_date="2026-01-31", entry_type="adjusting"):
    body = {
        "entry_date": entry_date,
        "description": "Accrued utilities",
        "entry_type": entry_type,
        "lines": [
            {"account_id": expense["id"], "debit": "80.00"},
            {"account_id": accrued["id"], "credit": "80.00"},
        ],
    }
    return client.post("/api/v1/journal-entries", json=body)


def test_adjusting_entry_lifecycle(client):
    expense = _account(client, "6100", "Utilities", "expense")
    accrued = _account(client, "2100", "Accrued liabilities", "liability")

    resp = _adjusting(client, expense, accrued)
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert entry["entry_type"] == "adjusting" and entry["status"] == "draft"

    # Same posting rules as any entry.
    posted = client.post(f"/api/v1/journal-entries/{entry['id']}/post")
    assert posted.status_code == 200 and posted.json()["entry_type"] == "adjusting"
    assert client.post(f"/api/v1/journal-entries/{entry['id']}/post").status_code == 409

    # Typical accrual handling: reverse at the start of the next period.
    reversal = client.post(
        f"/api/v1/journal-entries/{entry['id']}/reverse", json={"reversal_date": "2026-02-01"}
    ).json()
    assert reversal["entry_type"] == "reversal"
    assert reversal["reverses_entry_id"] == entry["id"]

    # Traceable: filterable by type, linked both ways, visible in reports.
    adjusting = client.get("/api/v1/journal-entries?entry_type=adjusting").json()
    assert [e["id"] for e in adjusting] == [entry["id"]]
    assert adjusting[0]["reversed_by_entry_id"] == reversal["id"]
    assert [e["id"] for e in client.get("/api/v1/journal-entries?entry_type=reversal").json()] == [
        reversal["id"]
    ]
    assert [e["id"] for e in client.get("/api/v1/journal-entries?status=posted").json()] == [
        entry["id"],
        reversal["id"],
    ]
    ledger = client.get(f"/api/v1/reports/general-ledger?account_id={accrued['id']}").json()
    assert [line["balance"] for line in ledger["accounts"][0]["lines"]] == ["-80.00", "0.00"]


def test_entry_type_validation_and_update(client):
    expense = _account(client, "6100", "Utilities", "expense")
    accrued = _account(client, "2100", "Accrued liabilities", "liability")

    for reserved in ("reversal", "recurring", "bogus"):
        assert _adjusting(client, expense, accrued, entry_type=reserved).status_code == 422

    entry = _adjusting(client, expense, accrued, entry_type="standard").json()
    assert entry["entry_type"] == "standard"
    # An ordinary edit cannot change the classification...
    edit = client.put(
        f"/api/v1/journal-entries/{entry['id']}",
        json={
            "entry_date": "2026-01-31",
            "description": "Reclassified as adjusting",
            "entry_type": "adjusting",
            "version": entry["version"],
            "lines": [
                {"account_id": expense["id"], "debit": "80.00"},
                {"account_id": accrued["id"], "credit": "80.00"},
            ],
        },
    )
    assert edit.status_code == 422
    assert "Use the reclassify operation" in edit.json()["detail"]
    # ...the explicit, versioned reclassification can.
    changed = client.post(
        f"/api/v1/journal-entries/{entry['id']}/reclassify",
        json={"entry_type": "adjusting", "version": entry["version"]},
    )
    assert changed.status_code == 200
    assert changed.json()["entry_type"] == "adjusting"
    assert changed.json()["version"] == entry["version"] + 1
    assert client.get("/api/v1/journal-entries?entry_type=standard").json() == []
