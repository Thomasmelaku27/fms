"""Accounts payable: payment runs (proposal -> review -> approval -> execution).

Revises ``fmspayables03``; adds ``payment_runs`` and ``payment_run_items`` with
their database rules. Nothing in a run touches the ledger until execution,
which goes through the ordinary vendor-payment service.

Database rules
--------------
* run status transitions: draft -> reviewed | cancelled; reviewed -> approved |
  draft | cancelled; approved -> executing | cancelled; executing -> completed |
  failed; failed -> executing | cancelled. Completed and cancelled are final.
* a run's selection parameters (pay date, due-through date, cash account, method,
  filters) are frozen once it leaves ``draft``; only draft runs can be deleted.
* items can be inserted / deleted only while the run is ``draft``; while
  ``draft`` any item column may change; while ``executing`` only the execution
  outcome columns (status, payment, execution reference, failure reason) may
  change; otherwise items are frozen.

Downgrade drops both tables (runs are operational records; the vendor payments
they produced are ordinary payments and survive).

Revision ID: fmspayables04
Revises: fmspayables03
Create Date: 2026-11-10 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fmspayables04"
down_revision: str | None = "fmspayables03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MSG_RUN_BAD_TRANSITION = "invalid payment run status transition"
MSG_RUN_FROZEN = "selection parameters of a non-draft payment run cannot be modified"
MSG_RUN_NO_DELETE = "only draft payment runs can be deleted"
MSG_ITEM_FROZEN = "payment run items can only change while the run is draft or executing"

SQL_RUN_TRANSITION = """(
       (OLD.status = 'draft' AND NEW.status IN ('reviewed', 'cancelled'))
    OR (OLD.status = 'reviewed' AND NEW.status IN ('approved', 'draft', 'cancelled'))
    OR (OLD.status = 'approved' AND NEW.status IN ('executing', 'cancelled'))
    OR (OLD.status = 'executing' AND NEW.status IN ('completed', 'failed'))
    OR (OLD.status = 'failed' AND NEW.status IN ('executing', 'cancelled'))
)"""
SQL_RUN_FROZEN_CHANGED = """(
       NEW.pay_date <> OLD.pay_date
    OR NEW.due_through <> OLD.due_through
    OR COALESCE(NEW.vendor_filter, '') <> COALESCE(OLD.vendor_filter, '')
    OR NEW.take_discounts <> OLD.take_discounts
    OR NEW.use_credits <> OLD.use_credits
    OR NEW.use_advances <> OLD.use_advances
    OR NEW.cash_account_id <> OLD.cash_account_id
    OR NEW.method <> OLD.method
)"""
# Columns an executing run may write on its items.
SQL_ITEM_PLAN_CHANGED = """(
       NEW.run_id <> OLD.run_id
    OR NEW.vendor_id <> OLD.vendor_id
    OR NEW.bill_id <> OLD.bill_id
    OR COALESCE(NEW.installment_no, -1) <> COALESCE(OLD.installment_no, -1)
    OR NEW.due_date <> OLD.due_date
    OR NEW.outstanding_snapshot <> OLD.outstanding_snapshot
    OR NEW.settle_amount <> OLD.settle_amount
    OR NEW.discount_amount <> OLD.discount_amount
    OR NEW.credit_amount <> OLD.credit_amount
    OR NEW.advance_amount <> OLD.advance_amount
    OR NEW.pay_amount <> OLD.pay_amount
    OR NEW.explanation <> OLD.explanation
    OR COALESCE(NEW.excluded_by, '') <> COALESCE(OLD.excluded_by, '')
    OR COALESCE(NEW.exclusion_reason, '') <> COALESCE(OLD.exclusion_reason, '')
)"""

PG_FUNCTIONS = {
    "fms_ap_payment_runs_guard": f"""
CREATE FUNCTION fms_ap_payment_runs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_RUN_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {SQL_RUN_TRANSITION} THEN
        RAISE EXCEPTION '{MSG_RUN_BAD_TRANSITION}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND {SQL_RUN_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{MSG_RUN_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_payment_run_items_guard": f"""
CREATE FUNCTION fms_ap_payment_run_items_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    run_status varchar;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO run_status FROM payment_runs WHERE id = OLD.run_id;
        IF run_status IS NOT NULL AND run_status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_ITEM_FROZEN}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    SELECT status INTO run_status FROM payment_runs WHERE id = NEW.run_id;
    IF TG_OP = 'INSERT' THEN
        IF run_status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_ITEM_FROZEN}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF run_status = 'draft' THEN
        RETURN NEW;
    END IF;
    IF run_status = 'executing' AND NOT {SQL_ITEM_PLAN_CHANGED} THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '{MSG_ITEM_FROZEN}'
        USING ERRCODE = 'integrity_constraint_violation';
END
$$
""",
}

PG_TRIGGERS = {
    "trg_ap_payment_runs_guard": """
CREATE TRIGGER trg_ap_payment_runs_guard
BEFORE UPDATE OR DELETE ON payment_runs
FOR EACH ROW EXECUTE FUNCTION fms_ap_payment_runs_guard()
""",
    "trg_ap_payment_run_items_guard": """
CREATE TRIGGER trg_ap_payment_run_items_guard
BEFORE INSERT OR UPDATE OR DELETE ON payment_run_items
FOR EACH ROW EXECUTE FUNCTION fms_ap_payment_run_items_guard()
""",
}

_RUN_STATUS = "(SELECT status FROM payment_runs WHERE id = {ref}.run_id)"

SQLITE_TRIGGERS = {
    "trg_ap_payment_runs_transition": f"""
