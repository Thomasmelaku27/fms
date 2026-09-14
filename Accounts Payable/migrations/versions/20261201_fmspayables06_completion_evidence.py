"""Accounts payable: completion evidence, accounting / investigation status, event hash
chain, close exceptions, run finalisation.

Revises ``fmspayables05`` (head of the payables chain). Previously applied revisions
are not modified; this revision replaces the ``payment_operations`` guard with a
stricter one and adds columns / one table.

Schema
------
* ``payment_operations``: ``accounting_status`` (pending | unrecorded | recorded |
  corrected | unverified | inconsistent), ``completion_kind``,
  ``completion_evidence_json``, ``completion_idempotency_key``, investigation columns
  (``investigation_status`` none | open | closed, note, closed_by, closed_at);
  UNIQUE ``gateway_confirmation_ref`` and UNIQUE ``payment_id`` (NULLs excepted): a
  gateway confirmation reference and an AP payment can each belong to one operation.
* ``payment_operation_events``: ``prev_hash`` / ``event_hash`` (sha256 chain per
  operation; see ``services.execution_state.event_hash``), back-filled.
* ``ap_close_attestations``: ``content_hash`` (back-filled), ``obligations_json``;
  new table ``ap_close_exceptions`` (blockers accepted by a forced close and their
  later resolution, written once each).
* ``payment_runs``: ``finalization_status``, ``settlement_summary_json``,
  ``finalized_at``; ``payment_run_items``: ``settlement_status``, ``actual_*`` amounts,
  ``settlement_note`` (approved plan stays in the original columns).

Database rules (replacing the fmspayables05 operation guard)
------------------------------------------------------------
* state transitions follow the machine (unchanged);
* ``-> completed`` requires completion evidence in the database itself: a linked
  ``vendor_payments`` row of the same vendor, amount and cash account with status
  ``recorded`` whose journal entry is ``posted`` and credits the cash account by the
  amount, a gateway confirmation reference, and ``accounting_status`` recorded /
  corrected;
* ``-> confirmed`` requires a gateway confirmation reference and ``gateway_status =
  'accepted'``; ``-> failed`` is refused once the gateway accepted the transfer;
* ``payment_id`` can only be set to a payment of the operation's vendor with the
  operation's amount, and only once;
* completed operations are frozen except investigation columns, recovery note,
  worker lease and version;
* ``ap_close_exceptions`` rows are insert-only except the one-time resolution.

Preflight (non-destructive)
---------------------------
Every operation already ``completed`` is checked against the same evidence rules.
Rows with evidence get ``accounting_status = 'recorded'`` (``'corrected'`` when they
were resolved manually); rows without evidence are labelled ``'unverified'`` and an
event is appended describing what is missing. Nothing is rewritten or deleted and no
state changes: remediation is explicit (``services.verification`` /
``services.recovery.complete_with_evidence``). The same report is available without
migrating through ``scripts/ap_preflight.py``.

Downgrade (restricted)
----------------------
Drops the evidence / investigation columns, the hash chain, close exceptions and run
finalisation data. Refused unless ``FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1``.

Revision ID: fmspayables06
Revises: fmspayables05
Create Date: 2026-12-01 00:00:00
"""

import hashlib
import importlib.util
import json
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "fmspayables06"
down_revision: str | None = "fmspayables05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sibling(filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V5 = _sibling("20261120_fmspayables05_payment_operations.py")

MSG_OP_COMPLETION_EVIDENCE = (
    "a payment operation can only be completed with a recorded vendor payment and a posted "
    "journal entry matching its vendor, amount and cash account"
)
MSG_OP_FAILED_AFTER_ACCEPT = (
    "an operation whose transfer was accepted by the gateway cannot be marked failed"
)
MSG_OP_CONFIRMED_NEEDS_REF = (
    "an operation can only be confirmed with an accepted gateway status and a confirmation reference"
)
MSG_OP_PAYMENT_LINK = (
    "the linked vendor payment must belong to the vendor of the operation with the amount of the operation"
)
MSG_OP_COMPLETED_FROZEN = (
    "completed payment operations are immutable except for investigation and lease columns"
)
MSG_CLOSE_EXCEPTION_IMMUTABLE = (
    "AP close exceptions are written once; only an unresolved exception can be resolved, once"
)

