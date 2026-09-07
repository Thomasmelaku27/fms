"""End-to-end scenario: recurring rent, month-end adjustments, close and lock.

Recurring template -> drafts generated -> reviewed/edited/posted -> adjusting
accrual at month end -> close-check blocks -> resolve -> close -> lock ->
locked period is final -> next period carries on (reversal of the accrual).
"""

API = "/api/v1"


def _ok(resp, status):
    assert resp.status_code == status, f"{resp.request.method} {resp.url}: {resp.text}"
    return resp.json() if resp.content else None


def test_recurring_month_end_close_and_lock(client):
    # Chart of accounts and two open periods.
    acc = {}
    for code, name, type_ in [
        ("1100", "Bank", "asset"),
        ("2100", "Accrued liabilities", "liability"),
        ("6000", "Rent", "expense"),
        ("6100", "Utilities", "expense"),
    ]:
        acc[code] = _ok(
            client.post(f"{API}/accounts", json={"code": code, "name": name, "type": type_}), 201
        )
    january = _ok(
        client.post(
            f"{API}/periods",
            json={"name": "2026-01", "start_date": "2026-01-01", "end_date": "2026-01-31"},
        ),
        201,
    )
    february = _ok(
        client.post(
            f"{API}/periods",
            json={"name": "2026-02", "start_date": "2026-02-01", "end_date": "2026-02-28"},
        ),
        201,
    )

    # 1. Recurring rent, generated for January and February in one go.
    template = _ok(
        client.post(
            f"{API}/recurring-templates",
            json={
                "name": "Office rent",
                "description": "Office rent",
                "frequency": "monthly",
                "start_date": "2026-01-31",
                "lines": [
                    {"account_id": acc["6000"]["id"], "debit": "1200.00"},
                    {"account_id": acc["1100"]["id"], "credit": "1200.00"},
                ],
            },
        ),
        201,
    )
    drafts = _ok(
        client.post(
            f"{API}/recurring-templates/{template['id']}/generate", json={"until": "2026-02-28"}
        ),
        201,
    )
    assert [d["entry_date"] for d in drafts] == ["2026-01-31", "2026-02-28"]
    jan_rent, feb_rent = drafts
    # A scheduler re-running the same horizon is harmless.
    assert (
        _ok(
            client.post(f"{API}/recurring-templates/generate-due", json={"until": "2026-02-28"}),
            201,
        )
        == []
    )

    # 2. Review: January's rent was actually 1250 this month; edit with version check, then post.
    edited = _ok(
        client.put(
            f"{API}/journal-entries/{jan_rent['id']}",
            json={
                "entry_date": "2026-01-31",
                "description": "Office rent (indexed)",
                "version": jan_rent["version"],
                "lines": [
                    {"account_id": acc["6000"]["id"], "debit": "1250.00"},
                    {"account_id": acc["1100"]["id"], "credit": "1250.00"},
                ],
            },
        ),
        200,
    )
    assert edited["version"] == 2
    stale = client.put(
        f"{API}/journal-entries/{jan_rent['id']}",
        json={
            **{k: edited[k] for k in ("entry_date", "description")},
            "version": 1,
            "lines": edited["lines"],
        },
    )
    assert stale.status_code == 409
    _ok(client.post(f"{API}/journal-entries/{jan_rent['id']}/post"), 200)

    # 3. Month-end adjusting entry: accrue January utilities.
    accrual = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-01-31",
                "description": "Accrued January utilities",
                "entry_type": "adjusting",
                "lines": [
                    {"account_id": acc["6100"]["id"], "debit": "80.00"},
                    {"account_id": acc["2100"]["id"], "credit": "80.00"},
                ],
            },
        ),
        201,
    )
    _ok(client.post(f"{API}/journal-entries/{accrual['id']}/post"), 200)

    # 4. Close-check: a stray January draft blocks the close; resolve it, then close.
    stray = _ok(
        client.post(
            f"{API}/journal-entries",
            json={
                "entry_date": "2026-01-20",
                "description": "Unfinished",
                "lines": [{"account_id": acc["6100"]["id"], "debit": "5.00"}],
            },
        ),
        201,
    )
    check = _ok(client.get(f"{API}/periods/{january['id']}/close-check"), 200)
    assert check["can_close"] is False and str(stray["id"]) in check["problems"][0]
    assert client.post(f"{API}/periods/{january['id']}/close").status_code == 409
    _ok(client.delete(f"{API}/journal-entries/{stray['id']}", headers={"If-Match": '"1"'}), 204)
    assert _ok(client.get(f"{API}/periods/{january['id']}/close-check"), 200)["can_close"] is True
    _ok(client.post(f"{API}/periods/{january['id']}/close"), 200)
    assert client.post(f"{API}/periods/{january['id']}/close").status_code == 409

    january_tb = _ok(client.get(f"{API}/reports/trial-balance?from=2026-01-01&to=2026-01-31"), 200)
    assert january_tb["is_balanced"] and january_tb["total_debit"] == "1330.00"

    # 5. Lock January: final. Nothing about it can change any more.
    _ok(client.post(f"{API}/periods/{january['id']}/lock"), 200)
    assert client.post(f"{API}/periods/{january['id']}/reopen").status_code == 409
    assert client.post(f"{API}/periods/{january['id']}/lock").status_code == 409
    blocked = client.post(
        f"{API}/journal-entries/{accrual['id']}/reverse", json={"reversal_date": "2026-01-31"}
    )
    assert blocked.status_code == 422

    # 6. February carries on: the accrual reverses on Feb 1, February rent is posted.
    reversal = _ok(
        client.post(
            f"{API}/journal-entries/{accrual['id']}/reverse", json={"reversal_date": "2026-02-01"}
        ),
        201,
    )
    assert reversal["entry_type"] == "reversal" and reversal["reverses_entry_id"] == accrual["id"]
    _ok(client.post(f"{API}/journal-entries/{feb_rent['id']}/post"), 200)

    # February cannot be closed before... it can: January is locked (closed counts).
    assert _ok(client.get(f"{API}/periods/{february['id']}/close-check"), 200)["can_close"] is True

    # 7. Reports: January unchanged, accrual nets out over both months, drafts never counted.
    assert (
        _ok(client.get(f"{API}/reports/trial-balance?from=2026-01-01&to=2026-01-31"), 200)
        == january_tb
    )
    gl = _ok(client.get(f"{API}/reports/general-ledger?account_id={acc['2100']['id']}"), 200)
    assert [line["balance"] for line in gl["accounts"][0]["lines"]] == ["-80.00", "0.00"]
    rent = _ok(client.get(f"{API}/reports/general-ledger?account_id={acc['6000']['id']}"), 200)
    assert rent["accounts"][0]["closing_balance"] == "2450.00"
    history = _ok(client.get(f"{API}/journal-entries?status=posted"), 200)
    assert [(e["entry_type"], e["entry_date"]) for e in history] == [
        ("recurring", "2026-01-31"),  # edited rent keeps its recurring provenance
        ("adjusting", "2026-01-31"),
        ("reversal", "2026-02-01"),
        ("recurring", "2026-02-28"),
    ]
