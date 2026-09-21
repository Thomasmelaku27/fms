"""Accounts Receivable: invoice lines, payments, adjustments, allocations, AR rules.

Builds on the module baseline (``fmsmodules01``):

* ``sales_invoices`` gains computed totals (``subtotal``, ``tax_total``),
  the running ``amount_settled``, links to the issuing/voiding journal
  entries and a ``void_date``. Existing draft rows keep their ``total`` and
  get ``subtotal = total``.
* New tables: ``sales_invoice_lines``, ``customer_payments``,
  ``customer_adjustments`` (credit notes and write-offs),
  ``invoice_allocations`` and the single-row ``receivable_settings`` that
  names the ledger accounts used by the module.
* CHECK constraints keep amounts non-negative, totals consistent and the
  settled/allocated running sums within their totals.
* Triggers (SQLite and PostgreSQL, same messages) protect the AR history the
  same way the ledger triggers protect posted journal entries:

  - lines of an invoice that is no longer a draft cannot change;
  - an issued invoice's amounts, customer, number, date and ledger link are
    frozen, an invoice never returns to draft and a non-draft cannot be deleted;
  - an allocation can only be added to an issued/partially paid/paid invoice,
    may not push the invoice above its total nor the payment/adjustment
    above its amount, and is append-only.

  Like the ledger triggers, the SQLite versions compare whole cents because
  SQLite stores NUMERIC as REAL.

Revision ID: fmsreceivables01
Revises: fmsmodules01
Create Date: 2026-11-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fmsreceivables01"
down_revision: str | None = "fmsmodules01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MSG_ISSUED_LINES_IMMUTABLE = "lines of an issued invoice cannot be modified"
MSG_ISSUED_INVOICE_IMMUTABLE = "an issued invoice cannot be modified or deleted"
MSG_BACK_TO_DRAFT = "an invoice cannot return to draft"
MSG_ALLOC_CLOSED_INVOICE = "cannot allocate to a draft or void invoice"
MSG_ALLOC_EXCEEDS_INVOICE = "allocation exceeds the invoice total"
MSG_ALLOC_EXCEEDS_SOURCE = "allocation exceeds the payment or adjustment amount"
MSG_ALLOC_IMMUTABLE = "invoice allocations cannot be modified or deleted"

MONEY = sa.Numeric(18, 2)
NOW = sa.func.now()


# --- PostgreSQL -----------------------------------------------------------------

PG_FUNCTIONS = {
    "fms_sales_invoice_lines_guard": f"""
CREATE FUNCTION fms_sales_invoice_lines_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
        SELECT 1 FROM sales_invoices WHERE id = OLD.invoice_id AND status <> 'draft'
    ) THEN
        RAISE EXCEPTION '{MSG_ISSUED_LINES_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
        SELECT 1 FROM sales_invoices WHERE id = NEW.invoice_id AND status <> 'draft'
    ) THEN
        RAISE EXCEPTION '{MSG_ISSUED_LINES_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_sales_invoices_guard": f"""
CREATE FUNCTION fms_sales_invoices_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_ISSUED_INVOICE_IMMUTABLE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'draft' AND NEW.status = 'draft' THEN
        RAISE EXCEPTION '{MSG_BACK_TO_DRAFT}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND (
        NEW.total <> OLD.total OR NEW.subtotal <> OLD.subtotal OR NEW.tax_total <> OLD.tax_total
        OR NEW.customer_id <> OLD.customer_id OR NEW.invoice_number <> OLD.invoice_number
        OR NEW.invoice_date <> OLD.invoice_date
        OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id
        OR (OLD.status = 'void' AND (
            NEW.status <> OLD.status OR NEW.amount_settled <> OLD.amount_settled
            OR NEW.void_date IS DISTINCT FROM OLD.void_date
            OR NEW.void_journal_entry_id IS DISTINCT FROM OLD.void_journal_entry_id))
    ) THEN
        RAISE EXCEPTION '{MSG_ISSUED_INVOICE_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_invoice_allocations_guard": f"""
