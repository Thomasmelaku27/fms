"""Recurring templates, entry versions/types, locked accounting periods.

Schema changes:

* ``journal_entries``: ``version`` (optimistic locking), ``entry_type``
  (standard / adjusting / recurring / reversal), ``recurring_template_id`` +
  ``scheduled_for`` with a unique (template, slot) constraint so generation can
  never create duplicate drafts.
* ``recurring_templates`` / ``recurring_template_lines``.
* ``accounting_periods.locked_at``; status gains the final value ``locked``.

Ledger rules (triggers, v3 -- replaces the v2 set):

* unchanged: posted entries/lines immutable, posting completeness/balance,
  reversal only of posted entries, periods never overlap;
* posting is blocked when the entry date is in a *closed or locked* period;
* a period can only become ``locked`` from ``closed``;
* a locked period row can never be updated or deleted.

SQLite: as in the previous revision, all triggers are dropped before the batch
table copies and the full v3 set is installed afterwards.

Revision ID: a9ed0215b3c6
Revises: f28dd4136bb8
Create Date: 2026-03-01 00:00:00

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import conv

revision: str = "a9ed0215b3c6"
down_revision: str | None = "f28dd4136bb8"
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
    IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
        SELECT 1 FROM journal_entries WHERE id = NEW.entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{MSG_POSTED_LINES_IMMUTABLE}' {ERRCODE};
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
}

# --- SQLite ---------------------------------------------------------------------

SQLITE_POSTED_ENTRY = (
    "SELECT 1 FROM journal_entries WHERE id = {ref}.entry_id AND status = 'posted'"
)
SQLITE_POSTING = "NEW.status = 'posted' AND OLD.status <> 'posted'"

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
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for ledger triggers: {name}")
    return name


def _previous_revision():
    """The v2 trigger DDL lives in the previous revision; load it for downgrade."""
    path = Path(__file__).with_name("20260201_f28dd4136bb8_reversals_periods_draft_lines.py")
    spec = importlib.util.spec_from_file_location("fms_revision_v2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_sqlite_triggers(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_recurring_tables() -> None:
    op.create_table(
        "recurring_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("frequency", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("next_run_date", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date <= end_date",
            name=op.f("ck_recurring_templates_dates_ordered"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_recurring_templates_name")),
    )
    op.create_table(
        "recurring_template_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("memo", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0", name=op.f("ck_recurring_template_lines_non_negative")
        ),
        sa.CheckConstraint(
            "ROUND(debit, 2) = debit AND ROUND(credit, 2) = credit",
            name=op.f("ck_recurring_template_lines_whole_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_recurring_template_lines_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["recurring_templates.id"],
            name=op.f("fk_recurring_template_lines_template_id_recurring_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_template_lines")),
    )


ENTRY_COLUMNS = [
    sa.Column("entry_type", sa.String(length=20), server_default="standard", nullable=False),
    sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    sa.Column("recurring_template_id", sa.Integer(), nullable=True),
    sa.Column("scheduled_for", sa.Date(), nullable=True),
]
# conv() marks the names as final (same as op.f(), but usable at import time).
FK_TEMPLATE = conv("fk_journal_entries_recurring_template_id_recurring_templates")
UQ_SLOT = conv("uq_journal_entries_recurring_slot")


def upgrade() -> None:
    dialect = _dialect()
    _create_recurring_tables()

    if dialect == "postgresql":
        for column in ENTRY_COLUMNS:
            op.add_column("journal_entries", column)
        op.create_foreign_key(
            FK_TEMPLATE,
            "journal_entries",
            "recurring_templates",
            ["recurring_template_id"],
            ["id"],
        )
        op.create_unique_constraint(
            UQ_SLOT, "journal_entries", ["recurring_template_id", "scheduled_for"]
        )
        op.add_column(
            "accounting_periods",
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute("DROP TRIGGER trg_accounting_periods_guard_overlap ON accounting_periods")
        op.execute("DROP FUNCTION fms_accounting_periods_guard_overlap()")
        for ddl in PG_FUNCTIONS.values():
            op.execute(ddl)
        op.execute(PG_TRIGGERS["trg_accounting_periods_guard"])
        return

    _drop_sqlite_triggers(_previous_revision().SQLITE_TRIGGERS)
    with op.batch_alter_table("journal_entries") as batch:
        for column in ENTRY_COLUMNS:
            batch.add_column(column)
        batch.create_foreign_key(
            FK_TEMPLATE, "recurring_templates", ["recurring_template_id"], ["id"]
        )
        batch.create_unique_constraint(UQ_SLOT, ["recurring_template_id", "scheduled_for"])
    op.add_column(
        "accounting_periods", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True)
    )
    for ddl in SQLITE_TRIGGERS.values():
        op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()
    previous = _previous_revision()

    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_accounting_periods_guard ON accounting_periods")
        op.execute("DROP FUNCTION fms_accounting_periods_guard()")
        op.drop_column("accounting_periods", "locked_at")
        op.drop_constraint(UQ_SLOT, "journal_entries", type_="unique")
        op.drop_constraint(FK_TEMPLATE, "journal_entries", type_="foreignkey")
        for column in ENTRY_COLUMNS:
            op.drop_column("journal_entries", column.name)
        op.drop_table("recurring_template_lines")
        op.drop_table("recurring_templates")
        for ddl in previous.PG_FUNCTIONS.values():
            op.execute(ddl)
        op.execute(previous.PG_TRIGGERS["trg_accounting_periods_guard_overlap"])
        return

    _drop_sqlite_triggers(SQLITE_TRIGGERS)
    with op.batch_alter_table("accounting_periods") as batch:
        batch.drop_column("locked_at")
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_constraint(UQ_SLOT, type_="unique")
        batch.drop_constraint(FK_TEMPLATE, type_="foreignkey")
        for column in ENTRY_COLUMNS:
            batch.drop_column(column.name)
    op.drop_table("recurring_template_lines")
    op.drop_table("recurring_templates")
    for ddl in previous.SQLITE_TRIGGERS.values():
        op.execute(ddl)
