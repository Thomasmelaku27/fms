"""Reconciliation state machine tolerant to partial processes and irregular history.

The session row is the single authoritative state of the certification cycle:
``status``, ``reopen_count`` (completed reopenings) and the new
``last_reopen_event_id``. Certifications and reopen events are evidence that
the database *requires* before a transition - but a stray or duplicated row
(raw SQL, aborted process) never changes the state and never blocks the next
legitimate transition:

* ``reconciliation_reopen_events.reopen_number`` (= ``reopen_count + 1`` at
  insert) replaces the one-event-per-certification uniqueness: several events
  may ask for the same reopening, exactly one completes it;
* ``reconciliation_sessions.last_reopen_event_id`` names the event that
  completed the latest reopening;
* ``reconciliation_certifications.reopen_event_id`` names the event that
  started the certified cycle (NULL for version 1).

The rules are *structural* (links between rows), never arithmetic on
``reopen_count``, so a database whose history is irregular (see below) keeps
working without any row being rewritten. Enforced on both backends:

* certifications and reopen events stay append-only;
* a certification needs an open session, the next version number, must
  reference the session's ``last_reopen_event_id``, and the latest
  certification (if any) must have been superseded by that completed
  reopening (one certification per cycle);
* a reopen event needs a closed session, the latest certification and
  ``reopen_number = reopen_count + 1``;
* ``open -> closed`` needs a latest certification that is not superseded;
* ``closed -> open`` must increment ``reopen_count`` by one and point
  ``last_reopen_event_id`` at an event of that number for the latest
  certification; ``reopen_count`` / ``last_reopen_event_id`` never change otherwise;
* a closed session's scope cannot change; a certified session cannot be deleted.

Existing data ("historical databases")
--------------------------------------
Nothing is rewritten or discarded; only the new columns are filled, and only
where the derivation is unambiguous:

* ``reopen_number`` = version of the certification the event references
  (under cashmgmt02 an event could only reference the latest certification);
* ``last_reopen_event_id`` = the single event of number ``reopen_count``
  (NULL when ``reopen_count`` is 0, when no such event exists, or when several do);
* ``certifications.reopen_event_id`` (version >= 2) = the single event of the
  previous version (NULL when missing or ambiguous).

Before touching the schema the migration inspects the data: broken references
(events of a missing certification or of another session's certification)
abort the upgrade with a report - they cannot be mapped honestly. Irregular
but representable history (reopen events that never completed, several
certifications without a completed reopening in between, ``reopen_count`` not
matching the events) is kept as is and logged; the application reports it
through the integrity endpoint as anomalies and remains fully operational.

Atomicity: PostgreSQL runs the whole revision in one transaction. SQLite does
not (DDL autocommits) - this revision therefore opens an explicit transaction
first, so a failure anywhere leaves the database exactly at cashmgmt02.

Revision ID: cashmgmt03
Revises: cashmgmt02
Create Date: 2026-11-20 00:00:00
"""

import importlib.util
import logging
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "cashmgmt03"
down_revision: str | None = "cashmgmt02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


class MigrationRefused(RuntimeError):
    """The data cannot be migrated honestly; nothing has been changed."""


