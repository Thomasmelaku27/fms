"""Configurable General Ledger load benchmark (not part of the test suite).

Usage:
    PYTHONPATH=. .venv/bin/python scripts/gl_benchmark.py sqlite:////tmp/bench.db 5000
    PYTHONPATH=. .venv/bin/python scripts/gl_benchmark.py postgresql+psycopg://u:p@host/db 5000

Creates a chart of 40 accounts under a 3-level hierarchy, posts N balanced
entries through the journal-processing batch endpoint (idempotent retries
included), then times: a retried batch, trial balance, General Ledger (full
and paginated), account balances from the aggregate and from the lines,
hierarchy roll-up, reconciliation, aggregate verification and entry listing.
The database must be a disposable one created for the run.
"""

import os
import random
import sys
import time
from datetime import date, timedelta
from decimal import Decimal

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def migrate(url):
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def timed(fn):
    t0 = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t0


def main(url: str, n: int, batch: int = 200, seed: int = 7) -> dict:
    if url.startswith("sqlite:///"):
        path = url.split("sqlite:///")[1]
        if os.path.exists(path):
            os.remove(path)
    migrate(url)
    app = create_app(Settings(database_url=url))
    rng = random.Random(seed)
    results = {}
    with TestClient(app) as c:
        # 3-level hierarchy: 5 roots -> 2 groups each -> 4 posting accounts each = 40 posting.
        posting = []
        for t_index, type_ in enumerate(["asset", "liability", "equity", "revenue", "expense"]):
            root = c.post(
                "/api/v1/accounts",
                json={"code": f"{t_index + 1}", "name": type_, "type": type_, "is_summary": True},
            ).json()
            for g in range(2):
                group = c.post(
                    "/api/v1/accounts",
                    json={
                        "code": f"{t_index + 1}{g}",
                        "name": f"{type_} {g}",
                        "type": type_,
                        "is_summary": True,
                        "parent_id": root["id"],
                    },
                ).json()
                for k in range(4):
                    posting.append(
                        c.post(
                            "/api/v1/accounts",
                            json={
                                "code": f"{t_index + 1}{g}{k}0",
                                "name": f"{type_} {g}.{k}",
                                "type": type_,
                                "parent_id": group["id"],
                            },
                        ).json()
                    )

        def post_all():
            last = None
            for b in range(n // batch):
                entries = []
                for i in range(batch):
                    chosen = rng.sample(posting, rng.randint(2, 4))
                    amounts = [Decimal(rng.randint(100, 99999)) / 100 for _ in chosen[:-1]]
                    lines = [
                        {"account_id": a["id"], "debit": f"{x:.2f}"}
                        for a, x in zip(chosen, amounts)
                    ]
                    lines.append({"account_id": chosen[-1]["id"], "credit": f"{sum(amounts):.2f}"})
                    entries.append(
                        {
                            "source_type": "load",
                            "source_id": f"D-{b}-{i}",
                            "operation_id": "post",
                            "entry_date": (
                                date(2026, 1, 1) + timedelta(days=rng.randint(0, 364))
                            ).isoformat(),
                            "description": "load",
                            "lines": lines,
                        }
                    )
                r = c.post("/api/v1/journal-processing/batches", json={"entries": entries})
                assert r.status_code == 200, r.text[:300]
                last = entries
            return last

        last, results["post_entries_s"] = timed(post_all)
        results["post_rate_per_s"] = round(n / results["post_entries_s"], 1)
        _, results["retry_batch_s"] = timed(
            lambda: c.post("/api/v1/journal-processing/batches", json={"entries": last})
        )
        tb, results["trial_balance_s"] = timed(
            lambda: c.get("/api/v1/reports/trial-balance").json()
        )
        assert tb["is_balanced"]
        gl, results["general_ledger_full_s"] = timed(
            lambda: c.get("/api/v1/reports/general-ledger").json()
        )
        results["lines"] = sum(len(a["lines"]) for a in gl["accounts"])
        _, results["general_ledger_page_500_s"] = timed(
            lambda: c.get("/api/v1/reports/general-ledger", params={"limit": 500}).json()
        )
        page = c.get("/api/v1/reports/general-ledger", params={"limit": 500}).json()
        _, results["general_ledger_page_after_s"] = timed(
            lambda: c.get(
                "/api/v1/reports/general-ledger",
                params={"limit": 500, "after": page["next_cursor"]},
            ).json()
        )
        _, results["account_balances_aggregate_s"] = timed(
            lambda: c.get(
                "/api/v1/reports/account-balances",
                params={"from": "2026-04-01", "to": "2026-09-30", "source": "aggregate"},
            ).json()
        )
        _, results["account_balances_lines_s"] = timed(
            lambda: c.get(
                "/api/v1/reports/account-balances",
                params={"from": "2026-04-01", "to": "2026-09-30", "source": "lines"},
            ).json()
        )
        _, results["hierarchy_s"] = timed(
            lambda: c.get(
                "/api/v1/reports/hierarchy", params={"from": "2026-01-01", "to": "2026-12-31"}
            ).json()
        )
        recon, results["reconciliation_s"] = timed(lambda: c.get("/api/v1/reconciliation").json())
        assert recon["ok"], [ch for ch in recon["checks"] if ch["findings"]]
        verify, results["verify_aggregate_s"] = timed(
            lambda: c.post("/api/v1/balances/verify").json()
        )
        assert verify["ok"]
        _, results["list_entries_full_s"] = timed(
            lambda: c.get("/api/v1/journal-entries?status=posted").json()
        )
        _, results["list_entries_page_100_s"] = timed(
            lambda: c.get("/api/v1/journal-entries/page", params={"limit": 100}).json()
        )
    app.state.engine.dispose()
    results["backend"] = url.split(":")[0]
    results["entries"] = n
    return results


if __name__ == "__main__":
    url, n = sys.argv[1], int(sys.argv[2])
    out = main(url, n)
    print(", ".join(f"{k}={v}" for k, v in out.items()))
