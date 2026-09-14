"""Non-destructive AP preflight: completion evidence of historical payment operations.

Usage::

    python scripts/ap_preflight.py [--url postgresql+psycopg://...] [--json]

Reads only. Reports every ``completed`` payment operation whose completion evidence is
not in the database (no linked payment, payment of another vendor / amount / cash
account, journal entry missing or not posted, entry not crediting cash by the amount,
no gateway confirmation reference) -- exactly what migration ``fmspayables06`` labels
``accounting_status = 'unverified'`` -- plus, on a migrated database, the full verifier
findings for every operation whose money moved. Nothing is changed; remediation is an
explicit workflow (``POST /payment-operations/{uid}/corrections`` or ``.../complete``).
"""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import make_engine  # noqa: E402


def _migration_preflight():
    path = ROOT / "migrations" / "versions" / "20261201_fmspayables06_completion_evidence.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.completion_preflight


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("FMS_DATABASE_URL"),
        help="database URL (default: FMS_DATABASE_URL)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or FMS_DATABASE_URL is required")
    engine = make_engine(args.url)
    report = {"completed_operations": [], "verifier": None}
    try:
        with engine.connect() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                        if engine.dialect.name == "postgresql"
                        else "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                )
            }
            if "payment_operations" not in tables:
                print("no payment_operations table: nothing to check (fmspayables05 not applied)")
                return 0
            report["completed_operations"] = _migration_preflight()(conn)
            columns = {
                r[1] if engine.dialect.name == "sqlite" else r[0]
                for r in conn.execute(
                    text(
                        "PRAGMA table_info(payment_operations)"
                        if engine.dialect.name == "sqlite"
                        else "SELECT column_name FROM information_schema.columns WHERE table_name = 'payment_operations'"
                    )
                )
            }
        if "accounting_status" in columns:
            from app.modules.payables.integration import Integrations
            from app.modules.payables.services import verification

            with Session(engine) as db:
                results = verification.verify_operations(
                    db, gateway=Integrations().gateway, limit=100000
                )
            report["verifier"] = [
                r.as_dict()
                for r in results
                if r.blocking_findings
                or (r.state == "completed" and not r.completion_policy_satisfied)
            ]
    finally:
        engine.dispose()
    rows = report["completed_operations"]
    bad = [r for r in rows if not r["evidence"]]
    if args.json:
        print(json.dumps(report, default=str, indent=2))
        return 1 if bad or report["verifier"] else 0
    print(f"completed operations: {len(rows)}; without completion evidence: {len(bad)}")
    for r in bad:
        print(
            f"  {r['operation_uid']} payment={r['payment_id']} manual={r['manual']}: "
            + "; ".join(r["problems"])
        )
    if report["verifier"] is not None:
        print(f"verifier: {len(report['verifier'])} operation(s) with blocking findings")
        for v in report["verifier"]:
            print(
                f"  {v['operation_uid']} [{v['state']}] accounting={v['accounting_status']}: "
                + "; ".join(
                    f"{f['code']}({f['severity']})"
                    for f in v["findings"]
                    if f["severity"] in ("critical", "error")
                )
            )
    return 1 if bad or report["verifier"] else 0


if __name__ == "__main__":
    sys.exit(main())
