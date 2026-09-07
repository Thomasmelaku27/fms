"""End-to-end scenario through the HTTP API: a month in the life of the ledger.

Accounts -> period -> draft prepared in steps -> posted -> protected -> reversal
-> reports -> period closed -> closed-period rules -> correction in next period.
"""

API = "/api/v1"


def _ok(resp, status):
    assert resp.status_code == status, f"{resp.request.method} {resp.url}: {resp.text}"
    return resp.json() if resp.content else None


def test_month_end_scenario(client):
    # 1. Chart of accounts and an open January.
    cash = _ok(
        client.post(f"{API}/accounts", json={"code": "1000", "name": "Cash", "type": "asset"}), 201
    )
    sales = _ok(
        client.post(f"{API}/accounts", json={"code": "4000", "name": "Sales", "type": "revenue"}),
        201,
    )
    rent = _ok(
        client.post(f"{API}/accounts", json={"code": "6000", "name": "Rent", "type": "expense"}),
        201,
    )
    january = _ok(
        client.post(
            f"{API}/periods",
            json={"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"},
        ),
        201,
    )
    _ok(
        client.post(
            f"{API}/periods",
            json={"name": "2026-02", "start_date": "2026-02-01", "end_date": "2026-02-28"},
        ),
        201,
    )

    # 2. A draft is prepared in steps: first incomplete, then both sides on a line, then fixed.
    sale = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-01-10",
                "description": "Cash sale",
                "lines": [
                    {"account_id": cash["id"], "debit": "300.00"},
                    {"account_id": sales["id"]},
                ],
            },
        ),
        201,
    )
    blocked = client.post(f"{API}/journal-entries/{sale['id']}/post")
    assert blocked.status_code == 422 and "line 2 has no amount" in blocked.json()["detail"]

    sale = _ok(
        client.put(
            f"{API}/journal-entries/{sale['id']}",
            json={
                "entry_date": "2026-01-10",
                "description": "Cash sale",
                "version": sale["version"],
                "lines": [
                    {"account_id": cash["id"], "debit": "300.00", "credit": "300.00"},
                    {"account_id": sales["id"], "credit": "300.00"},
                ],
            },
        ),
        200,
    )
    blocked = client.post(f"{API}/journal-entries/{sale['id']}/post")
    assert blocked.status_code == 422 and "both a debit and a credit" in blocked.json()["detail"]

    _ok(
        client.put(
            f"{API}/journal-entries/{sale['id']}",
            json={
                "entry_date": "2026-01-10",
                "description": "Cash sale",
                "reference": "R-1",
                "version": sale["version"],
                "lines": [
                    {"account_id": cash["id"], "debit": "300.00"},
                    {"account_id": sales["id"], "credit": "300.00"},
                ],
            },
        ),
        200,
    )
    posted_sale = _ok(client.post(f"{API}/journal-entries/{sale['id']}/post"), 200)
    assert posted_sale["status"] == "posted"

    # 3. Drafts never leak into reports; posted entries do.
    tb = _ok(client.get(f"{API}/reports/trial-balance"), 200)
    assert tb["is_balanced"] and tb["total_debit"] == "300.00"

    # 4. Rent paid by mistake twice: post, then reverse the duplicate.
    def rent_entry(day):
        return _ok(
            client.post(
                f"{API}/journal-entries",
                json={
                    "entry_date": f"2026-01-{day}",
                    "description": "Rent",
                    "lines": [
                        {"account_id": rent["id"], "debit": "120.00"},
                        {"account_id": cash["id"], "credit": "120.00"},
                    ],
                },
            ),
            201,
        )

    rent_1 = _ok(client.post(f"{API}/journal-entries/{rent_entry('20')['id']}/post"), 200)
    rent_2 = _ok(client.post(f"{API}/journal-entries/{rent_entry('21')['id']}/post"), 200)
    reversal = _ok(
        client.post(
            f"{API}/journal-entries/{rent_2['id']}/reverse",
            json={"reversal_date": "2026-01-22", "description": "Duplicate rent"},
        ),
        201,
    )
    assert reversal["reverses_entry_id"] == rent_2["id"]
    assert client.post(f"{API}/journal-entries/{rent_2['id']}/reverse").status_code == 409
    stale_edit = {
        "entry_date": "2026-01-21",
        "description": "x",
        "version": rent_2["version"],
        "lines": [{"account_id": cash["id"]}],
    }
    assert client.put(f"{API}/journal-entries/{rent_2['id']}", json=stale_edit).status_code == 409
    assert client.delete(f"{API}/journal-entries/{reversal['id']}").status_code == 409

    # 5. A leftover draft is deleted before month end.
    leftover = rent_entry("28")
    _ok(client.delete(f"{API}/journal-entries/{leftover['id']}"), 204)

    # 6. January reports.
    tb = _ok(client.get(f"{API}/reports/trial-balance?from=2026-01-01&to=2026-01-31"), 200)
    rows = {r["code"]: r for r in tb["rows"]}
    assert tb["is_balanced"] is True
    assert tb["total_debit"] == tb["total_credit"] == "660.00"  # 300 + 120 + 120 + 120
    assert rows["1000"]["balance"] == "180.00"  # 300 - 120 - 120 + 120
    assert rows["6000"]["balance"] == "120.00"  # one rent after the reversal
    assert rows["4000"]["balance"] == "-300.00"

    gl = _ok(client.get(f"{API}/reports/general-ledger?account_id={rent['id']}"), 200)
    lines = gl["accounts"][0]["lines"]
    assert [(line["entry_id"], line["balance"]) for line in lines] == [
        (rent_1["id"], "120.00"),
        (rent_2["id"], "240.00"),
        (reversal["id"], "120.00"),
    ]
    assert lines[1]["reversed_by_entry_id"] == reversal["id"]
    assert gl["accounts"][0]["closing_balance"] == "120.00"

    # 7. Close January: nothing dated inside can be posted or reversed any more.
    _ok(client.post(f"{API}/periods/{january['id']}/close"), 200)
    assert client.post(f"{API}/periods/{january['id']}/close").status_code == 409

    late = rent_entry("30")
    blocked = client.post(f"{API}/journal-entries/{late['id']}/post")
    assert blocked.status_code == 422 and "'2026-01' is closed" in blocked.json()["detail"]
    blocked = client.post(
        f"{API}/journal-entries/{rent_1['id']}/reverse", json={"reversal_date": "2026-01-31"}
    )
    assert blocked.status_code == 422 and "'2026-01' is closed" in blocked.json()["detail"]

    # 8. Corrections go into February instead; January's figures are frozen.
    correction = _ok(
        client.post(
            f"{API}/journal-entries/{rent_1['id']}/reverse", json={"reversal_date": "2026-02-01"}
        ),
        201,
    )
    assert correction["status"] == "posted"
    _ok(client.patch(f"{API}/accounts/{rent['id']}", json={"is_active": False}), 200)
    blocked = client.post(f"{API}/journal-entries/{late['id']}/post")
    assert blocked.status_code == 422

    january_again = _ok(
        client.get(f"{API}/reports/trial-balance?from=2026-01-01&to=2026-01-31"), 200
    )
    assert january_again == tb
    all_time = _ok(client.get(f"{API}/reports/trial-balance"), 200)
    assert (
        all_time["is_balanced"]
        and {r["code"]: r["balance"] for r in all_time["rows"]}["6000"] == "0.00"
    )

    # 9. Full audit history is still there: every posted entry, with its links.
    history = _ok(client.get(f"{API}/journal-entries"), 200)
    posted_ids = [e["id"] for e in history if e["status"] == "posted"]
    assert posted_ids == [sale["id"], rent_1["id"], rent_2["id"], reversal["id"], correction["id"]]
    by_id = {e["id"]: e for e in history}
    assert by_id[rent_2["id"]]["reversed_by_entry_id"] == reversal["id"]
    assert by_id[rent_1["id"]]["reversed_by_entry_id"] == correction["id"]
    assert by_id[late["id"]]["status"] == "draft"
