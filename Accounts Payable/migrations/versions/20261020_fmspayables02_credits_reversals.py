"""Accounts payable: DB-computed canonical numbers, credits, payment reversals.

Revises ``fmspayables01`` (the payables branch); touches only payables tables.

Fixes
-----
* The canonical ``invoice_number_normalized`` is now computed by the database on
  every INSERT / UPDATE (BEFORE trigger on PostgreSQL, AFTER trigger that rewrites
  the row on SQLite). Duplicate protection therefore holds for raw SQL that
  supplies a wrong canonical value. The expression is the same on both backends
  and is mirrored by ``models.sql_normalize`` for the API-layer check.

New
---
* ``vendor_payments``: ``status`` (recorded / reversed), reversal actor, timestamp,
  date, reason and reversing journal entry; ``corrects_payment_id`` on a
  replacement payment.
* ``supplier_credits`` / ``supplier_credit_lines``: supplier credit notes and
  internal credit adjustments with the same draft -> approved workflow as bills.
* ``credit_applications``: settlement of bills with credits (no ledger effect).

Changed database rules
----------------------
* Bill status transitions: the settlement states (approved, partially_paid, paid)
  may now move among themselves, because credits can be unapplied and payments
  reversed. Draft / rejected rules are unchanged.
* Allocation cap: allocations of *reversed* payments no longer count; credit
  applications do.
* Payments: a payment may be reversed exactly once (status recorded -> reversed)
  and only while setting the actor, date and reversing journal entry together;
  the reversal fields are frozen afterwards.

New rules: credits mirror the bill rules (frozen when non-draft, draft-only
delete, lines only while draft, reversal needs its entry and no applications);
credit applications only target open bills and open credits, never exceed the
credit total or the bill's remaining amount, and cannot be updated (unapply and
apply again).

Revision ID: fmspayables02
Revises: fmspayables01
Create Date: 2026-10-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fmspayables02"
down_revision: str | None = "fmspayables01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- messages (identical on both backends) ----------------------------------------------

MSG_BILL_BAD_TRANSITION = "invalid supplier bill status transition"
MSG_BILL_FROZEN = "financial fields of a non-draft supplier bill cannot be modified"
MSG_BILL_NO_DELETE = "only draft supplier bills can be deleted"
MSG_ALLOC_BILL_NOT_OPEN = "payments can only be allocated to approved or partially paid bills"
MSG_ALLOC_OVERPAY = "allocation exceeds the outstanding amount of the bill"
MSG_ALLOC_IMMUTABLE = "payment allocations cannot be modified or deleted"
MSG_PAYMENT_IMMUTABLE = "a recorded vendor payment cannot be modified or deleted"
MSG_PAYMENT_REVERSAL_INVALID = (
    "a payment reversal must set status, actor, date and reversing entry together, exactly once"
)
MSG_CREDIT_BAD_TRANSITION = "invalid supplier credit status transition"
MSG_CREDIT_FROZEN = "financial fields of a non-draft supplier credit cannot be modified"
MSG_CREDIT_NO_DELETE = "only draft supplier credits can be deleted"
MSG_CREDIT_LINES_FROZEN = "lines of a non-draft supplier credit cannot be modified"
MSG_CREDIT_REVERSAL_INVALID = (
    "a credit reversal must set actor, date and reversing entry together, exactly once"
)
MSG_CREDIT_REVERSE_APPLIED = "a supplier credit with applications cannot be reversed"
MSG_APPLY_BILL_NOT_OPEN = "credits can only be applied to approved or partially paid bills"
MSG_APPLY_CREDIT_NOT_OPEN = "only approved or partially applied credits can be applied"
MSG_APPLY_EXCEEDS_BILL = "credit application exceeds the outstanding amount of the bill"
MSG_APPLY_EXCEEDS_CREDIT = "credit application exceeds the remaining amount of the credit"
MSG_APPLICATION_IMMUTABLE = "credit applications cannot be modified; unapply and apply again"


# --- shared SQL fragments ------------------------------------------------------------------

SQL_BILL_TRANSITION_V2 = """(
       (OLD.status = 'draft' AND NEW.status IN ('approved', 'rejected'))
    OR (OLD.status = 'rejected' AND NEW.status = 'draft')
    OR (OLD.status IN ('approved', 'partially_paid', 'paid')
        AND NEW.status IN ('approved', 'partially_paid', 'paid'))
)"""

SQL_BILL_TRANSITION_V1 = """(
       (OLD.status = 'draft' AND NEW.status IN ('approved', 'rejected'))
    OR (OLD.status = 'rejected' AND NEW.status = 'draft')
    OR (OLD.status = 'approved' AND NEW.status IN ('partially_paid', 'paid'))
    OR (OLD.status = 'partially_paid' AND NEW.status = 'paid')
)"""

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

SQL_CREDIT_TRANSITION = """(
       (OLD.status = 'draft' AND NEW.status IN ('approved', 'rejected'))
    OR (OLD.status = 'rejected' AND NEW.status = 'draft')
    OR (OLD.status IN ('approved', 'partially_applied', 'applied')
        AND NEW.status IN ('approved', 'partially_applied', 'applied'))
    OR (OLD.status = 'approved' AND NEW.status = 'reversed')
)"""

SQL_CREDIT_FROZEN_CHANGED = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.kind <> OLD.kind
    OR NEW.credit_number <> OLD.credit_number
    OR NEW.credit_number_normalized <> OLD.credit_number_normalized
    OR NEW.credit_date <> OLD.credit_date
    OR NEW.total <> OLD.total
    OR COALESCE(NEW.related_bill_id, -1) <> COALESCE(OLD.related_bill_id, -1)
    OR COALESCE(NEW.payable_account_id, -1) <> COALESCE(OLD.payable_account_id, -1)
    OR COALESCE(NEW.journal_entry_id, -1) <> COALESCE(OLD.journal_entry_id, -1)
)"""

