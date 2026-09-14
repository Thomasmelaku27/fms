"""Run the AP recovery worker once, as its own process (development / operations utility).

Usage::

    python scripts/ap_recovery_worker.py --url <database url> [--worker-id w1] [--limit 50] [--json]

A restarted worker has no memory: everything it needs is in ``payment_operations`` /
``payment_operation_events`` and the gateway's durable store. It never submits a new
transfer for an operation whose money moved; see ``services/recovery.py``.
"""

import argparse
import json
import os
import socket
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from app.db import make_engine  # noqa: E402
from app.modules.payables.integration import Integrations  # noqa: E402
from app.modules.payables.services import recovery  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=os.environ.get("FMS_DATABASE_URL"))
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or FMS_DATABASE_URL is required")
    engine = make_engine(args.url)
    try:
        with Session(engine, expire_on_commit=False) as db:
            results = recovery.process(
                db,
                integrations=Integrations(),
                worker_id=args.worker_id,
                limit=args.limit,
                actor=args.worker_id,
            )
    finally:
        engine.dispose()
    if args.json:
        print(json.dumps([asdict(r) for r in results]))
    else:
        for r in results:
            print(f"{r.operation_uid} {r.action}: {r.before} -> {r.after} ({r.note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
