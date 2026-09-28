"""Reconciliation certification: frozen evidence of closed sessions, reopen history.

Closing a reconciliation session now records a *certification*: the canonical
content of what was approved (figures, matches, bank transactions and posted
ledger lines in scope) and its SHA-256 hash. Reopening records a *reopen
event* with a reason that supersedes the active certification; closing again
records a new certification chained to the previous hash.

Rules enforced in the database on both backends:

* certifications and reopen events are append-only (no UPDATE, no DELETE);
* a certification can only be inserted for an open session that has no
  active certification, with the next version number;
* a reopen event can only be inserted for a closed session and must reference
  the latest certification of that session (each certification can be
  superseded once: unique ``certification_id``);
* a session can only move to ``closed`` when it has an active certification,
  and back to ``open`` only when every certification has been superseded by
  a reopen event;
* while closed, a session's scope (period, balances, account, statement)
  cannot change, and a session with certifications cannot be deleted.

Revision ID: cashmgmt02
Revises: cashmgmt01
Create Date: 2026-11-15 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cashmgmt02"
down_revision: str | None = "cashmgmt01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MSG_CERT_APPEND_ONLY = "reconciliation certifications are append-only"
MSG_REOPEN_APPEND_ONLY = "reconciliation reopen events are append-only"
MSG_CERT_SESSION_OPEN = "a certification can only be recorded for an open reconciliation session"
MSG_CERT_ACTIVE_EXISTS = "the reconciliation session already has an active certification"
MSG_CERT_VERSION = "certification version must follow the previous version"
MSG_REOPEN_SESSION_CLOSED = "a reopen event can only be recorded for a closed reconciliation session"
MSG_REOPEN_CERT = "a reopen event must reference the latest certification of its session"
MSG_CLOSE_NEEDS_CERT = "a reconciliation session can only be closed with an active certification"
MSG_REOPEN_NEEDS_EVENT = "a reconciliation session can only be reopened with a recorded reopen event"
MSG_CLOSED_IMMUTABLE = "the scope of a closed reconciliation session cannot be changed"
MSG_SESSION_DELETE = "a reconciliation session with certifications cannot be deleted"

# An "active" certification is one that no reopen event has superseded.
ACTIVE_CERT_EXISTS = (
    "EXISTS (SELECT 1 FROM reconciliation_certifications c "
    "WHERE c.session_id = {session} AND NOT EXISTS "
    "(SELECT 1 FROM reconciliation_reopen_events r WHERE r.certification_id = c.id))"
)


# --- PostgreSQL ------------------------------------------------------------------

PG_FUNCTIONS = {
    "fms_reconciliation_certifications_append_only": f"""
CREATE FUNCTION fms_reconciliation_certifications_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{MSG_CERT_APPEND_ONLY}' USING ERRCODE = 'integrity_constraint_violation';
END
$$
""",
    "fms_reconciliation_reopen_events_append_only": f"""
CREATE FUNCTION fms_reconciliation_reopen_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{MSG_REOPEN_APPEND_ONLY}' USING ERRCODE = 'integrity_constraint_violation';
END
$$
""",
    "fms_reconciliation_certifications_guard": f"""
CREATE FUNCTION fms_reconciliation_certifications_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    sess_status varchar;
    last_version integer;
BEGIN
    SELECT status INTO sess_status FROM reconciliation_sessions WHERE id = NEW.session_id;
    IF sess_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION '{MSG_CERT_SESSION_OPEN}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {ACTIVE_CERT_EXISTS.format(session="NEW.session_id")} THEN
        RAISE EXCEPTION '{MSG_CERT_ACTIVE_EXISTS}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(MAX(version), 0) INTO last_version
      FROM reconciliation_certifications WHERE session_id = NEW.session_id;
    IF NEW.version <> last_version + 1 THEN
        RAISE EXCEPTION '{MSG_CERT_VERSION}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_reconciliation_reopen_events_guard": f"""
CREATE FUNCTION fms_reconciliation_reopen_events_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    sess_status varchar;
    latest_cert integer;
BEGIN
    SELECT status INTO sess_status FROM reconciliation_sessions WHERE id = NEW.session_id;
    IF sess_status IS DISTINCT FROM 'closed' THEN
        RAISE EXCEPTION '{MSG_REOPEN_SESSION_CLOSED}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT MAX(id) INTO latest_cert
      FROM reconciliation_certifications WHERE session_id = NEW.session_id;
    IF latest_cert IS NULL OR NEW.certification_id <> latest_cert THEN
        RAISE EXCEPTION '{MSG_REOPEN_CERT}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_reconciliation_sessions_guard": f"""