def _cents(expr: str, dialect: str) -> str:
    """Exact integer cents. SQLite stores NUMERIC as REAL, so sums are compared in cents
    (same convention as fmspayables02); PostgreSQL NUMERIC is exact as it is."""
    return f"CAST(ROUND({expr} * 100) AS INTEGER)" if dialect == "sqlite" else expr


def completion_evidence_sql(dialect: str) -> str:
    """Evidence that must exist in the database for NEW to become 'completed'."""
    c = lambda e: _cents(e, dialect)  # noqa: E731
    return f"""(
    NEW.payment_id IS NOT NULL
    AND NEW.gateway_confirmation_ref IS NOT NULL
    AND NEW.accounting_status IN ('recorded', 'corrected')
    AND EXISTS (
        SELECT 1 FROM vendor_payments p
        JOIN journal_entries j ON j.id = p.journal_entry_id
        WHERE p.id = NEW.payment_id
          AND p.vendor_id = NEW.vendor_id
          AND {c("p.amount")} = {c("NEW.amount")}
          AND p.cash_account_id = NEW.cash_account_id
          AND p.status = 'recorded'
          AND j.status = 'posted'
          AND (SELECT COALESCE(SUM({c("l.credit")}), 0) FROM journal_lines l
                WHERE l.entry_id = j.id AND l.account_id = NEW.cash_account_id) = {c("NEW.amount")}
          AND (SELECT COALESCE(SUM({c("a.amount")}), 0) FROM payment_allocations a
                WHERE a.payment_id = p.id AND a.unallocation_date IS NULL) <= {c("p.amount")}
    )
)"""


def payment_link_sql(dialect: str) -> str:
    c = lambda e: _cents(e, dialect)  # noqa: E731
    return f"""(
    NEW.payment_id IS NULL
    OR EXISTS (SELECT 1 FROM vendor_payments p WHERE p.id = NEW.payment_id
                AND p.vendor_id = NEW.vendor_id AND {c("p.amount")} = {c("NEW.amount")})
)"""


SQL_OP_COMPLETION_EVIDENCE = completion_evidence_sql("postgresql")
SQL_OP_PAYMENT_LINK_OK = payment_link_sql("postgresql")
SQL_OP_COMPLETED_CHANGED = """(
       COALESCE(NEW.payment_id, -1) <> COALESCE(OLD.payment_id, -1)
    OR COALESCE(NEW.gateway_confirmation_ref, '') <> COALESCE(OLD.gateway_confirmation_ref, '')
    OR COALESCE(NEW.gateway_status, '') <> COALESCE(OLD.gateway_status, '')
    OR NEW.recording_status <> OLD.recording_status
    OR NEW.ledger_status <> OLD.ledger_status
    OR NEW.allocation_status <> OLD.allocation_status
    OR NEW.accounting_status <> OLD.accounting_status
    OR COALESCE(NEW.completion_kind, '') <> COALESCE(OLD.completion_kind, '')
    OR COALESCE(NEW.completion_evidence_json, '') <> COALESCE(OLD.completion_evidence_json, '')
    OR COALESCE(NEW.completion_idempotency_key, '') <> COALESCE(OLD.completion_idempotency_key, '')
    OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
    OR COALESCE(NEW.attempts, -1) <> COALESCE(OLD.attempts, -1)
)"""
SQL_CLOSE_EXC_FROZEN_CHANGED = """(
       NEW.attestation_id <> OLD.attestation_id
    OR NEW.period_end <> OLD.period_end
    OR NEW.code <> OLD.code
    OR NEW.severity <> OLD.severity
    OR NEW.item_json <> OLD.item_json
    OR NEW.accepted_by <> OLD.accepted_by
    OR NEW.accept_reason <> OLD.accept_reason
    OR OLD.status = 'resolved'
    OR (NEW.status = 'resolved' AND (NEW.resolved_by IS NULL OR NEW.resolved_at IS NULL))
    OR (NEW.status = 'accepted' AND (NEW.resolved_by IS NOT NULL OR NEW.resolved_at IS NOT NULL))
)"""

