"""Accounts payable workflow: bill lines, approval, schedules, payments, allocations.

Branches from the shared module baseline (``fmsmodules01``) and only touches
tables owned by the payables module. Other module branches revise the same
baseline; ``alembic upgrade heads`` applies all of them until a merge revision
is added.

Schema changes
--------------
* ``vendors``: default AP control account (``payable_account_id``) and
  ``payment_terms_days``.
* ``supplier_bills``: canonical ``invoice_number_normalized`` with its own
  UNIQUE constraint (database-level duplicate supplier-invoice protection that
  ignores case and whitespace), AP account link, approval / rejection actor and
  timestamp, link to the posted journal entry, CHECK constraints on amounts,
  dates and status values.
* New tables ``supplier_bill_lines``, ``bill_payment_schedules``,
  ``vendor_payments`` and ``payment_allocations``.

Database-enforced AP rules (triggers, one block per backend as in the initial
migration, identical messages on both):

* bill status changes follow the workflow state machine;
* financial fields of a bill that left ``draft`` are frozen; only drafts can be deleted;
* bill lines can only be inserted / updated / deleted while the bill is a draft;
* payment schedules can only change while the bill is approved and not fully paid;
* allocations only target approved / partially paid bills and never exceed the
  bill total (integer-cent arithmetic on SQLite, exact NUMERIC on PostgreSQL);
* payments and allocations are append-only: no deletes, no changes to amounts,
  dates, parties or ledger links. Corrections are new postings.

Revision ID: fmspayables01
Revises: fmsmodules01
Create Date: 2026-10-15 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fmspayables01"
down_revision: str | None = "fmsmodules01"
branch_labels: str | Sequence[str] | None = ("payables",)
depends_on: str | Sequence[str] | None = None

BILL_STATUSES = "'draft', 'approved', 'rejected', 'partially_paid', 'paid'"

# Error messages raised by the triggers (identical on both backends).
MSG_BILL_BAD_TRANSITION = "invalid supplier bill status transition"
MSG_BILL_FROZEN = "financial fields of a non-draft supplier bill cannot be modified"
MSG_BILL_NO_DELETE = "only draft supplier bills can be deleted"
MSG_BILL_LINES_FROZEN = "lines of a non-draft supplier bill cannot be modified"
MSG_SCHEDULE_FROZEN = "payment schedule can only change while the bill is approved and unpaid"
MSG_ALLOC_BILL_NOT_OPEN = "payments can only be allocated to approved or partially paid bills"
MSG_ALLOC_OVERPAY = "allocation exceeds the outstanding amount of the bill"
MSG_ALLOC_IMMUTABLE = "payment allocations cannot be modified or deleted"
MSG_PAYMENT_IMMUTABLE = "a recorded vendor payment cannot be modified or deleted"

# Allowed status transitions, as a SQL predicate over OLD.status / NEW.status.
SQL_ALLOWED_TRANSITION = """(
       (OLD.status = 'draft' AND NEW.status IN ('approved', 'rejected'))
    OR (OLD.status = 'rejected' AND NEW.status = 'draft')
    OR (OLD.status = 'approved' AND NEW.status IN ('partially_paid', 'paid'))
    OR (OLD.status = 'partially_paid' AND NEW.status = 'paid')
)"""

# Columns of supplier_bills that are frozen once the bill has left "draft".
SQL_BILL_FROZEN_CHANGED = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.invoice_number <> OLD.invoice_number
    OR NEW.invoice_number_normalized <> OLD.invoice_number_normalized
    OR NEW.invoice_date <> OLD.invoice_date
    OR NEW.due_date <> OLD.due_date
    OR NEW.total <> OLD.total
    OR COALESCE(NEW.payable_account_id, -1) <> COALESCE(OLD.payable_account_id, -1)
    OR COALESCE(NEW.journal_entry_id, -1) <> COALESCE(OLD.journal_entry_id, -1)
)"""

