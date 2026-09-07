"""Fiscal years and the year-end close.

Schema changes:

* ``fiscal_years``: name, date range, status (open/closed), the retained
  earnings account the year closes into, and the ``closing`` journal entry
  produced by the year-end close.
* ``accounting_periods.fiscal_year_id``: periods may belong to a fiscal year
  (existing periods stay unassigned and keep working).

Ledger rules (triggers, v5 -- replaces the v4 set):

* unchanged: everything from v4;
* a ``closing`` journal entry (year-end close) may be posted into a *closed*
  period - that is the only exception to the closed-period rule; *locked*
  periods still refuse everything;
* ``opening`` and ``closing`` entries can never be reversed;
* a period that belongs to a closed fiscal year cannot be reopened;
* fiscal years never overlap; a closed fiscal year cannot be changed or
  deleted, and closing a year requires every period inside it to be closed
  or locked and ``closing_entry_id`` (when set) to reference a posted
  ``closing`` entry.

Revision ID: 78f70521def3
Revises: d5b5eb8280ba
Create Date: 2026-05-01 00:00:00

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import conv

revision: str = "78f70521def3"
down_revision: str | None = "d5b5eb8280ba"
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

MSG_REVERSE_SYSTEM_ENTRY = "opening and closing journal entries cannot be reversed"
MSG_REOPEN_CLOSED_YEAR = "a period of a closed fiscal year cannot be reopened"
MSG_YEARS_OVERLAP = "fiscal years cannot overlap"
MSG_YEAR_CLOSED = "a closed fiscal year cannot be modified or deleted"
MSG_YEAR_CLOSE_PERIODS = (
    "a fiscal year can only be closed when all of its periods are closed or locked"
)
MSG_YEAR_CLOSING_ENTRY = "closing_entry_id must reference a posted closing journal entry"

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
    IF NEW.reverses_entry_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM journal_entries
         WHERE id = NEW.reverses_entry_id AND entry_type IN ('opening', 'closing')
    ) THEN
        RAISE EXCEPTION '{MSG_REVERSE_SYSTEM_ENTRY}' {ERRCODE};
    END IF;

    IF NEW.status = 'posted' AND (TG_OP = 'INSERT' OR OLD.status <> 'posted') THEN
        IF EXISTS (
            SELECT 1 FROM accounting_periods
             WHERE NEW.entry_date BETWEEN start_date AND end_date
               AND (status = 'locked' OR (status = 'closed' AND NEW.entry_type <> 'closing'))
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
    IF TG_OP = 'UPDATE' AND NEW.status = 'open' AND OLD.status <> 'open' AND EXISTS (
        SELECT 1 FROM fiscal_years WHERE id = OLD.fiscal_year_id AND status = 'closed'
    ) THEN
        RAISE EXCEPTION '{MSG_REOPEN_CLOSED_YEAR}' {ERRCODE};
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

PG_FUNCTIONS["fms_fiscal_years_guard"] = f"""
CREATE OR REPLACE FUNCTION fms_fiscal_years_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.status = 'closed' THEN
        RAISE EXCEPTION '{MSG_YEAR_CLOSED}' {ERRCODE};
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF EXISTS (
        SELECT 1 FROM fiscal_years y
         WHERE y.id <> NEW.id AND NEW.start_date <= y.end_date AND y.start_date <= NEW.end_date
    ) THEN
        RAISE EXCEPTION '{MSG_YEARS_OVERLAP}' {ERRCODE};
    END IF;
    IF NEW.status = 'closed' THEN
        IF EXISTS (
            SELECT 1 FROM accounting_periods p
             WHERE p.fiscal_year_id = NEW.id AND p.status = 'open'
        ) THEN
            RAISE EXCEPTION '{MSG_YEAR_CLOSE_PERIODS}' {ERRCODE};
        END IF;
        IF NEW.closing_entry_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM journal_entries
             WHERE id = NEW.closing_entry_id AND status = 'posted' AND entry_type = 'closing'
        ) THEN
            RAISE EXCEPTION '{MSG_YEAR_CLOSING_ENTRY}' {ERRCODE};
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

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
    "trg_fiscal_years_guard": """
CREATE TRIGGER trg_fiscal_years_guard
BEFORE INSERT OR UPDATE OR DELETE ON fiscal_years
FOR EACH ROW EXECUTE FUNCTION fms_fiscal_years_guard()
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
    "trg_journal_entries_no_reverse_system_insert": f"""
CREATE TRIGGER trg_journal_entries_no_reverse_system_insert
BEFORE INSERT ON journal_entries FOR EACH ROW
WHEN NEW.reverses_entry_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM journal_entries
     WHERE id = NEW.reverses_entry_id AND entry_type IN ('opening', 'closing')
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_REVERSE_SYSTEM_ENTRY}');
END
""",
    "trg_journal_entries_no_reverse_system_update": f"""
CREATE TRIGGER trg_journal_entries_no_reverse_system_update
BEFORE UPDATE OF reverses_entry_id ON journal_entries FOR EACH ROW
WHEN NEW.reverses_entry_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM journal_entries
     WHERE id = NEW.reverses_entry_id AND entry_type IN ('opening', 'closing')
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_REVERSE_SYSTEM_ENTRY}');
END
""",
    "trg_journal_entries_post_in_closed_period": f"""
