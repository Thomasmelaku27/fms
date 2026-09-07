"""Posting metadata, closing attestations, reclassification records, hierarchy
integrity, trigger-maintained balance aggregates.

Schema changes:

* ``journal_entries``: ``posted_at``, ``posted_by``, ``posting_source``,
  ``correlation_id`` with a CHECK that they exist exactly when the entry is
  posted. Existing posted entries are backfilled (``posted_by = 'legacy'``,
  ``posting_source = 'legacy'``, ``posted_at = created_at``, generated
  correlation ids).
* ``fiscal_year_closings``: immutable attestation of each year-end close.
  Years already closed get a backfilled record (``ledger_hash =
  'legacy-backfill'``, ``actor_ref = 'migration'``).
* ``account_reclassifications``: effective-dated log of hierarchy/category
  changes of accounts with history.
* ``accounts``: CHECK that ``category`` belongs to ``type``.
* ``account_daily_totals``: posted debit/credit cents per account and day,
  maintained by trigger and backfilled from posted lines.
* ``balance_verification_runs``: log of aggregate verifications / rebuilds.
* indexes for reporting: ``journal_lines(account_id)``, ``journal_lines(entry_id)``,
  ``journal_entries(status, entry_date, id)``.

Ledger rules (triggers, v6 -- replaces the v5 set):

* unchanged rules from v5;
* the account hierarchy can never contain a cycle (recursive check);
* an account with journal lines can change ``parent_id`` / ``category`` only
  together with a reclassification record for exactly that change;
  ``normal_balance`` cannot change once the account has journal lines;
  a summary account with children keeps its role and type;
* a ``closing`` journal entry can only be posted while a ``fiscal_year_closings``
  record for its fiscal year is ``in_progress`` and names that entry - the
  only way into a closed period; a fiscal year can only become closed with
  such a record; completed closing records are immutable;
* posting an entry adds its lines to ``account_daily_totals`` in the same
  transaction (AFTER trigger).

Downgrade note: the downgrade keeps every journal entry and line but drops the
posting metadata columns, the closing attestations, the reclassification log,
the daily totals and the verification log. It is therefore safe for
accounting data but **irreversible for traceability data**; take a backup
before downgrading a production database.

Revision ID: a99cf5d9c85d
Revises: 78f70521def3
Create Date: 2026-06-01 00:00:00

"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import conv

revision: str = "a99cf5d9c85d"
down_revision: str | None = "78f70521def3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _previous_revision():
    path = Path(__file__).with_name("20260501_78f70521def3_fiscal_years.py")
    spec = importlib.util.spec_from_file_location("fms_revision_v5", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_v5 = _previous_revision()

MSG_CYCLE = "the account hierarchy cannot contain a cycle"
MSG_RECLASS_RECORD = (
    "an account with journal lines can only change parent or category through an "
    "effective-dated reclassification record"
)
MSG_NORMAL_BALANCE = "the normal balance of an account with journal lines cannot change"
MSG_SUMMARY_CHILDREN = "a summary account with child accounts keeps its role and type"
MSG_CLOSING_AUTH = (
    "a closing journal entry can only be posted by an in-progress fiscal year closing"
)
MSG_CLOSING_IMMUTABLE = "a completed fiscal year closing record cannot be modified or deleted"
MSG_YEAR_NEEDS_CLOSING = "a fiscal year can only be closed through a fiscal year closing record"

ERRCODE = _v5.ERRCODE

# --- PostgreSQL -----------------------------------------------------------------

PG_FUNCTIONS = dict(_v5.PG_FUNCTIONS)

PG_FUNCTIONS["fms_accounts_guard"] = f"""
CREATE OR REPLACE FUNCTION fms_accounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    has_lines boolean;
BEGIN
    SELECT EXISTS (SELECT 1 FROM journal_lines WHERE account_id = NEW.id) INTO has_lines;
    IF TG_OP = 'UPDATE' AND has_lines THEN
        IF NEW.type <> OLD.type OR (NEW.is_summary AND NOT OLD.is_summary) THEN
            RAISE EXCEPTION '{_v5.MSG_ACCOUNT_HAS_HISTORY}' {ERRCODE};
        END IF;
        IF NEW.normal_balance <> OLD.normal_balance THEN
            RAISE EXCEPTION '{MSG_NORMAL_BALANCE}' {ERRCODE};
        END IF;
        IF (NEW.parent_id IS DISTINCT FROM OLD.parent_id
            OR NEW.category IS DISTINCT FROM OLD.category)
           AND NOT EXISTS (
               SELECT 1 FROM account_reclassifications r
                WHERE r.account_id = NEW.id
                  AND r.new_parent_id IS NOT DISTINCT FROM NEW.parent_id
                  AND r.new_category IS NOT DISTINCT FROM NEW.category
                  AND r.old_parent_id IS NOT DISTINCT FROM OLD.parent_id
                  AND r.old_category IS NOT DISTINCT FROM OLD.category
           ) THEN
            RAISE EXCEPTION '{MSG_RECLASS_RECORD}' {ERRCODE};
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.type <> OLD.type OR (OLD.is_summary AND NOT NEW.is_summary))
       AND EXISTS (SELECT 1 FROM accounts c WHERE c.parent_id = OLD.id) THEN
        RAISE EXCEPTION '{MSG_SUMMARY_CHILDREN}' {ERRCODE};
    END IF;
    IF NEW.parent_id IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM accounts p
             WHERE p.id = NEW.parent_id AND p.is_summary AND p.type = NEW.type
        ) THEN
            RAISE EXCEPTION '{_v5.MSG_PARENT_INVALID}' {ERRCODE};
        END IF;
        IF EXISTS (
            WITH RECURSIVE up(id) AS (
                SELECT NEW.parent_id
                UNION ALL
                SELECT a.parent_id FROM accounts a JOIN up ON a.id = up.id
                 WHERE a.parent_id IS NOT NULL
            )
            SELECT 1 FROM up WHERE id = NEW.id
        ) THEN
            RAISE EXCEPTION '{MSG_CYCLE}' {ERRCODE};
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