# Columns of vendor_payments that are frozen once recorded. The ledger link may
# only be set once (NULL -> id), never changed afterwards.
SQL_PAYMENT_FROZEN_CHANGED = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.payment_date <> OLD.payment_date
    OR NEW.amount <> OLD.amount
    OR NEW.cash_account_id <> OLD.cash_account_id
    OR (OLD.journal_entry_id IS NOT NULL
        AND COALESCE(NEW.journal_entry_id, -1) <> OLD.journal_entry_id)
)"""


# --- PostgreSQL -----------------------------------------------------------------

PG_FUNCTIONS = {
    "fms_ap_supplier_bills_guard": f"""
CREATE FUNCTION fms_ap_supplier_bills_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_BILL_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {SQL_ALLOWED_TRANSITION} THEN
        RAISE EXCEPTION '{MSG_BILL_BAD_TRANSITION}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND {SQL_BILL_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{MSG_BILL_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_supplier_bill_lines_guard": f"""
CREATE FUNCTION fms_ap_supplier_bill_lines_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO bill_status FROM supplier_bills WHERE id = OLD.bill_id;
    ELSE
        SELECT status INTO bill_status FROM supplier_bills WHERE id = NEW.bill_id;
    END IF;
    IF bill_status IS NOT NULL AND bill_status <> 'draft' THEN
        RAISE EXCEPTION '{MSG_BILL_LINES_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_bill_payment_schedules_guard": f"""
CREATE FUNCTION fms_ap_bill_payment_schedules_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO bill_status FROM supplier_bills WHERE id = OLD.bill_id;
    ELSE
        SELECT status INTO bill_status FROM supplier_bills WHERE id = NEW.bill_id;
    END IF;
    IF bill_status IS NOT NULL AND bill_status NOT IN ('approved', 'partially_paid') THEN
        RAISE EXCEPTION '{MSG_SCHEDULE_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_payment_allocations_guard": f"""
CREATE FUNCTION fms_ap_payment_allocations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
    bill_total numeric;
    already_allocated numeric;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION '{MSG_ALLOC_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total INTO bill_status, bill_total
      FROM supplier_bills WHERE id = NEW.bill_id;
    IF bill_status NOT IN ('approved', 'partially_paid') THEN
        RAISE EXCEPTION '{MSG_ALLOC_BILL_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(SUM(amount), 0) INTO already_allocated
      FROM payment_allocations WHERE bill_id = NEW.bill_id;
    IF already_allocated + NEW.amount > bill_total THEN
        RAISE EXCEPTION '{MSG_ALLOC_OVERPAY}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_vendor_payments_guard": f"""
