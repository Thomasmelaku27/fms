"""Cash management: bank statements, reconciliation, planned cash flows.

Adds to the cash module:

* ``cash_accounts.minimum_balance`` - configurable liquidity alert threshold;
* ``bank_statements`` / ``bank_transactions`` - imported bank evidence. A
  statement is unique per (cash account, reference) and each transaction has a
  unique fingerprint per cash account, so repeated or overlapping imports can
  never create duplicates;
* ``reconciliation_sessions`` / ``reconciliation_matches`` - reconciliation of
  bank transactions against *posted* journal lines, possibly partially;
* ``cash_flow_classifications`` - operating/investing/financing per account;
* ``planned_cash_flows`` - forecast items for the liquidity projection.

Like the ledger rules of the initial migration, the reconciliation rules are
enforced in the database as well as in the application, on both backends:

* a match can only reference a line of a *posted* journal entry
  (drafts are never counted as financial transactions);
* the line must belong to the ledger account of the bank transaction's cash
  account and have the right direction (bank inflow <-> debit, outflow <-> credit);
* the matches of a bank transaction never exceed its amount, and the matches of
  a journal line never exceed the line amount (partial matches are allowed);
* matches can only be inserted, changed or deleted while their session is open;
* a bank transaction with matches cannot be deleted or have its amount changed.

Revision ID: cashmgmt01
Revises: fmsmodules01
Create Date: 2026-11-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cashmgmt01"
down_revision: str | None = "fmsmodules01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Messages raised by the triggers (identical on both backends).
MSG_SESSION_NOT_OPEN = "reconciliation session is not open"
MSG_LINE_NOT_POSTED = "only lines of posted journal entries can be reconciled"
MSG_WRONG_ACCOUNT = "journal line is not on the ledger account of the cash account of the bank transaction"
MSG_DIRECTION = "bank inflows match debits and bank outflows match credits of the cash ledger account"
MSG_BANK_OVER = "matches exceed the bank transaction amount"
MSG_LINE_OVER = "matches exceed the journal line amount"
MSG_BANK_TXN_MATCHED = "a bank transaction with reconciliation matches cannot be deleted or changed"


# --- PostgreSQL ------------------------------------------------------------------

PG_FUNCTIONS = {
    "fms_reconciliation_matches_guard": f"""
CREATE FUNCTION fms_reconciliation_matches_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    sess_status varchar;
    bank_amount numeric;
    ledger_account integer;
    line_debit numeric;
    line_credit numeric;
    line_account integer;
    entry_status varchar;
    bank_matched numeric;
    line_matched numeric;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT status INTO sess_status FROM reconciliation_sessions WHERE id = OLD.session_id;
        IF sess_status IS DISTINCT FROM 'open' THEN
            RAISE EXCEPTION '{MSG_SESSION_NOT_OPEN}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
    END IF;

    SELECT status INTO sess_status FROM reconciliation_sessions WHERE id = NEW.session_id;
    IF sess_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION '{MSG_SESSION_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT bt.amount, ca.ledger_account_id INTO bank_amount, ledger_account
      FROM bank_transactions bt JOIN cash_accounts ca ON ca.id = bt.cash_account_id
     WHERE bt.id = NEW.bank_transaction_id;
    SELECT jl.debit, jl.credit, jl.account_id, je.status
      INTO line_debit, line_credit, line_account, entry_status
      FROM journal_lines jl JOIN journal_entries je ON je.id = jl.entry_id
     WHERE jl.id = NEW.journal_line_id;

    IF entry_status IS DISTINCT FROM 'posted' THEN
        RAISE EXCEPTION '{MSG_LINE_NOT_POSTED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF line_account IS DISTINCT FROM ledger_account THEN
        RAISE EXCEPTION '{MSG_WRONG_ACCOUNT}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (bank_amount > 0 AND line_debit <= 0) OR (bank_amount < 0 AND line_credit <= 0) THEN
        RAISE EXCEPTION '{MSG_DIRECTION}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT COALESCE(SUM(amount), 0) INTO bank_matched
      FROM reconciliation_matches
     WHERE bank_transaction_id = NEW.bank_transaction_id
       AND (TG_OP = 'INSERT' OR id <> OLD.id);
    IF bank_matched + NEW.amount > ABS(bank_amount) THEN
        RAISE EXCEPTION '{MSG_BANK_OVER}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT COALESCE(SUM(amount), 0) INTO line_matched
      FROM reconciliation_matches
     WHERE journal_line_id = NEW.journal_line_id
       AND (TG_OP = 'INSERT' OR id <> OLD.id);
    IF line_matched + NEW.amount > line_debit + line_credit THEN
        RAISE EXCEPTION '{MSG_LINE_OVER}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_bank_transactions_guard_matched": f"""
