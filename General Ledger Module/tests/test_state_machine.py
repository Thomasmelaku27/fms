"""Randomised state-machine test of the ledger with deterministic seeds.

A model of what *should* be true is kept alongside the system: posted
amounts per account, which entries are posted/reversed, which periods are
closed. Operations are chosen at random (including invalid ones that must be
refused) and after each step the ledger must match the model and satisfy the
global invariants.
"""

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.helpers import account, draft

SEEDS = [3, 11, 29]
STEPS = 60


class Model:
    def __init__(self, client, rng):
        self.c, self.rng = client, rng
        self.accounts = [
            account(client, "1000", "Cash", "asset"),
            account(client, "1200", "AR", "asset"),
            account(client, "2000", "AP", "liability"),
            account(client, "4000", "Sales", "revenue"),
            account(client, "6000", "Rent", "expense"),
        ]
        self.periods = {}
        for m in range(1, 7):
            start = date(2026, m, 1)
            end = date(2026, m + 1, 1) - timedelta(days=1)
            p = client.post(
                "/api/v1/periods",
                json={
                    "name": f"2026-{m:02d}",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            ).json()
            self.periods[m] = {"id": p["id"], "status": "open"}
        self.balance = {a["id"]: Decimal("0") for a in self.accounts}
        self.posted: dict[int, dict] = {}  # id -> {"date", "lines", "reversed"}
        self.drafts: dict[int, dict] = {}
        self.snapshots: dict[int, dict] = {}  # period month -> balances at close time

    def _lines(self, balanced=True):
        a, b = self.rng.sample(self.accounts, 2)
        amount = Decimal(self.rng.randint(1, 9999)) / 100
        other = amount if balanced else amount + Decimal("0.01")
        return [(a["id"], amount, Decimal("0")), (b["id"], Decimal("0"), other)]

    def _day(self, month=None):
        month = month or self.rng.randint(1, 6)
        return date(2026, month, self.rng.randint(1, 28))

    # --- operations ------------------------------------------------------------

    def step_create_draft(self):
        day = self._day()
        lines = self._lines(balanced=self.rng.random() < 0.85)
        body = [{"account_id": a, "debit": f"{d:.2f}", "credit": f"{c:.2f}"} for a, d, c in lines]
        e = draft(self.c, body, entry_date=day.isoformat())
        self.drafts[e["id"]] = {"date": day, "lines": lines}

    def step_post(self):
        if not self.drafts:
            return
        eid = self.rng.choice(list(self.drafts))
        d = self.drafts[eid]
        balanced = sum(x[1] for x in d["lines"]) == sum(x[2] for x in d["lines"])
        closed = self.periods[d["date"].month]["status"] != "open"
        resp = self.c.post(f"/api/v1/journal-entries/{eid}/post")
        if balanced and not closed:
            assert resp.status_code == 200, resp.text
            self.posted[eid] = {**self.drafts.pop(eid), "reversed": False}
            for a, dr, cr in self.posted[eid]["lines"]:
                self.balance[a] += dr - cr
        else:
            assert resp.status_code == 422, resp.text
        assert self.c.post(f"/api/v1/journal-entries/{eid}/post").status_code in (409, 422)

    def step_reverse(self):
        candidates = [i for i, p in self.posted.items() if not p["reversed"]]
        if not candidates:
            return
        eid = self.rng.choice(candidates)
        original = self.posted[eid]
        day = self._day(self.rng.randint(original["date"].month, 6))
        if day < original["date"]:
            day = original["date"]
        resp = self.c.post(
            f"/api/v1/journal-entries/{eid}/reverse", json={"reversal_date": day.isoformat()}
        )
        if self.periods[day.month]["status"] != "open":
            assert resp.status_code == 422
            return
        assert resp.status_code == 201, resp.text
        original["reversed"] = True
        mirrored = [(a, cr, dr) for a, dr, cr in original["lines"]]
        self.posted[resp.json()["id"]] = {"date": day, "lines": mirrored, "reversed": False}
        for a, dr, cr in mirrored:
            self.balance[a] += dr - cr
        assert self.c.post(f"/api/v1/journal-entries/{eid}/reverse").status_code == 409

    def step_close_period(self):
        month = self.rng.randint(1, 6)
        p = self.periods[month]
        resp = self.c.post(f"/api/v1/periods/{p['id']}/close")
        drafts_inside = any(d["date"].month == month for d in self.drafts.values())
        earlier_open = any(self.periods[m]["status"] == "open" for m in range(1, month))
        if p["status"] != "open" or drafts_inside or earlier_open:
            assert resp.status_code == 409, resp.text
        else:
            assert resp.status_code == 200, resp.text
            p["status"] = "closed"
            self.snapshots[month] = dict(self.balance)

    def step_edit_draft(self):
        if not self.drafts:
            return
        eid = self.rng.choice(list(self.drafts))
        current = self.c.get(f"/api/v1/journal-entries/{eid}").json()
        day = self._day()
        lines = self._lines(True)
        body = {
            "entry_date": day.isoformat(),
            "description": "edited",
            "version": current["version"],
            "lines": [
                {"account_id": a, "debit": f"{d:.2f}", "credit": f"{c:.2f}"} for a, d, c in lines
            ],
        }
        resp = self.c.put(f"/api/v1/journal-entries/{eid}", json=body)
        assert resp.status_code == 200, resp.text
        self.drafts[eid] = {"date": day, "lines": lines}
        assert self.c.put(f"/api/v1/journal-entries/{eid}", json=body).status_code == 409  # stale

    def step_delete_draft(self):
        if not self.drafts:
            return
        eid = self.rng.choice(list(self.drafts))
        assert self.c.delete(f"/api/v1/journal-entries/{eid}").status_code == 204
        del self.drafts[eid]

    def step_batch(self):
        ids = list(self.drafts)[:3]
        if not ids:
            return
        resp = self.c.post("/api/v1/journal-entries/batch-post", json={"entry_ids": ids})
        ok = all(
            sum(x[1] for x in self.drafts[i]["lines"]) == sum(x[2] for x in self.drafts[i]["lines"])
            and self.periods[self.drafts[i]["date"].month]["status"] == "open"
            for i in ids
        )
        if ok:
            assert resp.status_code == 200, resp.text
            for i in ids:
                self.posted[i] = {**self.drafts.pop(i), "reversed": False}
                for a, dr, cr in self.posted[i]["lines"]:
                    self.balance[a] += dr - cr
        else:
            assert resp.status_code == 422, resp.text
            assert all(
                self.c.get(f"/api/v1/journal-entries/{i}").json()["status"] == "draft" for i in ids
            )

    # --- invariants --------------------------------------------------------------

    def check(self):
        tb = self.c.get("/api/v1/reports/trial-balance").json()
        assert tb["is_balanced"]
        rows = {r["account_id"]: Decimal(r["balance"]) for r in tb["rows"]}
        for acc_id, expected in self.balance.items():
            assert rows[acc_id] == expected.quantize(Decimal("0.01")), (
                acc_id,
                rows[acc_id],
                expected,
            )
        assert sum(self.balance.values()) == 0  # debit-credit conservation
        entries = {e["id"]: e for e in self.c.get("/api/v1/journal-entries").json()}
        for eid, p in self.posted.items():
            assert entries[eid]["status"] == "posted"
            assert entries[eid]["posted_at"] is not None
            assert (entries[eid]["reversed_by_entry_id"] is not None) == p["reversed"]
        for eid in self.drafts:
            assert entries[eid]["status"] == "draft"
        for month, snapshot in self.snapshots.items():
            end = (date(2026, month + 1, 1) - timedelta(days=1)).isoformat()
            frozen = {
                r["account_id"]: Decimal(r["balance"])
                for r in self.c.get("/api/v1/reports/trial-balance", params={"to": end}).json()[
                    "rows"
                ]
            }
            # A closed period's cumulative figures can only change through later periods, never
            # through postings dated inside it - so the activity *inside* the period is frozen.
            inside = {
                r["account_id"]: Decimal(r["balance"])
                for r in self.c.get(
                    "/api/v1/reports/trial-balance",
                    params={"from": f"2026-{month:02d}-01", "to": end},
                ).json()["rows"]
            }
            self.snapshots[month].setdefault("__inside__", inside)
            assert inside == self.snapshots[month]["__inside__"]
            del frozen
        recon = self.c.get("/api/v1/reconciliation").json()
        assert recon["ok"] and recon["error_count"] == 0, [
            c for c in recon["checks"] if c["findings"]
        ]
        assert self.c.post("/api/v1/balances/verify").json()["ok"] is True


@pytest.mark.parametrize("seed", SEEDS)
def test_ledger_state_machine(client, seed):
    rng = random.Random(seed)
    model = Model(client, rng)
    steps = (
        [model.step_create_draft] * 4
        + [model.step_post] * 4
        + [
            model.step_reverse,
            model.step_close_period,
            model.step_edit_draft,
            model.step_delete_draft,
            model.step_batch,
        ]
    )
    for i in range(STEPS):
        rng.choice(steps)()
        if i % 10 == 9:
            model.check()
    model.check()
    assert model.posted, "the run should have posted something"