def _revision_module(filename: str):
    """Load an earlier revision script (its trigger DDL is reused for rebuilds / downgrade)."""
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location("fms_" + filename.split("_")[1], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREV = _revision_module("20261115_cashmgmt02_reconciliation_certification.py")
FIRST = _revision_module("20261101_cashmgmt01_cash_management.py")

# cashmgmt01 triggers (on reconciliation_matches) that name reconciliation_sessions;
# SQLite refuses to rebuild a table while such triggers exist, so they are
# dropped around the batch operation and recreated unchanged.
SQLITE_MATCH_SESSION_TRIGGERS = (
    "trg_recon_matches_insert_session_open",
    "trg_recon_matches_update_session_open",
    "trg_recon_matches_delete_session_open",
)

MSG_CERT_SESSION_OPEN = PREV.MSG_CERT_SESSION_OPEN
MSG_CERT_VERSION = PREV.MSG_CERT_VERSION
MSG_CERT_CYCLE = (
    "the latest certification of the reconciliation session has not been superseded "
    "by a completed reopening"
)
MSG_CERT_REOPEN_LINK = "a certification must reference the reopen event that started its cycle"
MSG_REOPEN_SESSION_CLOSED = PREV.MSG_REOPEN_SESSION_CLOSED
MSG_REOPEN_CERT = PREV.MSG_REOPEN_CERT
MSG_REOPEN_NUMBER = "reopen event number must be the next reopening of its session"
MSG_CLOSE_NEEDS_CERT = (
    "a reconciliation session can only be closed with a latest certification that "
    "has not been superseded"
)
MSG_REOPEN_NEEDS_EVENT = (
    "a reconciliation session can only be reopened by pointing at a recorded reopen event "
    "for its latest certification"
)
MSG_REOPEN_COUNT = "reopening increments reopen_count by exactly one"
MSG_REOPEN_FIELDS = "reopen_count and last_reopen_event_id only change when a session is reopened"
MSG_CLOSED_IMMUTABLE = PREV.MSG_CLOSED_IMMUTABLE
MSG_SESSION_DELETE = PREV.MSG_SESSION_DELETE

LATEST_CERT_ID = "(SELECT MAX(id) FROM reconciliation_certifications WHERE session_id = {session})"
# The latest certification is superseded when the session's last completed
# reopening (last_reopen_event_id) was recorded for it.
LATEST_CERT_SUPERSEDED = (
    "EXISTS (SELECT 1 FROM reconciliation_reopen_events e "
    "WHERE e.id = {last_event} AND e.certification_id = " + LATEST_CERT_ID + ")"
)


# --- PostgreSQL ------------------------------------------------------------------

PG_FUNCTIONS = {
    "fms_recon_certifications_guard_v3": f"""
CREATE FUNCTION fms_recon_certifications_guard_v3() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    sess_status varchar;
    sess_last_event integer;
    last_version integer;
BEGIN
    SELECT status, last_reopen_event_id INTO sess_status, sess_last_event
      FROM reconciliation_sessions WHERE id = NEW.session_id;
    IF sess_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION '{MSG_CERT_SESSION_OPEN}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(MAX(version), 0) INTO last_version
      FROM reconciliation_certifications WHERE session_id = NEW.session_id;
    IF NEW.version <> last_version + 1 THEN
        RAISE EXCEPTION '{MSG_CERT_VERSION}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.reopen_event_id IS DISTINCT FROM sess_last_event THEN
        RAISE EXCEPTION '{MSG_CERT_REOPEN_LINK}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF last_version > 0 AND NOT {LATEST_CERT_SUPERSEDED.format(last_event="sess_last_event", session="NEW.session_id")} THEN
        RAISE EXCEPTION '{MSG_CERT_CYCLE}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_recon_reopen_events_guard_v3": f"""
CREATE FUNCTION fms_recon_reopen_events_guard_v3() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    sess_status varchar;
    sess_reopens integer;
    latest_cert integer;
BEGIN
    SELECT status, reopen_count INTO sess_status, sess_reopens
      FROM reconciliation_sessions WHERE id = NEW.session_id;
    IF sess_status IS DISTINCT FROM 'closed' THEN
        RAISE EXCEPTION '{MSG_REOPEN_SESSION_CLOSED}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT MAX(id) INTO latest_cert
      FROM reconciliation_certifications WHERE session_id = NEW.session_id;
    IF latest_cert IS NULL OR NEW.certification_id <> latest_cert THEN
        RAISE EXCEPTION '{MSG_REOPEN_CERT}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.reopen_number <> sess_reopens + 1 THEN
        RAISE EXCEPTION '{MSG_REOPEN_NUMBER}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_recon_sessions_guard_v3": f"""
CREATE FUNCTION fms_recon_sessions_guard_v3() RETURNS trigger
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
    IF OLD.status = 'closed' AND NEW.status <> 'closed' THEN
        IF NEW.reopen_count <> OLD.reopen_count + 1 THEN
            RAISE EXCEPTION '{MSG_REOPEN_COUNT}' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM reconciliation_reopen_events e
             WHERE e.id = NEW.last_reopen_event_id AND e.session_id = NEW.id
               AND e.reopen_number = NEW.reopen_count
               AND e.certification_id = {LATEST_CERT_ID.format(session="NEW.id")}
        ) THEN
            RAISE EXCEPTION '{MSG_REOPEN_NEEDS_EVENT}' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSE
        IF NEW.reopen_count <> OLD.reopen_count
           OR NEW.last_reopen_event_id IS DISTINCT FROM OLD.last_reopen_event_id THEN
            RAISE EXCEPTION '{MSG_REOPEN_FIELDS}' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF OLD.status <> 'closed' AND NEW.status = 'closed' AND (
        {LATEST_CERT_ID.format(session="NEW.id")} IS NULL
        OR {LATEST_CERT_SUPERSEDED.format(last_event="NEW.last_reopen_event_id", session="NEW.id")}
    ) THEN
        RAISE EXCEPTION '{MSG_CLOSE_NEEDS_CERT}' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS = {
    "trg_recon_certifications_guard_v3": """
CREATE TRIGGER trg_recon_certifications_guard_v3
BEFORE INSERT ON reconciliation_certifications
FOR EACH ROW EXECUTE FUNCTION fms_recon_certifications_guard_v3()
""",
    "trg_recon_reopen_events_guard_v3": """
CREATE TRIGGER trg_recon_reopen_events_guard_v3
BEFORE INSERT ON reconciliation_reopen_events
FOR EACH ROW EXECUTE FUNCTION fms_recon_reopen_events_guard_v3()
""",
    "trg_recon_sessions_guard_v3": """
CREATE TRIGGER trg_recon_sessions_guard_v3
BEFORE UPDATE OR DELETE ON reconciliation_sessions
FOR EACH ROW EXECUTE FUNCTION fms_recon_sessions_guard_v3()
""",
}
PG_TRIGGER_TABLES = {
    "trg_recon_certifications_guard_v3": "reconciliation_certifications",
    "trg_recon_reopen_events_guard_v3": "reconciliation_reopen_events",
    "trg_recon_sessions_guard_v3": "reconciliation_sessions",
}

# cashmgmt02 objects replaced here (append-only functions/triggers are kept on PostgreSQL).
PG_OLD_TRIGGERS = {
    "trg_reconciliation_certifications_guard": "reconciliation_certifications",
    "trg_reconciliation_reopen_events_guard": "reconciliation_reopen_events",
    "trg_reconciliation_sessions_guard": "reconciliation_sessions",
}
PG_APPEND_ONLY_TRIGGERS = {
    "trg_reconciliation_certifications_append_only": "reconciliation_certifications",
    "trg_reconciliation_reopen_events_append_only": "reconciliation_reopen_events",
}
PG_OLD_FUNCTIONS = (
    "fms_reconciliation_certifications_guard",
    "fms_reconciliation_reopen_events_guard",
    "fms_reconciliation_sessions_guard",
)


# --- SQLite ----------------------------------------------------------------------

_t = PREV._sqlite_trigger
SESSION_FIELD = "(SELECT {field} FROM reconciliation_sessions WHERE id = {ref}.session_id)"

SQLITE_TRIGGERS = {
    # append-only (same DDL as cashmgmt02; recreated because batch mode rebuilds the tables)
    "trg_recon_certs_no_update": PREV.SQLITE_TRIGGERS["trg_recon_certs_no_update"],
    "trg_recon_certs_no_delete": PREV.SQLITE_TRIGGERS["trg_recon_certs_no_delete"],
    "trg_recon_reopen_no_update": PREV.SQLITE_TRIGGERS["trg_recon_reopen_no_update"],
    "trg_recon_reopen_no_delete": PREV.SQLITE_TRIGGERS["trg_recon_reopen_no_delete"],
    # certifications
    "trg_recon_certs_insert_open": PREV.SQLITE_TRIGGERS["trg_recon_certs_insert_open"],
    "trg_recon_certs_insert_version": PREV.SQLITE_TRIGGERS["trg_recon_certs_insert_version"],
    "trg_recon_certs_insert_reopen_link": _t(
        "trg_recon_certs_insert_reopen_link", "INSERT", "reconciliation_certifications",
        f"NEW.reopen_event_id IS NOT {SESSION_FIELD.format(field='last_reopen_event_id', ref='NEW')}",
        MSG_CERT_REOPEN_LINK,
    ),
    "trg_recon_certs_insert_cycle": _t(
        "trg_recon_certs_insert_cycle", "INSERT", "reconciliation_certifications",
        f"{LATEST_CERT_ID.format(session='NEW.session_id')} IS NOT NULL AND NOT "
        + LATEST_CERT_SUPERSEDED.format(
            last_event=SESSION_FIELD.format(field="last_reopen_event_id", ref="NEW"),
            session="NEW.session_id",
        ),
        MSG_CERT_CYCLE,
    ),
    # reopen events
    "trg_recon_reopen_insert_closed": PREV.SQLITE_TRIGGERS["trg_recon_reopen_insert_closed"],
    "trg_recon_reopen_insert_cert": PREV.SQLITE_TRIGGERS["trg_recon_reopen_insert_cert"],
    "trg_recon_reopen_insert_number": _t(
        "trg_recon_reopen_insert_number", "INSERT", "reconciliation_reopen_events",
        f"NEW.reopen_number <> {SESSION_FIELD.format(field='reopen_count', ref='NEW')} + 1", MSG_REOPEN_NUMBER,
    ),
    # sessions
    "trg_recon_sessions_no_delete_certified": PREV.SQLITE_TRIGGERS["trg_recon_sessions_no_delete_certified"],
    "trg_recon_sessions_closed_scope": PREV.SQLITE_TRIGGERS["trg_recon_sessions_closed_scope"],
    "trg_recon_sessions_reopen_count": _t(
        "trg_recon_sessions_reopen_count", "UPDATE", "reconciliation_sessions",
        "OLD.status = 'closed' AND NEW.status <> 'closed' AND NEW.reopen_count <> OLD.reopen_count + 1",
        MSG_REOPEN_COUNT,
    ),
    "trg_recon_sessions_reopen_needs_event": _t(
        "trg_recon_sessions_reopen_needs_event", "UPDATE", "reconciliation_sessions",
        "OLD.status = 'closed' AND NEW.status <> 'closed' AND NOT EXISTS ("
        "SELECT 1 FROM reconciliation_reopen_events e WHERE e.id = NEW.last_reopen_event_id "
        "AND e.session_id = NEW.id AND e.reopen_number = NEW.reopen_count "
        f"AND e.certification_id = {LATEST_CERT_ID.format(session='NEW.id')})",
        MSG_REOPEN_NEEDS_EVENT,
    ),
    "trg_recon_sessions_reopen_fields": _t(
        "trg_recon_sessions_reopen_fields", "UPDATE", "reconciliation_sessions",
        "NOT (OLD.status = 'closed' AND NEW.status <> 'closed') AND (NEW.reopen_count <> OLD.reopen_count "
        "OR NEW.last_reopen_event_id IS NOT OLD.last_reopen_event_id)",
        MSG_REOPEN_FIELDS,
    ),
    "trg_recon_sessions_close_needs_cert": _t(
        "trg_recon_sessions_close_needs_cert", "UPDATE", "reconciliation_sessions",
        "OLD.status <> 'closed' AND NEW.status = 'closed' AND ("
        f"{LATEST_CERT_ID.format(session='NEW.id')} IS NULL OR "
        + LATEST_CERT_SUPERSEDED.format(last_event="NEW.last_reopen_event_id", session="NEW.id")
        + ")",
        MSG_CLOSE_NEEDS_CERT,
    ),
}


# --- Helpers ------------------------------------------------------------------------


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for cash triggers: {name}")
    return name


def _ensure_atomic() -> None:
    """Make the whole revision one transaction on SQLite too.

    Alembic treats SQLite DDL as non-transactional (pysqlite only begins a
    transaction before DML), so without this a failure half-way would leave a
    partially migrated schema. With an explicit BEGIN every statement of the
    revision - DDL, backfill, version stamp - commits or rolls back together.
    """
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        raw = bind.connection.dbapi_connection
        if not raw.in_transaction:
            raw.execute("BEGIN")


def _drop_sqlite_triggers(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _rows(sql: str, **params) -> list:
    return op.get_bind().execute(sa.text(sql), params).all()


def _preflight_upgrade() -> None:
    """Inspect existing data before any DDL. Read-only.

    Refuses (nothing changed) when references are broken, because no honest
    value exists for the new columns. Logs irregular-but-representable
    history; it is migrated as is and reported by the application.
    """
    broken = _rows(
        "SELECT e.id, e.session_id, e.certification_id FROM reconciliation_reopen_events e "
        "LEFT JOIN reconciliation_certifications c ON c.id = e.certification_id "
        "WHERE c.id IS NULL OR c.session_id <> e.session_id ORDER BY e.id"
    )
    if broken:
        raise MigrationRefused(
            "cashmgmt03 refused, nothing was changed: reopen events reference a missing "
            "certification or a certification of another session "
            f"(event id, session id, certification id): {[tuple(r) for r in broken]}. "
            "Repair these references explicitly before upgrading."
        )
    missing_sessions = _rows(
        "SELECT c.id, c.session_id FROM reconciliation_certifications c "
        "LEFT JOIN reconciliation_sessions s ON s.id = c.session_id WHERE s.id IS NULL"
    )
    if missing_sessions:
        raise MigrationRefused(
            "cashmgmt03 refused, nothing was changed: certifications reference missing "
            f"sessions (certification id, session id): {[tuple(r) for r in missing_sessions]}"
        )

    warnings = []
    for sid, count in _rows(
        "SELECT session_id, COUNT(*) FROM reconciliation_reopen_events GROUP BY session_id"
    ):
        reopen_count = _rows(
            "SELECT reopen_count FROM reconciliation_sessions WHERE id = :s", s=sid
        )[0][0]
        if count != reopen_count:
            warnings.append(
                f"session {sid}: {count} reopen event(s) but reopen_count = {reopen_count}; the "
                "extra events are kept and will be reported as orphaned/superseded"
            )
    for sid, version, has_event in _rows(
        "SELECT c.session_id, c.version, EXISTS (SELECT 1 FROM reconciliation_reopen_events e "
        "JOIN reconciliation_certifications p ON p.id = e.certification_id "
        "WHERE p.session_id = c.session_id AND p.version = c.version - 1) "
        "FROM reconciliation_certifications c WHERE c.version > 1 ORDER BY c.session_id, c.version"
    ):
        if not has_event:
            warnings.append(
                f"session {sid}: certification version {version} is not preceded by a recorded "
                "reopening; kept as is (reopen_event_id stays NULL) and reported as an anomaly"
            )
    for sid, versions in _rows(
        "SELECT session_id, COUNT(*) || '/' || MAX(version) FROM reconciliation_certifications "
        "GROUP BY session_id"
    ):
        count, highest = versions.split("/")
        if count != highest:
            warnings.append(f"session {sid}: certification versions are not contiguous (kept as is)")
    for line in warnings:
        log.warning("cashmgmt03: %s", line)


def _preflight_downgrade() -> None:
    """cashmgmt02 allows one reopen event per certification; refuse if the data has more."""
    multiple = _rows(
        "SELECT certification_id, COUNT(*) FROM reconciliation_reopen_events "
        "GROUP BY certification_id HAVING COUNT(*) > 1 ORDER BY certification_id"
    )
    if multiple:
        raise MigrationRefused(
            "downgrade to cashmgmt02 refused, nothing was changed: certifications "
            f"{[r[0] for r in multiple]} have several reopen events, which cashmgmt02 cannot "
            "represent. Events are evidence and are not deleted."
        )


# --- Upgrade / downgrade -----------------------------------------------------------


def upgrade() -> None:
    _ensure_atomic()
    dialect = _dialect()
    _preflight_upgrade()

    # 1. Remove the rules being replaced (and, on SQLite, every trigger of the
    #    tables that batch mode is about to rebuild).
    if dialect == "postgresql":
        for trigger, table in PG_OLD_TRIGGERS.items():
            op.execute(f"DROP TRIGGER {trigger} ON {table}")
        for name in PG_OLD_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
        # The append-only triggers stay, but must not block the one-off backfill below.
        for trigger, table in PG_APPEND_ONLY_TRIGGERS.items():
            op.execute(f"DROP TRIGGER {trigger} ON {table}")
    else:
        _drop_sqlite_triggers(PREV.SQLITE_TRIGGERS)

    # 2. Columns / constraints, then the backfill (new columns only; nothing else is touched).
    with op.batch_alter_table("reconciliation_reopen_events") as batch:
        batch.add_column(sa.Column("reopen_number", sa.Integer(), nullable=True))
        batch.drop_constraint("uq_reconciliation_reopen_events_certification_id", type_="unique")
    # An event asks for the reopening that ends the cycle of the certification it references.
    op.execute(
        "UPDATE reconciliation_reopen_events SET reopen_number = ("
        "SELECT c.version FROM reconciliation_certifications c "
        "WHERE c.id = reconciliation_reopen_events.certification_id)"
    )
    with op.batch_alter_table("reconciliation_reopen_events") as batch:
        batch.alter_column("reopen_number", existing_type=sa.Integer(), nullable=False)
        batch.create_index("ix_reconciliation_reopen_events_certification_id", ["certification_id"])
        batch.create_index(
            "ix_reconciliation_reopen_events_session_id_reopen_number", ["session_id", "reopen_number"]
        )

    if dialect == "sqlite":
        _drop_sqlite_triggers(SQLITE_MATCH_SESSION_TRIGGERS)
    with op.batch_alter_table("reconciliation_sessions") as batch:
        batch.add_column(sa.Column("last_reopen_event_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_recon_sessions_last_reopen_event",
            "reconciliation_reopen_events",
            ["last_reopen_event_id"],
            ["id"],
        )
    if dialect == "sqlite":
        for name in SQLITE_MATCH_SESSION_TRIGGERS:
            op.execute(FIRST.SQLITE_TRIGGERS[name])
    # The event that completed the latest reopening: only when exactly one qualifies.
    op.execute(
        "UPDATE reconciliation_sessions SET last_reopen_event_id = ("
        "SELECT MIN(e.id) FROM reconciliation_reopen_events e "
        "WHERE e.session_id = reconciliation_sessions.id "
        "AND e.reopen_number = reconciliation_sessions.reopen_count) "
        "WHERE reopen_count > 0 AND ("
        "SELECT COUNT(*) FROM reconciliation_reopen_events e "
        "WHERE e.session_id = reconciliation_sessions.id "
        "AND e.reopen_number = reconciliation_sessions.reopen_count) = 1"
    )

    with op.batch_alter_table("reconciliation_certifications") as batch:
        batch.add_column(sa.Column("reopen_event_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_recon_certifications_reopen_event",
            "reconciliation_reopen_events",
            ["reopen_event_id"],
            ["id"],
        )
    # The event that started the certified cycle = the single event of the previous version.
    op.execute(
        "UPDATE reconciliation_certifications SET reopen_event_id = ("
        "SELECT MIN(e.id) FROM reconciliation_reopen_events e "
        "JOIN reconciliation_certifications p ON p.id = e.certification_id "
        "WHERE p.session_id = reconciliation_certifications.session_id "
        "AND p.version = reconciliation_certifications.version - 1) "
        "WHERE version > 1 AND ("
        "SELECT COUNT(*) FROM reconciliation_reopen_events e "
        "JOIN reconciliation_certifications p ON p.id = e.certification_id "
        "WHERE p.session_id = reconciliation_certifications.session_id "
        "AND p.version = reconciliation_certifications.version - 1) = 1"
    )

    # 3. New rules (and the unchanged append-only triggers again on PostgreSQL).
    if dialect == "postgresql":
        for trigger in PG_APPEND_ONLY_TRIGGERS:
            op.execute(PREV.PG_TRIGGERS[trigger])
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    _ensure_atomic()
    dialect = _dialect()
    _preflight_downgrade()
    if dialect == "postgresql":
        for trigger, table in PG_TRIGGER_TABLES.items():
            op.execute(f"DROP TRIGGER {trigger} ON {table}")
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
    else:
        _drop_sqlite_triggers(SQLITE_TRIGGERS)

    with op.batch_alter_table("reconciliation_certifications") as batch:
        batch.drop_constraint("fk_recon_certifications_reopen_event", type_="foreignkey")
        batch.drop_column("reopen_event_id")
    if dialect == "sqlite":
        _drop_sqlite_triggers(SQLITE_MATCH_SESSION_TRIGGERS)
    with op.batch_alter_table("reconciliation_sessions") as batch:
        batch.drop_constraint("fk_recon_sessions_last_reopen_event", type_="foreignkey")
        batch.drop_column("last_reopen_event_id")
    if dialect == "sqlite":
        for name in SQLITE_MATCH_SESSION_TRIGGERS:
            op.execute(FIRST.SQLITE_TRIGGERS[name])
    with op.batch_alter_table("reconciliation_reopen_events") as batch:
        batch.drop_index("ix_reconciliation_reopen_events_session_id_reopen_number")
        batch.drop_index("ix_reconciliation_reopen_events_certification_id")
        batch.drop_column("reopen_number")
        batch.create_unique_constraint(
            "uq_reconciliation_reopen_events_certification_id", ["certification_id"]
        )

    if dialect == "postgresql":
        for name in PG_OLD_FUNCTIONS:
            op.execute(PREV.PG_FUNCTIONS[name])
        for trigger in PG_OLD_TRIGGERS:
            op.execute(PREV.PG_TRIGGERS[trigger])
    else:
        for ddl in PREV.SQLITE_TRIGGERS.values():
            op.execute(ddl)
