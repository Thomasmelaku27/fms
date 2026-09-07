"""Hierarchical chart of accounts, journal source references, close invariants.

Schema changes:

* ``accounts``: ``parent_id`` (self reference), ``is_summary``, ``normal_balance``
  (backfilled from ``type``: asset/expense -> debit, others -> credit),
  ``category``. Existing flat accounts stay valid posting accounts.
* ``journal_entries``: ``source_type`` / ``source_id`` / ``operation_id`` /
  ``request_fingerprint`` with a unique (type, id, operation) key - the
  idempotency contract of the journal-processing interface - and a CHECK that
  the three identifiers are either all set or all NULL.

Ledger rules (triggers, v4 -- replaces the v3 set):

* unchanged: posted entries/lines immutable, posting completeness/balance,
  closed/locked periods block posting, reversal only of posted entries,
  periods never overlap, lock only from closed, locked periods immutable;
* journal lines may only reference posting (non-summary) accounts;
* an account with journal lines cannot change type or become a summary
  account; a parent must be a summary account of the same type;
* a period can only move to ``closed`` when no draft is dated inside it and
  no earlier period is still open (the month-end invariant, re-checked in the
  database so a direct SQL close cannot bypass it).

Revision ID: d5b5eb8280ba
Revises: a9ed0215b3c6
Create Date: 2026-04-01 00:00:00

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import conv

revision: str = "d5b5eb8280ba"
down_revision: str | None = "a9ed0215b3c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MSG_POSTED_LINES_IMMUTABLE = "lines of a posted journal entry cannot be modified"
MSG_POST_INCOMPLETE_LINES = "journal entry has lines without an amount and cannot be posted"
MSG_POST_BOTH_SIDES = "journal entry has lines with both a debit and a credit and cannot be posted"
MSG_POST_MUST_BALANCE = "journal entry must have lines that balance before it can be posted"
MSG_POST_CLOSED_PERIOD = "journal entry is dated in a closed accounting period and cannot be posted"
MSG_POSTED_ENTRY_IMMUTABLE = "a posted journal entry cannot be modified or deleted"
MSG_REVERSE_ONLY_POSTED = "only a posted journal entry can be reversed"
MSG_PERIODS_OVERLAP = "accounting periods cannot overlap"
MSG_PERIOD_LOCKED = "a locked accounting period cannot be modified or deleted"
MSG_LOCK_FROM_CLOSED = "an accounting period can only be locked when it is closed"
MSG_SUMMARY_ACCOUNT = "journal lines cannot reference a summary account"
MSG_ACCOUNT_HAS_HISTORY = "an account with journal lines cannot change its type or become a summary"
MSG_PARENT_INVALID = "the parent of an account must be a summary account of the same type"
MSG_CLOSE_DRAFTS = (
    "an accounting period cannot be closed while draft journal entries are dated in it"
)
MSG_CLOSE_ORDER = "an accounting period cannot be closed while an earlier period is still open"

ERRCODE = "USING ERRCODE = 'integrity_constraint_violation'"

# --- PostgreSQL -----------------------------------------------------------------

PG_FUNCTIONS = {
    "fms_journal_lines_guard_posted": f"""