CREATE FUNCTION fms_bank_transactions_guard_matched() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.amount <> OLD.amount OR NEW.cash_account_id <> OLD.cash_account_id THEN
        IF EXISTS (SELECT 1 FROM reconciliation_matches WHERE bank_transaction_id = OLD.id) THEN
            RAISE EXCEPTION '{MSG_BANK_TXN_MATCHED}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS = {
    "trg_reconciliation_matches_guard": """
CREATE TRIGGER trg_reconciliation_matches_guard
BEFORE INSERT OR UPDATE OR DELETE ON reconciliation_matches
FOR EACH ROW EXECUTE FUNCTION fms_reconciliation_matches_guard()
""",
    "trg_bank_transactions_guard_matched": """
CREATE TRIGGER trg_bank_transactions_guard_matched
BEFORE UPDATE OR DELETE ON bank_transactions
FOR EACH ROW EXECUTE FUNCTION fms_bank_transactions_guard_matched()
""",
}


# --- SQLite ----------------------------------------------------------------------
# One trigger per event and rule. Amount sums use integer cents (see the initial
# migration): every stored amount is whole cents, so the comparison is exact.

SESSION_OPEN = "EXISTS (SELECT 1 FROM reconciliation_sessions WHERE id = {ref}.session_id AND status = 'open')"
LINE_POSTED = (
    "EXISTS (SELECT 1 FROM journal_lines jl JOIN journal_entries je ON je.id = jl.entry_id "
    "WHERE jl.id = {ref}.journal_line_id AND je.status = 'posted')"
)
SAME_ACCOUNT = (
    "EXISTS (SELECT 1 FROM journal_lines jl, bank_transactions bt, cash_accounts ca "
    "WHERE jl.id = {ref}.journal_line_id AND bt.id = {ref}.bank_transaction_id "
    "AND ca.id = bt.cash_account_id AND jl.account_id = ca.ledger_account_id)"
)
DIRECTION_OK = (
    "EXISTS (SELECT 1 FROM journal_lines jl, bank_transactions bt "
    "WHERE jl.id = {ref}.journal_line_id AND bt.id = {ref}.bank_transaction_id "
    "AND ((bt.amount > 0 AND jl.debit > 0) OR (bt.amount < 0 AND jl.credit > 0)))"
)
CENTS = "CAST(ROUND(({expr}) * 100) AS INTEGER)"
BANK_OVER = (
    "(SELECT COALESCE(SUM(" + CENTS.format(expr="amount") + "), 0) FROM reconciliation_matches "
    "WHERE bank_transaction_id = NEW.bank_transaction_id{exclude}) + "
    + CENTS.format(expr="NEW.amount")
    + " > (SELECT " + CENTS.format(expr="ABS(amount)") + " FROM bank_transactions WHERE id = NEW.bank_transaction_id)"
)
LINE_OVER = (
    "(SELECT COALESCE(SUM(" + CENTS.format(expr="amount") + "), 0) FROM reconciliation_matches "
    "WHERE journal_line_id = NEW.journal_line_id{exclude}) + "
    + CENTS.format(expr="NEW.amount")
    + " > (SELECT " + CENTS.format(expr="debit + credit") + " FROM journal_lines WHERE id = NEW.journal_line_id)"
)
HAS_MATCHES = "EXISTS (SELECT 1 FROM reconciliation_matches WHERE bank_transaction_id = OLD.id)"


def _sqlite_trigger(name: str, event: str, table: str, when: str, message: str) -> str:
    return f"""
CREATE TRIGGER {name}
BEFORE {event} ON {table} FOR EACH ROW
WHEN {when}
BEGIN
    SELECT RAISE(ABORT, '{message}');
END
"""