PG_FUNCTIONS["fms_journal_entries_guard_status"] = f"""
CREATE OR REPLACE FUNCTION fms_journal_entries_guard_status() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    line_count integer;
    total_debit numeric;
    total_credit numeric;
    authorised boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'posted' THEN
            RAISE EXCEPTION '{_v5.MSG_POSTED_ENTRY_IMMUTABLE}' {ERRCODE};
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.status = 'posted' THEN
        RAISE EXCEPTION '{_v5.MSG_POSTED_ENTRY_IMMUTABLE}' {ERRCODE};
    END IF;

    IF NEW.reverses_entry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM journal_entries WHERE id = NEW.reverses_entry_id AND status = 'posted'
    ) THEN
        RAISE EXCEPTION '{_v5.MSG_REVERSE_ONLY_POSTED}' {ERRCODE};
    END IF;
    IF NEW.reverses_entry_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM journal_entries
         WHERE id = NEW.reverses_entry_id AND entry_type IN ('opening', 'closing')
    ) THEN
        RAISE EXCEPTION '{_v5.MSG_REVERSE_SYSTEM_ENTRY}' {ERRCODE};
    END IF;

    IF NEW.status = 'posted' AND (TG_OP = 'INSERT' OR OLD.status <> 'posted') THEN
        IF NEW.entry_type = 'closing' THEN
            SELECT EXISTS (
                SELECT 1 FROM fiscal_year_closings c
                  JOIN fiscal_years y ON y.id = c.fiscal_year_id
                 WHERE c.closing_entry_id = NEW.id AND c.status = 'in_progress'
                   AND y.status = 'open' AND NEW.entry_date = y.end_date
            ) INTO authorised;
            IF NOT authorised THEN
                RAISE EXCEPTION '{MSG_CLOSING_AUTH}' {ERRCODE};
            END IF;
        END IF;
        IF EXISTS (
            SELECT 1 FROM accounting_periods
             WHERE NEW.entry_date BETWEEN start_date AND end_date
               AND (status = 'locked' OR (status = 'closed' AND NOT authorised))
        ) THEN
            RAISE EXCEPTION '{_v5.MSG_POST_CLOSED_PERIOD}' {ERRCODE};
        END IF;
        IF EXISTS (
            SELECT 1 FROM journal_lines
             WHERE entry_id = NEW.id AND debit = 0 AND credit = 0
        ) THEN
            RAISE EXCEPTION '{_v5.MSG_POST_INCOMPLETE_LINES}' {ERRCODE};
        END IF;
        IF EXISTS (
            SELECT 1 FROM journal_lines
             WHERE entry_id = NEW.id AND debit > 0 AND credit > 0
        ) THEN
            RAISE EXCEPTION '{_v5.MSG_POST_BOTH_SIDES}' {ERRCODE};
        END IF;
        SELECT COUNT(*), COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
          INTO line_count, total_debit, total_credit
          FROM journal_lines WHERE entry_id = NEW.id;
        IF line_count < 2 OR total_debit <> total_credit THEN
            RAISE EXCEPTION '{_v5.MSG_POST_MUST_BALANCE}' {ERRCODE};
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

PG_FUNCTIONS["fms_fiscal_years_guard"] = f"""
CREATE OR REPLACE FUNCTION fms_fiscal_years_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.status = 'closed' THEN
        RAISE EXCEPTION '{_v5.MSG_YEAR_CLOSED}' {ERRCODE};
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF EXISTS (
        SELECT 1 FROM fiscal_years y
         WHERE y.id <> NEW.id AND NEW.start_date <= y.end_date AND y.start_date <= NEW.end_date
    ) THEN
        RAISE EXCEPTION '{_v5.MSG_YEARS_OVERLAP}' {ERRCODE};
    END IF;
    IF NEW.status = 'closed' THEN
        IF EXISTS (
            SELECT 1 FROM accounting_periods p
             WHERE p.fiscal_year_id = NEW.id AND p.status = 'open'
        ) THEN
            RAISE EXCEPTION '{_v5.MSG_YEAR_CLOSE_PERIODS}' {ERRCODE};
        END IF;
        IF NEW.closing_entry_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM journal_entries
             WHERE id = NEW.closing_entry_id AND status = 'posted' AND entry_type = 'closing'
        ) THEN
            RAISE EXCEPTION '{_v5.MSG_YEAR_CLOSING_ENTRY}' {ERRCODE};
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM fiscal_year_closings c
             WHERE c.fiscal_year_id = NEW.id
               AND c.closing_entry_id IS NOT DISTINCT FROM NEW.closing_entry_id
        ) THEN
            RAISE EXCEPTION '{MSG_YEAR_NEEDS_CLOSING}' {ERRCODE};
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