CREATE FUNCTION fms_invoice_allocations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    inv RECORD;
    source_amount numeric;
    already numeric;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION '{MSG_ALLOC_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total INTO inv FROM sales_invoices WHERE id = NEW.invoice_id;
    IF inv.status IN ('draft', 'void') THEN
        RAISE EXCEPTION '{MSG_ALLOC_CLOSED_INVOICE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(SUM(amount), 0) INTO already
      FROM invoice_allocations WHERE invoice_id = NEW.invoice_id;
    IF already + NEW.amount > inv.total THEN
        RAISE EXCEPTION '{MSG_ALLOC_EXCEEDS_INVOICE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.payment_id IS NOT NULL THEN
        SELECT amount INTO source_amount FROM customer_payments WHERE id = NEW.payment_id;
        SELECT COALESCE(SUM(amount), 0) INTO already
          FROM invoice_allocations WHERE payment_id = NEW.payment_id;
    ELSE
        SELECT amount INTO source_amount FROM customer_adjustments WHERE id = NEW.adjustment_id;
        SELECT COALESCE(SUM(amount), 0) INTO already
          FROM invoice_allocations WHERE adjustment_id = NEW.adjustment_id;
    END IF;
    IF already + NEW.amount > source_amount THEN
        RAISE EXCEPTION '{MSG_ALLOC_EXCEEDS_SOURCE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS = {
    "trg_sales_invoice_lines_guard": """
CREATE TRIGGER trg_sales_invoice_lines_guard
BEFORE INSERT OR UPDATE OR DELETE ON sales_invoice_lines
FOR EACH ROW EXECUTE FUNCTION fms_sales_invoice_lines_guard()
""",
    "trg_sales_invoices_guard": """
CREATE TRIGGER trg_sales_invoices_guard
BEFORE UPDATE OR DELETE ON sales_invoices
FOR EACH ROW EXECUTE FUNCTION fms_sales_invoices_guard()
""",
    "trg_invoice_allocations_guard": """
CREATE TRIGGER trg_invoice_allocations_guard
BEFORE INSERT OR UPDATE OR DELETE ON invoice_allocations
FOR EACH ROW EXECUTE FUNCTION fms_invoice_allocations_guard()
""",
}


# --- SQLite ---------------------------------------------------------------------

NOT_DRAFT = "SELECT 1 FROM sales_invoices WHERE id = {ref}.invoice_id AND status <> 'draft'"
CENTS = "CAST(ROUND(({expr}) * 100) AS INTEGER)"

