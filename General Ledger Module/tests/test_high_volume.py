"""Realistic larger ledger: throughput of batch processing, reports and reconciliation.

Sizes are deliberately modest so the suite stays fast on both backends; the
timings are printed (run with ``-s``) and asserted only against generous
ceilings. Larger one-off measurements are reported in the handoff notes.
"""

import os
import random
import time
from datetime import date, timedelta
from decimal import Decimal

from tests.helpers import account, trial_balance

ENTRIES = int(os.environ.get("FMS_LOAD_ENTRIES", "600"))  # configurable load size
BATCH = 100


def _chart(client):
    types = [("asset", 8), ("liability", 4), ("equity", 2), ("revenue", 6), ("expense", 10)]
    accounts = []
    code = 1000
    for type_, count in types:
        for _ in range(count):
            accounts.append(account(client, str(code), f"A{code}", type_))
            code += 100
    return accounts


def test_high_volume_batches_reports_and_reconciliation(client):
    rng = random.Random(42)
    accounts = _chart(client)
    timings = {}

    t0 = time.perf_counter()
    for batch_index in range(ENTRIES // BATCH):
        entries = []
        for i in range(BATCH):
            n = rng.randint(2, 4)
            chosen = rng.sample(accounts, n)
            amounts = [Decimal(rng.randint(100, 99999)) / 100 for _ in range(n - 1)]
            lines = [
                {"account_id": a["id"], "debit": f"{amt:.2f}"} for a, amt in zip(chosen, amounts)
            ]
            lines.append({"account_id": chosen[-1]["id"], "credit": f"{sum(amounts):.2f}"})
            entries.append(
                {
                    "source_type": "load",
                    "source_id": f"DOC-{batch_index}-{i}",
                    "operation_id": "post",
                    "entry_date": (
                        date(2026, 1, 1) + timedelta(days=rng.randint(0, 364))
                    ).isoformat(),
                    "description": f"load {batch_index}/{i}",
                    "lines": lines,
                }
            )
        resp = client.post("/api/v1/journal-processing/batches", json={"entries": entries})
        assert resp.status_code == 200, resp.text[:300]
    timings["post_entries"] = time.perf_counter() - t0

    # A retried batch is an exact no-op (and fast: no writes).
    t0 = time.perf_counter()
    again = client.post("/api/v1/journal-processing/batches", json={"entries": entries})
    timings["retry_batch"] = time.perf_counter() - t0
    assert all(r["created"] is False for r in again.json()["results"])

    t0 = time.perf_counter()
    report = trial_balance(client)
    timings["trial_balance"] = time.perf_counter() - t0
    assert report["is_balanced"] and Decimal(report["total_debit"]) > 0

    t0 = time.perf_counter()
    gl = client.get("/api/v1/reports/general-ledger").json()
    timings["general_ledger"] = time.perf_counter() - t0
    assert sum(len(a["lines"]) for a in gl["accounts"]) >= ENTRIES * 2

    t0 = time.perf_counter()
    recon = client.get("/api/v1/reconciliation").json()
    timings["reconciliation"] = time.perf_counter() - t0
    assert recon["ok"], recon["checks"]

    t0 = time.perf_counter()
    listing = client.get("/api/v1/journal-entries?status=posted").json()
    timings["list_entries"] = time.perf_counter() - t0
    assert len(listing) == ENTRIES

    t0 = time.perf_counter()
    agg = client.get(
        "/api/v1/reports/account-balances", params={"from": "2026-04-01", "to": "2026-09-30"}
    ).json()
    timings["balances_aggregate"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    lines_src = client.get(
        "/api/v1/reports/account-balances",
        params={"from": "2026-04-01", "to": "2026-09-30", "source": "lines"},
    ).json()
    timings["balances_lines"] = time.perf_counter() - t0
    assert [r["closing_balance"] for r in agg["rows"]] == [
        r["closing_balance"] for r in lines_src["rows"]
    ]
    t0 = time.perf_counter()
    page = client.get("/api/v1/reports/general-ledger", params={"limit": 200}).json()
    timings["gl_page_200"] = time.perf_counter() - t0
    assert page["next_cursor"] is not None
    t0 = time.perf_counter()
    assert client.post("/api/v1/balances/verify").json()["ok"]
    timings["verify_aggregate"] = time.perf_counter() - t0

    backend = client.app.state.engine.dialect.name
    print(
        f"\n[{backend}] {ENTRIES} entries: "
        + ", ".join(f"{k}={v:.2f}s" for k, v in timings.items())
    )
    assert (
        timings["post_entries"] < 120
        and timings["trial_balance"] < 10
        and timings["reconciliation"] < 30
    )