PG_FUNCTIONS["fms_fiscal_year_closings_guard"] = f"""
CREATE OR REPLACE FUNCTION fms_fiscal_year_closings_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.status = 'completed' THEN
        RAISE EXCEPTION '{MSG_CLOSING_IMMUTABLE}' {ERRCODE};
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
"""

PG_FUNCTIONS["fms_journal_entries_daily_totals"] = """
CREATE OR REPLACE FUNCTION fms_journal_entries_daily_totals() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO account_daily_totals (account_id, entry_date, debit_cents, credit_cents)
    SELECT account_id, NEW.entry_date,
           SUM(CAST(ROUND(debit * 100) AS BIGINT)), SUM(CAST(ROUND(credit * 100) AS BIGINT))
      FROM journal_lines WHERE entry_id = NEW.id GROUP BY account_id
    ON CONFLICT (account_id, entry_date) DO UPDATE
       SET debit_cents = account_daily_totals.debit_cents + EXCLUDED.debit_cents,
           credit_cents = account_daily_totals.credit_cents + EXCLUDED.credit_cents;
    RETURN NULL;
END
$$
"""

PG_TRIGGERS = dict(_v5.PG_TRIGGERS)
PG_TRIGGERS["trg_fiscal_year_closings_guard"] = """
CREATE TRIGGER trg_fiscal_year_closings_guard
BEFORE UPDATE OR DELETE ON fiscal_year_closings
FOR EACH ROW EXECUTE FUNCTION fms_fiscal_year_closings_guard()
"""
PG_TRIGGERS["trg_journal_entries_daily_totals"] = """
CREATE TRIGGER trg_journal_entries_daily_totals
AFTER UPDATE OF status ON journal_entries
FOR EACH ROW WHEN (NEW.status = 'posted' AND OLD.status <> 'posted')
EXECUTE FUNCTION fms_journal_entries_daily_totals()
"""