CREATE FUNCTION fms_reconciliation_sessions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (SELECT 1 FROM reconciliation_certifications WHERE session_id = OLD.id) THEN
            RAISE EXCEPTION '{MSG_SESSION_DELETE}' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'closed' AND (
        NEW.cash_account_id <> OLD.cash_account_id
        OR NEW.statement_id IS DISTINCT FROM OLD.statement_id
        OR NEW.period_start <> OLD.period_start
        OR NEW.period_end <> OLD.period_end
        OR NEW.opening_balance <> OLD.opening_balance
        OR NEW.closing_balance <> OLD.closing_balance
    ) THEN
        RAISE EXCEPTION '{MSG_CLOSED_IMMUTABLE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'closed' AND NEW.status = 'closed'
       AND NOT {ACTIVE_CERT_EXISTS.format(session="NEW.id")} THEN
        RAISE EXCEPTION '{MSG_CLOSE_NEEDS_CERT}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'closed' AND NEW.status <> 'closed'
       AND {ACTIVE_CERT_EXISTS.format(session="NEW.id")} THEN
        RAISE EXCEPTION '{MSG_REOPEN_NEEDS_EVENT}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS = {
    "trg_reconciliation_certifications_append_only": """
CREATE TRIGGER trg_reconciliation_certifications_append_only
BEFORE UPDATE OR DELETE ON reconciliation_certifications
FOR EACH ROW EXECUTE FUNCTION fms_reconciliation_certifications_append_only()
""",
    "trg_reconciliation_certifications_guard": """
CREATE TRIGGER trg_reconciliation_certifications_guard
BEFORE INSERT ON reconciliation_certifications
FOR EACH ROW EXECUTE FUNCTION fms_reconciliation_certifications_guard()
""",
    "trg_reconciliation_reopen_events_append_only": """
CREATE TRIGGER trg_reconciliation_reopen_events_append_only
BEFORE UPDATE OR DELETE ON reconciliation_reopen_events
FOR EACH ROW EXECUTE FUNCTION fms_reconciliation_reopen_events_append_only()
""",
    "trg_reconciliation_reopen_events_guard": """
CREATE TRIGGER trg_reconciliation_reopen_events_guard
BEFORE INSERT ON reconciliation_reopen_events
FOR EACH ROW EXECUTE FUNCTION fms_reconciliation_reopen_events_guard()
""",
    "trg_reconciliation_sessions_guard": """
CREATE TRIGGER trg_reconciliation_sessions_guard
BEFORE UPDATE OR DELETE ON reconciliation_sessions
FOR EACH ROW EXECUTE FUNCTION fms_reconciliation_sessions_guard()
""",
}


# --- SQLite ----------------------------------------------------------------------


def _sqlite_trigger(name: str, event: str, table: str, when: str | None, message: str) -> str:
    clause = f"WHEN {when}\n" if when else ""
    return f"""
CREATE TRIGGER {name}
BEFORE {event} ON {table} FOR EACH ROW
{clause}BEGIN
    SELECT RAISE(ABORT, '{message}');
END
"""


SESSION_STATUS = "(SELECT status FROM reconciliation_sessions WHERE id = {ref}.session_id)"