CREATE TRIGGER trg_ap_payment_runs_transition
BEFORE UPDATE OF status ON payment_runs FOR EACH ROW
WHEN OLD.status <> NEW.status AND NOT {SQL_RUN_TRANSITION}
BEGIN
    SELECT RAISE(ABORT, '{MSG_RUN_BAD_TRANSITION}');
END
""",
    "trg_ap_payment_runs_frozen": f"""
CREATE TRIGGER trg_ap_payment_runs_frozen
BEFORE UPDATE ON payment_runs FOR EACH ROW
WHEN OLD.status <> 'draft' AND {SQL_RUN_FROZEN_CHANGED}
BEGIN
    SELECT RAISE(ABORT, '{MSG_RUN_FROZEN}');
END
""",
    "trg_ap_payment_runs_no_delete": f"""
CREATE TRIGGER trg_ap_payment_runs_no_delete
BEFORE DELETE ON payment_runs FOR EACH ROW
WHEN OLD.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_RUN_NO_DELETE}');
END
""",
    "trg_ap_payment_run_items_insert_guard": f"""
CREATE TRIGGER trg_ap_payment_run_items_insert_guard
BEFORE INSERT ON payment_run_items FOR EACH ROW
WHEN {_RUN_STATUS.format(ref="NEW")} <> 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_ITEM_FROZEN}');
END
""",
    "trg_ap_payment_run_items_delete_guard": f"""
CREATE TRIGGER trg_ap_payment_run_items_delete_guard
BEFORE DELETE ON payment_run_items FOR EACH ROW
WHEN {_RUN_STATUS.format(ref="OLD")} IS NOT NULL AND {_RUN_STATUS.format(ref="OLD")} <> 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_ITEM_FROZEN}');
END
""",
    "trg_ap_payment_run_items_update_guard": f"""
CREATE TRIGGER trg_ap_payment_run_items_update_guard
BEFORE UPDATE ON payment_run_items FOR EACH ROW
WHEN NOT (
       {_RUN_STATUS.format(ref="NEW")} = 'draft'
    OR ({_RUN_STATUS.format(ref="NEW")} = 'executing' AND NOT {SQL_ITEM_PLAN_CHANGED})
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_ITEM_FROZEN}');
END
""",
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for payables triggers: {name}")
    return name


def upgrade() -> None:
    op.create_table(
        "payment_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("due_through", sa.Date(), nullable=False),
        sa.Column("vendor_filter", sa.String(length=500), nullable=True),
        sa.Column("take_discounts", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("use_credits", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("use_advances", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("cash_account_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_by", sa.String(length=100), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_by", sa.String(length=100), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'reviewed', 'approved', 'executing', 'completed', 'failed', "
            "'cancelled')",
            name=op.f("ck_payment_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["cash_account_id"],
            ["accounts.id"],
            name=op.f("fk_payment_runs_cash_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_runs")),
        sa.UniqueConstraint("reference", name="uq_payment_runs_reference"),
    )
    op.create_index(op.f("ix_payment_runs_status"), "payment_runs", ["status"])

    op.create_table(
        "payment_run_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("installment_no", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("outstanding_snapshot", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("settle_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("credit_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("advance_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("pay_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("explanation", sa.String(length=1000), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("execution_reference", sa.String(length=100), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("excluded_by", sa.String(length=100), nullable=True),
        sa.Column("exclusion_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed', 'excluded', 'paid', 'failed')",
            name=op.f("ck_payment_run_items_status_valid"),
        ),
        sa.CheckConstraint(
            "pay_amount >= 0", name=op.f("ck_payment_run_items_pay_amount_non_negative")
        ),
        sa.CheckConstraint(
            "discount_amount >= 0", name=op.f("ck_payment_run_items_discount_non_negative")
        ),
        sa.CheckConstraint(
            "credit_amount >= 0", name=op.f("ck_payment_run_items_credit_non_negative")
        ),
        sa.CheckConstraint(
            "advance_amount >= 0", name=op.f("ck_payment_run_items_advance_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_payment_run_items_bill_id_supplier_bills"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["vendor_payments.id"],
            name=op.f("fk_payment_run_items_payment_id_vendor_payments"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payment_runs.id"],
            name=op.f("fk_payment_run_items_run_id_payment_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"], ["vendors.id"], name=op.f("fk_payment_run_items_vendor_id_vendors")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_run_items")),
        sa.UniqueConstraint("run_id", "bill_id", name="uq_payment_run_items_run_bill"),
    )
    op.create_index(op.f("ix_payment_run_items_bill_id"), "payment_run_items", ["bill_id"])
    op.create_index(op.f("ix_payment_run_items_run_id"), "payment_run_items", ["run_id"])
    op.create_index(op.f("ix_payment_run_items_vendor_id"), "payment_run_items", ["vendor_id"])

    if _dialect() == "postgresql":
        for ddl in (*PG_FUNCTIONS.values(), *PG_TRIGGERS.values()):
            op.execute(ddl)
    else:
        for ddl in SQLITE_TRIGGERS.values():
            op.execute(ddl)


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_run_items_vendor_id"), table_name="payment_run_items")
    op.drop_index(op.f("ix_payment_run_items_run_id"), table_name="payment_run_items")
    op.drop_index(op.f("ix_payment_run_items_bill_id"), table_name="payment_run_items")
    op.drop_table("payment_run_items")
    op.drop_index(op.f("ix_payment_runs_status"), table_name="payment_runs")
    op.drop_table("payment_runs")
    if _dialect() == "postgresql":
        for name in PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION {name}()")
