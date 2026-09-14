"""Accounts payable: durable payment operations, run versioning, close attestations,
supplier statement imports.

Revises ``fmspayables04`` (the head of the payables chain in this branch).

Schema
------
* ``payment_operations`` / ``payment_operation_events``: durable payment intents with
  a compare-and-swap state machine and an append-only history.
* ``simulated_gateway_transfers``: durable store of the *simulated* gateway.
* ``payment_runs``: ``version`` (optimistic locking), ``proposal_fingerprint``,
  ``approved_fingerprint``, ``approval_invalidated_reason``;
  ``payment_run_items.destination_reference`` (payment instruction snapshot).
* ``ap_close_attestations``: immutable record of every close attempt.
* ``supplier_statement_imports`` / ``supplier_statement_lines``: imported supplier
  statements as review evidence (never posted).

Back-fill
---------
Every run item already ``paid`` through a payment run gets a ``completed``
``payment_operations`` row per (run, vendor) pointing at its recorded payment, so
diagnostics and recovery scans see historical executions.

Database rules
--------------
* operation state transitions follow the documented machine; identity and
  instruction columns are frozen; gateway confirmation and AP payment links are set
  once; terminal operations never change state;
* operation events and close attestations are append-only;
* payment runs additionally allow ``approved -> draft`` (approval invalidated by a
  material change; the run must be reviewed and approved again).

Downgrade (restricted)
----------------------
The downgrade drops execution records, close attestations and statement imports.
It refuses to run while any of those tables contains rows that were not produced
by this revision's back-fill, unless ``FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1`` is set.
Posted AP and GL history is never touched either way.

Revision ID: fmspayables05
Revises: fmspayables04
Create Date: 2026-11-20 00:00:00
"""

import importlib.util
import os
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "fmspayables05"
down_revision: str | None = "fmspayables04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sibling(filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V4 = _sibling("20261110_fmspayables04_payment_runs.py")

MSG_OP_BAD_TRANSITION = "invalid payment operation state transition"
MSG_OP_FROZEN = "identity and instruction fields of a payment operation cannot be modified"
MSG_OP_NO_DELETE = "payment operations are never deleted"
MSG_OP_EVENTS_IMMUTABLE = "payment operation events are append-only"
MSG_ATTESTATION_IMMUTABLE = "AP close attestations are append-only"

SQL_OP_TRANSITION = """(
       (OLD.state = 'prepared' AND NEW.state IN ('submitted', 'failed'))
    OR (OLD.state = 'submitted' AND NEW.state IN ('confirmed', 'confirmation_pending', 'failed',
                                                  'reconciliation_required'))
    OR (OLD.state = 'confirmation_pending' AND NEW.state IN ('confirmed', 'failed',
                                                             'reconciliation_required'))
    OR (OLD.state = 'confirmed' AND NEW.state IN ('recording', 'reconciliation_required'))
    OR (OLD.state = 'recording' AND NEW.state IN ('completed', 'reconciliation_required'))
    OR (OLD.state = 'reconciliation_required' AND NEW.state IN ('recording', 'completed'))
)"""
SQL_OP_FROZEN_CHANGED = """(
       NEW.operation_uid <> OLD.operation_uid
    OR NEW.idempotency_key <> OLD.idempotency_key
    OR NEW.fingerprint <> OLD.fingerprint
    OR COALESCE(NEW.run_id, -1) <> COALESCE(OLD.run_id, -1)
    OR NEW.vendor_id <> OLD.vendor_id
    OR NEW.amount <> OLD.amount
    OR NEW.currency <> OLD.currency
    OR NEW.payment_date <> OLD.payment_date
    OR NEW.method <> OLD.method
    OR NEW.cash_account_id <> OLD.cash_account_id
    OR NEW.plan_json <> OLD.plan_json
    OR NEW.created_by <> OLD.created_by
    OR (OLD.gateway_confirmation_ref IS NOT NULL
        AND COALESCE(NEW.gateway_confirmation_ref, '') <> OLD.gateway_confirmation_ref)
    OR (OLD.payment_id IS NOT NULL AND COALESCE(NEW.payment_id, -1) <> OLD.payment_id)
)"""