CREATE OR REPLACE FUNCTION fms_journal_lines_guard_posted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
        SELECT 1 FROM journal_entries WHERE id = OLD.entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{MSG_POSTED_LINES_IMMUTABLE}' {ERRCODE};
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        IF EXISTS (
            SELECT 1 FROM journal_entries WHERE id = NEW.entry_id AND status = 'posted'
        ) THEN
            RAISE EXCEPTION '{MSG_POSTED_LINES_IMMUTABLE}' {ERRCODE};
        END IF;
        IF EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id AND is_summary) THEN
            RAISE EXCEPTION '{MSG_SUMMARY_ACCOUNT}' {ERRCODE};
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_journal_entries_guard_status": f"""
CREATE OR REPLACE FUNCTION fms_journal_entries_guard_status() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    line_count integer;
    total_debit numeric;
    total_credit numeric;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'posted' THEN
            RAISE EXCEPTION '{MSG_POSTED_ENTRY_IMMUTABLE}' {ERRCODE};
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.status = 'posted' THEN
        RAISE EXCEPTION '{MSG_POSTED_ENTRY_IMMUTABLE}' {ERRCODE};
    END IF;

    IF NEW.reverses_entry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM journal_entries WHERE id = NEW.reverses_entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{MSG_REVERSE_ONLY_POSTED}' {ERRCODE};
    END IF;

    IF NEW.status = 'posted' AND (TG_OP = 'INSERT' OR OLD.status <> 'posted') THEN
        IF EXISTS (
            SELECT 1 FROM accounting_periods
             WHERE status IN ('closed', 'locked')
               AND NEW.entry_date BETWEEN start_date AND end_date
        ) THEN
            RAISE EXCEPTION '{MSG_POST_CLOSED_PERIOD}' {ERRCODE};
        END IF;
        IF EXISTS (
            SELECT 1 FROM journal_lines
             WHERE entry_id = NEW.id AND debit = 0 AND credit = 0
        ) THEN
            RAISE EXCEPTION '{MSG_POST_INCOMPLETE_LINES}' {ERRCODE};
        END IF;
        IF EXISTS (
            SELECT 1 FROM journal_lines
             WHERE entry_id = NEW.id AND debit > 0 AND credit > 0
        ) THEN
            RAISE EXCEPTION '{MSG_POST_BOTH_SIDES}' {ERRCODE};
        END IF;
        SELECT COUNT(*), COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
          INTO line_count, total_debit, total_credit
          FROM journal_lines WHERE entry_id = NEW.id;
        IF line_count < 2 OR total_debit <> total_credit THEN
            RAISE EXCEPTION '{MSG_POST_MUST_BALANCE}' {ERRCODE};
        END IF;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_accounting_periods_guard": f"""
CREATE OR REPLACE FUNCTION fms_accounting_periods_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.status = 'locked' THEN
        RAISE EXCEPTION '{MSG_PERIOD_LOCKED}' {ERRCODE};
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF NEW.status = 'locked' AND (TG_OP = 'INSERT' OR OLD.status <> 'closed') THEN
        RAISE EXCEPTION '{MSG_LOCK_FROM_CLOSED}' {ERRCODE};
    END IF;
    IF NEW.status = 'closed' AND (TG_OP = 'INSERT' OR OLD.status = 'open') THEN
        IF EXISTS (
            SELECT 1 FROM journal_entries
             WHERE status = 'draft' AND entry_date BETWEEN NEW.start_date AND NEW.end_date
        ) THEN
            RAISE EXCEPTION '{MSG_CLOSE_DRAFTS}' {ERRCODE};
        END IF;
        IF EXISTS (
            SELECT 1 FROM accounting_periods p
             WHERE p.id <> NEW.id AND p.end_date < NEW.start_date AND p.status = 'open'
        ) THEN
            RAISE EXCEPTION '{MSG_CLOSE_ORDER}' {ERRCODE};
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM accounting_periods p
         WHERE p.id <> NEW.id
           AND NEW.start_date <= p.end_date AND p.start_date <= NEW.end_date
    ) THEN
        RAISE EXCEPTION '{MSG_PERIODS_OVERLAP}' {ERRCODE};
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_accounts_guard": f"""
CREATE OR REPLACE FUNCTION fms_accounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.type <> OLD.type OR (NEW.is_summary AND NOT OLD.is_summary))
       AND EXISTS (SELECT 1 FROM journal_lines WHERE account_id = OLD.id) THEN
        RAISE EXCEPTION '{MSG_ACCOUNT_HAS_HISTORY}' {ERRCODE};
    END IF;
    IF NEW.parent_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM accounts p
         WHERE p.id = NEW.parent_id AND p.is_summary AND p.type = NEW.type
    ) THEN
        RAISE EXCEPTION '{MSG_PARENT_INVALID}' {ERRCODE};
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS = {
    "trg_journal_lines_guard_posted": """
CREATE TRIGGER trg_journal_lines_guard_posted
BEFORE INSERT OR UPDATE OR DELETE ON journal_lines
FOR EACH ROW EXECUTE FUNCTION fms_journal_lines_guard_posted()
""",
    "trg_journal_entries_guard_status": """
CREATE TRIGGER trg_journal_entries_guard_status
BEFORE INSERT OR UPDATE OR DELETE ON journal_entries
FOR EACH ROW EXECUTE FUNCTION fms_journal_entries_guard_status()
""",
    "trg_accounting_periods_guard": """
CREATE TRIGGER trg_accounting_periods_guard
BEFORE INSERT OR UPDATE OR DELETE ON accounting_periods
FOR EACH ROW EXECUTE FUNCTION fms_accounting_periods_guard()
""",
    "trg_accounts_guard": """
CREATE TRIGGER trg_accounts_guard
BEFORE INSERT OR UPDATE ON accounts
FOR EACH ROW EXECUTE FUNCTION fms_accounts_guard()
""",
}