SQLITE_TRIGGERS = {
    "trg_sales_invoice_lines_no_insert_issued": f"""
CREATE TRIGGER trg_sales_invoice_lines_no_insert_issued
BEFORE INSERT ON sales_invoice_lines FOR EACH ROW
WHEN EXISTS ({NOT_DRAFT.format(ref="NEW")})
BEGIN
    SELECT RAISE(ABORT, '{MSG_ISSUED_LINES_IMMUTABLE}');
END
""",
    "trg_sales_invoice_lines_no_update_issued": f"""
CREATE TRIGGER trg_sales_invoice_lines_no_update_issued
BEFORE UPDATE ON sales_invoice_lines FOR EACH ROW
WHEN EXISTS ({NOT_DRAFT.format(ref="OLD")}) OR EXISTS ({NOT_DRAFT.format(ref="NEW")})
BEGIN
    SELECT RAISE(ABORT, '{MSG_ISSUED_LINES_IMMUTABLE}');
END
""",
    "trg_sales_invoice_lines_no_delete_issued": f"""
CREATE TRIGGER trg_sales_invoice_lines_no_delete_issued
BEFORE DELETE ON sales_invoice_lines FOR EACH ROW
WHEN EXISTS ({NOT_DRAFT.format(ref="OLD")})
BEGIN
    SELECT RAISE(ABORT, '{MSG_ISSUED_LINES_IMMUTABLE}');
END
""",
    "trg_sales_invoices_no_back_to_draft": f"""
CREATE TRIGGER trg_sales_invoices_no_back_to_draft
BEFORE UPDATE ON sales_invoices FOR EACH ROW
WHEN OLD.status <> 'draft' AND NEW.status = 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_BACK_TO_DRAFT}');
END
""",
    "trg_sales_invoices_guard_issued": f"""
CREATE TRIGGER trg_sales_invoices_guard_issued
BEFORE UPDATE ON sales_invoices FOR EACH ROW
WHEN OLD.status <> 'draft' AND (
    {CENTS.format(expr="NEW.total")} <> {CENTS.format(expr="OLD.total")}
    OR {CENTS.format(expr="NEW.subtotal")} <> {CENTS.format(expr="OLD.subtotal")}
    OR {CENTS.format(expr="NEW.tax_total")} <> {CENTS.format(expr="OLD.tax_total")}
    OR NEW.customer_id <> OLD.customer_id OR NEW.invoice_number <> OLD.invoice_number
    OR NEW.invoice_date <> OLD.invoice_date
    OR NEW.journal_entry_id IS NOT OLD.journal_entry_id
    OR (OLD.status = 'void' AND (
        NEW.status <> OLD.status
        OR {CENTS.format(expr="NEW.amount_settled")} <> {CENTS.format(expr="OLD.amount_settled")}
        OR NEW.void_date IS NOT OLD.void_date
        OR NEW.void_journal_entry_id IS NOT OLD.void_journal_entry_id))
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_ISSUED_INVOICE_IMMUTABLE}');
END
""",
    "trg_sales_invoices_no_delete_issued": f"""
CREATE TRIGGER trg_sales_invoices_no_delete_issued
BEFORE DELETE ON sales_invoices FOR EACH ROW
WHEN OLD.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_ISSUED_INVOICE_IMMUTABLE}');
END
""",
    "trg_invoice_allocations_closed_invoice": f"""
CREATE TRIGGER trg_invoice_allocations_closed_invoice
BEFORE INSERT ON invoice_allocations FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM sales_invoices WHERE id = NEW.invoice_id AND status IN ('draft', 'void')
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_CLOSED_INVOICE}');
END
""",
    "trg_invoice_allocations_within_invoice": f"""
CREATE TRIGGER trg_invoice_allocations_within_invoice
BEFORE INSERT ON invoice_allocations FOR EACH ROW
WHEN {CENTS.format(expr="(SELECT COALESCE(SUM(amount), 0) FROM invoice_allocations WHERE invoice_id = NEW.invoice_id) + NEW.amount")}
   > {CENTS.format(expr="(SELECT total FROM sales_invoices WHERE id = NEW.invoice_id)")}
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_EXCEEDS_INVOICE}');
END
""",
    "trg_invoice_allocations_within_payment": f"""
CREATE TRIGGER trg_invoice_allocations_within_payment
BEFORE INSERT ON invoice_allocations FOR EACH ROW
WHEN NEW.payment_id IS NOT NULL AND
   {CENTS.format(expr="(SELECT COALESCE(SUM(amount), 0) FROM invoice_allocations WHERE payment_id = NEW.payment_id) + NEW.amount")}
   > {CENTS.format(expr="(SELECT amount FROM customer_payments WHERE id = NEW.payment_id)")}
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_EXCEEDS_SOURCE}');
END
""",
    "trg_invoice_allocations_within_adjustment": f"""
CREATE TRIGGER trg_invoice_allocations_within_adjustment
BEFORE INSERT ON invoice_allocations FOR EACH ROW
WHEN NEW.adjustment_id IS NOT NULL AND
   {CENTS.format(expr="(SELECT COALESCE(SUM(amount), 0) FROM invoice_allocations WHERE adjustment_id = NEW.adjustment_id) + NEW.amount")}
   > {CENTS.format(expr="(SELECT amount FROM customer_adjustments WHERE id = NEW.adjustment_id)")}
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_EXCEEDS_SOURCE}');
END
""",
    "trg_invoice_allocations_no_update": f"""
CREATE TRIGGER trg_invoice_allocations_no_update
BEFORE UPDATE ON invoice_allocations FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_IMMUTABLE}');
END
""",
    "trg_invoice_allocations_no_delete": f"""
CREATE TRIGGER trg_invoice_allocations_no_delete
BEFORE DELETE ON invoice_allocations FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_IMMUTABLE}');
END
""",
}

SQLITE_INVOICE_TRIGGERS = (
    "trg_sales_invoices_no_back_to_draft",
    "trg_sales_invoices_guard_issued",
    "trg_sales_invoices_no_delete_issued",
)


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for receivables triggers: {name}")
    return name