# Reversal columns shared by vendor_payments and supplier_credits.
SQL_REVERSAL_CHANGED = """(
       COALESCE(NEW.reversed_by, '') <> COALESCE(OLD.reversed_by, '')
    OR COALESCE(NEW.reversal_date, '1900-01-01') <> COALESCE(OLD.reversal_date, '1900-01-01')
    OR COALESCE(NEW.reversal_reason, '') <> COALESCE(OLD.reversal_reason, '')
    OR COALESCE(NEW.reversal_journal_entry_id, -1) <> COALESCE(OLD.reversal_journal_entry_id, -1)
)"""
SQL_REVERSAL_INCOMPLETE = """(
       NEW.reversed_by IS NULL
    OR NEW.reversal_date IS NULL
    OR NEW.reversal_journal_entry_id IS NULL
)"""
SQL_REVERSAL_PRESENT = """(
       NEW.reversed_by IS NOT NULL
    OR NEW.reversed_at IS NOT NULL
    OR NEW.reversal_date IS NOT NULL
    OR NEW.reversal_reason IS NOT NULL
    OR NEW.reversal_journal_entry_id IS NOT NULL
)"""

SQL_PAYMENT_FROZEN_V1 = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.payment_date <> OLD.payment_date
    OR NEW.amount <> OLD.amount
    OR NEW.cash_account_id <> OLD.cash_account_id
    OR (OLD.journal_entry_id IS NOT NULL
        AND COALESCE(NEW.journal_entry_id, -1) <> OLD.journal_entry_id)
)"""
SQL_PAYMENT_FROZEN_V2 = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.payment_date <> OLD.payment_date
    OR NEW.amount <> OLD.amount
    OR NEW.cash_account_id <> OLD.cash_account_id
    OR COALESCE(NEW.corrects_payment_id, -1) <> COALESCE(OLD.corrects_payment_id, -1)
    OR (OLD.journal_entry_id IS NOT NULL
        AND COALESCE(NEW.journal_entry_id, -1) <> OLD.journal_entry_id)
)"""
# Everything that is wrong with a payment reversal, as one predicate.
SQL_PAYMENT_REVERSAL_INVALID = f"""(
       (NEW.status <> OLD.status AND NOT (OLD.status = 'recorded' AND NEW.status = 'reversed'))
    OR (OLD.status = 'reversed' AND {SQL_REVERSAL_CHANGED})
    OR (NEW.status = 'reversed' AND {SQL_REVERSAL_INCOMPLETE})
    OR (NEW.status = 'recorded' AND {SQL_REVERSAL_PRESENT})
)"""
SQL_CREDIT_REVERSAL_INVALID = f"""(
       (OLD.status = 'reversed' AND {SQL_REVERSAL_CHANGED})
    OR (NEW.status = 'reversed' AND {SQL_REVERSAL_INCOMPLETE})
    OR (NEW.status <> 'reversed' AND {SQL_REVERSAL_PRESENT})
)"""


