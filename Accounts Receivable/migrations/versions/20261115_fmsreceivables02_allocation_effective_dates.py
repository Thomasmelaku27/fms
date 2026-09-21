"""Allocation effective dates and idempotent allocation requests.

Additive follow-up to ``fmsreceivables01`` (which is left untouched):

* ``invoice_allocations.effective_date`` - the date an allocation entered the
  books. "As of" reports filter on it instead of ``allocation_date`` so that
  applying an old payment today cannot change an earlier aging/statement
  figure. Existing rows are backfilled with their ``allocation_date``, which
  is what they were reported on before.
* ``allocation_requests`` - one row per idempotent allocation request
  (unique key + hash of every significant field), giving allocation batches
  the same replay/conflict guarantees payments already have, and
  ``invoice_allocations.request_id`` linking the rows of a batch to it.
  Allocations recorded before this migration are linked by their
  ``idempotency_key`` with ``request_hash = NULL`` (legacy: replayed without
  comparison).

The backfill has to update ``invoice_allocations``, which migration
``fmsreceivables01`` made append-only. The guard is therefore suspended for
the duration of the UPDATE and restored immediately afterwards (PostgreSQL:
the trigger is disabled/enabled; SQLite: the UPDATE trigger is dropped and
re-created from the same DDL).

Revision history
----------------
* 2026-11-15: original.
* 2026-12-05 (controlled edit, after ``fmsreceivables03`` existed): the original
  backfill failed on ``ck_allocation_requests_exactly_one_source`` when the old
  code had used one allocation key for a payment batch *and* a credit batch;
  on SQLite (non-transactional DDL) that left the database half-upgraded.
  Two changes, both documented here rather than made silently:

  - such *mixed* keys are excluded from the backfill (the table cannot hold two
    sources for one key). Their allocation rows are untouched and keep the key;
    the application treats a key that has allocation rows but no request row as
    unverifiable and refuses any reuse of it (409), see
    ``services._allocate_existing``;
  - every step is guarded by inspection, so a database left half-upgraded by
    the original version is completed by simply running the upgrade again.
* 2026-12-12: the table and its two indexes shared one existence check, so an
  upgrade interrupted between them left the indexes missing for good. Every
  object (table, each index, column, trigger) and every backfill statement is
  now checked individually (``STEPS``), and the end state is verified; a
  pre-existing ``allocation_requests`` table of a different shape makes the
  upgrade stop with an explanation instead of building on it.

  For databases that already passed this revision nothing runs again, and the
  new body yields exactly the same end state for them (they cannot contain a
  mixed key, otherwise the original would have failed) - asserted by
  ``tests/receivables/test_mixed_key_upgrade.py``.

Revision ID: fmsreceivables02
Revises: fmsreceivables01
Create Date: 2026-11-15 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fmsreceivables02"
down_revision: str | None = "fmsreceivables01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Repeated verbatim from fmsreceivables01 so this migration is self-contained.
MSG_ALLOC_IMMUTABLE = "invoice allocations cannot be modified or deleted"

SQLITE_NO_UPDATE_TRIGGER = f"""
CREATE TRIGGER trg_invoice_allocations_no_update
BEFORE UPDATE ON invoice_allocations FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_IMMUTABLE}');
END
"""

# Transient default: the only way to add a NOT NULL column to existing rows on
# both backends. Every row is given its real value by the backfill below, and
# the application always sets the column explicitly.
PLACEHOLDER_DATE = "1900-01-01"


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for receivables: {name}")
    return name


# --- Individually idempotent steps ---------------------------------------------------------
#
# ``upgrade`` runs STEPS in order. Each step first checks whether its own object
# or effect is already present, so the upgrade can be re-run after an
# interruption at any point (SQLite DDL is not transactional). The tests drive
# these steps directly to simulate such interruptions.

EXPECTED_COLUMNS = {"id", "idempotency_key", "payment_id", "adjustment_id", "request_hash", "effective_date", "created_at"}
INDEXES = {
    "ix_allocation_requests_payment_id": ["payment_id"],
    "ix_allocation_requests_adjustment_id": ["adjustment_id"],
}
SQLITE_ALLOCATION_TRIGGERS = (
    "trg_invoice_allocations_closed_invoice",
    "trg_invoice_allocations_within_invoice",
    "trg_invoice_allocations_within_payment",
    "trg_invoice_allocations_within_adjustment",
    "trg_invoice_allocations_no_update",
    "trg_invoice_allocations_no_delete",
)


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(name: str) -> bool:
    return name in _inspector().get_table_names()


def _column(table: str, column: str) -> dict | None:
    return next((c for c in _inspector().get_columns(table) if c["name"] == column), None)


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in _inspector().get_indexes(table)}


def _sqlite_trigger_names() -> set[str]:
    rows = op.get_bind().execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
    return set(rows.scalars())


def _create_table() -> None:
    if _table_exists("allocation_requests"):
        _verify_table_shape()
        return
    op.create_table(
        "allocation_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("adjustment_id", sa.Integer(), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(payment_id IS NOT NULL AND adjustment_id IS NULL)"
            " OR (payment_id IS NULL AND adjustment_id IS NOT NULL)",
            name=op.f("ck_allocation_requests_exactly_one_source"),
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["customer_payments.id"], name=op.f("fk_allocation_requests_payment_id_customer_payments")),
        sa.ForeignKeyConstraint(["adjustment_id"], ["customer_adjustments.id"], name=op.f("fk_allocation_requests_adjustment_id_customer_adjustments")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocation_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_allocation_requests_idempotency_key")),
    )


def _verify_table_shape() -> None:
    """An existing ``allocation_requests`` must be the one this revision creates."""
    inspector = _inspector()
    problems = []
    columns = {c["name"] for c in inspector.get_columns("allocation_requests")}
    if columns != EXPECTED_COLUMNS:
        problems.append(f"columns are {sorted(columns)}, expected {sorted(EXPECTED_COLUMNS)}")
    unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("allocation_requests")}
    if ("idempotency_key",) not in unique:
        problems.append("unique constraint on idempotency_key is missing")
    checks = {c["name"] for c in inspector.get_check_constraints("allocation_requests")}
    if "ck_allocation_requests_exactly_one_source" not in checks:
        problems.append("check constraint ck_allocation_requests_exactly_one_source is missing")
    foreign = {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("allocation_requests")}
    for column in ("payment_id", "adjustment_id"):
        if (column,) not in foreign:
            problems.append(f"foreign key on {column} is missing")
    if problems:
        raise RuntimeError(
            "fmsreceivables02: an allocation_requests table exists but is not the one this revision creates "
            "(" + "; ".join(problems) + "). It was not created by this migration; inspect it before upgrading"
        )


def _create_indexes() -> None:
    present = _index_names("allocation_requests")
    for name, columns in INDEXES.items():
        if name not in present:
            op.create_index(op.f(name), "allocation_requests", columns)


def _add_column() -> None:
    column = _column("invoice_allocations", "effective_date")
    if column is None:
        # Plain ADD COLUMN on both backends: SQLite keeps the table (and therefore
        # the append-only triggers installed by fmsreceivables01) in place.
        op.add_column(
            "invoice_allocations",
            sa.Column("effective_date", sa.Date(), nullable=False, server_default=PLACEHOLDER_DATE),
        )
    elif column["nullable"]:
        raise RuntimeError(
            "fmsreceivables02: invoice_allocations.effective_date exists but is nullable; it was not added "
            "by this migration. Inspect it before upgrading"
        )


def _suspend_guard() -> None:
    # Both statements are idempotent; the guard is restored by _restore_guard.
    if _dialect() == "postgresql":
        op.execute("ALTER TABLE invoice_allocations DISABLE TRIGGER trg_invoice_allocations_guard")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_invoice_allocations_no_update")


def _backfill_dates() -> None:
    # Only rows still carrying the placeholder; real data is never overwritten.
    op.execute(
        f"UPDATE invoice_allocations SET effective_date = allocation_date WHERE effective_date = '{PLACEHOLDER_DATE}'"
    )


def _backfill_requests() -> None:
    # One request row per pre-existing keyed batch (hash unknown -> NULL). Keys
    # already recorded are skipped; keys shared by a payment and a credit batch
    # are left without a row (see revision history).
    op.execute(
        """
        INSERT INTO allocation_requests (idempotency_key, payment_id, adjustment_id, request_hash, effective_date)
        SELECT idempotency_key, MAX(payment_id), MAX(adjustment_id), NULL, MIN(allocation_date)
          FROM invoice_allocations
         WHERE idempotency_key IS NOT NULL
           AND idempotency_key NOT IN (SELECT idempotency_key FROM allocation_requests)
      GROUP BY idempotency_key
        HAVING COUNT(payment_id) = 0 OR COUNT(adjustment_id) = 0
        """
    )


def _restore_guard() -> None:
    if _dialect() == "postgresql":
        op.execute("ALTER TABLE invoice_allocations ENABLE TRIGGER trg_invoice_allocations_guard")
    elif "trg_invoice_allocations_no_update" not in _sqlite_trigger_names():
        op.execute(SQLITE_NO_UPDATE_TRIGGER)


def _verify_end_state() -> None:
    """Everything this revision is responsible for must be in place before the version is stamped."""
    problems = []
    if not _table_exists("allocation_requests"):
        problems.append("table allocation_requests is missing")
    else:
        problems.extend(f"index {name} is missing" for name in INDEXES if name not in _index_names("allocation_requests"))
    if _column("invoice_allocations", "effective_date") is None:
        problems.append("column invoice_allocations.effective_date is missing")
    else:
        placeholders = op.get_bind().execute(
            sa.text(f"SELECT COUNT(*) FROM invoice_allocations WHERE effective_date = '{PLACEHOLDER_DATE}'")
        ).scalar()
        if placeholders:
            problems.append(f"{placeholders} allocation rows still carry the placeholder effective date")
    if _dialect() == "postgresql":
        enabled = op.get_bind().execute(
            sa.text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'trg_invoice_allocations_guard'")
        ).scalar()
        if enabled != "O":
            problems.append("trigger trg_invoice_allocations_guard is missing or disabled")
    else:
        missing = sorted(set(SQLITE_ALLOCATION_TRIGGERS) - _sqlite_trigger_names())
        problems.extend(f"trigger {name} is missing" for name in missing)
    if problems:
        raise RuntimeError("fmsreceivables02 did not reach its end state: " + "; ".join(problems))


STEPS = (
    ("create table allocation_requests", _create_table),
    ("create its indexes", _create_indexes),
    ("add invoice_allocations.effective_date", _add_column),
    ("suspend the append-only guard", _suspend_guard),
    ("backfill effective dates", _backfill_dates),
    ("backfill request rows", _backfill_requests),
    ("restore the append-only guard", _restore_guard),
    ("verify the end state", _verify_end_state),
)


def upgrade() -> None:
    _dialect()
    for _, step in STEPS:
        step()


def downgrade() -> None:
    _dialect()
    # SQLite >= 3.35 supports DROP COLUMN; no table rebuild, so the triggers survive.
    op.drop_column("invoice_allocations", "effective_date")
    op.drop_index(op.f("ix_allocation_requests_adjustment_id"), table_name="allocation_requests")
    op.drop_index(op.f("ix_allocation_requests_payment_id"), table_name="allocation_requests")
    op.drop_table("allocation_requests")
