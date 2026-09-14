"""Administrative cleanup of leaked PostgreSQL scratch databases (test infrastructure).

Usage::

    python scripts/ap_scratch_cleanup.py --url postgresql+psycopg://... --list
    python scripts/ap_scratch_cleanup.py --url ... --operator jane --confirm NAME [NAME ...]
    python scripts/ap_scratch_cleanup.py --url ... --operator jane --confirm --grace-minutes 30 NAME

Only *listed* names are considered and each one is dropped only when it is proven
abandoned (created on this host and boot by a process that no longer exists, older than
the grace period, no sessions connected) or when its owner token is supplied with
``--token NAME=TOKEN``. Never ``DROP DATABASE IF EXISTS``, never by pattern, never forced.
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.payables_support import dbscratch  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--operator", default="")
    parser.add_argument("--grace-minutes", type=int, default=10)
    parser.add_argument(
        "--token", action="append", default=[], help="NAME=TOKEN handed over by the owning run"
    )
    parser.add_argument("names", nargs="*")
    args = parser.parse_args(argv)
    if args.list or not args.names:
        for entry in dbscratch.list_scratch_databases(args.url):
            own = entry.ownership
            print(
                f"{entry.name}  connections={entry.connections}  "
                + (
                    f"host={own.host} pid={own.pid} created={own.created}"
                    if own
                    else "ownership: unknown"
                )
            )
        return 0
    tokens = dict(t.split("=", 1) for t in args.token)
    decisions = dbscratch.administrative_cleanup(
        args.url,
        args.names,
        confirm=args.confirm,
        operator=args.operator,
        grace=timedelta(minutes=args.grace_minutes),
        tokens=tokens or None,
    )
    for d in decisions:
        print(f"{d.action:8} {d.name}: {d.reason}")
    return 0 if all(d.action == "dropped" for d in decisions) else 1


if __name__ == "__main__":
    sys.exit(main())
