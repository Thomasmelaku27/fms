"""Foundational database schema for financial business modules.

Revision ID: fmsmodules01
Revises: 61ade0be8ecb
"""
from alembic import op
import sqlalchemy as sa
revision = "fmsmodules01"
down_revision = "61ade0be8ecb"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("vendors",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("code",sa.String(30),nullable=False),sa.Column("name",sa.String(200),nullable=False),sa.Column("is_active",sa.Boolean(),nullable=False),sa.UniqueConstraint("code",name="uq_vendors_code"))
    op.create_table("supplier_bills",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("vendor_id",sa.Integer(),sa.ForeignKey("vendors.id"),nullable=False),sa.Column("invoice_number",sa.String(80),nullable=False),sa.Column("invoice_date",sa.Date(),nullable=False),sa.Column("due_date",sa.Date(),nullable=False),sa.Column("total",sa.Numeric(18,2),nullable=False),sa.Column("status",sa.String(20),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),sa.UniqueConstraint("vendor_id","invoice_number",name="uq_supplier_bills_vendor_invoice"))
    op.create_table("customers",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("code",sa.String(30),nullable=False),sa.Column("name",sa.String(200),nullable=False),sa.Column("is_active",sa.Boolean(),nullable=False),sa.UniqueConstraint("code",name="uq_customers_code"))
    op.create_table("sales_invoices",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("customer_id",sa.Integer(),sa.ForeignKey("customers.id"),nullable=False),sa.Column("invoice_number",sa.String(80),nullable=False),sa.Column("invoice_date",sa.Date(),nullable=False),sa.Column("due_date",sa.Date(),nullable=False),sa.Column("total",sa.Numeric(18,2),nullable=False),sa.Column("status",sa.String(20),nullable=False),sa.UniqueConstraint("customer_id","invoice_number",name="uq_sales_invoices_customer_invoice"))
    op.create_table("budgets",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("name",sa.String(120),nullable=False),sa.Column("start_date",sa.Date(),nullable=False),sa.Column("end_date",sa.Date(),nullable=False),sa.Column("status",sa.String(20),nullable=False))
    op.create_table("budget_lines",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("budget_id",sa.Integer(),sa.ForeignKey("budgets.id",ondelete="CASCADE"),nullable=False),sa.Column("account_id",sa.Integer(),sa.ForeignKey("accounts.id"),nullable=False),sa.Column("amount",sa.Numeric(18,2),nullable=False),sa.UniqueConstraint("budget_id","account_id",name="uq_budget_lines_budget_account"))
    op.create_table("cash_accounts",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("ledger_account_id",sa.Integer(),sa.ForeignKey("accounts.id"),nullable=False),sa.Column("label",sa.String(100),nullable=False),sa.UniqueConstraint("ledger_account_id",name="uq_cash_accounts_ledger_account_id"))
    op.create_table("tax_codes",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("code",sa.String(30),nullable=False),sa.Column("description",sa.String(200),nullable=False),sa.Column("rate",sa.Numeric(10,6),nullable=False),sa.Column("jurisdiction",sa.String(100),nullable=False),sa.Column("is_active",sa.Boolean(),nullable=False),sa.UniqueConstraint("code",name="uq_tax_codes_code"))
    op.create_table("review_suggestions",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("description",sa.String(500),nullable=False),sa.Column("suggested_account_id",sa.Integer(),sa.ForeignKey("accounts.id"),nullable=False),sa.Column("confidence",sa.Numeric(4,3),nullable=False),sa.Column("status",sa.String(20),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_table("audit_events",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("actor",sa.String(100),nullable=False),sa.Column("action",sa.String(100),nullable=False),sa.Column("resource",sa.String(150),nullable=False),sa.Column("occurred_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))

def downgrade():
    for name in ("audit_events","review_suggestions","tax_codes","cash_accounts","budget_lines","budgets","sales_invoices","customers","supplier_bills","vendors"):
        op.drop_table(name)