SQLITE_TRIGGERS = {
    # session must be open for every change to matches
    "trg_recon_matches_insert_session_open": _sqlite_trigger(
        "trg_recon_matches_insert_session_open", "INSERT", "reconciliation_matches",
        f"NOT {SESSION_OPEN.format(ref='NEW')}", MSG_SESSION_NOT_OPEN,
    ),
    "trg_recon_matches_update_session_open": _sqlite_trigger(
        "trg_recon_matches_update_session_open", "UPDATE", "reconciliation_matches",
        f"NOT {SESSION_OPEN.format(ref='OLD')} OR NOT {SESSION_OPEN.format(ref='NEW')}",
        MSG_SESSION_NOT_OPEN,
    ),
    "trg_recon_matches_delete_session_open": _sqlite_trigger(
        "trg_recon_matches_delete_session_open", "DELETE", "reconciliation_matches",
        f"NOT {SESSION_OPEN.format(ref='OLD')}", MSG_SESSION_NOT_OPEN,
    ),
    # only posted lines
    "trg_recon_matches_insert_posted": _sqlite_trigger(
        "trg_recon_matches_insert_posted", "INSERT", "reconciliation_matches",
        f"NOT {LINE_POSTED.format(ref='NEW')}", MSG_LINE_NOT_POSTED,
    ),
    "trg_recon_matches_update_posted": _sqlite_trigger(
        "trg_recon_matches_update_posted", "UPDATE", "reconciliation_matches",
        f"NOT {LINE_POSTED.format(ref='NEW')}", MSG_LINE_NOT_POSTED,
    ),
    # same ledger account
    "trg_recon_matches_insert_account": _sqlite_trigger(
        "trg_recon_matches_insert_account", "INSERT", "reconciliation_matches",
        f"NOT {SAME_ACCOUNT.format(ref='NEW')}", MSG_WRONG_ACCOUNT,
    ),
    "trg_recon_matches_update_account": _sqlite_trigger(
        "trg_recon_matches_update_account", "UPDATE", "reconciliation_matches",
        f"NOT {SAME_ACCOUNT.format(ref='NEW')}", MSG_WRONG_ACCOUNT,
    ),
    # direction
    "trg_recon_matches_insert_direction": _sqlite_trigger(
        "trg_recon_matches_insert_direction", "INSERT", "reconciliation_matches",
        f"NOT {DIRECTION_OK.format(ref='NEW')}", MSG_DIRECTION,
    ),
    "trg_recon_matches_update_direction": _sqlite_trigger(
        "trg_recon_matches_update_direction", "UPDATE", "reconciliation_matches",
        f"NOT {DIRECTION_OK.format(ref='NEW')}", MSG_DIRECTION,
    ),
    # never more than the bank transaction / journal line amount
    "trg_recon_matches_insert_bank_over": _sqlite_trigger(
        "trg_recon_matches_insert_bank_over", "INSERT", "reconciliation_matches",
        BANK_OVER.format(exclude=""), MSG_BANK_OVER,
    ),
    "trg_recon_matches_update_bank_over": _sqlite_trigger(
        "trg_recon_matches_update_bank_over", "UPDATE", "reconciliation_matches",
        BANK_OVER.format(exclude=" AND id <> OLD.id"), MSG_BANK_OVER,
    ),
    "trg_recon_matches_insert_line_over": _sqlite_trigger(
        "trg_recon_matches_insert_line_over", "INSERT", "reconciliation_matches",
        LINE_OVER.format(exclude=""), MSG_LINE_OVER,
    ),
    "trg_recon_matches_update_line_over": _sqlite_trigger(
        "trg_recon_matches_update_line_over", "UPDATE", "reconciliation_matches",
        LINE_OVER.format(exclude=" AND id <> OLD.id"), MSG_LINE_OVER,
    ),
    # matched bank transactions are frozen
    "trg_bank_transactions_no_delete_matched": _sqlite_trigger(
        "trg_bank_transactions_no_delete_matched", "DELETE", "bank_transactions",
        HAS_MATCHES, MSG_BANK_TXN_MATCHED,
    ),
    "trg_bank_transactions_no_update_matched": _sqlite_trigger(
        "trg_bank_transactions_no_update_matched", "UPDATE", "bank_transactions",
        f"(NEW.amount <> OLD.amount OR NEW.cash_account_id <> OLD.cash_account_id) AND {HAS_MATCHES}",
        MSG_BANK_TXN_MATCHED,
    ),
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for cash triggers: {name}")
    return name


def upgrade() -> None:
    op.add_column(
        "cash_accounts", sa.Column("minimum_balance", sa.Numeric(precision=18, scale=2), nullable=True)
    )

    op.create_table(
        "bank_statements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("statement_reference", sa.String(length=100), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("opening_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("closing_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("period_start <= period_end", name=op.f("ck_bank_statements_period_order")),
        sa.CheckConstraint(
            "ROUND(opening_balance, 2) = opening_balance AND ROUND(closing_balance, 2) = closing_balance",
            name=op.f("ck_bank_statements_whole_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["cash_accounts.id"],
            name=op.f("fk_bank_statements_cash_account_id_cash_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bank_statements")),
        sa.UniqueConstraint(
            "cash_account_id", "statement_reference", name="uq_bank_statements_cash_account_reference"
        ),
    )

    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_bank_transactions_non_zero")),
        sa.CheckConstraint("ROUND(amount, 2) = amount", name=op.f("ck_bank_transactions_whole_cents")),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["cash_accounts.id"],
            name=op.f("fk_bank_transactions_cash_account_id_cash_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["bank_statements.id"],
            name=op.f("fk_bank_transactions_statement_id_bank_statements"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bank_transactions")),
        sa.UniqueConstraint(
            "cash_account_id", "fingerprint", name="uq_bank_transactions_cash_account_fingerprint"
        ),
        sa.UniqueConstraint("statement_id", "line_no", name="uq_bank_transactions_statement_line"),
    )
    op.create_index(
        "ix_bank_transactions_cash_account_id_transaction_date",
        "bank_transactions",
        ["cash_account_id", "transaction_date"],
    )

    op.create_table(
        "reconciliation_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("opening_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("closing_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopen_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "period_start <= period_end", name=op.f("ck_reconciliation_sessions_period_order")
        ),
        sa.CheckConstraint(
            "ROUND(opening_balance, 2) = opening_balance AND ROUND(closing_balance, 2) = closing_balance",
            name=op.f("ck_reconciliation_sessions_whole_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["cash_accounts.id"],
            name=op.f("fk_reconciliation_sessions_cash_account_id_cash_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["bank_statements.id"],
            name=op.f("fk_reconciliation_sessions_statement_id_bank_statements"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_sessions")),
        sa.UniqueConstraint(
            "cash_account_id",
            "period_start",
            "period_end",
            name="uq_reconciliation_sessions_cash_account_period",
        ),
    )

    op.create_table(
        "reconciliation_matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("bank_transaction_id", sa.Integer(), nullable=False),
        sa.Column("journal_line_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_reconciliation_matches_positive")),
        sa.CheckConstraint(
            "ROUND(amount, 2) = amount", name=op.f("ck_reconciliation_matches_whole_cents")
        ),
        sa.ForeignKeyConstraint(
            ["bank_transaction_id"],
            ["bank_transactions.id"],
            name=op.f("fk_reconciliation_matches_bank_transaction_id_bank_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_line_id"],
            ["journal_lines.id"],
            name=op.f("fk_reconciliation_matches_journal_line_id_journal_lines"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["reconciliation_sessions.id"],
            name=op.f("fk_reconciliation_matches_session_id_reconciliation_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_matches")),
        sa.UniqueConstraint(
            "bank_transaction_id",
            "journal_line_id",
            name="uq_reconciliation_matches_bank_transaction_journal_line",
        ),
    )
    op.create_index(
        "ix_reconciliation_matches_journal_line_id", "reconciliation_matches", ["journal_line_id"]
    )

    op.create_table(
        "cash_flow_classifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("activity", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_cash_flow_classifications_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_flow_classifications")),
        sa.UniqueConstraint("account_id", name=op.f("uq_cash_flow_classifications_account_id")),
    )

    op.create_table(
        "planned_cash_flows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("expected_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("activity", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("external_reference", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_planned_cash_flows_non_zero")),
        sa.CheckConstraint("ROUND(amount, 2) = amount", name=op.f("ck_planned_cash_flows_whole_cents")),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["cash_accounts.id"],
            name=op.f("fk_planned_cash_flows_cash_account_id_cash_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_planned_cash_flows_journal_entry_id_journal_entries"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planned_cash_flows")),
        sa.UniqueConstraint(
            "cash_account_id",
            "source",
            "external_reference",
            name="uq_planned_cash_flows_cash_account_source_reference",
        ),
    )
    op.create_index(
        "ix_planned_cash_flows_cash_account_id_expected_date",
        "planned_cash_flows",
        ["cash_account_id", "expected_date"],
    )

    if _dialect() == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    # Triggers go with their tables; PostgreSQL functions are dropped explicitly.
    op.drop_index("ix_planned_cash_flows_cash_account_id_expected_date", table_name="planned_cash_flows")
    op.drop_table("planned_cash_flows")
    op.drop_table("cash_flow_classifications")
    op.drop_index("ix_reconciliation_matches_journal_line_id", table_name="reconciliation_matches")
    op.drop_table("reconciliation_matches")
    op.drop_table("reconciliation_sessions")
    op.drop_index(
        "ix_bank_transactions_cash_account_id_transaction_date", table_name="bank_transactions"
    )
    op.drop_table("bank_transactions")
    op.drop_table("bank_statements")
    if _dialect() == "postgresql":
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
    with op.batch_alter_table("cash_accounts") as batch:
        batch.drop_column("minimum_balance")