SQL_RUN_TRANSITION_V5 = """(
       (OLD.status = 'draft' AND NEW.status IN ('reviewed', 'cancelled'))
    OR (OLD.status = 'reviewed' AND NEW.status IN ('approved', 'draft', 'cancelled'))
    OR (OLD.status = 'approved' AND NEW.status IN ('executing', 'cancelled', 'draft'))
    OR (OLD.status = 'executing' AND NEW.status IN ('completed', 'failed'))
    OR (OLD.status = 'failed' AND NEW.status IN ('executing', 'cancelled'))
)"""

PG_FUNCTIONS = {
    "fms_ap_payment_operations_guard": f"""
CREATE FUNCTION fms_ap_payment_operations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{MSG_OP_NO_DELETE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {SQL_OP_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{MSG_OP_FROZEN}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state <> NEW.state AND NOT {SQL_OP_TRANSITION} THEN
        RAISE EXCEPTION '{MSG_OP_BAD_TRANSITION}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_payment_operation_events_guard": f"""
CREATE FUNCTION fms_ap_payment_operation_events_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{MSG_OP_EVENTS_IMMUTABLE}' USING ERRCODE = 'integrity_constraint_violation';
END
$$
""",
    "fms_ap_close_attestations_guard": f"""
CREATE FUNCTION fms_ap_close_attestations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{MSG_ATTESTATION_IMMUTABLE}' USING ERRCODE = 'integrity_constraint_violation';
END
$$
""",
    "fms_ap_payment_runs_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_payment_runs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{V4.MSG_RUN_NO_DELETE}' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {SQL_RUN_TRANSITION_V5} THEN
        RAISE EXCEPTION '{V4.MSG_RUN_BAD_TRANSITION}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND {V4.SQL_RUN_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{V4.MSG_RUN_FROZEN}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}
PG_TRIGGERS = {
    "trg_ap_payment_operations_guard": """
CREATE TRIGGER trg_ap_payment_operations_guard
BEFORE UPDATE OR DELETE ON payment_operations
FOR EACH ROW EXECUTE FUNCTION fms_ap_payment_operations_guard()
""",
    "trg_ap_payment_operation_events_guard": """
CREATE TRIGGER trg_ap_payment_operation_events_guard
BEFORE UPDATE OR DELETE ON payment_operation_events
FOR EACH ROW EXECUTE FUNCTION fms_ap_payment_operation_events_guard()
""",
    "trg_ap_close_attestations_guard": """
CREATE TRIGGER trg_ap_close_attestations_guard
BEFORE UPDATE OR DELETE ON ap_close_attestations
FOR EACH ROW EXECUTE FUNCTION fms_ap_close_attestations_guard()
""",
}


def _abort(name, event, table, when, msg):
    when_clause = f"WHEN {when}\n" if when else ""
    return f"""
CREATE TRIGGER {name}
BEFORE {event} ON {table} FOR EACH ROW
{when_clause}BEGIN
    SELECT RAISE(ABORT, '{msg}');
END
"""


SQLITE_TRIGGERS_NEW = {
    "trg_ap_payment_operations_transition": _abort(
        "trg_ap_payment_operations_transition", "UPDATE", "payment_operations",
        f"OLD.state <> NEW.state AND NOT {SQL_OP_TRANSITION}", MSG_OP_BAD_TRANSITION),
    "trg_ap_payment_operations_frozen": _abort(
        "trg_ap_payment_operations_frozen", "UPDATE", "payment_operations", SQL_OP_FROZEN_CHANGED, MSG_OP_FROZEN),
    "trg_ap_payment_operations_no_delete": _abort(
        "trg_ap_payment_operations_no_delete", "DELETE", "payment_operations", "", MSG_OP_NO_DELETE),
    "trg_ap_payment_operation_events_no_update": _abort(
        "trg_ap_payment_operation_events_no_update", "UPDATE", "payment_operation_events", "", MSG_OP_EVENTS_IMMUTABLE),
    "trg_ap_payment_operation_events_no_delete": _abort(
        "trg_ap_payment_operation_events_no_delete", "DELETE", "payment_operation_events", "", MSG_OP_EVENTS_IMMUTABLE),
    "trg_ap_close_attestations_no_update": _abort(
        "trg_ap_close_attestations_no_update", "UPDATE", "ap_close_attestations", "", MSG_ATTESTATION_IMMUTABLE),
    "trg_ap_close_attestations_no_delete": _abort(
        "trg_ap_close_attestations_no_delete", "DELETE", "ap_close_attestations", "", MSG_ATTESTATION_IMMUTABLE),
}
SQLITE_RUN_TRIGGERS_V5 = {
    **V4.SQLITE_TRIGGERS,
    "trg_ap_payment_runs_transition": V4.SQLITE_TRIGGERS["trg_ap_payment_runs_transition"].replace(
        V4.SQL_RUN_TRANSITION, SQL_RUN_TRANSITION_V5
    ),
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for payables triggers: {name}")
    return name


def _sqlite_drop(names):
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def upgrade() -> None:
    dialect = _dialect()
    if dialect == "sqlite":
        op.execute("PRAGMA legacy_alter_table = ON")

    # --- payment runs: versioning and fingerprints
    with op.batch_alter_table("payment_runs") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("proposal_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("approved_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("approval_invalidated_reason", sa.String(1000), nullable=True))
    with op.batch_alter_table("payment_run_items") as batch:
        batch.add_column(sa.Column("destination_reference", sa.String(100), nullable=True))

    # --- durable payment operations
    op.create_table(
        "payment_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operation_uid", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("run_version", sa.Integer(), nullable=True),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("destination_reference", sa.String(length=100), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="XXX", nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("plan_json", sa.String(length=8000), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("last_error_category", sa.String(length=40), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("gateway_submission_ref", sa.String(length=100), nullable=True),
        sa.Column("gateway_confirmation_ref", sa.String(length=100), nullable=True),
        sa.Column("gateway_status", sa.String(length=20), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("recording_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("ledger_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("allocation_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("recovery_status", sa.String(length=20), server_default="none", nullable=False),
        sa.Column("recovery_note", sa.String(length=1000), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_operations_amount_positive")),
        sa.CheckConstraint("ROUND(amount, 2) = amount", name=op.f("ck_payment_operations_amount_whole_cents")),
        sa.CheckConstraint(
            "state IN ('prepared', 'submitted', 'confirmation_pending', 'confirmed', 'recording', "
            "'completed', 'failed', 'reconciliation_required')",
            name=op.f("ck_payment_operations_state_valid"),
        ),
        sa.ForeignKeyConstraint(["cash_account_id"], ["accounts.id"], name=op.f("fk_payment_operations_cash_account_id_accounts")),
        sa.ForeignKeyConstraint(["payment_id"], ["vendor_payments.id"], name=op.f("fk_payment_operations_payment_id_vendor_payments")),
        sa.ForeignKeyConstraint(["run_id"], ["payment_runs.id"], name=op.f("fk_payment_operations_run_id_payment_runs")),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], name=op.f("fk_payment_operations_vendor_id_vendors")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_operations")),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_operations_idempotency_key"),
        sa.UniqueConstraint("operation_uid", name="uq_payment_operations_operation_uid"),
    )
    op.create_index(op.f("ix_payment_operations_run_id"), "payment_operations", ["run_id"])
    op.create_index(op.f("ix_payment_operations_state"), "payment_operations", ["state"])
    op.create_index(op.f("ix_payment_operations_vendor_id"), "payment_operations", ["vendor_id"])

    op.create_table(
        "payment_operation_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=30), nullable=True),
        sa.Column("to_state", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["operation_id"], ["payment_operations.id"], name=op.f("fk_payment_operation_events_operation_id_payment_operations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_operation_events")),
        sa.UniqueConstraint("operation_id", "seq", name="uq_payment_operation_events_seq"),
    )
    op.create_index(op.f("ix_payment_operation_events_operation_id"), "payment_operation_events", ["operation_id"])

    op.create_table(
        "simulated_gateway_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=80), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("vendor_code", sa.String(length=30), nullable=False),
        sa.Column("destination_reference", sa.String(length=100), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("lookups", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_simulated_gateway_transfers")),
        sa.UniqueConstraint("token", name="uq_simulated_gateway_transfers_token"),
    )

    # --- close attestations
    op.create_table(
        "ap_close_attestations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("attempted_by", sa.String(length=100), nullable=False),
        sa.Column("forced", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("force_reason", sa.String(length=500), nullable=True),
        sa.Column("liability_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("open_bill_count", sa.Integer(), nullable=False),
        sa.Column("vendor_count", sa.Integer(), nullable=False),
        sa.Column("checks_json", sa.String(length=8000), nullable=False),
        sa.Column("exceptions_json", sa.String(length=8000), nullable=False),
        sa.Column("reconciliation_json", sa.String(length=4000), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("gl_period_reference", sa.String(length=100), nullable=True),
        sa.Column("lock_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("outcome IN ('closed', 'forced', 'failed')", name=op.f("ck_ap_close_attestations_outcome_valid")),
        sa.ForeignKeyConstraint(["lock_id"], ["ap_period_locks.id"], name=op.f("fk_ap_close_attestations_lock_id_ap_period_locks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ap_close_attestations")),
    )
    op.create_index(op.f("ix_ap_close_attestations_period_end"), "ap_close_attestations", ["period_end"])

    # --- supplier statement imports
    op.create_table(
        "supplier_statement_imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("imported_by", sa.String(length=100), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.String(length=4000), nullable=False),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], name=op.f("fk_supplier_statement_imports_vendor_id_vendors")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_statement_imports")),
        sa.UniqueConstraint("vendor_id", "content_hash", name="uq_supplier_statement_imports_content"),
    )
    op.create_index(op.f("ix_supplier_statement_imports_vendor_id"), "supplier_statement_imports", ["vendor_id"])
    op.create_table(
        "supplier_statement_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reference", sa.String(length=80), nullable=False),
        sa.Column("normalized_reference", sa.String(length=80), nullable=False),
        sa.Column("line_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("match_status", sa.String(length=20), nullable=False),
        sa.Column("matched_type", sa.String(length=20), nullable=True),
        sa.Column("matched_id", sa.Integer(), nullable=True),
        sa.Column("internal_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("internal_date", sa.Date(), nullable=True),
        sa.Column("explanation", sa.String(length=1000), nullable=False),
        sa.Column("review_status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("resolution_note", sa.String(length=500), nullable=True),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('invoice', 'credit', 'payment')", name=op.f("ck_supplier_statement_lines_kind_valid")),
        sa.CheckConstraint(
            "match_status IN ('matched', 'near_match', 'amount_discrepancy', 'timing_difference', 'unmatched')",
            name=op.f("ck_supplier_statement_lines_match_status_valid"),
        ),
        sa.ForeignKeyConstraint(["import_id"], ["supplier_statement_imports.id"],
                                name=op.f("fk_supplier_statement_lines_import_id_supplier_statement_imports"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_statement_lines")),
        sa.UniqueConstraint("import_id", "line_no", name="uq_supplier_statement_lines_import_line"),
    )
    op.create_index(op.f("ix_supplier_statement_lines_import_id"), "supplier_statement_lines", ["import_id"])

    # --- back-fill execution records for runs already executed through payment runs
    _backfill_operations()

    # --- rules
    if dialect == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        _sqlite_drop(SQLITE_RUN_TRIGGERS_V5)
        for ddl in (*SQLITE_RUN_TRIGGERS_V5.values(), *SQLITE_TRIGGERS_NEW.values()):
            op.execute(ddl)
        op.execute("PRAGMA legacy_alter_table = OFF")


def _backfill_operations() -> None:
    """One ``completed`` operation per (run, vendor) group of paid items."""
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT i.run_id, i.vendor_id, i.payment_id, r.reference, r.pay_date, r.method, r.cash_account_id, "
        "r.executed_by, r.version, i.bill_id, i.settle_amount, i.discount_amount, i.credit_amount, "
        "i.advance_amount, i.pay_amount, i.execution_reference "
        "FROM payment_run_items i JOIN payment_runs r ON r.id = i.run_id "
        "WHERE i.status = 'paid' ORDER BY i.run_id, i.vendor_id, i.bill_id"
    )).all()
    groups: dict[tuple[int, int], list] = {}
    for row in rows:
        groups.setdefault((row.run_id, row.vendor_id), []).append(row)
    for (run_id, vendor_id), items in groups.items():
        first = items[0]
        plan = "[" + ",".join(
            f'{{"bill_id":{i.bill_id},"settle":"{i.settle_amount:.2f}","discount":"{i.discount_amount:.2f}",'
            f'"credit":"{i.credit_amount:.2f}","advance":"{i.advance_amount:.2f}","pay":"{i.pay_amount:.2f}"}}'
            for i in items
        ) + "]"
        total = sum((i.pay_amount for i in items), 0)
        if total <= 0:
            continue  # settled by credits / advances only: no transfer, no operation
        bind.execute(sa.text(
            "INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, run_id, run_version, vendor_id, "
            "amount, currency, payment_date, method, cash_account_id, plan_json, state, version, attempts, max_attempts, "
            "gateway_submission_ref, gateway_confirmation_ref, gateway_status, payment_id, recording_status, ledger_status, "
            "allocation_status, recovery_status, recovery_note, created_by, correlation_id) "
            "VALUES (:uid, :key, 'backfill', :run, :rv, :vendor, :amount, 'XXX', :pd, :method, :cash, :plan, 'completed', 0, 1, 5, "
            ":ref, :ref, 'accepted', :payment, 'recorded', 'posted', 'committed', 'none', "
            "'back-filled by fmspayables05 from payment_run_items', :actor, :corr)"
        ), {
            "uid": str(uuid.uuid4()), "key": f"run:{run_id}:vendor:{vendor_id}:backfill", "run": run_id,
            "rv": first.version, "vendor": vendor_id, "amount": total, "pd": first.pay_date, "method": first.method,
            "cash": first.cash_account_id, "plan": plan[:8000], "ref": first.execution_reference,
            "payment": first.payment_id, "actor": first.executed_by or "backfill", "corr": f"backfill-run-{run_id}",
        })


def downgrade() -> None:
    dialect = _dialect()
    bind = op.get_bind()
    if os.environ.get("FMS_ALLOW_DESTRUCTIVE_DOWNGRADE") != "1":
        counts = {
            table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}{where}")).scalar()
            for table, where in (
                ("payment_operations", " WHERE fingerprint <> 'backfill'"),
                ("ap_close_attestations", ""),
                ("supplier_statement_imports", ""),
            )
        }
        if any(counts.values()):
            raise RuntimeError(
                "fmspayables05 downgrade would delete execution records / close attestations / "
                f"statement imports ({counts}); set FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1 to confirm"
            )
    if dialect == "sqlite":
        op.execute("PRAGMA legacy_alter_table = ON")
        _sqlite_drop(SQLITE_TRIGGERS_NEW)
    else:
        for name, table in (
            ("trg_ap_payment_operations_guard", "payment_operations"),
            ("trg_ap_payment_operation_events_guard", "payment_operation_events"),
            ("trg_ap_close_attestations_guard", "ap_close_attestations"),
        ):
            op.execute(f"DROP TRIGGER {name} ON {table}")
        op.execute(V4.PG_FUNCTIONS["fms_ap_payment_runs_guard"].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
        for name in ("fms_ap_payment_operations_guard", "fms_ap_payment_operation_events_guard", "fms_ap_close_attestations_guard"):
            op.execute(f"DROP FUNCTION {name}()")

    op.drop_index(op.f("ix_supplier_statement_lines_import_id"), table_name="supplier_statement_lines")
    op.drop_table("supplier_statement_lines")
    op.drop_index(op.f("ix_supplier_statement_imports_vendor_id"), table_name="supplier_statement_imports")
    op.drop_table("supplier_statement_imports")
    op.drop_index(op.f("ix_ap_close_attestations_period_end"), table_name="ap_close_attestations")
    op.drop_table("ap_close_attestations")
    op.drop_table("simulated_gateway_transfers")
    op.drop_index(op.f("ix_payment_operation_events_operation_id"), table_name="payment_operation_events")
    op.drop_table("payment_operation_events")
    op.drop_index(op.f("ix_payment_operations_vendor_id"), table_name="payment_operations")
    op.drop_index(op.f("ix_payment_operations_state"), table_name="payment_operations")
    op.drop_index(op.f("ix_payment_operations_run_id"), table_name="payment_operations")
    op.drop_table("payment_operations")
    with op.batch_alter_table("payment_run_items") as batch:
        batch.drop_column("destination_reference")
    with op.batch_alter_table("payment_runs") as batch:
        for column in ("approval_invalidated_reason", "approved_fingerprint", "proposal_fingerprint", "version"):
            batch.drop_column(column)
    if dialect == "sqlite":
        _sqlite_drop(SQLITE_RUN_TRIGGERS_V5)
        for ddl in V4.SQLITE_TRIGGERS.values():
            op.execute(ddl)
        op.execute("PRAGMA legacy_alter_table = OFF")