# --- SQLite ---------------------------------------------------------------------

SQLITE_POSTED_ENTRY = (
    "SELECT 1 FROM journal_entries WHERE id = {ref}.entry_id AND status = 'posted'"
)
SQLITE_POSTING = "NEW.status = 'posted' AND OLD.status <> 'posted'"
SQLITE_SUMMARY = "SELECT 1 FROM accounts WHERE id = NEW.account_id AND is_summary"

SQLITE_TRIGGERS = {
    "trg_journal_lines_no_insert_posted": f"""
CREATE TRIGGER trg_journal_lines_no_insert_posted
BEFORE INSERT ON journal_lines FOR EACH ROW
WHEN EXISTS ({SQLITE_POSTED_ENTRY.format(ref="NEW")})
BEGIN
    SELECT RAISE(ABORT, '{MSG_POSTED_LINES_IMMUTABLE}');
END
""",
    "trg_journal_lines_no_update_posted": f"""
CREATE TRIGGER trg_journal_lines_no_update_posted
BEFORE UPDATE ON journal_lines FOR EACH ROW
WHEN EXISTS ({SQLITE_POSTED_ENTRY.format(ref="OLD")})
  OR EXISTS ({SQLITE_POSTED_ENTRY.format(ref="NEW")})
BEGIN
    SELECT RAISE(ABORT, '{MSG_POSTED_LINES_IMMUTABLE}');
END
""",
    "trg_journal_lines_no_delete_posted": f"""
CREATE TRIGGER trg_journal_lines_no_delete_posted
BEFORE DELETE ON journal_lines FOR EACH ROW
WHEN EXISTS ({SQLITE_POSTED_ENTRY.format(ref="OLD")})
BEGIN
    SELECT RAISE(ABORT, '{MSG_POSTED_LINES_IMMUTABLE}');
END
""",
    "trg_journal_lines_no_summary_insert": f"""
CREATE TRIGGER trg_journal_lines_no_summary_insert
BEFORE INSERT ON journal_lines FOR EACH ROW
WHEN EXISTS ({SQLITE_SUMMARY})
BEGIN
    SELECT RAISE(ABORT, '{MSG_SUMMARY_ACCOUNT}');
END
""",
    "trg_journal_lines_no_summary_update": f"""
CREATE TRIGGER trg_journal_lines_no_summary_update
BEFORE UPDATE OF account_id ON journal_lines FOR EACH ROW
WHEN EXISTS ({SQLITE_SUMMARY})
BEGIN
    SELECT RAISE(ABORT, '{MSG_SUMMARY_ACCOUNT}');
END
""",
    "trg_journal_entries_no_insert_posted": f"""
CREATE TRIGGER trg_journal_entries_no_insert_posted
BEFORE INSERT ON journal_entries FOR EACH ROW
WHEN NEW.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, '{MSG_POST_MUST_BALANCE}');
END
""",
    "trg_journal_entries_reverse_only_posted_insert": f"""
CREATE TRIGGER trg_journal_entries_reverse_only_posted_insert
BEFORE INSERT ON journal_entries FOR EACH ROW
WHEN NEW.reverses_entry_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM journal_entries WHERE id = NEW.reverses_entry_id AND status = 'posted'
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_REVERSE_ONLY_POSTED}');
END
""",
    "trg_journal_entries_reverse_only_posted_update": f"""
CREATE TRIGGER trg_journal_entries_reverse_only_posted_update
BEFORE UPDATE OF reverses_entry_id ON journal_entries FOR EACH ROW
WHEN NEW.reverses_entry_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM journal_entries WHERE id = NEW.reverses_entry_id AND status = 'posted'
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_REVERSE_ONLY_POSTED}');
END
""",
    "trg_journal_entries_post_in_closed_period": f"""
CREATE TRIGGER trg_journal_entries_post_in_closed_period
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND EXISTS (
    SELECT 1 FROM accounting_periods
     WHERE status IN ('closed', 'locked') AND NEW.entry_date BETWEEN start_date AND end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_POST_CLOSED_PERIOD}');
END
""",
    "trg_journal_entries_post_requires_complete_lines": f"""
CREATE TRIGGER trg_journal_entries_post_requires_complete_lines
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND EXISTS (
    SELECT 1 FROM journal_lines WHERE entry_id = NEW.id AND debit = 0 AND credit = 0
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_POST_INCOMPLETE_LINES}');
END
""",
    "trg_journal_entries_post_rejects_both_sides": f"""
CREATE TRIGGER trg_journal_entries_post_rejects_both_sides
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND EXISTS (
    SELECT 1 FROM journal_lines WHERE entry_id = NEW.id AND debit > 0 AND credit > 0
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_POST_BOTH_SIDES}');
END
""",
    "trg_journal_entries_post_must_balance": f"""
CREATE TRIGGER trg_journal_entries_post_must_balance
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND (
    SELECT COUNT(*) < 2
        OR SUM(CAST(ROUND(debit * 100) AS INTEGER)) <> SUM(CAST(ROUND(credit * 100) AS INTEGER))
    FROM journal_lines WHERE entry_id = NEW.id
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_POST_MUST_BALANCE}');
END
""",
    "trg_journal_entries_no_update_posted": f"""
CREATE TRIGGER trg_journal_entries_no_update_posted
BEFORE UPDATE ON journal_entries FOR EACH ROW
WHEN OLD.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, '{MSG_POSTED_ENTRY_IMMUTABLE}');
END
""",
    "trg_journal_entries_no_delete_posted": f"""
CREATE TRIGGER trg_journal_entries_no_delete_posted
BEFORE DELETE ON journal_entries FOR EACH ROW
WHEN OLD.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, '{MSG_POSTED_ENTRY_IMMUTABLE}');
END
""",
    "trg_accounting_periods_no_overlap_insert": f"""
CREATE TRIGGER trg_accounting_periods_no_overlap_insert
BEFORE INSERT ON accounting_periods FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM accounting_periods p
     WHERE NEW.start_date <= p.end_date AND p.start_date <= NEW.end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_PERIODS_OVERLAP}');
END
""",
    "trg_accounting_periods_no_overlap_update": f"""
CREATE TRIGGER trg_accounting_periods_no_overlap_update
BEFORE UPDATE OF start_date, end_date ON accounting_periods FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM accounting_periods p
     WHERE p.id <> NEW.id AND NEW.start_date <= p.end_date AND p.start_date <= NEW.end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_PERIODS_OVERLAP}');
END
""",
    "trg_accounting_periods_no_insert_locked": f"""
CREATE TRIGGER trg_accounting_periods_no_insert_locked
BEFORE INSERT ON accounting_periods FOR EACH ROW
WHEN NEW.status = 'locked'
BEGIN
    SELECT RAISE(ABORT, '{MSG_LOCK_FROM_CLOSED}');
END
""",
    "trg_accounting_periods_lock_from_closed": f"""
CREATE TRIGGER trg_accounting_periods_lock_from_closed
BEFORE UPDATE OF status ON accounting_periods FOR EACH ROW
WHEN NEW.status = 'locked' AND OLD.status <> 'closed'
BEGIN
    SELECT RAISE(ABORT, '{MSG_LOCK_FROM_CLOSED}');
END
""",
    "trg_accounting_periods_no_update_locked": f"""
CREATE TRIGGER trg_accounting_periods_no_update_locked
BEFORE UPDATE ON accounting_periods FOR EACH ROW
WHEN OLD.status = 'locked'
BEGIN
    SELECT RAISE(ABORT, '{MSG_PERIOD_LOCKED}');
END
""",
    "trg_accounting_periods_no_delete_locked": f"""
CREATE TRIGGER trg_accounting_periods_no_delete_locked
BEFORE DELETE ON accounting_periods FOR EACH ROW
WHEN OLD.status = 'locked'
BEGIN
    SELECT RAISE(ABORT, '{MSG_PERIOD_LOCKED}');
END
""",
    "trg_accounting_periods_close_requires_no_drafts": f"""
CREATE TRIGGER trg_accounting_periods_close_requires_no_drafts
BEFORE UPDATE OF status ON accounting_periods FOR EACH ROW
WHEN NEW.status = 'closed' AND OLD.status = 'open' AND EXISTS (
    SELECT 1 FROM journal_entries
     WHERE status = 'draft' AND entry_date BETWEEN NEW.start_date AND NEW.end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_CLOSE_DRAFTS}');
END
""",
    "trg_accounting_periods_close_in_order": f"""
CREATE TRIGGER trg_accounting_periods_close_in_order
BEFORE UPDATE OF status ON accounting_periods FOR EACH ROW
WHEN NEW.status = 'closed' AND OLD.status = 'open' AND EXISTS (
    SELECT 1 FROM accounting_periods p
     WHERE p.id <> NEW.id AND p.end_date < NEW.start_date AND p.status = 'open'
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_CLOSE_ORDER}');
END
""",
    "trg_accounting_periods_no_insert_closed_with_drafts": f"""
CREATE TRIGGER trg_accounting_periods_no_insert_closed_with_drafts
BEFORE INSERT ON accounting_periods FOR EACH ROW
WHEN NEW.status = 'closed' AND EXISTS (
    SELECT 1 FROM journal_entries
     WHERE status = 'draft' AND entry_date BETWEEN NEW.start_date AND NEW.end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_CLOSE_DRAFTS}');
END
""",
    "trg_accounts_history_update": f"""
CREATE TRIGGER trg_accounts_history_update
BEFORE UPDATE OF type, is_summary ON accounts FOR EACH ROW
WHEN (NEW.type <> OLD.type OR (NEW.is_summary AND NOT OLD.is_summary))
  AND EXISTS (SELECT 1 FROM journal_lines WHERE account_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, '{MSG_ACCOUNT_HAS_HISTORY}');
END
""",
    "trg_accounts_parent_insert": f"""
CREATE TRIGGER trg_accounts_parent_insert
BEFORE INSERT ON accounts FOR EACH ROW
WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM accounts p WHERE p.id = NEW.parent_id AND p.is_summary AND p.type = NEW.type
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_PARENT_INVALID}');
END
""",
    "trg_accounts_parent_update": f"""
CREATE TRIGGER trg_accounts_parent_update
BEFORE UPDATE OF parent_id, type ON accounts FOR EACH ROW
WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM accounts p WHERE p.id = NEW.parent_id AND p.is_summary AND p.type = NEW.type
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_PARENT_INVALID}');
END
""",
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for ledger triggers: {name}")
    return name


def _previous_revision():
    path = Path(__file__).with_name("20260301_a9ed0215b3c6_recurring_locking_versions.py")
    spec = importlib.util.spec_from_file_location("fms_revision_v3", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_sqlite_triggers(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


ACCOUNT_COLUMNS = [
    sa.Column("category", sa.String(length=30), nullable=True),
    sa.Column("normal_balance", sa.String(length=20), server_default="debit", nullable=False),
    sa.Column("is_summary", sa.Boolean(), server_default="0", nullable=False),
    sa.Column("parent_id", sa.Integer(), nullable=True),
]
FK_PARENT = conv("fk_accounts_parent_id_accounts")
CK_NOT_OWN_PARENT = conv("ck_accounts_not_own_parent")

SOURCE_COLUMNS = [
    sa.Column("source_type", sa.String(length=50), nullable=True),
    sa.Column("source_id", sa.String(length=100), nullable=True),
    sa.Column("operation_id", sa.String(length=100), nullable=True),
    sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
]
UQ_SOURCE = conv("uq_journal_entries_source_operation")
CK_SOURCE = conv("ck_journal_entries_source_complete")
CK_SOURCE_SQL = (
    "(source_type IS NULL AND source_id IS NULL AND operation_id IS NULL) OR "
    "(source_type IS NOT NULL AND source_id IS NOT NULL AND operation_id IS NOT NULL)"
)
BACKFILL_NORMAL_BALANCE = (
    "UPDATE accounts SET normal_balance = CASE WHEN type IN ('asset', 'expense') "
    "THEN 'debit' ELSE 'credit' END"
)


def upgrade() -> None:
    dialect = _dialect()

    if dialect == "postgresql":
        for column in ACCOUNT_COLUMNS:
            op.add_column("accounts", column)
        op.create_foreign_key(FK_PARENT, "accounts", "accounts", ["parent_id"], ["id"])
        op.create_check_constraint(
            CK_NOT_OWN_PARENT, "accounts", "parent_id IS NULL OR parent_id <> id"
        )
        op.execute(BACKFILL_NORMAL_BALANCE)
        for column in SOURCE_COLUMNS:
            op.add_column("journal_entries", column)
        op.create_unique_constraint(
            UQ_SOURCE, "journal_entries", ["source_type", "source_id", "operation_id"]
        )
        op.create_check_constraint(CK_SOURCE, "journal_entries", CK_SOURCE_SQL)
        for ddl in PG_FUNCTIONS.values():
            op.execute(ddl)
        op.execute(PG_TRIGGERS["trg_accounts_guard"])
        return

    _drop_sqlite_triggers(_previous_revision().SQLITE_TRIGGERS)
    with op.batch_alter_table("accounts") as batch:
        for column in ACCOUNT_COLUMNS:
            batch.add_column(column)
        batch.create_foreign_key(FK_PARENT, "accounts", ["parent_id"], ["id"])
        batch.create_check_constraint(CK_NOT_OWN_PARENT, "parent_id IS NULL OR parent_id <> id")
    op.execute(BACKFILL_NORMAL_BALANCE)
    with op.batch_alter_table("journal_entries") as batch:
        for column in SOURCE_COLUMNS:
            batch.add_column(column)
        batch.create_unique_constraint(UQ_SOURCE, ["source_type", "source_id", "operation_id"])
        batch.create_check_constraint(CK_SOURCE, CK_SOURCE_SQL)
    for ddl in SQLITE_TRIGGERS.values():
        op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()
    previous = _previous_revision()

    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_accounts_guard ON accounts")
        op.execute("DROP FUNCTION fms_accounts_guard()")
        op.drop_constraint(CK_SOURCE, "journal_entries", type_="check")
        op.drop_constraint(UQ_SOURCE, "journal_entries", type_="unique")
        for column in SOURCE_COLUMNS:
            op.drop_column("journal_entries", column.name)
        op.drop_constraint(CK_NOT_OWN_PARENT, "accounts", type_="check")
        op.drop_constraint(FK_PARENT, "accounts", type_="foreignkey")
        for column in ACCOUNT_COLUMNS:
            op.drop_column("accounts", column.name)
        for ddl in previous.PG_FUNCTIONS.values():
            op.execute(ddl)
        return

    _drop_sqlite_triggers(SQLITE_TRIGGERS)
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_constraint(CK_SOURCE, type_="check")
        batch.drop_constraint(UQ_SOURCE, type_="unique")
        for column in SOURCE_COLUMNS:
            batch.drop_column(column.name)
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint(CK_NOT_OWN_PARENT, type_="check")
        batch.drop_constraint(FK_PARENT, type_="foreignkey")
        for column in ACCOUNT_COLUMNS:
            batch.drop_column(column.name)
    for ddl in previous.SQLITE_TRIGGERS.values():
        op.execute(ddl)