CREATE FUNCTION fms_ap_vendor_payments_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR {SQL_PAYMENT_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{MSG_PAYMENT_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS = {
    "trg_ap_supplier_bills_guard": """
CREATE TRIGGER trg_ap_supplier_bills_guard
BEFORE UPDATE OR DELETE ON supplier_bills
FOR EACH ROW EXECUTE FUNCTION fms_ap_supplier_bills_guard()
""",
    "trg_ap_supplier_bill_lines_guard": """
CREATE TRIGGER trg_ap_supplier_bill_lines_guard
BEFORE INSERT OR UPDATE OR DELETE ON supplier_bill_lines
FOR EACH ROW EXECUTE FUNCTION fms_ap_supplier_bill_lines_guard()
""",
    "trg_ap_bill_payment_schedules_guard": """
CREATE TRIGGER trg_ap_bill_payment_schedules_guard
BEFORE INSERT OR UPDATE OR DELETE ON bill_payment_schedules
FOR EACH ROW EXECUTE FUNCTION fms_ap_bill_payment_schedules_guard()
""",
    "trg_ap_payment_allocations_guard": """
CREATE TRIGGER trg_ap_payment_allocations_guard
BEFORE INSERT OR UPDATE OR DELETE ON payment_allocations
FOR EACH ROW EXECUTE FUNCTION fms_ap_payment_allocations_guard()
""",
    "trg_ap_vendor_payments_guard": """
CREATE TRIGGER trg_ap_vendor_payments_guard
BEFORE UPDATE OR DELETE ON vendor_payments
FOR EACH ROW EXECUTE FUNCTION fms_ap_vendor_payments_guard()
""",
}


# --- SQLite ---------------------------------------------------------------------
# One trigger per event. Amount comparisons use integer cents because SQLite
# stores NUMERIC as REAL (same technique as the core ledger triggers).

SQLITE_BILL_STATUS = "(SELECT status FROM supplier_bills WHERE id = {ref}.bill_id)"


def _sqlite_bill_status_trigger(name: str, event: str, ref: str, condition: str, msg: str) -> str:
    table = {"L": "supplier_bill_lines", "S": "bill_payment_schedules"}[name[0]]
    return f"""
CREATE TRIGGER trg_ap_{table}_{event.lower()}_guard
BEFORE {event} ON {table} FOR EACH ROW
WHEN {SQLITE_BILL_STATUS.format(ref=ref)} {condition}
BEGIN
    SELECT RAISE(ABORT, '{msg}');
END
"""


SQLITE_TRIGGERS = {
    "trg_ap_supplier_bills_transition": f"""
CREATE TRIGGER trg_ap_supplier_bills_transition
BEFORE UPDATE OF status ON supplier_bills FOR EACH ROW
WHEN OLD.status <> NEW.status AND NOT {SQL_ALLOWED_TRANSITION}
BEGIN
    SELECT RAISE(ABORT, '{MSG_BILL_BAD_TRANSITION}');
END
""",
    "trg_ap_supplier_bills_frozen": f"""
CREATE TRIGGER trg_ap_supplier_bills_frozen
BEFORE UPDATE ON supplier_bills FOR EACH ROW
WHEN OLD.status <> 'draft' AND {SQL_BILL_FROZEN_CHANGED}
BEGIN
    SELECT RAISE(ABORT, '{MSG_BILL_FROZEN}');
END
""",
    "trg_ap_supplier_bills_no_delete": f"""
CREATE TRIGGER trg_ap_supplier_bills_no_delete
BEFORE DELETE ON supplier_bills FOR EACH ROW
WHEN OLD.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_BILL_NO_DELETE}');
END
""",
    **{
        f"trg_ap_supplier_bill_lines_{event.lower()}_guard": _sqlite_bill_status_trigger(
            "L", event, ref, "<> 'draft'", MSG_BILL_LINES_FROZEN
        )
        for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD"))
    },
    **{
        f"trg_ap_bill_payment_schedules_{event.lower()}_guard": _sqlite_bill_status_trigger(
            "S", event, ref, "NOT IN ('approved', 'partially_paid')", MSG_SCHEDULE_FROZEN
        )
        for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD"))
    },
    "trg_ap_payment_allocations_bill_open": f"""
CREATE TRIGGER trg_ap_payment_allocations_bill_open
BEFORE INSERT ON payment_allocations FOR EACH ROW
WHEN {SQLITE_BILL_STATUS.format(ref="NEW")} NOT IN ('approved', 'partially_paid')
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_BILL_NOT_OPEN}');
END
""",
    "trg_ap_payment_allocations_no_overpay": f"""
CREATE TRIGGER trg_ap_payment_allocations_no_overpay
BEFORE INSERT ON payment_allocations FOR EACH ROW
WHEN (
    SELECT COALESCE(SUM(CAST(ROUND(amount * 100) AS INTEGER)), 0)
      FROM payment_allocations WHERE bill_id = NEW.bill_id
) + CAST(ROUND(NEW.amount * 100) AS INTEGER) > (
    SELECT CAST(ROUND(total * 100) AS INTEGER) FROM supplier_bills WHERE id = NEW.bill_id
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_OVERPAY}');
END
""",
    "trg_ap_payment_allocations_no_update": f"""
CREATE TRIGGER trg_ap_payment_allocations_no_update
BEFORE UPDATE ON payment_allocations FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_IMMUTABLE}');
END
""",
    "trg_ap_payment_allocations_no_delete": f"""
CREATE TRIGGER trg_ap_payment_allocations_no_delete
BEFORE DELETE ON payment_allocations FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_IMMUTABLE}');
END
""",
    "trg_ap_vendor_payments_frozen": f"""
CREATE TRIGGER trg_ap_vendor_payments_frozen
BEFORE UPDATE ON vendor_payments FOR EACH ROW
WHEN {SQL_PAYMENT_FROZEN_CHANGED}
BEGIN
    SELECT RAISE(ABORT, '{MSG_PAYMENT_IMMUTABLE}');
END
""",
    "trg_ap_vendor_payments_no_delete": f"""
CREATE TRIGGER trg_ap_vendor_payments_no_delete
BEFORE DELETE ON vendor_payments FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_PAYMENT_IMMUTABLE}');
END
""",
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for payables triggers: {name}")
    return name


def upgrade() -> None:
    # --- vendors: default AP account and payment terms (batch = table rebuild on SQLite)
    with op.batch_alter_table("vendors") as batch:
        batch.add_column(sa.Column("payable_account_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "payment_terms_days", sa.Integer(), nullable=False, server_default="30"
            )
        )
        batch.create_foreign_key(
            op.f("fk_vendors_payable_account_id_accounts"),
            "accounts",
            ["payable_account_id"],
            ["id"],
        )

    # --- supplier_bills: workflow columns, canonical invoice number, CHECKs
    with op.batch_alter_table("supplier_bills") as batch:
        batch.add_column(sa.Column("invoice_number_normalized", sa.String(80), nullable=True))
        batch.add_column(sa.Column("description", sa.String(500), nullable=True))
        batch.add_column(sa.Column("payable_account_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("journal_entry_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("approved_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejected_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejection_reason", sa.String(500), nullable=True))
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )

    # Backfill the canonical invoice number for rows created by the baseline.
    op.execute(
        "UPDATE supplier_bills SET invoice_number_normalized = UPPER(TRIM(invoice_number)) "
        "WHERE invoice_number_normalized IS NULL"
    )

    with op.batch_alter_table("supplier_bills") as batch:
        batch.alter_column(
            "invoice_number_normalized", existing_type=sa.String(80), nullable=False
        )
        batch.create_unique_constraint(
            "uq_supplier_bills_vendor_invoice_normalized",
            ["vendor_id", "invoice_number_normalized"],
        )
        batch.create_foreign_key(
            op.f("fk_supplier_bills_payable_account_id_accounts"),
            "accounts",
            ["payable_account_id"],
            ["id"],
        )
        batch.create_foreign_key(
            op.f("fk_supplier_bills_journal_entry_id_journal_entries"),
            "journal_entries",
            ["journal_entry_id"],
            ["id"],
        )
        batch.create_check_constraint(op.f("ck_supplier_bills_total_positive"), "total > 0")
        batch.create_check_constraint(
            op.f("ck_supplier_bills_total_whole_cents"), "ROUND(total, 2) = total"
        )
        batch.create_check_constraint(
            op.f("ck_supplier_bills_due_date_after_invoice_date"), "due_date >= invoice_date"
        )
        batch.create_check_constraint(
            op.f("ck_supplier_bills_status_valid"), f"status IN ({BILL_STATUSES})"
        )
        batch.create_index(op.f("ix_supplier_bills_due_date"), ["due_date"])
        batch.create_index(op.f("ix_supplier_bills_status"), ["status"])

    # --- new tables
    op.create_table(
        "supplier_bill_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_supplier_bill_lines_quantity_positive")),
        sa.CheckConstraint(
            "unit_price > 0", name=op.f("ck_supplier_bill_lines_unit_price_positive")
        ),
        sa.CheckConstraint(
            "line_total > 0", name=op.f("ck_supplier_bill_lines_line_total_positive")
        ),
        sa.CheckConstraint(
            "ROUND(line_total, 2) = line_total",
            name=op.f("ck_supplier_bill_lines_line_total_whole_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_supplier_bill_lines_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_supplier_bill_lines_bill_id_supplier_bills"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_bill_lines")),
        sa.UniqueConstraint("bill_id", "line_no", name="uq_supplier_bill_lines_bill_line_no"),
    )

    op.create_table(
        "bill_payment_schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("installment_no", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_bill_payment_schedules_amount_positive")),
        sa.CheckConstraint(
            "ROUND(amount, 2) = amount", name=op.f("ck_bill_payment_schedules_amount_whole_cents")
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_bill_payment_schedules_bill_id_supplier_bills"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bill_payment_schedules")),
        sa.UniqueConstraint(
            "bill_id", "installment_no", name="uq_bill_payment_schedules_bill_installment"
        ),
    )
    op.create_index(
        op.f("ix_bill_payment_schedules_due_date"), "bill_payment_schedules", ["due_date"]
    )

    op.create_table(
        "vendor_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("recorded_by", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_vendor_payments_amount_positive")),
        sa.CheckConstraint(
            "ROUND(amount, 2) = amount", name=op.f("ck_vendor_payments_amount_whole_cents")
        ),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["accounts.id"],
            name=op.f("fk_vendor_payments_cash_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_vendor_payments_journal_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"], ["vendors.id"], name=op.f("fk_vendor_payments_vendor_id_vendors")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_payments")),
        sa.UniqueConstraint("idempotency_key", name="uq_vendor_payments_idempotency_key"),
    )

    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_allocations_amount_positive")),
        sa.CheckConstraint(
            "ROUND(amount, 2) = amount", name=op.f("ck_payment_allocations_amount_whole_cents")
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_payment_allocations_bill_id_supplier_bills"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["vendor_payments.id"],
            name=op.f("fk_payment_allocations_payment_id_vendor_payments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_allocations")),
        sa.UniqueConstraint("payment_id", "bill_id", name="uq_payment_allocations_payment_bill"),
    )
    op.create_index(op.f("ix_payment_allocations_bill_id"), "payment_allocations", ["bill_id"])

    # --- AP rules enforced by the database
    if _dialect() == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    # Triggers go with their tables; the SQLite ones on supplier_bills are dropped
    # explicitly since that table survives. PostgreSQL functions are separate objects.
    if _dialect() == "postgresql":
        op.execute("DROP TRIGGER trg_ap_supplier_bills_guard ON supplier_bills")
    else:
        for name in (
            "trg_ap_supplier_bills_transition",
            "trg_ap_supplier_bills_frozen",
            "trg_ap_supplier_bills_no_delete",
        ):
            op.execute(f"DROP TRIGGER {name}")

    op.drop_index(op.f("ix_payment_allocations_bill_id"), table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_table("vendor_payments")
    op.drop_index(op.f("ix_bill_payment_schedules_due_date"), table_name="bill_payment_schedules")
    op.drop_table("bill_payment_schedules")
    op.drop_table("supplier_bill_lines")

    if _dialect() == "postgresql":
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")

    with op.batch_alter_table("supplier_bills") as batch:
        batch.drop_index(op.f("ix_supplier_bills_status"))
        batch.drop_index(op.f("ix_supplier_bills_due_date"))
        batch.drop_constraint(op.f("ck_supplier_bills_status_valid"), type_="check")
        batch.drop_constraint(op.f("ck_supplier_bills_due_date_after_invoice_date"), type_="check")
        batch.drop_constraint(op.f("ck_supplier_bills_total_whole_cents"), type_="check")
        batch.drop_constraint(op.f("ck_supplier_bills_total_positive"), type_="check")
        batch.drop_constraint(
            op.f("fk_supplier_bills_journal_entry_id_journal_entries"), type_="foreignkey"
        )
        batch.drop_constraint(
            op.f("fk_supplier_bills_payable_account_id_accounts"), type_="foreignkey"
        )
        batch.drop_constraint("uq_supplier_bills_vendor_invoice_normalized", type_="unique")
        for column in (
            "updated_at",
            "rejection_reason",
            "rejected_at",
            "rejected_by",
            "approved_at",
            "approved_by",
            "journal_entry_id",
            "payable_account_id",
            "description",
            "invoice_number_normalized",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("vendors") as batch:
        batch.drop_constraint(op.f("fk_vendors_payable_account_id_accounts"), type_="foreignkey")
        batch.drop_column("payment_terms_days")
        batch.drop_column("payable_account_id")