def sql_normalize(expr: str, chr_fn: str) -> str:
    """Canonical number: whitespace -> single spaces, trimmed, upper-case.

    Same algorithm as ``app.modules.payables.models.sql_normalize``; ``chr_fn`` is
    ``char`` on SQLite and ``chr`` on PostgreSQL.
    """
    out = expr
    for code in (9, 10, 11, 12, 13):
        out = f"REPLACE({out}, {chr_fn}({code}), ' ')"
    out = f"REPLACE({out}, ' ', {chr_fn}(1) || {chr_fn}(2))"
    out = f"REPLACE({out}, {chr_fn}(2) || {chr_fn}(1), '')"
    out = f"REPLACE({out}, {chr_fn}(1) || {chr_fn}(2), ' ')"
    return f"UPPER(TRIM({out}))"


def _cents(expr: str) -> str:
    return f"CAST(ROUND({expr} * 100) AS INTEGER)"


# Settled amount of a bill: allocations of payments still in force + credit applications.
SQLITE_BILL_SETTLED = f"""(
    (SELECT COALESCE(SUM({_cents("a.amount")}), 0)
       FROM payment_allocations a JOIN vendor_payments p ON p.id = a.payment_id
      WHERE a.bill_id = {{ref}}.bill_id AND p.status = 'recorded')
  + (SELECT COALESCE(SUM({_cents("amount")}), 0)
       FROM credit_applications WHERE bill_id = {{ref}}.bill_id)
)"""
SQLITE_BILL_TOTAL = f"(SELECT {_cents('total')} FROM supplier_bills WHERE id = {{ref}}.bill_id)"
SQLITE_BILL_STATUS = "(SELECT status FROM supplier_bills WHERE id = {ref}.bill_id)"
SQLITE_CREDIT_STATUS = "(SELECT status FROM supplier_credits WHERE id = {ref}.credit_id)"

PG_BILL_SETTLED = """(
    (SELECT COALESCE(SUM(a.amount), 0)
       FROM payment_allocations a JOIN vendor_payments p ON p.id = a.payment_id
      WHERE a.bill_id = NEW.bill_id AND p.status = 'recorded')
  + (SELECT COALESCE(SUM(amount), 0) FROM credit_applications WHERE bill_id = NEW.bill_id)
)"""


# --- PostgreSQL ----------------------------------------------------------------------------------

PG_NORMALIZE_FUNCTION = f"""
CREATE FUNCTION fms_ap_normalize_number(value text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT {sql_normalize("value", "chr")}
$$
"""