# --- SQLite ---------------------------------------------------------------------

SQLITE_TRIGGERS = dict(_v5.SQLITE_TRIGGERS)
SQLITE_POSTING = _v5.SQLITE_POSTING
CLOSING_AUTHORISED = (
    "EXISTS (SELECT 1 FROM fiscal_year_closings c JOIN fiscal_years y ON y.id = c.fiscal_year_id "
    "WHERE c.closing_entry_id = NEW.id AND c.status = 'in_progress' AND y.status = 'open' "
    "AND NEW.entry_date = y.end_date)"
)

SQLITE_TRIGGERS["trg_journal_entries_post_in_closed_period"] = f"""
CREATE TRIGGER trg_journal_entries_post_in_closed_period
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND EXISTS (
    SELECT 1 FROM accounting_periods
     WHERE NEW.entry_date BETWEEN start_date AND end_date
       AND (status = 'locked' OR (status = 'closed' AND NOT {CLOSING_AUTHORISED}))
)
BEGIN
    SELECT RAISE(ABORT, '{_v5.MSG_POST_CLOSED_PERIOD}');
END
"""
SQLITE_TRIGGERS["trg_journal_entries_closing_requires_authorisation"] = f"""
CREATE TRIGGER trg_journal_entries_closing_requires_authorisation
BEFORE UPDATE OF status ON journal_entries FOR EACH ROW
WHEN {SQLITE_POSTING} AND NEW.entry_type = 'closing' AND NOT {CLOSING_AUTHORISED}
BEGIN
    SELECT RAISE(ABORT, '{MSG_CLOSING_AUTH}');
END
"""
SQLITE_TRIGGERS["trg_journal_entries_daily_totals"] = """
CREATE TRIGGER trg_journal_entries_daily_totals
AFTER UPDATE OF status ON journal_entries FOR EACH ROW
WHEN NEW.status = 'posted' AND OLD.status <> 'posted'
BEGIN
    INSERT INTO account_daily_totals (account_id, entry_date, debit_cents, credit_cents)
    SELECT account_id, NEW.entry_date,
           SUM(CAST(ROUND(debit * 100) AS INTEGER)), SUM(CAST(ROUND(credit * 100) AS INTEGER))
      FROM journal_lines WHERE entry_id = NEW.id GROUP BY account_id
    ON CONFLICT (account_id, entry_date) DO UPDATE
       SET debit_cents = debit_cents + excluded.debit_cents,
           credit_cents = credit_cents + excluded.credit_cents;
END
"""
SQLITE_TRIGGERS["trg_fiscal_years_closing_entry"] = f"""
CREATE TRIGGER trg_fiscal_years_closing_entry
BEFORE UPDATE OF status, closing_entry_id ON fiscal_years FOR EACH ROW
WHEN NEW.status = 'closed' AND NEW.closing_entry_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM journal_entries
     WHERE id = NEW.closing_entry_id AND status = 'posted' AND entry_type = 'closing'
)
BEGIN
    SELECT RAISE(ABORT, '{_v5.MSG_YEAR_CLOSING_ENTRY}');
END
"""
SQLITE_TRIGGERS["trg_fiscal_years_close_requires_record"] = f"""
CREATE TRIGGER trg_fiscal_years_close_requires_record
BEFORE UPDATE OF status ON fiscal_years FOR EACH ROW
WHEN NEW.status = 'closed' AND NOT EXISTS (
    SELECT 1 FROM fiscal_year_closings c
     WHERE c.fiscal_year_id = NEW.id AND c.closing_entry_id IS NEW.closing_entry_id
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_YEAR_NEEDS_CLOSING}');
END
"""
SQLITE_TRIGGERS["trg_fiscal_year_closings_no_update_completed"] = f"""
CREATE TRIGGER trg_fiscal_year_closings_no_update_completed
BEFORE UPDATE ON fiscal_year_closings FOR EACH ROW
WHEN OLD.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, '{MSG_CLOSING_IMMUTABLE}');
END
"""
SQLITE_TRIGGERS["trg_fiscal_year_closings_no_delete_completed"] = f"""
CREATE TRIGGER trg_fiscal_year_closings_no_delete_completed
BEFORE DELETE ON fiscal_year_closings FOR EACH ROW
WHEN OLD.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, '{MSG_CLOSING_IMMUTABLE}');
END
"""
SQLITE_CYCLE = """EXISTS (
    WITH RECURSIVE up(id) AS (
        SELECT NEW.parent_id
        UNION ALL
        SELECT a.parent_id FROM accounts a JOIN up ON a.id = up.id WHERE a.parent_id IS NOT NULL
    )
    SELECT 1 FROM up WHERE id = NEW.id
)"""
SQLITE_TRIGGERS["trg_accounts_no_cycle_insert"] = f"""
CREATE TRIGGER trg_accounts_no_cycle_insert
BEFORE INSERT ON accounts FOR EACH ROW
WHEN NEW.parent_id IS NOT NULL AND {SQLITE_CYCLE}
BEGIN
    SELECT RAISE(ABORT, '{MSG_CYCLE}');
END
"""
SQLITE_TRIGGERS["trg_accounts_no_cycle_update"] = f"""
CREATE TRIGGER trg_accounts_no_cycle_update
BEFORE UPDATE OF parent_id ON accounts FOR EACH ROW
WHEN NEW.parent_id IS NOT NULL AND {SQLITE_CYCLE}
BEGIN
    SELECT RAISE(ABORT, '{MSG_CYCLE}');
END
"""
SQLITE_TRIGGERS["trg_accounts_history_reclassification"] = f"""
CREATE TRIGGER trg_accounts_history_reclassification
BEFORE UPDATE OF parent_id, category ON accounts FOR EACH ROW
WHEN (NEW.parent_id IS NOT OLD.parent_id OR NEW.category IS NOT OLD.category)
  AND EXISTS (SELECT 1 FROM journal_lines WHERE account_id = OLD.id)
  AND NOT EXISTS (
      SELECT 1 FROM account_reclassifications r
       WHERE r.account_id = NEW.id
         AND r.new_parent_id IS NEW.parent_id AND r.new_category IS NEW.category
         AND r.old_parent_id IS OLD.parent_id AND r.old_category IS OLD.category
  )
BEGIN
    SELECT RAISE(ABORT, '{MSG_RECLASS_RECORD}');
END
"""
SQLITE_TRIGGERS["trg_accounts_history_normal_balance"] = f"""
CREATE TRIGGER trg_accounts_history_normal_balance
BEFORE UPDATE OF normal_balance ON accounts FOR EACH ROW
WHEN NEW.normal_balance <> OLD.normal_balance
  AND EXISTS (SELECT 1 FROM journal_lines WHERE account_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, '{MSG_NORMAL_BALANCE}');
END
"""
SQLITE_TRIGGERS["trg_accounts_summary_children"] = f"""
CREATE TRIGGER trg_accounts_summary_children
BEFORE UPDATE OF type, is_summary ON accounts FOR EACH ROW
WHEN (NEW.type <> OLD.type OR (OLD.is_summary AND NOT NEW.is_summary))
  AND EXISTS (SELECT 1 FROM accounts c WHERE c.parent_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, '{MSG_SUMMARY_CHILDREN}');
END
"""


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for ledger triggers: {name}")
    return name


