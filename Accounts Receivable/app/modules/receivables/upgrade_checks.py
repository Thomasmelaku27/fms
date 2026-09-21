"""Read-only checks around the ``fmsreceivables02`` upgrade.

Nothing here writes to the database. Run it against the database configured in
``FMS_DATABASE_URL`` before (or after a failed) ``alembic upgrade head``::

    python -m app.modules.receivables.upgrade_checks

It reports:

* allocation keys that the old code used for both a payment batch and a credit
  batch. ``fmsreceivables02`` (since its 2026-12 revision) leaves such keys
  without a request row; their allocations are kept untouched and the
  application refuses any reuse of the key as unverifiable;
* a database left half-upgraded by the *original* ``fmsreceivables02`` on
  SQLite (version still ``fmsreceivables01`` but parts of the revision
  applied). Running ``alembic upgrade head`` again completes it.
"""

from __future__ import annotations

import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.db import make_engine

MIXED_SOURCE_KEYS = text(
    "SELECT idempotency_key FROM invoice_allocations "
    "WHERE idempotency_key IS NOT NULL "
    "GROUP BY idempotency_key "
    "HAVING COUNT(payment_id) > 0 AND COUNT(adjustment_id) > 0 "
    "ORDER BY idempotency_key"
)


def mixed_source_allocation_keys(conn: Connection) -> list[str]:
    """Allocation keys shared by a payment batch and a credit batch (read-only)."""
    if "invoice_allocations" not in inspect(conn).get_table_names():
        return []
    return list(conn.execute(MIXED_SOURCE_KEYS).scalars())


def half_applied_fmsreceivables02(conn: Connection) -> list[str]:
    """Signs that the original fmsreceivables02 stopped half-way (SQLite); empty if none."""
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables or "invoice_allocations" not in tables:
        return []
    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if version != "fmsreceivables01":
        return []
    findings = []
    if "allocation_requests" in tables:
        findings.append("table allocation_requests exists")
    if "effective_date" in {c["name"] for c in inspector.get_columns("invoice_allocations")}:
        findings.append("column invoice_allocations.effective_date exists")
    if conn.dialect.name == "sqlite":
        present = conn.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_invoice_allocations_no_update'")
        ).scalar()
        if not present:
            findings.append("append-only UPDATE trigger on invoice_allocations is missing")
    return findings


def main() -> int:
    engine = make_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            mixed = mixed_source_allocation_keys(conn)
            partial = half_applied_fmsreceivables02(conn)
            conn.rollback()  # nothing was written; make that explicit
    finally:
        engine.dispose()
    if mixed:
        print("Allocation keys used for both a payment and a credit batch (kept as they are; the upgrade "
              "records no request row for them and any reuse of the key is refused as unverifiable):")
        for key in mixed:
            print(f"  {key}")
    if partial:
        print("The original fmsreceivables02 stopped half-way on this database; run `alembic upgrade head` "
              "to complete it:")
        for finding in partial:
            print(f"  {finding}")
    if not mixed and not partial:
        print("Nothing to report for fmsreceivables02.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