CREATE TRIGGER trg_journal_entries_post_in_closed_period
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND EXISTS (
    SELECT 1 FROM accounting_periods
     WHERE NEW.entry_date BETWEEN start_date AND end_date
       AND (status = 'locked' OR (status = 'closed' AND NEW.entry_type <> 'closing'))
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
    "trg_accounting_periods_no_reopen_closed_year": f"""
CREATE TRIGGER trg_accounting_periods_no_reopen_closed_year
BEFORE UPDATE OF status ON accounting_periods FOR EACH ROW
WHEN NEW.status = 'open' AND OLD.status <> 'open' AND EXISTS (
    SELECT 1 FROM fiscal_years WHERE id = OLD.fiscal_year_id AND status = 'closed'
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_REOPEN_CLOSED_YEAR}');
END
""",
    "trg_fiscal_years_no_update_closed": f"""
CREATE TRIGGER trg_fiscal_years_no_update_closed
BEFORE UPDATE ON fiscal_years FOR EACH ROW
WHEN OLD.status = 'closed'
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEAR_CLOSED}');
END
""",
    "trg_fiscal_years_no_delete_closed": f"""
CREATE TRIGGER trg_fiscal_years_no_delete_closed
BEFORE DELETE ON fiscal_years FOR EACH ROW
WHEN OLD.status = 'closed'
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEAR_CLOSED}');
END
""",
    "trg_fiscal_years_no_overlap_insert": f"""
CREATE TRIGGER trg_fiscal_years_no_overlap_insert
BEFORE INSERT ON fiscal_years FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM fiscal_years y
     WHERE NEW.start_date <= y.end_date AND y.start_date <= NEW.end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEARS_OVERLAP}');
END
""",
    "trg_fiscal_years_no_overlap_update": f"""
CREATE TRIGGER trg_fiscal_years_no_overlap_update
BEFORE UPDATE OF start_date, end_date ON fiscal_years FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM fiscal_years y
     WHERE y.id <> NEW.id AND NEW.start_date <= y.end_date AND y.start_date <= NEW.end_date
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEARS_OVERLAP}');
END
""",
    "trg_fiscal_years_close_requires_periods": f"""
CREATE TRIGGER trg_fiscal_years_close_requires_periods
BEFORE UPDATE OF status ON fiscal_years FOR EACH ROW
WHEN NEW.status = 'closed' AND EXISTS (
    SELECT 1 FROM accounting_periods p WHERE p.fiscal_year_id = NEW.id AND p.status = 'open'
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEAR_CLOSE_PERIODS}');
END
""",
    "trg_fiscal_years_closing_entry": f"""
CREATE TRIGGER trg_fiscal_years_closing_entry
BEFORE UPDATE OF status, closing_entry_id ON fiscal_years FOR EACH ROW
WHEN NEW.status = 'closed' AND NEW.closing_entry_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM journal_entries
     WHERE id = NEW.closing_entry_id AND status = 'posted' AND entry_type = 'closing'
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEAR_CLOSING_ENTRY}');
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
    path = Path(__file__).with_name("20260401_d5b5eb8280ba_account_hierarchy_source_refs.py")
    spec = importlib.util.spec_from_file_location("fms_revision_v4", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_sqlite_triggers(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


FK_PERIOD_YEAR = conv("fk_accounting_periods_fiscal_year_id_fiscal_years")


def _create_fiscal_years() -> None:
    op.create_table(
        "fiscal_years",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("retained_earnings_account_id", sa.Integer(), nullable=True),
        sa.Column("closing_entry_id", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_fiscal_years_dates_ordered")),
        sa.ForeignKeyConstraint(
            ["closing_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_fiscal_years_closing_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["retained_earnings_account_id"],
            ["accounts.id"],
            name=op.f("fk_fiscal_years_retained_earnings_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fiscal_years")),
        sa.UniqueConstraint("closing_entry_id", name=op.f("uq_fiscal_years_closing_entry_id")),
        sa.UniqueConstraint("name", name=op.f("uq_fiscal_years_name")),
    )


def upgrade() -> None:
    dialect = _dialect()
    _create_fiscal_years()

    if dialect == "postgresql":
        op.add_column(
            "accounting_periods", sa.Column("fiscal_year_id", sa.Integer(), nullable=True)
        )
        op.create_foreign_key(
            FK_PERIOD_YEAR, "accounting_periods", "fiscal_years", ["fiscal_year_id"], ["id"]
        )
        for ddl in PG_FUNCTIONS.values():
            op.execute(ddl)
        op.execute(PG_TRIGGERS["trg_fiscal_years_guard"])
        return

    _drop_sqlite_triggers(_previous_revision().SQLITE_TRIGGERS)
    with op.batch_alter_table("accounting_periods") as batch:
        batch.add_column(sa.Column("fiscal_year_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(FK_PERIOD_YEAR, "fiscal_years", ["fiscal_year_id"], ["id"])
    for ddl in SQLITE_TRIGGERS.values():
        op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()
    previous = _previous_revision()

    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_fiscal_years_guard ON fiscal_years")
        op.execute("DROP FUNCTION fms_fiscal_years_guard()")
        op.drop_constraint(FK_PERIOD_YEAR, "accounting_periods", type_="foreignkey")
        op.drop_column("accounting_periods", "fiscal_year_id")
        op.drop_table("fiscal_years")
        for ddl in previous.PG_FUNCTIONS.values():
            op.execute(ddl)
        return

    _drop_sqlite_triggers(SQLITE_TRIGGERS)
    with op.batch_alter_table("accounting_periods") as batch:
        batch.drop_constraint(FK_PERIOD_YEAR, type_="foreignkey")
        batch.drop_column("fiscal_year_id")
    op.drop_table("fiscal_years")
    for ddl in previous.SQLITE_TRIGGERS.values():
        op.execute(ddl)
