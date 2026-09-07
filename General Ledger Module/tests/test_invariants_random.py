"""Randomised accounting-invariant tests (deterministic seeds).

A seeded generator drives a mixed workload - valid and invalid drafts, posting
attempts, edits, reversals, batch posts, processing requests with retries -
and after every step the ledger must satisfy:

* the trial balance balances and equals the Decimal sum of posted lines;
* every posted entry balances and has >= 2 complete lines;
* summary roll-ups equal the sum of their descendants;
* the database aggregate agrees with the Python Decimal aggregate;
* the reconciliation report is clean;
* failed or repeated operations never change the totals.
"""

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry, JournalLine
from tests.helpers import account, balances, trial_balance

SEEDS = [1, 7, 2026]


def _money(rng, low=1, high=50000) -> str:
    return f"{Decimal(rng.randint(low, high)) / 100:.2f}"


class Ledger:
    def __init__(self, client, rng):
        self.client, self.rng = client, rng
        self.assets = account(client, "1", "Assets", "asset", is_summary=True)
        self.current = account(
            client, "10", "Current", "asset", is_summary=True, parent_id=self.assets["id"]
        )
        self.posting = [
            account(client, "1000", "Cash", "asset", parent_id=self.current["id"]),
            account(client, "1100", "Bank", "asset", parent_id=self.current["id"]),
            account(client, "1500", "Equipment", "asset", parent_id=self.assets["id"]),
            account(client, "2000", "Payables", "liability"),
            account(client, "3000", "Equity", "equity"),
            account(client, "4000", "Sales", "revenue"),
            account(client, "6000", "Rent", "expense"),
            account(client, "6100", "Supplies", "expense"),
        ]
        self.entries: list[int] = []
        self.posted: list[int] = []
        self.reversed: set[int] = set()
        self.source_counter = 0

    def random_lines(self, balanced=True):
        rng = self.rng
        n = rng.randint(2, 5)
        accounts = rng.sample(self.posting, k=min(n, len(self.posting)))
        debits = [Decimal(_money(rng)) for _ in range(len(accounts) - 1)]
        total = sum(debits)
        lines = [{"account_id": a["id"], "debit": f"{d:.2f}"} for a, d in zip(accounts, debits)]
        credit = total if balanced else total + Decimal("0.01")
        lines.append({"account_id": accounts[-1]["id"], "credit": f"{credit:.2f}"})
        rng.shuffle(lines)
        return lines

    def day(self):
        return (date(2026, 1, 1) + timedelta(days=self.rng.randint(0, 364))).isoformat()

    # --- operations -----------------------------------------------------------------

    def op_create_and_post(self):
        lines = self.random_lines(balanced=self.rng.random() < 0.8)
        entry = self.client.post(
            "/api/v1/journal-entries",
            json={"entry_date": self.day(), "description": "rnd", "lines": lines},
        ).json()
        self.entries.append(entry["id"])
        resp = self.client.post(f"/api/v1/journal-entries/{entry['id']}/post")
        if resp.status_code == 200:
            self.posted.append(entry["id"])
        else:
            assert resp.status_code == 422
        # Repeating the post never changes anything.
        again = self.client.post(f"/api/v1/journal-entries/{entry['id']}/post")
        assert again.status_code in (409, 422)

    def op_reverse(self):
        candidates = [e for e in self.posted if e not in self.reversed]
        if not candidates:
            return
        target = self.rng.choice(candidates)
        original = self.client.get(f"/api/v1/journal-entries/{target}").json()
        resp = self.client.post(
            f"/api/v1/journal-entries/{target}/reverse",
            json={"reversal_date": max(original["entry_date"], self.day())},
        )
        assert resp.status_code == 201, resp.text
        self.reversed.add(target)
        self.posted.append(resp.json()["id"])
        assert self.client.post(f"/api/v1/journal-entries/{target}/reverse").status_code == 409

    def op_batch(self):
        ids = [
            self.client.post(
                "/api/v1/journal-entries",
                json={
                    "entry_date": self.day(),
                    "description": "b",
                    "lines": self.random_lines(True),
                },
            ).json()["id"]
            for _ in range(self.rng.randint(1, 4))
        ]
        if self.rng.random() < 0.3:  # poison one
            bad = self.client.post(
                "/api/v1/journal-entries",
                json={
                    "entry_date": self.day(),
                    "description": "bad",
                    "lines": self.random_lines(False),
                },
            ).json()["id"]
            resp = self.client.post(
                "/api/v1/journal-entries/batch-post", json={"entry_ids": ids + [bad]}
            )
            assert resp.status_code == 422
            return
        resp = self.client.post("/api/v1/journal-entries/batch-post", json={"entry_ids": ids})
        assert resp.status_code == 200, resp.text
        self.posted.extend(ids)
        assert (
            self.client.post("/api/v1/journal-entries/batch-post", json={"entry_ids": ids}).json()[
                "posted_now"
            ]
            == []
        )

    def op_processing(self):
        self.source_counter += 1
        body = {
            "source_type": "sim",
            "source_id": f"DOC-{self.source_counter}",
            "operation_id": "post",
            "entry_date": self.day(),
            "description": "sim",
            "lines": self.random_lines(True),
        }
        first = self.client.post("/api/v1/journal-processing/entries", json=body)
        assert first.status_code == 200, first.text
        self.posted.append(first.json()["entry"]["id"])
        retry = self.client.post("/api/v1/journal-processing/entries", json=body).json()
        assert retry["created"] is False and retry["entry"]["id"] == first.json()["entry"]["id"]
        conflict = dict(body)
        conflict["lines"] = self.random_lines(True)
        assert (
            self.client.post("/api/v1/journal-processing/entries", json=conflict).status_code == 409
        )

    def op_edit_draft(self):
        drafts = [e for e in self.entries if e not in self.posted]
        if not drafts:
            return
        target = self.rng.choice(drafts)
        current = self.client.get(f"/api/v1/journal-entries/{target}").json()
        if current["status"] != "draft":
            return
        resp = self.client.put(
            f"/api/v1/journal-entries/{target}",
            json={
                "entry_date": self.day(),
                "description": "edited",
                "version": current["version"],
                "lines": self.random_lines(True),
            },
        )
        assert resp.status_code == 200, resp.text
        stale = self.client.put(
            f"/api/v1/journal-entries/{target}",
            json={
                "entry_date": self.day(),
                "description": "stale",
                "version": current["version"],
                "lines": self.random_lines(True),
            },
        )
        assert stale.status_code == 409

    # --- invariants ------------------------------------------------------------------

    def check_invariants(self):
        report = trial_balance(self.client)
        assert report["is_balanced"], report
        rows = {r["code"]: r for r in report["rows"]}
        with Session(self.client.app.state.engine) as db:
            posted = db.scalars(
                select(JournalEntry).where(JournalEntry.status == EntryStatus.POSTED)
            ).all()
            detail = {}
            for entry in posted:
                debit = sum((ln.debit for ln in entry.lines), Decimal("0"))
                credit = sum((ln.credit for ln in entry.lines), Decimal("0"))
                assert debit == credit and len(entry.lines) >= 2, entry.id
                for ln in entry.lines:
                    assert (ln.debit > 0) != (ln.credit > 0)
                    d, c = detail.get(ln.account_id, (Decimal("0"), Decimal("0")))
                    detail[ln.account_id] = (d + ln.debit, c + ln.credit)
            aggregate = {
                acc: (
                    Decimal(str(d or 0)).quantize(Decimal("0.01")),
                    Decimal(str(c or 0)).quantize(Decimal("0.01")),
                )
                for acc, d, c in db.execute(
                    select(
                        JournalLine.account_id,
                        func.sum(JournalLine.debit),
                        func.sum(JournalLine.credit),
                    )
                    .join(JournalEntry)
                    .where(JournalEntry.status == EntryStatus.POSTED)
                    .group_by(JournalLine.account_id)
                )
            }
        assert aggregate == detail
        for acc in self.posting:
            d, c = detail.get(acc["id"], (Decimal("0"), Decimal("0")))
            assert (
                Decimal(rows[acc["code"]]["debit"]) == d
                and Decimal(rows[acc["code"]]["credit"]) == c
            )
        # Summary roll-ups.
        assert Decimal(rows["10"]["debit"]) == Decimal(rows["1000"]["debit"]) + Decimal(
            rows["1100"]["debit"]
        )
        assert Decimal(rows["1"]["credit"]) == Decimal(rows["10"]["credit"]) + Decimal(
            rows["1500"]["credit"]
        )
        assert Decimal(report["total_debit"]) == sum(d for d, _ in detail.values())
        recon = self.client.get("/api/v1/reconciliation").json()
        assert recon["ok"] and recon["error_count"] == 0, recon


@pytest.mark.parametrize("seed", SEEDS)
def test_random_workload_keeps_invariants(client, seed):
    rng = random.Random(seed)
    ledger = Ledger(client, rng)
    operations = [
        ledger.op_create_and_post,
        ledger.op_reverse,
        ledger.op_batch,
        ledger.op_processing,
        ledger.op_edit_draft,
    ]
    for step in range(40):
        rng.choice(operations)()
        if step % 8 == 7:
            ledger.check_invariants()
    ledger.check_invariants()
    totals_before = balances(client)
    # A burst of failures and repeats must leave the totals unchanged.
    for _ in range(5):
        bad = client.post(
            "/api/v1/journal-entries",
            json={
                "entry_date": ledger.day(),
                "description": "x",
                "lines": ledger.random_lines(False),
            },
        ).json()
        assert client.post(f"/api/v1/journal-entries/{bad['id']}/post").status_code == 422
        client.delete(f"/api/v1/journal-entries/{bad['id']}")
        if ledger.posted:
            assert (
                client.post(f"/api/v1/journal-entries/{ledger.posted[0]}/post").status_code == 409
            )
    assert balances(client) == totals_before
    assert len(ledger.posted) > 0