SQLITE_TRIGGERS = {
    "trg_recon_certs_no_update": _sqlite_trigger(
        "trg_recon_certs_no_update", "UPDATE", "reconciliation_certifications", None, MSG_CERT_APPEND_ONLY
    ),
    "trg_recon_certs_no_delete": _sqlite_trigger(
        "trg_recon_certs_no_delete", "DELETE", "reconciliation_certifications", None, MSG_CERT_APPEND_ONLY
    ),
    "trg_recon_certs_insert_open": _sqlite_trigger(
        "trg_recon_certs_insert_open", "INSERT", "reconciliation_certifications",
        f"{SESSION_STATUS.format(ref='NEW')} IS NOT 'open'", MSG_CERT_SESSION_OPEN,
    ),
    "trg_recon_certs_insert_active": _sqlite_trigger(
        "trg_recon_certs_insert_active", "INSERT", "reconciliation_certifications",
        ACTIVE_CERT_EXISTS.format(session="NEW.session_id"), MSG_CERT_ACTIVE_EXISTS,
    ),
    "trg_recon_certs_insert_version": _sqlite_trigger(
        "trg_recon_certs_insert_version", "INSERT", "reconciliation_certifications",
        "NEW.version <> (SELECT COALESCE(MAX(version), 0) + 1 FROM reconciliation_certifications "
        "WHERE session_id = NEW.session_id)",
        MSG_CERT_VERSION,
    ),
    "trg_recon_reopen_no_update": _sqlite_trigger(
        "trg_recon_reopen_no_update", "UPDATE", "reconciliation_reopen_events", None, MSG_REOPEN_APPEND_ONLY
    ),
    "trg_recon_reopen_no_delete": _sqlite_trigger(
        "trg_recon_reopen_no_delete", "DELETE", "reconciliation_reopen_events", None, MSG_REOPEN_APPEND_ONLY
    ),
    "trg_recon_reopen_insert_closed": _sqlite_trigger(
        "trg_recon_reopen_insert_closed", "INSERT", "reconciliation_reopen_events",
        f"{SESSION_STATUS.format(ref='NEW')} IS NOT 'closed'", MSG_REOPEN_SESSION_CLOSED,
    ),
    "trg_recon_reopen_insert_cert": _sqlite_trigger(
        "trg_recon_reopen_insert_cert", "INSERT", "reconciliation_reopen_events",
        "NEW.certification_id IS NOT (SELECT MAX(id) FROM reconciliation_certifications "
        "WHERE session_id = NEW.session_id)",
        MSG_REOPEN_CERT,
    ),
    "trg_recon_sessions_no_delete_certified": _sqlite_trigger(
        "trg_recon_sessions_no_delete_certified", "DELETE", "reconciliation_sessions",
        "EXISTS (SELECT 1 FROM reconciliation_certifications WHERE session_id = OLD.id)",
        MSG_SESSION_DELETE,
    ),
    "trg_recon_sessions_closed_scope": _sqlite_trigger(
        "trg_recon_sessions_closed_scope", "UPDATE", "reconciliation_sessions",
        "OLD.status = 'closed' AND (NEW.cash_account_id <> OLD.cash_account_id "
        "OR NEW.statement_id IS NOT OLD.statement_id OR NEW.period_start <> OLD.period_start "
        "OR NEW.period_end <> OLD.period_end OR NEW.opening_balance <> OLD.opening_balance "
        "OR NEW.closing_balance <> OLD.closing_balance)",
        MSG_CLOSED_IMMUTABLE,
    ),
    "trg_recon_sessions_close_needs_cert": _sqlite_trigger(
        "trg_recon_sessions_close_needs_cert", "UPDATE", "reconciliation_sessions",
        f"OLD.status <> 'closed' AND NEW.status = 'closed' AND NOT {ACTIVE_CERT_EXISTS.format(session='NEW.id')}",
        MSG_CLOSE_NEEDS_CERT,
    ),
    "trg_recon_sessions_reopen_needs_event": _sqlite_trigger(
        "trg_recon_sessions_reopen_needs_event", "UPDATE", "reconciliation_sessions",
        f"OLD.status = 'closed' AND NEW.status <> 'closed' AND {ACTIVE_CERT_EXISTS.format(session='NEW.id')}",
        MSG_REOPEN_NEEDS_EVENT,
    ),
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for cash triggers: {name}")
    return name


def upgrade() -> None:
    op.create_table(
        "reconciliation_certifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_balanced", sa.Boolean(), nullable=False),
        sa.Column("forced", sa.Boolean(), nullable=False),
        sa.Column("difference", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("cleared_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("ledger_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("matches_count", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["reconciliation_sessions.id"],
            name=op.f("fk_reconciliation_certifications_session_id_reconciliation_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_certifications")),
        sa.UniqueConstraint(
            "session_id", "version", name="uq_reconciliation_certifications_session_version"
        ),
    )
    op.create_table(
        "reconciliation_reopen_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("certification_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["certification_id"],
            ["reconciliation_certifications.id"],
            name=op.f(
                "fk_reconciliation_reopen_events_certification_id_reconciliation_certifications"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["reconciliation_sessions.id"],
            name=op.f("fk_reconciliation_reopen_events_session_id_reconciliation_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_reopen_events")),
        sa.UniqueConstraint(
            "certification_id", name=op.f("uq_reconciliation_reopen_events_certification_id")
        ),
    )

    if _dialect() == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    if _dialect() == "postgresql":
        op.execute("DROP TRIGGER trg_reconciliation_sessions_guard ON reconciliation_sessions")
    else:
        for name in (
            "trg_recon_sessions_no_delete_certified",
            "trg_recon_sessions_closed_scope",
            "trg_recon_sessions_close_needs_cert",
            "trg_recon_sessions_reopen_needs_event",
        ):
            op.execute(f"DROP TRIGGER {name}")
    op.drop_table("reconciliation_reopen_events")
    op.drop_table("reconciliation_certifications")
    if _dialect() == "postgresql":
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