PG_FUNCTIONS = {
    "fms_ap_payment_operations_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_payment_operations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{V5.MSG_OP_NO_DELETE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {V5.SQL_OP_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{V5.MSG_OP_FROZEN}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state <> NEW.state AND NOT {V5.SQL_OP_TRANSITION} THEN
        RAISE EXCEPTION '{V5.MSG_OP_BAD_TRANSITION}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'completed' AND {SQL_OP_COMPLETED_CHANGED} THEN
        RAISE EXCEPTION '{MSG_OP_COMPLETED_FROZEN}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'completed' AND OLD.state <> 'completed' AND NOT {SQL_OP_COMPLETION_EVIDENCE} THEN
        RAISE EXCEPTION '{MSG_OP_COMPLETION_EVIDENCE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'failed' AND OLD.state <> 'failed'
       AND (OLD.gateway_status = 'accepted' OR NEW.gateway_status = 'accepted'
            OR OLD.gateway_confirmation_ref IS NOT NULL) THEN
        RAISE EXCEPTION '{MSG_OP_FAILED_AFTER_ACCEPT}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'confirmed' AND OLD.state <> 'confirmed'
       AND (NEW.gateway_confirmation_ref IS NULL OR NEW.gateway_status IS DISTINCT FROM 'accepted') THEN
        RAISE EXCEPTION '{MSG_OP_CONFIRMED_NEEDS_REF}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF COALESCE(NEW.payment_id, -1) <> COALESCE(OLD.payment_id, -1) AND NOT {SQL_OP_PAYMENT_LINK_OK} THEN
        RAISE EXCEPTION '{MSG_OP_PAYMENT_LINK}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_close_exceptions_guard": f"""
CREATE FUNCTION fms_ap_close_exceptions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{MSG_CLOSE_EXCEPTION_IMMUTABLE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {SQL_CLOSE_EXC_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{MSG_CLOSE_EXCEPTION_IMMUTABLE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}
PG_TRIGGERS = {
    "trg_ap_close_exceptions_guard": """
CREATE TRIGGER trg_ap_close_exceptions_guard
BEFORE UPDATE OR DELETE ON ap_close_exceptions
FOR EACH ROW EXECUTE FUNCTION fms_ap_close_exceptions_guard()
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


# SQLite has no IS DISTINCT FROM; COALESCE does the same job here.
_SQLITE_COMPLETED_CHANGED = SQL_OP_COMPLETED_CHANGED.replace(
    "NEW.completed_at IS DISTINCT FROM OLD.completed_at", "NEW.completed_at IS NOT OLD.completed_at"
)
# SQLite fires the new triggers next to the fmspayables05 transition trigger; the extra
# ``AND <valid transition>`` keeps the message of an *invalid* transition the same on both backends.
SQLITE_TRIGGERS_NEW = {
    "trg_ap_payment_operations_completion_evidence": _abort(
        "trg_ap_payment_operations_completion_evidence", "UPDATE", "payment_operations",
        f"NEW.state = 'completed' AND OLD.state <> 'completed' AND {V5.SQL_OP_TRANSITION} "
        f"AND NOT {completion_evidence_sql('sqlite')}",
        MSG_OP_COMPLETION_EVIDENCE),
    "trg_ap_payment_operations_completed_frozen": _abort(
        "trg_ap_payment_operations_completed_frozen", "UPDATE", "payment_operations",
        f"OLD.state = 'completed' AND {_SQLITE_COMPLETED_CHANGED}", MSG_OP_COMPLETED_FROZEN),
    "trg_ap_payment_operations_failed_after_accept": _abort(
        "trg_ap_payment_operations_failed_after_accept", "UPDATE", "payment_operations",
        f"NEW.state = 'failed' AND OLD.state <> 'failed' AND {V5.SQL_OP_TRANSITION} AND (OLD.gateway_status = 'accepted' "
        "OR NEW.gateway_status = 'accepted' OR OLD.gateway_confirmation_ref IS NOT NULL)",
        MSG_OP_FAILED_AFTER_ACCEPT),
    "trg_ap_payment_operations_confirmed_needs_ref": _abort(
        "trg_ap_payment_operations_confirmed_needs_ref", "UPDATE", "payment_operations",
        f"NEW.state = 'confirmed' AND OLD.state <> 'confirmed' AND {V5.SQL_OP_TRANSITION} AND (NEW.gateway_confirmation_ref IS NULL "
        "OR COALESCE(NEW.gateway_status, '') <> 'accepted')",
        MSG_OP_CONFIRMED_NEEDS_REF),
    "trg_ap_payment_operations_payment_link": _abort(
        "trg_ap_payment_operations_payment_link", "UPDATE", "payment_operations",
        f"COALESCE(NEW.payment_id, -1) <> COALESCE(OLD.payment_id, -1) AND NOT {payment_link_sql('sqlite')}",
        MSG_OP_PAYMENT_LINK),
    "trg_ap_close_exceptions_no_delete": _abort(
        "trg_ap_close_exceptions_no_delete", "DELETE", "ap_close_exceptions", "", MSG_CLOSE_EXCEPTION_IMMUTABLE),
    "trg_ap_close_exceptions_frozen": _abort(
        "trg_ap_close_exceptions_frozen", "UPDATE", "ap_close_exceptions", SQL_CLOSE_EXC_FROZEN_CHANGED,
        MSG_CLOSE_EXCEPTION_IMMUTABLE),
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for payables triggers: {name}")
    return name


def _sqlite_drop(names):
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


# --- evidence hashes (must match services.execution_state / services.closing) -------------------


def event_hash(prev_hash, operation_uid, seq, from_state, to_state, category, detail, actor) -> str:
    payload = "|".join(
        [prev_hash or "", operation_uid, str(seq), from_state or "", to_state, category, detail or "", actor]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def attestation_content_hash(row: dict) -> str:
    keys = (
        "period_end", "outcome", "attempted_by", "forced", "force_reason", "liability_balance",
        "open_bill_count", "vendor_count", "checks_json", "exceptions_json", "reconciliation_json",
        "source_fingerprint", "gl_period_reference", "lock_id", "obligations_json",
    )
    normal = {}
    for key in keys:
        value = row.get(key)
        if key == "liability_balance" and value is not None:
            value = f"{Decimal(str(value)):.2f}"
        elif key == "forced":
            value = bool(value)
        elif key == "period_end" and value is not None:
            value = str(value)[:10]
        normal[key] = value
    return hashlib.sha256(json.dumps(normal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- preflight ---------------------------------------------------------------------------------------


def completion_preflight(bind) -> list[dict]:
    """Completed operations and whether the database holds their completion evidence.

    Read-only. Each row: ``operation_uid``, ``payment_id``, ``evidence`` (bool),
    ``problems`` (list of str), ``manual`` (resolved by hand in Turn 4).
    """
    rows = bind.execute(sa.text(
        "SELECT o.id, o.operation_uid, o.vendor_id, o.amount, o.cash_account_id, o.payment_id, "
        "o.gateway_confirmation_ref, o.recovery_status, o.recovery_note, "
        "p.vendor_id AS p_vendor, p.amount AS p_amount, p.cash_account_id AS p_cash, p.status AS p_status, "
        "p.journal_entry_id AS p_entry, j.status AS j_status, "
        "(SELECT COALESCE(SUM(l.credit), 0) FROM journal_lines l WHERE l.entry_id = p.journal_entry_id "
        " AND l.account_id = o.cash_account_id) AS cash_credit "
        "FROM payment_operations o "
        "LEFT JOIN vendor_payments p ON p.id = o.payment_id "
        "LEFT JOIN journal_entries j ON j.id = p.journal_entry_id "
        "WHERE o.state = 'completed' ORDER BY o.id"
    )).mappings().all()
    report = []
    for r in rows:
        problems = []
        if r["payment_id"] is None:
            problems.append("no AP payment linked")
        else:
            if r["p_vendor"] is None:
                problems.append(f"linked payment {r['payment_id']} does not exist")
            else:
                if r["p_vendor"] != r["vendor_id"]:
                    problems.append("linked payment belongs to another vendor")
                if Decimal(str(r["p_amount"])) != Decimal(str(r["amount"])):
                    problems.append(f"linked payment amount {r['p_amount']} differs from {r['amount']}")
                if r["p_cash"] != r["cash_account_id"]:
                    problems.append("linked payment uses another cash account")
                if r["p_status"] != "recorded":
                    problems.append(f"linked payment is {r['p_status']}")
                if r["j_status"] != "posted":
                    problems.append("journal entry missing or not posted")
                elif Decimal(str(r["cash_credit"])) != Decimal(str(r["amount"])):
                    problems.append("posted entry does not credit the cash account by the amount")
        if r["gateway_confirmation_ref"] is None:
            problems.append("no gateway confirmation reference")
        report.append({
            "id": r["id"], "operation_uid": r["operation_uid"], "payment_id": r["payment_id"],
            "evidence": not problems, "problems": problems, "manual": r["recovery_status"] == "resolved",
        })
    return report


def upgrade() -> None:
    dialect = _dialect()
    bind = op.get_bind()
    if dialect == "sqlite":
        op.execute("PRAGMA legacy_alter_table = ON")

    # --- uniqueness preconditions (non-destructive: refuse with the offending rows)
    for column in ("gateway_confirmation_ref", "payment_id"):
        dupes = bind.execute(sa.text(
            f"SELECT {column}, COUNT(*) FROM payment_operations WHERE {column} IS NOT NULL "
            f"GROUP BY {column} HAVING COUNT(*) > 1"
        )).all()
        if dupes:
            raise RuntimeError(
                f"fmspayables06: payment_operations.{column} is shared by several operations "
                f"({dupes[:5]}); investigate and resolve before upgrading (nothing was changed)"
            )

    # --- columns
    with op.batch_alter_table("payment_operations") as batch:
        batch.add_column(sa.Column("accounting_status", sa.String(20), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("completion_kind", sa.String(20), nullable=True))
        batch.add_column(sa.Column("completion_evidence_json", sa.String(4000), nullable=True))
        batch.add_column(sa.Column("completion_idempotency_key", sa.String(120), nullable=True))
        batch.add_column(sa.Column("investigation_status", sa.String(20), nullable=False, server_default="none"))
        batch.add_column(sa.Column("investigation_note", sa.String(1000), nullable=True))
        batch.add_column(sa.Column("investigation_closed_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("investigation_closed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_payment_operations_gateway_confirmation_ref", ["gateway_confirmation_ref"])
        batch.create_unique_constraint("uq_payment_operations_payment_id", ["payment_id"])
    with op.batch_alter_table("payment_operation_events") as batch:
        batch.add_column(sa.Column("prev_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("event_hash", sa.String(64), nullable=True))
    with op.batch_alter_table("ap_close_attestations") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("obligations_json", sa.String(8000), nullable=True))
    with op.batch_alter_table("payment_runs") as batch:
        batch.add_column(sa.Column("finalization_status", sa.String(30), nullable=True))
        batch.add_column(sa.Column("settlement_summary_json", sa.String(4000), nullable=True))
        batch.add_column(sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("payment_run_items") as batch:
        batch.add_column(sa.Column("settlement_status", sa.String(30), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("actual_pay_amount", sa.Numeric(18, 2), nullable=True))
        batch.add_column(sa.Column("actual_discount_amount", sa.Numeric(18, 2), nullable=True))
        batch.add_column(sa.Column("actual_credit_amount", sa.Numeric(18, 2), nullable=True))
        batch.add_column(sa.Column("actual_advance_amount", sa.Numeric(18, 2), nullable=True))
        batch.add_column(sa.Column("settlement_note", sa.String(500), nullable=True))

    op.create_table(
        "ap_close_exceptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attestation_id", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("item_json", sa.String(length=2000), nullable=False),
        sa.Column("accepted_by", sa.String(length=100), nullable=False),
        sa.Column("accept_reason", sa.String(length=500), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="accepted", nullable=False),
        sa.Column("resolved_by", sa.String(length=100), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.String(length=1000), nullable=True),
        sa.Column("resolution_reference", sa.String(length=200), nullable=True),
        sa.CheckConstraint("status IN ('accepted', 'resolved')", name=op.f("ck_ap_close_exceptions_status_valid")),
        sa.ForeignKeyConstraint(["attestation_id"], ["ap_close_attestations.id"],
                                name=op.f("fk_ap_close_exceptions_attestation_id_ap_close_attestations")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ap_close_exceptions")),
    )
    op.create_index(op.f("ix_ap_close_exceptions_attestation_id"), "ap_close_exceptions", ["attestation_id"])

    # --- back-fill: accounting status from the execution state, then the preflight labels
    bind.execute(sa.text(
        "UPDATE payment_operations SET accounting_status = CASE "
        "WHEN state IN ('confirmed', 'recording', 'reconciliation_required') THEN 'unrecorded' "
        "WHEN state = 'completed' THEN 'recorded' ELSE 'pending' END"
    ))
    for row in completion_preflight(bind):
        if row["evidence"]:
            if row["manual"]:
                bind.execute(sa.text(
                    "UPDATE payment_operations SET accounting_status = 'corrected', completion_kind = 'legacy_manual' WHERE id = :id"
                ), {"id": row["id"]})
            else:
                bind.execute(sa.text(
                    "UPDATE payment_operations SET completion_kind = CASE WHEN fingerprint = 'backfill' "
                    "THEN 'legacy_backfill' ELSE 'automatic' END WHERE id = :id"
                ), {"id": row["id"]})
            continue
        bind.execute(sa.text(
            "UPDATE payment_operations SET accounting_status = 'unverified', completion_kind = :kind WHERE id = :id"
        ), {"id": row["id"], "kind": "legacy_manual" if row["manual"] else "legacy_unverified"})
        seq = (bind.execute(sa.text(
            "SELECT COALESCE(MAX(seq), 0) FROM payment_operation_events WHERE operation_id = :id"
        ), {"id": row["id"]}).scalar() or 0) + 1
        bind.execute(sa.text(
            "INSERT INTO payment_operation_events (operation_id, seq, from_state, to_state, category, detail, actor) "
            "VALUES (:id, :seq, 'completed', 'completed', 'verification', :detail, 'migration:fmspayables06')"
        ), {"id": row["id"], "seq": seq,
            "detail": ("completion lacks accounting evidence: " + "; ".join(row["problems"]) +
                       "; remediation required (record the payment through the correction workflow and "
                       "complete with evidence)")[:1000]})

    # --- back-fill: event hash chain
    events = bind.execute(sa.text(
        "SELECT e.id, e.operation_id, o.operation_uid, e.seq, e.from_state, e.to_state, e.category, e.detail, e.actor "
        "FROM payment_operation_events e JOIN payment_operations o ON o.id = e.operation_id "
        "ORDER BY e.operation_id, e.seq"
    )).all()
    prev: dict[int, str | None] = {}
    if events:
        # The append-only guard is lifted for the back-fill only (the hash columns did not exist).
        if dialect == "postgresql":
            op.execute("ALTER TABLE payment_operation_events DISABLE TRIGGER trg_ap_payment_operation_events_guard")
        else:
            op.execute("DROP TRIGGER IF EXISTS trg_ap_payment_operation_events_no_update")
    for e in events:
        h = event_hash(prev.get(e.operation_id), e.operation_uid, e.seq, e.from_state, e.to_state, e.category, e.detail, e.actor)
        bind.execute(sa.text("UPDATE payment_operation_events SET prev_hash = :p, event_hash = :h WHERE id = :id"),
                     {"p": prev.get(e.operation_id), "h": h, "id": e.id})
        prev[e.operation_id] = h
    if events and dialect == "postgresql":
        op.execute("ALTER TABLE payment_operation_events ENABLE TRIGGER trg_ap_payment_operation_events_guard")

    # --- back-fill: attestation content hashes (the append-only guard must be lifted briefly)
    attestations = bind.execute(sa.text("SELECT * FROM ap_close_attestations ORDER BY id")).mappings().all()
    if attestations:
        if dialect == "postgresql":
            op.execute("ALTER TABLE ap_close_attestations DISABLE TRIGGER trg_ap_close_attestations_guard")
        else:
            op.execute("DROP TRIGGER IF EXISTS trg_ap_close_attestations_no_update")
        for row in attestations:
            bind.execute(sa.text("UPDATE ap_close_attestations SET content_hash = :h WHERE id = :id"),
                         {"h": attestation_content_hash(dict(row)), "id": row["id"]})
        if dialect == "postgresql":
            op.execute("ALTER TABLE ap_close_attestations ENABLE TRIGGER trg_ap_close_attestations_guard")

    # --- rules
    if dialect == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        # Table rebuilds (if any) drop triggers: recreate the complete sets.
        _sqlite_drop(V5.SQLITE_TRIGGERS_NEW)
        _sqlite_drop(V5.SQLITE_RUN_TRIGGERS_V5)
        _sqlite_drop(SQLITE_TRIGGERS_NEW)
        for ddl in (*V5.SQLITE_RUN_TRIGGERS_V5.values(), *V5.SQLITE_TRIGGERS_NEW.values(), *SQLITE_TRIGGERS_NEW.values()):
            op.execute(ddl)
        op.execute("PRAGMA legacy_alter_table = OFF")


def downgrade() -> None:
    dialect = _dialect()
    bind = op.get_bind()
    if os.environ.get("FMS_ALLOW_DESTRUCTIVE_DOWNGRADE") != "1":
        counts = {
            "ap_close_exceptions": bind.execute(sa.text("SELECT COUNT(*) FROM ap_close_exceptions")).scalar(),
            "completion_evidence": bind.execute(sa.text(
                "SELECT COUNT(*) FROM payment_operations WHERE completion_evidence_json IS NOT NULL "
                "OR investigation_status <> 'none'")).scalar(),
        }
        if any(counts.values()):
            raise RuntimeError(
                "fmspayables06 downgrade would delete completion evidence / investigation records / close "
                f"exceptions ({counts}); set FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1 to confirm"
            )
    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_ap_close_exceptions_guard ON ap_close_exceptions")
        op.execute("DROP FUNCTION fms_ap_close_exceptions_guard()")
        op.execute(V5.PG_FUNCTIONS["fms_ap_payment_operations_guard"].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    else:
        op.execute("PRAGMA legacy_alter_table = ON")
        _sqlite_drop(SQLITE_TRIGGERS_NEW)

    op.drop_index(op.f("ix_ap_close_exceptions_attestation_id"), table_name="ap_close_exceptions")
    op.drop_table("ap_close_exceptions")
    with op.batch_alter_table("payment_run_items") as batch:
        for column in ("settlement_note", "actual_advance_amount", "actual_credit_amount", "actual_discount_amount",
                       "actual_pay_amount", "settlement_status"):
            batch.drop_column(column)
    with op.batch_alter_table("payment_runs") as batch:
        for column in ("finalized_at", "settlement_summary_json", "finalization_status"):
            batch.drop_column(column)
    with op.batch_alter_table("ap_close_attestations") as batch:
        batch.drop_column("obligations_json")
        batch.drop_column("content_hash")
    with op.batch_alter_table("payment_operation_events") as batch:
        batch.drop_column("event_hash")
        batch.drop_column("prev_hash")
    with op.batch_alter_table("payment_operations") as batch:
        batch.drop_constraint("uq_payment_operations_payment_id", type_="unique")
        batch.drop_constraint("uq_payment_operations_gateway_confirmation_ref", type_="unique")
        for column in ("investigation_closed_at", "investigation_closed_by", "investigation_note", "investigation_status",
                       "completion_idempotency_key", "completion_evidence_json", "completion_kind", "accounting_status"):
            batch.drop_column(column)
    if dialect == "sqlite":
        _sqlite_drop(V5.SQLITE_TRIGGERS_NEW)
        _sqlite_drop(V5.SQLITE_RUN_TRIGGERS_V5)
        for ddl in (*V5.SQLITE_RUN_TRIGGERS_V5.values(), *V5.SQLITE_TRIGGERS_NEW.values()):
            op.execute(ddl)
        op.execute("PRAGMA legacy_alter_table = OFF")
