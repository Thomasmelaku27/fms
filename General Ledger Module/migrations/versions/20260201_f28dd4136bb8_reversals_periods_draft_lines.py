"""Reversals, accounting periods, relaxed draft lines.

Schema changes:

* ``journal_lines``: drop ``ck_journal_lines_not_both_sides``. A draft may now
  hold a line with both sides filled in while it is being prepared; such lines
  are rejected when the entry is posted instead (service + trigger).
* ``journal_entries.reverses_entry_id``: link from a reversal to the posted
  entry it cancels. Unique, so an entry can be reversed at most once.
* ``accounting_periods``: date ranges that can be closed.

Ledger rules (triggers, v2 -- replaces the v1 set from the initial revision):

* lines of a posted entry cannot be inserted, updated or deleted (unchanged);
* a posted entry row cannot be updated or deleted (unchanged);
* posting requires: no line without an amount, no line with both sides, at
  least two lines, debits equal credits, entry date not in a closed period;
* a reversal may only reference a posted entry;
* accounting periods never overlap.

SQLite note: the schema changes need batch mode (table copy), and SQLite refuses
to recreate a table while triggers elsewhere reference it, so every v1 trigger
is dropped first and the full v2 set is installed afterwards.

Revision ID: f28dd4136bb8
Revises: 61ade0be8ecb
Create Date: 2026-02-01 00:00:00

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "f28dd4136bb8"
down_revision: str | None = "61ade0be8ecb"
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

    -- Posted entries are immutable: no column may change, including status.
    IF TG_OP = 'UPDATE' AND OLD.status = 'posted' THEN
        RAISE EXCEPTION '{MSG_POSTED_ENTRY_IMMUTABLE}' {ERRCODE};
    END IF;

    -- A reversal can only point at a posted entry.
    IF NEW.reverses_entry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM journal_entries WHERE id = NEW.reverses_entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{MSG_REVERSE_ONLY_POSTED}' {ERRCODE};
    END IF;

    -- Transition into "posted" (including inserting directly as posted, which
    -- can never balance because the entry has no lines yet).
    IF NEW.status = 'posted' AND (TG_OP = 'INSERT' OR OLD.status <> 'posted') THEN
        IF EXISTS (
            SELECT 1 FROM accounting_periods
             WHERE status = 'closed' AND NEW.entry_date BETWEEN start_date AND end_date
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
    "fms_accounting_periods_guard_overlap": f"""
CREATE OR REPLACE FUNCTION fms_accounting_periods_guard_overlap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
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
    "trg_accounting_periods_guard_overlap": """
CREATE TRIGGER trg_accounting_periods_guard_overlap
BEFORE INSERT OR UPDATE ON accounting_periods
FOR EACH ROW EXECUTE FUNCTION fms_accounting_periods_guard_overlap()
""",
}

# --- SQLite ---------------------------------------------------------------------
# Amounts are REAL in SQLite; ck_journal_lines_whole_cents guarantees whole cents,
# so the balance check sums exact integer cents (see the initial revision).

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
     WHERE status = 'closed' AND NEW.entry_date BETWEEN start_date AND end_date
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
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for ledger triggers: {name}")
    return name


def _initial_revision():
    """The v1 trigger DDL lives in the initial revision; load it for downgrade."""
    path = Path(__file__).with_name("20260101_61ade0be8ecb_initial_schema.py")
    spec = importlib.util.spec_from_file_location("fms_initial_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_sqlite_triggers(names: list[str]) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_periods_table() -> None:
    op.create_table(
        "accounting_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "start_date <= end_date", name=op.f("ck_accounting_periods_dates_ordered")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_periods")),
        sa.UniqueConstraint("name", name=op.f("uq_accounting_periods_name")),
    )


def upgrade() -> None:
    dialect = _dialect()
    _create_periods_table()

    if dialect == "postgresql":
        op.drop_constraint(op.f("ck_journal_lines_not_both_sides"), "journal_lines", type_="check")
        op.add_column(
            "journal_entries", sa.Column("reverses_entry_id", sa.Integer(), nullable=True)
        )
        op.create_foreign_key(
            op.f("fk_journal_entries_reverses_entry_id_journal_entries"),
            "journal_entries",
            "journal_entries",
            ["reverses_entry_id"],
            ["id"],
        )
        op.create_unique_constraint(
            op.f("uq_journal_entries_reverses_entry_id"), "journal_entries", ["reverses_entry_id"]
        )
        for ddl in PG_FUNCTIONS.values():  # CREATE OR REPLACE; existing triggers keep working
            op.execute(ddl)
        op.execute(PG_TRIGGERS["trg_accounting_periods_guard_overlap"])
        return

    # SQLite: batch mode copies tables; no trigger may reference them meanwhile.
    _drop_sqlite_triggers(list(_initial_revision().SQLITE_TRIGGERS))
    with op.batch_alter_table("journal_lines") as batch:
        batch.drop_constraint(op.f("ck_journal_lines_not_both_sides"), type_="check")
    with op.batch_alter_table("journal_entries") as batch:
        batch.add_column(sa.Column("reverses_entry_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_journal_entries_reverses_entry_id_journal_entries"),
            "journal_entries",
            ["reverses_entry_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            op.f("uq_journal_entries_reverses_entry_id"), ["reverses_entry_id"]
        )
    for ddl in SQLITE_TRIGGERS.values():
        op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()
    initial = _initial_revision()

    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_accounting_periods_guard_overlap ON accounting_periods")
        op.execute("DROP FUNCTION fms_accounting_periods_guard_overlap()")
        op.drop_constraint(
            op.f("uq_journal_entries_reverses_entry_id"), "journal_entries", type_="unique"
        )
        op.drop_constraint(
            op.f("fk_journal_entries_reverses_entry_id_journal_entries"),
            "journal_entries",
            type_="foreignkey",
        )
        op.drop_column("journal_entries", "reverses_entry_id")
        op.create_check_constraint(
            op.f("ck_journal_lines_not_both_sides"), "journal_lines", "debit = 0 OR credit = 0"
        )
        op.drop_table("accounting_periods")
        for ddl in initial.PG_FUNCTIONS.values():  # v1 bodies (CREATE FUNCTION -> OR REPLACE)
            op.execute(ddl.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
        return

    _drop_sqlite_triggers(list(SQLITE_TRIGGERS))
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_constraint(op.f("uq_journal_entries_reverses_entry_id"), type_="unique")
        batch.drop_constraint(
            op.f("fk_journal_entries_reverses_entry_id_journal_entries"), type_="foreignkey"
        )
        batch.drop_column("reverses_entry_id")
    with op.batch_alter_table("journal_lines") as batch:
        batch.create_check_constraint(
            op.f("ck_journal_lines_not_both_sides"), "debit = 0 OR credit = 0"
        )
    op.drop_table("accounting_periods")
    for ddl in initial.SQLITE_TRIGGERS.values():
        op.execute(ddl)