PG_FUNCTIONS_NEW = {
    "fms_ap_supplier_bills_normalize": """
CREATE FUNCTION fms_ap_supplier_bills_normalize() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.invoice_number_normalized := fms_ap_normalize_number(NEW.invoice_number);
    RETURN NEW;
END
$$
""",
    "fms_ap_supplier_credits_normalize": """
CREATE FUNCTION fms_ap_supplier_credits_normalize() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.credit_number_normalized := fms_ap_normalize_number(NEW.credit_number);
    RETURN NEW;
END
$$
""",
    "fms_ap_supplier_credits_guard": f"""
CREATE FUNCTION fms_ap_supplier_credits_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_CREDIT_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {SQL_CREDIT_TRANSITION} THEN
        RAISE EXCEPTION '{MSG_CREDIT_BAD_TRANSITION}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND {SQL_CREDIT_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{MSG_CREDIT_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {SQL_CREDIT_REVERSAL_INVALID} THEN
        RAISE EXCEPTION '{MSG_CREDIT_REVERSAL_INVALID}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'reversed' AND OLD.status <> 'reversed'
       AND EXISTS (SELECT 1 FROM credit_applications WHERE credit_id = NEW.id) THEN
        RAISE EXCEPTION '{MSG_CREDIT_REVERSE_APPLIED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_supplier_credit_lines_guard": f"""
CREATE FUNCTION fms_ap_supplier_credit_lines_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    credit_status varchar;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO credit_status FROM supplier_credits WHERE id = OLD.credit_id;
    ELSE
        SELECT status INTO credit_status FROM supplier_credits WHERE id = NEW.credit_id;
    END IF;
    IF credit_status IS NOT NULL AND credit_status <> 'draft' THEN
        RAISE EXCEPTION '{MSG_CREDIT_LINES_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_credit_applications_guard": f"""
CREATE FUNCTION fms_ap_credit_applications_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
    bill_total numeric;
    credit_status varchar;
    credit_total numeric;
    credit_applied numeric;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION '{MSG_APPLICATION_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    SELECT status, total INTO bill_status, bill_total
      FROM supplier_bills WHERE id = NEW.bill_id;
    IF bill_status NOT IN ('approved', 'partially_paid') THEN
        RAISE EXCEPTION '{MSG_APPLY_BILL_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total INTO credit_status, credit_total
      FROM supplier_credits WHERE id = NEW.credit_id;
    IF credit_status NOT IN ('approved', 'partially_applied') THEN
        RAISE EXCEPTION '{MSG_APPLY_CREDIT_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(SUM(amount), 0) INTO credit_applied
      FROM credit_applications WHERE credit_id = NEW.credit_id;
    IF credit_applied + NEW.amount > credit_total THEN
        RAISE EXCEPTION '{MSG_APPLY_EXCEEDS_CREDIT}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {PG_BILL_SETTLED} + NEW.amount > bill_total THEN
        RAISE EXCEPTION '{MSG_APPLY_EXCEEDS_BILL}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

# Replacements of fmspayables01 functions (CREATE OR REPLACE keeps the triggers).
PG_FUNCTIONS_V2 = {
    "fms_ap_supplier_bills_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_supplier_bills_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_BILL_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {SQL_BILL_TRANSITION_V2} THEN
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
    "fms_ap_payment_allocations_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_payment_allocations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
    bill_total numeric;
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
    IF {PG_BILL_SETTLED} + NEW.amount > bill_total THEN
        RAISE EXCEPTION '{MSG_ALLOC_OVERPAY}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_vendor_payments_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_vendor_payments_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR {SQL_PAYMENT_FROZEN_V2} THEN
        RAISE EXCEPTION '{MSG_PAYMENT_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {SQL_PAYMENT_REVERSAL_INVALID} THEN
        RAISE EXCEPTION '{MSG_PAYMENT_REVERSAL_INVALID}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

# fmspayables01 bodies, restored on downgrade.
PG_FUNCTIONS_V1 = {
    "fms_ap_supplier_bills_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_supplier_bills_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{MSG_BILL_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {SQL_BILL_TRANSITION_V1} THEN
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
    "fms_ap_payment_allocations_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_payment_allocations_guard() RETURNS trigger
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
CREATE OR REPLACE FUNCTION fms_ap_vendor_payments_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR {SQL_PAYMENT_FROZEN_V1} THEN
        RAISE EXCEPTION '{MSG_PAYMENT_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
}

PG_TRIGGERS_NEW = {
    "trg_ap_supplier_bills_normalize": """
CREATE TRIGGER trg_ap_supplier_bills_normalize
BEFORE INSERT OR UPDATE ON supplier_bills
FOR EACH ROW EXECUTE FUNCTION fms_ap_supplier_bills_normalize()
""",
    "trg_ap_supplier_credits_normalize": """
CREATE TRIGGER trg_ap_supplier_credits_normalize
BEFORE INSERT OR UPDATE ON supplier_credits
FOR EACH ROW EXECUTE FUNCTION fms_ap_supplier_credits_normalize()
""",
    "trg_ap_supplier_credits_guard": """
CREATE TRIGGER trg_ap_supplier_credits_guard
BEFORE UPDATE OR DELETE ON supplier_credits
FOR EACH ROW EXECUTE FUNCTION fms_ap_supplier_credits_guard()
""",
    "trg_ap_supplier_credit_lines_guard": """
CREATE TRIGGER trg_ap_supplier_credit_lines_guard
BEFORE INSERT OR UPDATE OR DELETE ON supplier_credit_lines
FOR EACH ROW EXECUTE FUNCTION fms_ap_supplier_credit_lines_guard()
""",
    "trg_ap_credit_applications_guard": """
CREATE TRIGGER trg_ap_credit_applications_guard
BEFORE INSERT OR UPDATE OR DELETE ON credit_applications
FOR EACH ROW EXECUTE FUNCTION fms_ap_credit_applications_guard()
""",
}


# --- SQLite -------------------------------------------------------------------------------------


def _sqlite_normalize_triggers(table: str, number_col: str, normalized_col: str) -> dict[str, str]:
    """AFTER triggers rewriting the canonical column when the supplied value is wrong.

    SQLite cannot change NEW in a BEFORE trigger, so the row is corrected right after
    the write. The UNIQUE constraint is re-checked by that inner UPDATE, which aborts
    the whole triggering statement when the corrected value is a duplicate.
    """
    norm = sql_normalize(f"NEW.{number_col}", "char")
    body = f"""
WHEN NEW.{normalized_col} IS NOT {norm}
BEGIN
    UPDATE {table} SET {normalized_col} = {norm} WHERE id = NEW.id;
END
"""
    return {
        f"trg_ap_{table}_normalize_insert": f"""
CREATE TRIGGER trg_ap_{table}_normalize_insert
AFTER INSERT ON {table} FOR EACH ROW{body}""",
        f"trg_ap_{table}_normalize_update": f"""
CREATE TRIGGER trg_ap_{table}_normalize_update
AFTER UPDATE OF {number_col}, {normalized_col} ON {table} FOR EACH ROW{body}""",
    }


def _sqlite_status_guards(table: str, parent: str, parent_col: str, condition: str, msg: str):
    status = f"(SELECT status FROM {parent} WHERE id = {{ref}}.{parent_col})"
    return {
        f"trg_ap_{table}_{event.lower()}_guard": f"""
CREATE TRIGGER trg_ap_{table}_{event.lower()}_guard
BEFORE {event} ON {table} FOR EACH ROW
WHEN {status.format(ref=ref)} {condition}
BEGIN
    SELECT RAISE(ABORT, '{msg}');
END
"""
        for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD"))
    }


SQLITE_BILLS_TRANSITION_V1 = f"""
CREATE TRIGGER trg_ap_supplier_bills_transition
BEFORE UPDATE OF status ON supplier_bills FOR EACH ROW
WHEN OLD.status <> NEW.status AND NOT {SQL_BILL_TRANSITION_V1}
BEGIN
    SELECT RAISE(ABORT, '{MSG_BILL_BAD_TRANSITION}');
END
"""
SQLITE_BILLS_TRANSITION_V2 = SQLITE_BILLS_TRANSITION_V1.replace(
    SQL_BILL_TRANSITION_V1, SQL_BILL_TRANSITION_V2
)

SQLITE_ALLOC_OVERPAY_V1 = f"""
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
"""
SQLITE_ALLOC_OVERPAY_V2 = f"""
CREATE TRIGGER trg_ap_payment_allocations_no_overpay
BEFORE INSERT ON payment_allocations FOR EACH ROW
WHEN {SQLITE_BILL_SETTLED.format(ref="NEW")} + {_cents("NEW.amount")}
   > {SQLITE_BILL_TOTAL.format(ref="NEW")}
BEGIN
    SELECT RAISE(ABORT, '{MSG_ALLOC_OVERPAY}');
END
"""

# vendor_payments triggers (v1 set and v2 set). SQLite drops a table's triggers when
# Alembic's batch mode rebuilds the table, so the whole set is (re)created explicitly.
SQLITE_PAYMENT_TRIGGERS_V1 = {
    "trg_ap_vendor_payments_frozen": f"""
CREATE TRIGGER trg_ap_vendor_payments_frozen
BEFORE UPDATE ON vendor_payments FOR EACH ROW
WHEN {SQL_PAYMENT_FROZEN_V1}
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
_V1_FROZEN = SQLITE_PAYMENT_TRIGGERS_V1["trg_ap_vendor_payments_frozen"]
SQLITE_PAYMENT_TRIGGERS_V2 = {
    "trg_ap_vendor_payments_frozen": _V1_FROZEN.replace(
        SQL_PAYMENT_FROZEN_V1, SQL_PAYMENT_FROZEN_V2
    ),
    "trg_ap_vendor_payments_no_delete": SQLITE_PAYMENT_TRIGGERS_V1[
        "trg_ap_vendor_payments_no_delete"
    ],
    "trg_ap_vendor_payments_reversal": f"""
CREATE TRIGGER trg_ap_vendor_payments_reversal
BEFORE UPDATE ON vendor_payments FOR EACH ROW
WHEN {SQL_PAYMENT_REVERSAL_INVALID}
BEGIN
    SELECT RAISE(ABORT, '{MSG_PAYMENT_REVERSAL_INVALID}');
END
""",
}

SQLITE_TRIGGERS_NEW = {
    **_sqlite_normalize_triggers("supplier_bills", "invoice_number", "invoice_number_normalized"),
    **_sqlite_normalize_triggers("supplier_credits", "credit_number", "credit_number_normalized"),
    "trg_ap_supplier_credits_transition": f"""
CREATE TRIGGER trg_ap_supplier_credits_transition
BEFORE UPDATE OF status ON supplier_credits FOR EACH ROW
WHEN OLD.status <> NEW.status AND NOT {SQL_CREDIT_TRANSITION}
BEGIN
    SELECT RAISE(ABORT, '{MSG_CREDIT_BAD_TRANSITION}');
END
""",
    "trg_ap_supplier_credits_frozen": f"""
CREATE TRIGGER trg_ap_supplier_credits_frozen
BEFORE UPDATE ON supplier_credits FOR EACH ROW
WHEN OLD.status <> 'draft' AND {SQL_CREDIT_FROZEN_CHANGED}
BEGIN
    SELECT RAISE(ABORT, '{MSG_CREDIT_FROZEN}');
END
""",
    "trg_ap_supplier_credits_reversal": f"""
CREATE TRIGGER trg_ap_supplier_credits_reversal
BEFORE UPDATE ON supplier_credits FOR EACH ROW
WHEN {SQL_CREDIT_REVERSAL_INVALID}
BEGIN
    SELECT RAISE(ABORT, '{MSG_CREDIT_REVERSAL_INVALID}');
END
""",
    "trg_ap_supplier_credits_reverse_applied": f"""
CREATE TRIGGER trg_ap_supplier_credits_reverse_applied
BEFORE UPDATE OF status ON supplier_credits FOR EACH ROW
WHEN NEW.status = 'reversed' AND OLD.status <> 'reversed'
 AND EXISTS (SELECT 1 FROM credit_applications WHERE credit_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, '{MSG_CREDIT_REVERSE_APPLIED}');
END
""",
    "trg_ap_supplier_credits_no_delete": f"""
CREATE TRIGGER trg_ap_supplier_credits_no_delete
BEFORE DELETE ON supplier_credits FOR EACH ROW
WHEN OLD.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, '{MSG_CREDIT_NO_DELETE}');
END
""",
    **_sqlite_status_guards(
        "supplier_credit_lines",
        "supplier_credits",
        "credit_id",
        "<> 'draft'",
        MSG_CREDIT_LINES_FROZEN,
    ),
    "trg_ap_credit_applications_bill_open": f"""
CREATE TRIGGER trg_ap_credit_applications_bill_open
BEFORE INSERT ON credit_applications FOR EACH ROW
WHEN {SQLITE_BILL_STATUS.format(ref="NEW")} NOT IN ('approved', 'partially_paid')
BEGIN
    SELECT RAISE(ABORT, '{MSG_APPLY_BILL_NOT_OPEN}');
END
""",
    "trg_ap_credit_applications_credit_open": f"""
CREATE TRIGGER trg_ap_credit_applications_credit_open
BEFORE INSERT ON credit_applications FOR EACH ROW
WHEN {SQLITE_CREDIT_STATUS.format(ref="NEW")} NOT IN ('approved', 'partially_applied')
BEGIN
    SELECT RAISE(ABORT, '{MSG_APPLY_CREDIT_NOT_OPEN}');
END
""",
    "trg_ap_credit_applications_no_overapply_credit": f"""
CREATE TRIGGER trg_ap_credit_applications_no_overapply_credit
BEFORE INSERT ON credit_applications FOR EACH ROW
WHEN (
    SELECT COALESCE(SUM({_cents("amount")}), 0)
      FROM credit_applications WHERE credit_id = NEW.credit_id
) + {_cents("NEW.amount")} > (
    SELECT {_cents("total")} FROM supplier_credits WHERE id = NEW.credit_id
)
BEGIN
    SELECT RAISE(ABORT, '{MSG_APPLY_EXCEEDS_CREDIT}');
END
""",
    "trg_ap_credit_applications_no_overapply_bill": f"""
CREATE TRIGGER trg_ap_credit_applications_no_overapply_bill
BEFORE INSERT ON credit_applications FOR EACH ROW
WHEN {SQLITE_BILL_SETTLED.format(ref="NEW")} + {_cents("NEW.amount")}
   > {SQLITE_BILL_TOTAL.format(ref="NEW")}
BEGIN
    SELECT RAISE(ABORT, '{MSG_APPLY_EXCEEDS_BILL}');
END
""",
    "trg_ap_credit_applications_no_update": f"""
CREATE TRIGGER trg_ap_credit_applications_no_update
BEFORE UPDATE ON credit_applications FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, '{MSG_APPLICATION_IMMUTABLE}');
END
""",
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for payables triggers: {name}")
    return name


def _sqlite_drop(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


# --- upgrade / downgrade ------------------------------------------------------------------------


def upgrade() -> None:
    dialect = _dialect()

    # --- vendor_payments: status and reversal columns
    with op.batch_alter_table("vendor_payments") as batch:
        batch.add_column(
            sa.Column("status", sa.String(20), nullable=False, server_default="recorded")
        )
        batch.add_column(sa.Column("reversed_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reversal_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("reversal_reason", sa.String(500), nullable=True))
        batch.add_column(sa.Column("reversal_journal_entry_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("corrects_payment_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_vendor_payments_reversal_journal_entry_id_journal_entries"),
            "journal_entries",
            ["reversal_journal_entry_id"],
            ["id"],
        )
        batch.create_foreign_key(
            op.f("fk_vendor_payments_corrects_payment_id_vendor_payments"),
            "vendor_payments",
            ["corrects_payment_id"],
            ["id"],
        )
        batch.create_check_constraint(
            op.f("ck_vendor_payments_status_valid"), "status IN ('recorded', 'reversed')"
        )
        batch.create_index(op.f("ix_vendor_payments_status"), ["status"])

    # --- supplier credits
    op.create_table(
        "supplier_credits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("credit_number", sa.String(length=80), nullable=False),
        sa.Column("credit_number_normalized", sa.String(length=80), nullable=False),
        sa.Column("credit_date", sa.Date(), nullable=False),
        sa.Column("total", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("related_bill_id", sa.Integer(), nullable=True),
        sa.Column("payable_account_id", sa.Integer(), nullable=True),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.String(length=100), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("reversed_by", sa.String(length=100), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_date", sa.Date(), nullable=True),
        sa.Column("reversal_reason", sa.String(length=500), nullable=True),
        sa.Column("reversal_journal_entry_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("total > 0", name=op.f("ck_supplier_credits_total_positive")),
        sa.CheckConstraint(
            "ROUND(total, 2) = total", name=op.f("ck_supplier_credits_total_whole_cents")
        ),
        sa.CheckConstraint(
            "kind IN ('credit_note', 'adjustment')", name=op.f("ck_supplier_credits_kind_valid")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'partially_applied', 'applied', "
            "'reversed')",
            name=op.f("ck_supplier_credits_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_supplier_credits_journal_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["payable_account_id"],
            ["accounts.id"],
            name=op.f("fk_supplier_credits_payable_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["related_bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_supplier_credits_related_bill_id_supplier_bills"),
        ),
        sa.ForeignKeyConstraint(
            ["reversal_journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_supplier_credits_reversal_journal_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"], ["vendors.id"], name=op.f("fk_supplier_credits_vendor_id_vendors")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_credits")),
        sa.UniqueConstraint("vendor_id", "credit_number", name="uq_supplier_credits_vendor_number"),
        sa.UniqueConstraint(
            "vendor_id",
            "credit_number_normalized",
            name="uq_supplier_credits_vendor_number_normalized",
        ),
    )
    op.create_index(op.f("ix_supplier_credits_credit_date"), "supplier_credits", ["credit_date"])
    op.create_index(op.f("ix_supplier_credits_status"), "supplier_credits", ["status"])

    op.create_table(
        "supplier_credit_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("credit_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_supplier_credit_lines_quantity_positive")
        ),
        sa.CheckConstraint(
            "unit_price > 0", name=op.f("ck_supplier_credit_lines_unit_price_positive")
        ),
        sa.CheckConstraint(
            "line_total > 0", name=op.f("ck_supplier_credit_lines_line_total_positive")
        ),
        sa.CheckConstraint(
            "ROUND(line_total, 2) = line_total",
            name=op.f("ck_supplier_credit_lines_line_total_whole_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_supplier_credit_lines_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["credit_id"],
            ["supplier_credits.id"],
            name=op.f("fk_supplier_credit_lines_credit_id_supplier_credits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_credit_lines")),
        sa.UniqueConstraint(
            "credit_id", "line_no", name="uq_supplier_credit_lines_credit_line_no"
        ),
    )

    op.create_table(
        "credit_applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("credit_id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("application_date", sa.Date(), nullable=False),
        sa.Column("applied_by", sa.String(length=100), nullable=False),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_credit_applications_amount_positive")),
        sa.CheckConstraint(
            "ROUND(amount, 2) = amount", name=op.f("ck_credit_applications_amount_whole_cents")
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_credit_applications_bill_id_supplier_bills"),
        ),
        sa.ForeignKeyConstraint(
            ["credit_id"],
            ["supplier_credits.id"],
            name=op.f("fk_credit_applications_credit_id_supplier_credits"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_applications")),
        sa.UniqueConstraint("credit_id", "bill_id", name="uq_credit_applications_credit_bill"),
    )
    op.create_index(op.f("ix_credit_applications_bill_id"), "credit_applications", ["bill_id"])

    # --- rules
    if dialect == "postgresql":
        op.execute(PG_NORMALIZE_FUNCTION)
        for group in (PG_FUNCTIONS_NEW, PG_FUNCTIONS_V2, PG_TRIGGERS_NEW):
            for ddl in group.values():
                op.execute(ddl)
    else:
        _sqlite_drop(
            [
                "trg_ap_supplier_bills_transition",
                "trg_ap_payment_allocations_no_overpay",
                *SQLITE_PAYMENT_TRIGGERS_V1,
            ]
        )
        op.execute(SQLITE_BILLS_TRANSITION_V2)
        op.execute(SQLITE_ALLOC_OVERPAY_V2)
        for ddl in (*SQLITE_PAYMENT_TRIGGERS_V2.values(), *SQLITE_TRIGGERS_NEW.values()):
            op.execute(ddl)


def downgrade() -> None:
    dialect = _dialect()

    if dialect == "postgresql":
        op.execute("DROP TRIGGER trg_ap_supplier_bills_normalize ON supplier_bills")
        for ddl in PG_FUNCTIONS_V1.values():
            op.execute(ddl)
    else:
        _sqlite_drop(
            [
                name
                for name in SQLITE_TRIGGERS_NEW
                if name.startswith("trg_ap_supplier_bills_")
            ]
        )
        _sqlite_drop(["trg_ap_supplier_bills_transition", "trg_ap_payment_allocations_no_overpay"])
        op.execute(SQLITE_BILLS_TRANSITION_V1)
        op.execute(SQLITE_ALLOC_OVERPAY_V1)

    op.drop_index(op.f("ix_credit_applications_bill_id"), table_name="credit_applications")
    op.drop_table("credit_applications")
    op.drop_table("supplier_credit_lines")
    op.drop_index(op.f("ix_supplier_credits_status"), table_name="supplier_credits")
    op.drop_index(op.f("ix_supplier_credits_credit_date"), table_name="supplier_credits")
    op.drop_table("supplier_credits")

    if dialect == "postgresql":
        for name in PG_FUNCTIONS_NEW:
            op.execute(f"DROP FUNCTION {name}()")
        op.execute("DROP FUNCTION fms_ap_normalize_number(text)")

    with op.batch_alter_table("vendor_payments") as batch:
        batch.drop_index(op.f("ix_vendor_payments_status"))
        batch.drop_constraint(op.f("ck_vendor_payments_status_valid"), type_="check")
        batch.drop_constraint(
            op.f("fk_vendor_payments_corrects_payment_id_vendor_payments"), type_="foreignkey"
        )
        batch.drop_constraint(
            op.f("fk_vendor_payments_reversal_journal_entry_id_journal_entries"),
            type_="foreignkey",
        )
        for column in (
            "corrects_payment_id",
            "reversal_journal_entry_id",
            "reversal_reason",
            "reversal_date",
            "reversed_at",
            "reversed_by",
            "status",
        ):
            batch.drop_column(column)

    if dialect == "sqlite":
        # The batch rebuild above dropped every trigger on vendor_payments.
        _sqlite_drop(SQLITE_PAYMENT_TRIGGERS_V2)
        for ddl in SQLITE_PAYMENT_TRIGGERS_V1.values():
            op.execute(ddl)