def upgrade() -> None:
    op.create_table(
        "receivable_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("receivable_account_id", sa.Integer(), nullable=False),
        sa.Column("tax_account_id", sa.Integer(), nullable=False),
        sa.Column("credit_account_id", sa.Integer(), nullable=False),
        sa.Column("write_off_account_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_receivable_settings_single_row")),
        sa.ForeignKeyConstraint(["receivable_account_id"], ["accounts.id"], name=op.f("fk_receivable_settings_receivable_account_id_accounts")),
        sa.ForeignKeyConstraint(["tax_account_id"], ["accounts.id"], name=op.f("fk_receivable_settings_tax_account_id_accounts")),
        sa.ForeignKeyConstraint(["credit_account_id"], ["accounts.id"], name=op.f("fk_receivable_settings_credit_account_id_accounts")),
        sa.ForeignKeyConstraint(["write_off_account_id"], ["accounts.id"], name=op.f("fk_receivable_settings_write_off_account_id_accounts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receivable_settings")),
    )

    # Batch mode: SQLite recreates the table, PostgreSQL emits plain ALTERs.
    with op.batch_alter_table("sales_invoices") as batch_op:
        batch_op.add_column(sa.Column("subtotal", MONEY, server_default="0", nullable=False))
        batch_op.add_column(sa.Column("tax_total", MONEY, server_default="0", nullable=False))
        batch_op.add_column(sa.Column("amount_settled", MONEY, server_default="0", nullable=False))
        batch_op.add_column(sa.Column("notes", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("journal_entry_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("void_journal_entry_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("void_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False))
        batch_op.create_foreign_key(op.f("fk_sales_invoices_journal_entry_id_journal_entries"), "journal_entries", ["journal_entry_id"], ["id"])
        batch_op.create_foreign_key(op.f("fk_sales_invoices_void_journal_entry_id_journal_entries"), "journal_entries", ["void_journal_entry_id"], ["id"])
        batch_op.create_index("ix_sales_invoices_status", ["status"])
        batch_op.create_index("ix_sales_invoices_due_date", ["due_date"])
    # Invoices created before line items existed carried only a total; backfill before the
    # consistency checks below are installed, otherwise they would reject the existing rows.
    op.execute("UPDATE sales_invoices SET subtotal = total WHERE subtotal = 0")
    with op.batch_alter_table("sales_invoices") as batch_op:
        batch_op.create_check_constraint(op.f("ck_sales_invoices_non_negative"), "total >= 0 AND subtotal >= 0 AND tax_total >= 0")
        batch_op.create_check_constraint(op.f("ck_sales_invoices_total_is_sum"), "ROUND(subtotal + tax_total, 2) = total")
        batch_op.create_check_constraint(op.f("ck_sales_invoices_settled_within_total"), "amount_settled >= 0 AND amount_settled <= total")

    op.create_table(
        "sales_invoice_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("revenue_account_id", sa.Integer(), nullable=False),
        sa.Column("tax_code_id", sa.Integer(), nullable=True),
        sa.Column("tax_rate", sa.Numeric(10, 6), nullable=True),
        sa.Column("net_amount", MONEY, nullable=False),
        sa.Column("tax_amount", MONEY, nullable=False),
        sa.Column("line_total", MONEY, nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_sales_invoice_lines_positive_quantity")),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_sales_invoice_lines_non_negative_price")),
        sa.CheckConstraint("net_amount >= 0 AND tax_amount >= 0 AND line_total >= 0", name=op.f("ck_sales_invoice_lines_non_negative")),
        sa.CheckConstraint("ROUND(net_amount + tax_amount, 2) = line_total", name=op.f("ck_sales_invoice_lines_total_is_sum")),
        sa.ForeignKeyConstraint(["invoice_id"], ["sales_invoices.id"], name=op.f("fk_sales_invoice_lines_invoice_id_sales_invoices"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revenue_account_id"], ["accounts.id"], name=op.f("fk_sales_invoice_lines_revenue_account_id_accounts")),
        sa.ForeignKeyConstraint(["tax_code_id"], ["tax_codes.id"], name=op.f("fk_sales_invoice_lines_tax_code_id_tax_codes")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales_invoice_lines")),
        sa.UniqueConstraint("invoice_id", "line_no", name="uq_sales_invoice_lines_invoice_line_no"),
    )
    op.create_index(op.f("ix_sales_invoice_lines_invoice_id"), "sales_invoice_lines", ["invoice_id"])

    op.create_table(
        "customer_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("amount_allocated", MONEY, server_default="0", nullable=False),
        sa.Column("deposit_account_id", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_customer_payments_positive_amount")),
        sa.CheckConstraint("amount_allocated >= 0 AND amount_allocated <= amount", name=op.f("ck_customer_payments_allocated_within_amount")),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_customer_payments_customer_id_customers")),
        sa.ForeignKeyConstraint(["deposit_account_id"], ["accounts.id"], name=op.f("fk_customer_payments_deposit_account_id_accounts")),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entries.id"], name=op.f("fk_customer_payments_journal_entry_id_journal_entries")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_payments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_customer_payments_idempotency_key")),
    )
    op.create_index(op.f("ix_customer_payments_customer_id"), "customer_payments", ["customer_id"])

    op.create_table(
        "customer_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("adjustment_date", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("amount_allocated", MONEY, server_default="0", nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("journal_entry_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_customer_adjustments_positive_amount")),
        sa.CheckConstraint("amount_allocated >= 0 AND amount_allocated <= amount", name=op.f("ck_customer_adjustments_allocated_within_amount")),
        sa.CheckConstraint("kind IN ('credit', 'write_off')", name=op.f("ck_customer_adjustments_kind")),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_customer_adjustments_customer_id_customers")),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entries.id"], name=op.f("fk_customer_adjustments_journal_entry_id_journal_entries")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_adjustments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_customer_adjustments_idempotency_key")),
    )
    op.create_index(op.f("ix_customer_adjustments_customer_id"), "customer_adjustments", ["customer_id"])

    op.create_table(
        "invoice_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("adjustment_id", sa.Integer(), nullable=True),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("allocation_date", sa.Date(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_invoice_allocations_positive_amount")),
        sa.CheckConstraint(
            "(payment_id IS NOT NULL AND adjustment_id IS NULL) OR (payment_id IS NULL AND adjustment_id IS NOT NULL)",
            name=op.f("ck_invoice_allocations_exactly_one_source"),
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["sales_invoices.id"], name=op.f("fk_invoice_allocations_invoice_id_sales_invoices")),
        sa.ForeignKeyConstraint(["payment_id"], ["customer_payments.id"], name=op.f("fk_invoice_allocations_payment_id_customer_payments")),
        sa.ForeignKeyConstraint(["adjustment_id"], ["customer_adjustments.id"], name=op.f("fk_invoice_allocations_adjustment_id_customer_adjustments")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_allocations")),
        sa.UniqueConstraint("idempotency_key", "invoice_id", name="uq_invoice_allocations_key_invoice"),
    )
    op.create_index(op.f("ix_invoice_allocations_invoice_id"), "invoice_allocations", ["invoice_id"])
    op.create_index(op.f("ix_invoice_allocations_payment_id"), "invoice_allocations", ["payment_id"])
    op.create_index(op.f("ix_invoice_allocations_adjustment_id"), "invoice_allocations", ["adjustment_id"])

    if _dialect() == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()
    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_sales_invoices_guard ON sales_invoices")
    else:
        for name in SQLITE_INVOICE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")

    for table in ("invoice_allocations", "customer_adjustments", "customer_payments", "sales_invoice_lines"):
        op.drop_table(table)  # indexes and their triggers go with the table
    if dialect == "postgresql":
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")

    with op.batch_alter_table("sales_invoices") as batch_op:
        batch_op.drop_index("ix_sales_invoices_due_date")
        batch_op.drop_index("ix_sales_invoices_status")
        batch_op.drop_constraint(op.f("ck_sales_invoices_settled_within_total"), type_="check")
        batch_op.drop_constraint(op.f("ck_sales_invoices_total_is_sum"), type_="check")
        batch_op.drop_constraint(op.f("ck_sales_invoices_non_negative"), type_="check")
        batch_op.drop_constraint(op.f("fk_sales_invoices_void_journal_entry_id_journal_entries"), type_="foreignkey")
        batch_op.drop_constraint(op.f("fk_sales_invoices_journal_entry_id_journal_entries"), type_="foreignkey")
        for column in ("created_at", "void_date", "void_journal_entry_id", "journal_entry_id", "notes", "amount_settled", "tax_total", "subtotal"):
            batch_op.drop_column(column)

    op.drop_table("receivable_settings")
