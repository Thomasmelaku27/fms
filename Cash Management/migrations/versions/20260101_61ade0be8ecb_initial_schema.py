"""Initial schema: chart of accounts, journal entries/lines, ledger rules.

Besides the tables, this migration installs the core double-entry rules in the
database itself so that neither application bugs nor manual SQL can create
unbalanced posted entries or alter posted history:

* CHECK constraints on ``journal_lines``: amounts are non-negative whole cents
  and a line never carries both a debit and a credit.
* Triggers on ``journal_lines``: rows belonging to a posted entry cannot be
  inserted, updated or deleted.
* Triggers on ``journal_entries``: an entry can only become ``posted`` when
  every line has an amount, there are at least two lines and debits equal
  credits; once posted, the entry row itself cannot be updated (any column,
  including status) or deleted.

Drafts are deliberately unconstrained beyond the per-line CHECKs: they may be
unbalanced, contain lines without an amount yet, have a single line or no lines
at all. Completeness and balance are verified only at the moment an entry
transitions to ``posted``.

The triggers are written once per supported backend (SQLite and PostgreSQL).
Both raise an integrity error with the same messages so the application can
handle them uniformly.

Revision ID: 61ade0be8ecb
Revises:
Create Date: 2026-01-01 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "61ade0be8ecb"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Error messages raised by the triggers (identical on both backends).
MSG_POSTED_LINES_IMMUTABLE = "lines of a posted journal entry cannot be modified"
MSG_POST_INCOMPLETE_LINES = "journal entry has lines without an amount and cannot be posted"
MSG_POST_MUST_BALANCE = "journal entry must have lines that balance before it can be posted"
MSG_POSTED_ENTRY_IMMUTABLE = "a posted journal entry cannot be modified or deleted"


# --- PostgreSQL -----------------------------------------------------------------
# Row-level triggers backed by PL/pgSQL functions. NUMERIC is exact, so sums are
# compared directly.

PG_FUNCTIONS = {
    "fms_journal_lines_guard_posted": f"""
CREATE FUNCTION fms_journal_lines_guard_posted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
        SELECT 1 FROM journal_entries WHERE id = OLD.entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{MSG_POSTED_LINES_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
        SELECT 1 FROM journal_entries WHERE id = NEW.entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{MSG_POSTED_LINES_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_journal_entries_guard_status": f"""
CREATE FUNCTION fms_journal_entries_guard_status() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    line_count integer;
    total_debit numeric;
    total_credit numeric;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'posted' THEN
            RAISE EXCEPTION '{MSG_POSTED_ENTRY_IMMUTABLE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- Posted entries are immutable: no column may change, including status.
    IF TG_OP = 'UPDATE' AND OLD.status = 'posted' THEN
        RAISE EXCEPTION '{MSG_POSTED_ENTRY_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- Transition into "posted" (including inserting directly as posted, which
    -- can never balance because the entry has no lines yet).
    IF NEW.status = 'posted' AND (TG_OP = 'INSERT' OR OLD.status <> 'posted') THEN
        IF EXISTS (
            SELECT 1 FROM journal_lines
             WHERE entry_id = NEW.id AND debit = 0 AND credit = 0
        ) THEN
            RAISE EXCEPTION '{MSG_POST_INCOMPLETE_LINES}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT COUNT(*), COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
          INTO line_count, total_debit, total_credit
          FROM journal_lines WHERE entry_id = NEW.id;
        IF line_count < 2 OR total_debit <> total_credit THEN
            RAISE EXCEPTION '{MSG_POST_MUST_BALANCE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
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
}


# --- SQLite ---------------------------------------------------------------------
# SQLite triggers cover a single event each and cannot call functions, so the
# same rules are spelled out per event.
#
# SQLite stores NUMERIC as REAL, so floating-point sums are not exact
# (0.10 + 0.20 <> 0.30). The balance check therefore sums *integer cents*:
# ck_journal_lines_whole_cents guarantees every stored amount is a whole number
# of cents, so CAST(ROUND(x * 100) AS INTEGER) is an exact conversion and the
# integer SUMs are compared exactly -- no tolerance is applied to the totals.

SQLITE_POSTED_ENTRY = (
    "SELECT 1 FROM journal_entries WHERE id = {ref}.entry_id AND status = 'posted'"
)

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
    "trg_journal_entries_post_requires_complete_lines": f"""
CREATE TRIGGER trg_journal_entries_post_requires_complete_lines
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN NEW.status = 'posted' AND OLD.status <> 'posted' AND EXISTS (
    SELECT 1 FROM journal_lines WHERE entry_id = NEW.id AND debit = 0 AND credit = 0
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_POST_INCOMPLETE_LINES}');
END
""",
    "trg_journal_entries_post_must_balance": f"""
CREATE TRIGGER trg_journal_entries_post_must_balance
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN NEW.status = 'posted' AND OLD.status <> 'posted' AND (
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
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for ledger triggers: {name}")
    return name


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index(op.f("ix_accounts_code"), "accounts", ["code"], unique=True)

    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entries")),
    )

    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("memo", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "debit >= 0 AND credit >= 0", name=op.f("ck_journal_lines_non_negative")
        ),
        sa.CheckConstraint(
            "debit = 0 OR credit = 0", name=op.f("ck_journal_lines_not_both_sides")
        ),
        sa.CheckConstraint(
            "ROUND(debit, 2) = debit AND ROUND(credit, 2) = credit",
            name=op.f("ck_journal_lines_whole_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_journal_lines_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_journal_lines_entry_id_journal_entries"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_lines")),
    )

    if _dialect() == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    # Triggers are dropped together with their tables on both backends;
    # PostgreSQL functions are separate objects and must be dropped explicitly.
    op.drop_table("journal_lines")
    op.drop_table("journal_entries")
    op.drop_index(op.f("ix_accounts_code"), table_name="accounts")
    op.drop_table("accounts")
    if _dialect() == "postgresql":
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