def _drop_sqlite_triggers(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


METADATA_COLUMNS = [
    sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("posted_by", sa.String(length=100), nullable=True),
    sa.Column("posting_source", sa.String(length=30), nullable=True),
    sa.Column("correlation_id", sa.String(length=36), nullable=True),
]
CK_POSTING_METADATA = conv("ck_journal_entries_posting_metadata")
CK_POSTING_METADATA_SQL = (
    "(status = 'draft' AND posted_at IS NULL AND posted_by IS NULL "
    "AND posting_source IS NULL AND correlation_id IS NULL) OR "
    "(status = 'posted' AND posted_at IS NOT NULL AND posted_by IS NOT NULL "
    "AND posting_source IS NOT NULL AND correlation_id IS NOT NULL)"
)
CK_CATEGORY_TYPE = conv("ck_accounts_category_type")
CATEGORY_TYPE_SQL = "category IS NULL OR " + " OR ".join(
    f"(category = '{category}' AND type = '{type_}')"
    for category, type_ in [
        ("current_asset", "asset"),
        ("fixed_asset", "asset"),
        ("other_asset", "asset"),
        ("current_liability", "liability"),
        ("long_term_liability", "liability"),
        ("equity", "equity"),
        ("retained_earnings", "equity"),
        ("operating_revenue", "revenue"),
        ("other_revenue", "revenue"),
        ("operating_expense", "expense"),
        ("other_expense", "expense"),
    ]
)


def _create_tables() -> None:
    op.create_table(
        "fiscal_year_closings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fiscal_year_id", sa.Integer(), nullable=False),
        sa.Column("closing_entry_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("covered_period_ids", sa.Text(), nullable=False),
        sa.Column("net_income", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("total_debit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("total_credit", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("ledger_hash", sa.String(length=64), nullable=False),
        sa.Column("reconciliation_ok", sa.Boolean(), nullable=False),
        sa.Column("reconciliation_summary", sa.Text(), nullable=False),
        sa.Column("actor_ref", sa.String(length=100), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["closing_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_fiscal_year_closings_closing_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["fiscal_year_id"],
            ["fiscal_years.id"],
            name=op.f("fk_fiscal_year_closings_fiscal_year_id_fiscal_years"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fiscal_year_closings")),
        sa.UniqueConstraint(
            "closing_entry_id", name=op.f("uq_fiscal_year_closings_closing_entry_id")
        ),
        sa.UniqueConstraint("fiscal_year_id", name=op.f("uq_fiscal_year_closings_fiscal_year_id")),
    )
    op.create_table(
        "account_reclassifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("old_parent_id", sa.Integer(), nullable=True),
        sa.Column("new_parent_id", sa.Integer(), nullable=True),
        sa.Column("old_category", sa.String(length=30), nullable=True),
        sa.Column("new_category", sa.String(length=30), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("actor_ref", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_account_reclassifications_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["new_parent_id"],
            ["accounts.id"],
            name=op.f("fk_account_reclassifications_new_parent_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["old_parent_id"],
            ["accounts.id"],
            name=op.f("fk_account_reclassifications_old_parent_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_reclassifications")),
    )
    op.create_table(
        "account_daily_totals",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("debit_cents", sa.BigInteger(), nullable=False),
        sa.Column("credit_cents", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_account_daily_totals_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("account_id", "entry_date", name=op.f("pk_account_daily_totals")),
    )
    op.create_table(
        "balance_verification_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("discrepancy_count", sa.Integer(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("actor_ref", sa.String(length=100), nullable=False),
        sa.Column(
            "run_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_verification_runs")),
    )


BACKFILL_TOTALS = (
    "INSERT INTO account_daily_totals (account_id, entry_date, debit_cents, credit_cents) "
    "SELECT l.account_id, e.entry_date, "
    "SUM(CAST(ROUND(l.debit * 100) AS {int})), SUM(CAST(ROUND(l.credit * 100) AS {int})) "
    "FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
    "WHERE e.status = 'posted' GROUP BY l.account_id, e.entry_date"
)
BACKFILL_CLOSINGS = (
    "INSERT INTO fiscal_year_closings (fiscal_year_id, closing_entry_id, status, "
    "covered_period_ids, net_income, total_debit, total_credit, ledger_hash, reconciliation_ok, "
    "reconciliation_summary, actor_ref, correlation_id, started_at, completed_at) "
    "SELECT id, closing_entry_id, 'completed', '[]', 0, 0, 0, 'legacy-backfill', {false}, "
    "'{{\"backfilled\": true}}', 'migration', {uuid}, closed_at, closed_at "
    "FROM fiscal_years WHERE status = 'closed'"
)


INDEXES = [
    ("ix_journal_lines_account_id", "journal_lines", ["account_id"]),
    ("ix_journal_lines_entry_id", "journal_lines", ["entry_id"]),
    ("ix_journal_entries_status_entry_date", "journal_entries", ["status", "entry_date", "id"]),
]


def upgrade() -> None:
    dialect = _dialect()
    _create_tables()
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)

    if dialect == "postgresql":
        for column in METADATA_COLUMNS:
            op.add_column("journal_entries", column)
        # Posted rows are immutable by trigger; the one-off backfill of the new columns
        # is the sole legitimate exception, so the guard is suspended for that statement.
        op.execute("ALTER TABLE journal_entries DISABLE TRIGGER trg_journal_entries_guard_status")
        op.execute(
            "UPDATE journal_entries SET posted_at = created_at, posted_by = 'legacy', "
            "posting_source = 'legacy', correlation_id = gen_random_uuid()::text "
            "WHERE status = 'posted'"
        )
        op.execute("ALTER TABLE journal_entries ENABLE TRIGGER trg_journal_entries_guard_status")
        op.create_check_constraint(CK_POSTING_METADATA, "journal_entries", CK_POSTING_METADATA_SQL)
        op.create_check_constraint(CK_CATEGORY_TYPE, "accounts", CATEGORY_TYPE_SQL)
        op.execute(BACKFILL_TOTALS.format(int="BIGINT"))
        op.execute(BACKFILL_CLOSINGS.format(false="false", uuid="gen_random_uuid()::text"))
        for ddl in PG_FUNCTIONS.values():
            op.execute(ddl)
        op.execute(PG_TRIGGERS["trg_fiscal_year_closings_guard"])
        op.execute(PG_TRIGGERS["trg_journal_entries_daily_totals"])
        return

    _drop_sqlite_triggers(_v5.SQLITE_TRIGGERS)
    with op.batch_alter_table("journal_entries") as batch:
        for column in METADATA_COLUMNS:
            batch.add_column(column)
    op.execute(
        "UPDATE journal_entries SET posted_at = created_at, posted_by = 'legacy', "
        "posting_source = 'legacy', correlation_id = lower(hex(randomblob(16))) "
        "WHERE status = 'posted'"
    )
    with op.batch_alter_table("journal_entries") as batch:
        batch.create_check_constraint(CK_POSTING_METADATA, CK_POSTING_METADATA_SQL)
    with op.batch_alter_table("accounts") as batch:
        batch.create_check_constraint(CK_CATEGORY_TYPE, CATEGORY_TYPE_SQL)
    op.execute(BACKFILL_TOTALS.format(int="INTEGER"))
    op.execute(BACKFILL_CLOSINGS.format(false="0", uuid="lower(hex(randomblob(16)))"))
    for ddl in SQLITE_TRIGGERS.values():
        op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()
    for name, table, _columns in INDEXES:
        op.drop_index(name, table_name=table)

    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_journal_entries_daily_totals ON journal_entries")
        op.execute("DROP FUNCTION fms_journal_entries_daily_totals()")
        op.execute("DROP TRIGGER trg_fiscal_year_closings_guard ON fiscal_year_closings")
        op.execute("DROP FUNCTION fms_fiscal_year_closings_guard()")
        for ddl in _v5.PG_FUNCTIONS.values():
            op.execute(ddl)
        op.drop_constraint(CK_CATEGORY_TYPE, "accounts", type_="check")
        op.drop_constraint(CK_POSTING_METADATA, "journal_entries", type_="check")
        for column in METADATA_COLUMNS:
            op.drop_column("journal_entries", column.name)
        for table in (
            "balance_verification_runs",
            "account_daily_totals",
            "account_reclassifications",
            "fiscal_year_closings",
        ):
            op.drop_table(table)
        return

    _drop_sqlite_triggers(SQLITE_TRIGGERS)
    for table in ("balance_verification_runs", "account_daily_totals", "account_reclassifications"):
        op.drop_table(table)
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint(CK_CATEGORY_TYPE, type_="check")
    with op.batch_alter_table("journal_entries") as batch:
        batch.drop_constraint(CK_POSTING_METADATA, type_="check")
        for column in METADATA_COLUMNS:
            batch.drop_column(column.name)
    op.drop_table("fiscal_year_closings")
    for ddl in _v5.SQLITE_TRIGGERS.values():
        op.execute(ddl)
