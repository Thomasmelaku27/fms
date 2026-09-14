"""Accounts payable: durable settlement history, payment immutability, advances,
vendor master data, holds, discounts, duplicate review, AP period locks.

Revises ``fmspayables02``; touches only payables tables.

Schema
------
* ``credit_applications``: ``unapplication_date`` / ``unapplied_by`` / ``unapplied_at`` /
  ``unapplication_reason``. Unapplying *closes* a row instead of deleting it.
* ``payment_allocations``: ``kind`` (original / advance), ``allocation_date``,
  ``allocated_by`` (back-filled from the payment), unallocation stamps.
* ``vendor_payments``: ``payable_account_id`` -- AP control account that carries an
  unapplied (advance) remainder.
* ``vendors``: ``status`` (active / on_hold / suspended / inactive, back-filled from
  ``is_active``), status reason / actor / time, ``preferred_method``,
  ``bank_reference``, ``discount_policy_id``.
* ``supplier_bills``: discount snapshot (``discount_date``, ``discount_amount``,
  ``discount_account_id``); indexes on vendor and invoice date.
* New tables ``discount_policies``, ``bill_holds``, ``duplicate_flags``,
  ``bill_discounts``, ``ap_period_locks``; supporting indexes.

Database rules (replacing the ``fmspayables02`` trigger bodies where noted)
--------------------------------------------------------------------------
* recorded payments: *every* descriptive and financial field is frozen, including
  method, reference, recorded-by, idempotency key and payable account. Corrections
  are explicit reversal / replacement workflows.
* payment allocations: ``original`` rows are immutable; ``advance`` rows may be
  unallocated exactly once (dated, with actor). Never deleted. Advance rows require
  a recorded payment with enough unapplied remainder and a bill posting to the
  payment's payable account. Allocations to held / disputed bills are refused.
* credit applications: unapplied exactly once (dated, with actor); never deleted.
* bill discounts: append-only, reversed exactly once, capped by the bill's
  entitlement and remaining amount.
* bill holds: released exactly once, never deleted. Duplicate flags: reviewed once.
* settlement ceilings count only rows in force (recorded payments, open
  allocations / applications / discounts).
* AP period locks are append-only; approvals, payments, allocations,
  applications, discounts, reversals, unallocations and unapplications dated on
  or before the latest ``closed_through`` are refused.
* bills with pending *blocking* duplicate flags cannot be approved (advisory flags
  only inform the reviewer).

Downgrade
---------
Restores the ``fmspayables02`` trigger bodies (loaded from that revision file) and
drops the new columns / tables. **Irreversible**: closed (unapplied) credit
applications and unallocated advance allocations are deleted (the old schema had
no way to represent them); discounts, holds, duplicate flags, period locks,
discount policies and vendor status details are lost; payments with an unapplied
remainder keep their rows but the old application expected allocations to equal
the amount.

Revision ID: fmspayables03
Revises: fmspayables02
Create Date: 2026-11-01 00:00:00
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "fmspayables03"
down_revision: str | None = "fmspayables02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sibling(filename: str):
    """Load a sibling revision module (its frozen DDL is reused on downgrade)."""
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V2 = _sibling("20261020_fmspayables02_credits_reversals.py")

# --- messages -----------------------------------------------------------------------------------

MSG_PERIOD_CLOSED = "the AP period containing this date is closed"
MSG_BILL_FROZEN = V2.MSG_BILL_FROZEN
MSG_BILL_DUPLICATE_PENDING = "a bill with unresolved blocking duplicate flags cannot be approved"
MSG_BILL_ON_HOLD = "payments cannot be allocated to a bill that is on hold or disputed"
MSG_PAYMENT_IMMUTABLE = V2.MSG_PAYMENT_IMMUTABLE
MSG_ALLOC_IMMUTABLE = (
    "payment allocations cannot be modified or deleted; advance allocations may be unallocated once"
)
MSG_ALLOC_BILL_NOT_OPEN = V2.MSG_ALLOC_BILL_NOT_OPEN
MSG_ALLOC_OVERPAY = V2.MSG_ALLOC_OVERPAY
MSG_ALLOC_EXCEEDS_PAYMENT = "allocations exceed the payment amount"
MSG_ADVANCE_NOT_RECORDED = "advance allocations require a recorded payment"
MSG_ADVANCE_ACCOUNT_MISMATCH = (
    "advance allocations must target a bill posting to the payable account of the payment"
)
MSG_ADVANCE_EXCEEDS = "advance allocation exceeds the unapplied amount of the payment"
MSG_APPLICATION_IMMUTABLE = "credit applications cannot be modified; unapplying closes them once"
MSG_APPLICATION_NO_DELETE = "credit applications are never deleted; unapply them instead"
MSG_DISCOUNT_IMMUTABLE = "bill discounts cannot be modified or deleted; they are reversed once"
MSG_DISCOUNT_EXCEEDS = "discount exceeds the discount entitlement of the bill"
MSG_DISCOUNT_BILL_NOT_OPEN = "discounts can only be taken on approved or partially paid bills"
MSG_HOLD_IMMUTABLE = "bill holds cannot be modified or deleted; they are released once"
MSG_FLAG_IMMUTABLE = "duplicate flags are reviewed once and never re-opened"
MSG_PERIOD_LOCK_IMMUTABLE = "AP period locks are append-only"

# --- shared fragments -------------------------------------------------------------------------

SQL_BILL_FROZEN_V3 = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.invoice_number <> OLD.invoice_number
    OR NEW.invoice_number_normalized <> OLD.invoice_number_normalized
    OR NEW.invoice_date <> OLD.invoice_date
    OR NEW.due_date <> OLD.due_date
    OR NEW.total <> OLD.total
    OR COALESCE(NEW.payable_account_id, -1) <> COALESCE(OLD.payable_account_id, -1)
    OR COALESCE(NEW.journal_entry_id, -1) <> COALESCE(OLD.journal_entry_id, -1)
    OR COALESCE(NEW.discount_date, '1900-01-01') <> COALESCE(OLD.discount_date, '1900-01-01')
    OR NEW.discount_amount <> OLD.discount_amount
    OR COALESCE(NEW.discount_account_id, -1) <> COALESCE(OLD.discount_account_id, -1)
)"""

SQL_PAYMENT_FROZEN_V3 = """(
       NEW.vendor_id <> OLD.vendor_id
    OR NEW.payment_date <> OLD.payment_date
    OR NEW.amount <> OLD.amount
    OR NEW.method <> OLD.method
    OR COALESCE(NEW.reference, '') <> COALESCE(OLD.reference, '')
    OR NEW.cash_account_id <> OLD.cash_account_id
    OR NEW.recorded_by <> OLD.recorded_by
    OR COALESCE(NEW.idempotency_key, '') <> COALESCE(OLD.idempotency_key, '')
    OR COALESCE(NEW.payable_account_id, -1) <> COALESCE(OLD.payable_account_id, -1)
    OR COALESCE(NEW.corrects_payment_id, -1) <> COALESCE(OLD.corrects_payment_id, -1)
    OR (OLD.journal_entry_id IS NOT NULL
        AND COALESCE(NEW.journal_entry_id, -1) <> OLD.journal_entry_id)
)"""

# A dated close of an append-only row: exactly these columns change, exactly once.
SQL_ALLOC_FIXED_CHANGED = """(
       NEW.payment_id <> OLD.payment_id
    OR NEW.bill_id <> OLD.bill_id
    OR NEW.amount <> OLD.amount
    OR NEW.kind <> OLD.kind
    OR NEW.allocation_date <> OLD.allocation_date
    OR NEW.allocated_by <> OLD.allocated_by
)"""
SQL_ALLOC_UNALLOCATION_VALID = """(
        OLD.kind = 'advance'
    AND OLD.unallocation_date IS NULL
    AND NEW.unallocation_date IS NOT NULL
    AND NEW.unallocated_by IS NOT NULL
)"""
SQL_APP_FIXED_CHANGED = """(
       NEW.credit_id <> OLD.credit_id
    OR NEW.bill_id <> OLD.bill_id
    OR NEW.amount <> OLD.amount
    OR NEW.application_date <> OLD.application_date
    OR NEW.applied_by <> OLD.applied_by
)"""
SQL_APP_UNAPPLICATION_VALID = """(
        OLD.unapplication_date IS NULL
    AND NEW.unapplication_date IS NOT NULL
    AND NEW.unapplied_by IS NOT NULL
)"""
SQL_DISCOUNT_FIXED_CHANGED = """(
       NEW.bill_id <> OLD.bill_id
    OR COALESCE(NEW.payment_id, -1) <> COALESCE(OLD.payment_id, -1)
    OR NEW.amount <> OLD.amount
    OR NEW.taken_on <> OLD.taken_on
    OR NEW.taken_by <> OLD.taken_by
    OR NEW.discount_account_id <> OLD.discount_account_id
    OR (OLD.journal_entry_id IS NOT NULL
        AND COALESCE(NEW.journal_entry_id, -1) <> OLD.journal_entry_id)
)"""
SQL_DISCOUNT_REVERSAL_VALID = """(
        OLD.reversal_date IS NULL
    AND NEW.reversal_date IS NOT NULL
    AND NEW.reversed_by IS NOT NULL
    AND NEW.reversal_journal_entry_id IS NOT NULL
)"""
SQL_DISCOUNT_REVERSAL_UNCHANGED = """(
        COALESCE(NEW.reversal_date, '1900-01-01') = COALESCE(OLD.reversal_date, '1900-01-01')
    AND COALESCE(NEW.reversed_by, '') = COALESCE(OLD.reversed_by, '')
    AND COALESCE(NEW.reversal_journal_entry_id, -1) = COALESCE(OLD.reversal_journal_entry_id, -1)
)"""
SQL_HOLD_FIXED_CHANGED = """(
       NEW.bill_id <> OLD.bill_id
    OR NEW.kind <> OLD.kind
    OR NEW.reason <> OLD.reason
    OR NEW.placed_by <> OLD.placed_by
)"""
SQL_HOLD_RELEASE_VALID = """(
        OLD.released_at IS NULL
    AND NEW.released_at IS NOT NULL
    AND NEW.released_by IS NOT NULL
)"""
SQL_FLAG_REVIEW_VALID = """(
        OLD.status = 'pending'
    AND NEW.status IN ('cleared', 'confirmed')
    AND NEW.reviewed_by IS NOT NULL
    AND NEW.bill_id = OLD.bill_id
    AND NEW.suspected_bill_id = OLD.suspected_bill_id
)"""


def _cents(expr: str) -> str:
    return f"CAST(ROUND({expr} * 100) AS INTEGER)"


def _settled(ref: str, cents: bool) -> str:
    """Settled amount of {ref}.bill_id: allocations + applications + discounts in force."""
    f = _cents if cents else (lambda e: e)
    return f"""(
    (SELECT COALESCE(SUM({f("a.amount")}), 0)
       FROM payment_allocations a JOIN vendor_payments p ON p.id = a.payment_id
      WHERE a.bill_id = {ref}.bill_id AND p.status = 'recorded' AND a.unallocation_date IS NULL)
  + (SELECT COALESCE(SUM({f("amount")}), 0)
       FROM credit_applications WHERE bill_id = {ref}.bill_id AND unapplication_date IS NULL)
  + (SELECT COALESCE(SUM({f("amount")}), 0)
       FROM bill_discounts WHERE bill_id = {ref}.bill_id AND reversal_date IS NULL)
)"""


def _period_closed(date_expr: str) -> str:
    return f"({date_expr} <= (SELECT MAX(closed_through) FROM ap_period_locks))"


SQLITE_BILL_STATUS = "(SELECT status FROM supplier_bills WHERE id = {ref}.bill_id)"
SQLITE_BILL_TOTAL = f"(SELECT {_cents('total')} FROM supplier_bills WHERE id = {{ref}}.bill_id)"
SQLITE_BILL_HELD = (
    "EXISTS (SELECT 1 FROM bill_holds WHERE bill_id = {ref}.bill_id AND released_at IS NULL)"
)


# --- PostgreSQL ----------------------------------------------------------------------------------

PG_FUNCTIONS_V3 = {
    "fms_ap_period_closed": f"""
CREATE FUNCTION fms_ap_period_closed(d date) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT COALESCE({_period_closed("d")}, false)
$$
""",
    "fms_ap_supplier_bills_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_supplier_bills_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{V2.MSG_BILL_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {V2.SQL_BILL_TRANSITION_V2} THEN
        RAISE EXCEPTION '{V2.MSG_BILL_BAD_TRANSITION}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND {SQL_BILL_FROZEN_V3} THEN
        RAISE EXCEPTION '{MSG_BILL_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'draft' AND NEW.status = 'approved' THEN
        IF fms_ap_period_closed(NEW.invoice_date) THEN
            RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM duplicate_flags
                    WHERE bill_id = NEW.id AND status = 'pending' AND blocks_approval) THEN
            RAISE EXCEPTION '{MSG_BILL_DUPLICATE_PENDING}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_supplier_credits_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_supplier_credits_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '{V2.MSG_CREDIT_NO_DELETE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> NEW.status AND NOT {V2.SQL_CREDIT_TRANSITION} THEN
        RAISE EXCEPTION '{V2.MSG_CREDIT_BAD_TRANSITION}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'draft' AND {V2.SQL_CREDIT_FROZEN_CHANGED} THEN
        RAISE EXCEPTION '{V2.MSG_CREDIT_FROZEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {V2.SQL_CREDIT_REVERSAL_INVALID} THEN
        RAISE EXCEPTION '{V2.MSG_CREDIT_REVERSAL_INVALID}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'reversed' AND OLD.status <> 'reversed'
       AND EXISTS (SELECT 1 FROM credit_applications
                    WHERE credit_id = NEW.id AND unapplication_date IS NULL) THEN
        RAISE EXCEPTION '{V2.MSG_CREDIT_REVERSE_APPLIED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (OLD.status = 'draft' AND NEW.status = 'approved'
        AND fms_ap_period_closed(NEW.credit_date))
       OR (NEW.status = 'reversed' AND OLD.status <> 'reversed'
        AND fms_ap_period_closed(NEW.reversal_date)) THEN
        RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
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
    IF TG_OP = 'DELETE' OR {SQL_PAYMENT_FROZEN_V3} THEN
        RAISE EXCEPTION '{MSG_PAYMENT_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {V2.SQL_PAYMENT_REVERSAL_INVALID} THEN
        RAISE EXCEPTION '{V2.MSG_PAYMENT_REVERSAL_INVALID}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'reversed' AND OLD.status = 'recorded'
       AND fms_ap_period_closed(NEW.reversal_date) THEN
        RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_vendor_payments_insert_guard": f"""
CREATE FUNCTION fms_ap_vendor_payments_insert_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF fms_ap_period_closed(NEW.payment_date) THEN
        RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
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
    bill_account integer;
    pay_status varchar;
    pay_amount numeric;
    pay_account integer;
    pay_allocated numeric;
    pay_original numeric;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{MSG_ALLOC_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NOT {SQL_ALLOC_UNALLOCATION_VALID} OR {SQL_ALLOC_FIXED_CHANGED} THEN
            RAISE EXCEPTION '{MSG_ALLOC_IMMUTABLE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF fms_ap_period_closed(NEW.unallocation_date) THEN
            RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    -- INSERT
    IF fms_ap_period_closed(NEW.allocation_date) THEN
        RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total, payable_account_id INTO bill_status, bill_total, bill_account
      FROM supplier_bills WHERE id = NEW.bill_id;
    IF bill_status NOT IN ('approved', 'partially_paid') THEN
        RAISE EXCEPTION '{MSG_ALLOC_BILL_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM bill_holds WHERE bill_id = NEW.bill_id AND released_at IS NULL) THEN
        RAISE EXCEPTION '{MSG_BILL_ON_HOLD}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {_settled("NEW", False)} + NEW.amount > bill_total THEN
        RAISE EXCEPTION '{MSG_ALLOC_OVERPAY}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, amount, payable_account_id INTO pay_status, pay_amount, pay_account
      FROM vendor_payments WHERE id = NEW.payment_id;
    SELECT COALESCE(SUM(amount), 0) INTO pay_allocated
      FROM payment_allocations WHERE payment_id = NEW.payment_id AND unallocation_date IS NULL;
    IF NEW.kind = 'advance' THEN
        IF pay_status <> 'recorded' THEN
            RAISE EXCEPTION '{MSG_ADVANCE_NOT_RECORDED}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF pay_account IS NULL OR bill_account IS DISTINCT FROM pay_account THEN
            RAISE EXCEPTION '{MSG_ADVANCE_ACCOUNT_MISMATCH}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF pay_allocated + NEW.amount > pay_amount THEN
            RAISE EXCEPTION '{MSG_ADVANCE_EXCEEDS}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSE
        SELECT COALESCE(SUM(amount), 0) INTO pay_original
          FROM payment_allocations WHERE payment_id = NEW.payment_id AND kind = 'original';
        IF pay_original + NEW.amount > pay_amount THEN
            RAISE EXCEPTION '{MSG_ALLOC_EXCEEDS_PAYMENT}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_credit_applications_guard": f"""
CREATE OR REPLACE FUNCTION fms_ap_credit_applications_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
    bill_total numeric;
    credit_status varchar;
    credit_total numeric;
    credit_applied numeric;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{MSG_APPLICATION_NO_DELETE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NOT {SQL_APP_UNAPPLICATION_VALID} OR {SQL_APP_FIXED_CHANGED} THEN
            RAISE EXCEPTION '{MSG_APPLICATION_IMMUTABLE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF fms_ap_period_closed(NEW.unapplication_date) THEN
            RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF fms_ap_period_closed(NEW.application_date) THEN
        RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total INTO bill_status, bill_total
      FROM supplier_bills WHERE id = NEW.bill_id;
    IF bill_status NOT IN ('approved', 'partially_paid') THEN
        RAISE EXCEPTION '{V2.MSG_APPLY_BILL_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total INTO credit_status, credit_total
      FROM supplier_credits WHERE id = NEW.credit_id;
    IF credit_status NOT IN ('approved', 'partially_applied') THEN
        RAISE EXCEPTION '{V2.MSG_APPLY_CREDIT_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(SUM(amount), 0) INTO credit_applied
      FROM credit_applications WHERE credit_id = NEW.credit_id AND unapplication_date IS NULL;
    IF credit_applied + NEW.amount > credit_total THEN
        RAISE EXCEPTION '{V2.MSG_APPLY_EXCEEDS_CREDIT}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {_settled("NEW", False)} + NEW.amount > bill_total THEN
        RAISE EXCEPTION '{V2.MSG_APPLY_EXCEEDS_BILL}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_bill_discounts_guard": f"""
CREATE FUNCTION fms_ap_bill_discounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bill_status varchar;
    bill_total numeric;
    entitlement numeric;
    taken numeric;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{MSG_DISCOUNT_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF {SQL_DISCOUNT_FIXED_CHANGED}
           OR NOT ({SQL_DISCOUNT_REVERSAL_VALID} OR {SQL_DISCOUNT_REVERSAL_UNCHANGED}) THEN
            RAISE EXCEPTION '{MSG_DISCOUNT_IMMUTABLE}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.reversal_date IS NOT NULL AND OLD.reversal_date IS NULL
           AND fms_ap_period_closed(NEW.reversal_date) THEN
            RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF fms_ap_period_closed(NEW.taken_on) THEN
        RAISE EXCEPTION '{MSG_PERIOD_CLOSED}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, total, discount_amount INTO bill_status, bill_total, entitlement
      FROM supplier_bills WHERE id = NEW.bill_id;
    IF bill_status NOT IN ('approved', 'partially_paid') THEN
        RAISE EXCEPTION '{MSG_DISCOUNT_BILL_NOT_OPEN}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(SUM(amount), 0) INTO taken
      FROM bill_discounts WHERE bill_id = NEW.bill_id AND reversal_date IS NULL;
    IF taken + NEW.amount > entitlement THEN
        RAISE EXCEPTION '{MSG_DISCOUNT_EXCEEDS}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF {_settled("NEW", False)} + NEW.amount > bill_total THEN
        RAISE EXCEPTION '{MSG_ALLOC_OVERPAY}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_bill_holds_guard": f"""
CREATE FUNCTION fms_ap_bill_holds_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR {SQL_HOLD_FIXED_CHANGED} OR NOT {SQL_HOLD_RELEASE_VALID} THEN
        RAISE EXCEPTION '{MSG_HOLD_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_duplicate_flags_guard": f"""
CREATE FUNCTION fms_ap_duplicate_flags_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT {SQL_FLAG_REVIEW_VALID} THEN
        RAISE EXCEPTION '{MSG_FLAG_IMMUTABLE}'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$
""",
    "fms_ap_period_locks_guard": f"""
CREATE FUNCTION fms_ap_period_locks_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{MSG_PERIOD_LOCK_IMMUTABLE}'
        USING ERRCODE = 'integrity_constraint_violation';
END
$$
""",
}

PG_NEW_FUNCTIONS = (
    "fms_ap_period_closed",
    "fms_ap_vendor_payments_insert_guard",
    "fms_ap_bill_discounts_guard",
    "fms_ap_bill_holds_guard",
    "fms_ap_duplicate_flags_guard",
    "fms_ap_period_locks_guard",
)

PG_TRIGGERS_NEW = {
    "trg_ap_vendor_payments_insert_guard": """
CREATE TRIGGER trg_ap_vendor_payments_insert_guard
BEFORE INSERT ON vendor_payments
FOR EACH ROW EXECUTE FUNCTION fms_ap_vendor_payments_insert_guard()
""",
    "trg_ap_bill_discounts_guard": """
CREATE TRIGGER trg_ap_bill_discounts_guard
BEFORE INSERT OR UPDATE OR DELETE ON bill_discounts
FOR EACH ROW EXECUTE FUNCTION fms_ap_bill_discounts_guard()
""",
    "trg_ap_bill_holds_guard": """
CREATE TRIGGER trg_ap_bill_holds_guard
BEFORE UPDATE OR DELETE ON bill_holds
FOR EACH ROW EXECUTE FUNCTION fms_ap_bill_holds_guard()
""",
    "trg_ap_duplicate_flags_guard": """
CREATE TRIGGER trg_ap_duplicate_flags_guard
BEFORE UPDATE ON duplicate_flags
FOR EACH ROW EXECUTE FUNCTION fms_ap_duplicate_flags_guard()
""",
    "trg_ap_period_locks_guard": """
CREATE TRIGGER trg_ap_period_locks_guard
BEFORE UPDATE OR DELETE ON ap_period_locks
FOR EACH ROW EXECUTE FUNCTION fms_ap_period_locks_guard()
""",
}


# --- SQLite --------------------------------------------------------------------------------------


def _abort(name: str, event: str, table: str, when: str, msg: str, of: str = "") -> str:
    of_clause = f" OF {of}" if of else ""
    when_clause = f"WHEN {when}\n" if when else ""
    return f"""
CREATE TRIGGER {name}
BEFORE {event}{of_clause} ON {table} FOR EACH ROW
{when_clause}BEGIN
    SELECT RAISE(ABORT, '{msg}');
END
"""


# supplier_bills: the v2 set with the v3 frozen predicate plus approval checks.
SQLITE_BILL_TRIGGERS_V3 = {
    "trg_ap_supplier_bills_transition": V2.SQLITE_BILLS_TRANSITION_V2,
    "trg_ap_supplier_bills_frozen": _abort(
        "trg_ap_supplier_bills_frozen",
        "UPDATE",
        "supplier_bills",
        f"OLD.status <> 'draft' AND {SQL_BILL_FROZEN_V3}",
        MSG_BILL_FROZEN,
    ),
    "trg_ap_supplier_bills_no_delete": _abort(
        "trg_ap_supplier_bills_no_delete",
        "DELETE",
        "supplier_bills",
        "OLD.status <> 'draft'",
        V2.MSG_BILL_NO_DELETE,
    ),
    **{k: v for k, v in V2.SQLITE_TRIGGERS_NEW.items() if k.startswith("trg_ap_supplier_bills_")},
    "trg_ap_supplier_bills_approve_period": _abort(
        "trg_ap_supplier_bills_approve_period",
        "UPDATE",
        "supplier_bills",
        f"OLD.status = 'draft' AND NEW.status = 'approved' AND {_period_closed('NEW.invoice_date')}",
        MSG_PERIOD_CLOSED,
        of="status",
    ),
    "trg_ap_supplier_bills_approve_duplicates": _abort(
        "trg_ap_supplier_bills_approve_duplicates",
        "UPDATE",
        "supplier_bills",
        "OLD.status = 'draft' AND NEW.status = 'approved' AND EXISTS "
        "(SELECT 1 FROM duplicate_flags WHERE bill_id = NEW.id AND status = 'pending' AND blocks_approval)",
        MSG_BILL_DUPLICATE_PENDING,
        of="status",
    ),
}
SQLITE_BILL_TRIGGERS_V2 = {
    "trg_ap_supplier_bills_transition": V2.SQLITE_BILLS_TRANSITION_V2,
    "trg_ap_supplier_bills_frozen": _abort(
        "trg_ap_supplier_bills_frozen",
        "UPDATE",
        "supplier_bills",
        f"OLD.status <> 'draft' AND {V2.SQL_BILL_FROZEN_CHANGED}",
        MSG_BILL_FROZEN,
    ),
    "trg_ap_supplier_bills_no_delete": SQLITE_BILL_TRIGGERS_V3["trg_ap_supplier_bills_no_delete"],
    **{k: v for k, v in V2.SQLITE_TRIGGERS_NEW.items() if k.startswith("trg_ap_supplier_bills_")},
}

# supplier_credits: v2 set untouched; two period-lock triggers added.
SQLITE_CREDIT_TRIGGERS_NEW = {
    "trg_ap_supplier_credits_approve_period": _abort(
        "trg_ap_supplier_credits_approve_period",
        "UPDATE",
        "supplier_credits",
        f"OLD.status = 'draft' AND NEW.status = 'approved' AND {_period_closed('NEW.credit_date')}",
        MSG_PERIOD_CLOSED,
        of="status",
    ),
    "trg_ap_supplier_credits_reverse_period": _abort(
        "trg_ap_supplier_credits_reverse_period",
        "UPDATE",
        "supplier_credits",
        f"NEW.status = 'reversed' AND OLD.status <> 'reversed' AND {_period_closed('NEW.reversal_date')}",
        MSG_PERIOD_CLOSED,
        of="status",
    ),
    "trg_ap_supplier_credits_reverse_applied": _abort(
        "trg_ap_supplier_credits_reverse_applied",
        "UPDATE",
        "supplier_credits",
        "NEW.status = 'reversed' AND OLD.status <> 'reversed' AND EXISTS "
        "(SELECT 1 FROM credit_applications WHERE credit_id = NEW.id AND unapplication_date IS NULL)",
        V2.MSG_CREDIT_REVERSE_APPLIED,
        of="status",
    ),
}

# vendor_payments
SQLITE_PAYMENT_TRIGGERS_V3 = {
    "trg_ap_vendor_payments_frozen": _abort(
        "trg_ap_vendor_payments_frozen",
        "UPDATE",
        "vendor_payments",
        SQL_PAYMENT_FROZEN_V3,
        MSG_PAYMENT_IMMUTABLE,
    ),
    "trg_ap_vendor_payments_no_delete": V2.SQLITE_PAYMENT_TRIGGERS_V2[
        "trg_ap_vendor_payments_no_delete"
    ],
    "trg_ap_vendor_payments_reversal": V2.SQLITE_PAYMENT_TRIGGERS_V2[
        "trg_ap_vendor_payments_reversal"
    ],
    "trg_ap_vendor_payments_reverse_period": _abort(
        "trg_ap_vendor_payments_reverse_period",
        "UPDATE",
        "vendor_payments",
        f"NEW.status = 'reversed' AND OLD.status = 'recorded' AND {_period_closed('NEW.reversal_date')}",
        MSG_PERIOD_CLOSED,
        of="status",
    ),
    "trg_ap_vendor_payments_insert_period": _abort(
        "trg_ap_vendor_payments_insert_period",
        "INSERT",
        "vendor_payments",
        _period_closed("NEW.payment_date"),
        MSG_PERIOD_CLOSED,
    ),
}

# payment_allocations
_PAY = "(SELECT {col} FROM vendor_payments WHERE id = NEW.payment_id)"
_PAY_ALLOCATED = (
    f"(SELECT COALESCE(SUM({_cents('amount')}), 0) FROM payment_allocations "
    "WHERE payment_id = NEW.payment_id AND unallocation_date IS NULL)"
)
_PAY_ORIGINAL = (
    f"(SELECT COALESCE(SUM({_cents('amount')}), 0) FROM payment_allocations "
    "WHERE payment_id = NEW.payment_id AND kind = 'original')"
)
SQLITE_ALLOCATION_TRIGGERS_V3 = {
    "trg_ap_payment_allocations_no_delete": _abort(
        "trg_ap_payment_allocations_no_delete",
        "DELETE",
        "payment_allocations",
        "",
        MSG_ALLOC_IMMUTABLE,
    ),
    "trg_ap_payment_allocations_update_guard": _abort(
        "trg_ap_payment_allocations_update_guard",
        "UPDATE",
        "payment_allocations",
        f"NOT {SQL_ALLOC_UNALLOCATION_VALID} OR {SQL_ALLOC_FIXED_CHANGED}",
        MSG_ALLOC_IMMUTABLE,
    ),
    "trg_ap_payment_allocations_unallocate_period": _abort(
        "trg_ap_payment_allocations_unallocate_period",
        "UPDATE",
        "payment_allocations",
        f"OLD.unallocation_date IS NULL AND {_period_closed('NEW.unallocation_date')}",
        MSG_PERIOD_CLOSED,
    ),
    "trg_ap_payment_allocations_insert_period": _abort(
        "trg_ap_payment_allocations_insert_period",
        "INSERT",
        "payment_allocations",
        _period_closed("NEW.allocation_date"),
        MSG_PERIOD_CLOSED,
    ),
    "trg_ap_payment_allocations_bill_open": _abort(
        "trg_ap_payment_allocations_bill_open",
        "INSERT",
        "payment_allocations",
        f"{SQLITE_BILL_STATUS.format(ref='NEW')} NOT IN ('approved', 'partially_paid')",
        MSG_ALLOC_BILL_NOT_OPEN,
    ),
    "trg_ap_payment_allocations_bill_held": _abort(
        "trg_ap_payment_allocations_bill_held",
        "INSERT",
        "payment_allocations",
        SQLITE_BILL_HELD.format(ref="NEW"),
        MSG_BILL_ON_HOLD,
    ),
    "trg_ap_payment_allocations_no_overpay": _abort(
        "trg_ap_payment_allocations_no_overpay",
        "INSERT",
        "payment_allocations",
        f"{_settled('NEW', True)} + {_cents('NEW.amount')} > {SQLITE_BILL_TOTAL.format(ref='NEW')}",
        MSG_ALLOC_OVERPAY,
    ),
    "trg_ap_payment_allocations_original_cap": _abort(
        "trg_ap_payment_allocations_original_cap",
        "INSERT",
        "payment_allocations",
        f"NEW.kind = 'original' AND {_PAY_ORIGINAL} + {_cents('NEW.amount')} > {_cents(_PAY.format(col='amount'))}",
        MSG_ALLOC_EXCEEDS_PAYMENT,
    ),
    "trg_ap_payment_allocations_advance_recorded": _abort(
        "trg_ap_payment_allocations_advance_recorded",
        "INSERT",
        "payment_allocations",
        f"NEW.kind = 'advance' AND {_PAY.format(col='status')} IS NOT 'recorded'",
        MSG_ADVANCE_NOT_RECORDED,
    ),
    "trg_ap_payment_allocations_advance_account": _abort(
        "trg_ap_payment_allocations_advance_account",
        "INSERT",
        "payment_allocations",
        f"NEW.kind = 'advance' AND ({_PAY.format(col='payable_account_id')} IS NULL OR "
        f"(SELECT payable_account_id FROM supplier_bills WHERE id = NEW.bill_id) "
        f"IS NOT {_PAY.format(col='payable_account_id')})",
        MSG_ADVANCE_ACCOUNT_MISMATCH,
    ),
    "trg_ap_payment_allocations_advance_cap": _abort(
        "trg_ap_payment_allocations_advance_cap",
        "INSERT",
        "payment_allocations",
        f"NEW.kind = 'advance' AND {_PAY_ALLOCATED} + {_cents('NEW.amount')} > {_cents(_PAY.format(col='amount'))}",
        MSG_ADVANCE_EXCEEDS,
    ),
}
SQLITE_ALLOCATION_TRIGGERS_V2 = {
    "trg_ap_payment_allocations_bill_open": _abort(
        "trg_ap_payment_allocations_bill_open",
        "INSERT",
        "payment_allocations",
        f"{SQLITE_BILL_STATUS.format(ref='NEW')} NOT IN ('approved', 'partially_paid')",
        MSG_ALLOC_BILL_NOT_OPEN,
    ),
    "trg_ap_payment_allocations_no_overpay": V2.SQLITE_ALLOC_OVERPAY_V2,
    "trg_ap_payment_allocations_no_update": _abort(
        "trg_ap_payment_allocations_no_update",
        "UPDATE",
        "payment_allocations",
        "",
        V2.MSG_ALLOC_IMMUTABLE,
    ),
    "trg_ap_payment_allocations_no_delete": _abort(
        "trg_ap_payment_allocations_no_delete",
        "DELETE",
        "payment_allocations",
        "",
        V2.MSG_ALLOC_IMMUTABLE,
    ),
}

# credit_applications
_CREDIT_APPLIED = (
    f"(SELECT COALESCE(SUM({_cents('amount')}), 0) FROM credit_applications "
    "WHERE credit_id = NEW.credit_id AND unapplication_date IS NULL)"
)
SQLITE_APPLICATION_TRIGGERS_V3 = {
    "trg_ap_credit_applications_no_delete": _abort(
        "trg_ap_credit_applications_no_delete",
        "DELETE",
        "credit_applications",
        "",
        MSG_APPLICATION_NO_DELETE,
    ),
    "trg_ap_credit_applications_update_guard": _abort(
        "trg_ap_credit_applications_update_guard",
        "UPDATE",
        "credit_applications",
        f"NOT {SQL_APP_UNAPPLICATION_VALID} OR {SQL_APP_FIXED_CHANGED}",
        MSG_APPLICATION_IMMUTABLE,
    ),
    "trg_ap_credit_applications_unapply_period": _abort(
        "trg_ap_credit_applications_unapply_period",
        "UPDATE",
        "credit_applications",
        f"OLD.unapplication_date IS NULL AND {_period_closed('NEW.unapplication_date')}",
        MSG_PERIOD_CLOSED,
    ),
    "trg_ap_credit_applications_insert_period": _abort(
        "trg_ap_credit_applications_insert_period",
        "INSERT",
        "credit_applications",
        _period_closed("NEW.application_date"),
        MSG_PERIOD_CLOSED,
    ),
    "trg_ap_credit_applications_bill_open": _abort(
        "trg_ap_credit_applications_bill_open",
        "INSERT",
        "credit_applications",
        f"{SQLITE_BILL_STATUS.format(ref='NEW')} NOT IN ('approved', 'partially_paid')",
        V2.MSG_APPLY_BILL_NOT_OPEN,
    ),
    "trg_ap_credit_applications_credit_open": _abort(
        "trg_ap_credit_applications_credit_open",
        "INSERT",
        "credit_applications",
        "(SELECT status FROM supplier_credits WHERE id = NEW.credit_id) NOT IN ('approved', 'partially_applied')",
        V2.MSG_APPLY_CREDIT_NOT_OPEN,
    ),
    "trg_ap_credit_applications_no_overapply_credit": _abort(
        "trg_ap_credit_applications_no_overapply_credit",
        "INSERT",
        "credit_applications",
        f"{_CREDIT_APPLIED} + {_cents('NEW.amount')} > (SELECT {_cents('total')} FROM supplier_credits WHERE id = NEW.credit_id)",
        V2.MSG_APPLY_EXCEEDS_CREDIT,
    ),
    "trg_ap_credit_applications_no_overapply_bill": _abort(
        "trg_ap_credit_applications_no_overapply_bill",
        "INSERT",
        "credit_applications",
        f"{_settled('NEW', True)} + {_cents('NEW.amount')} > {SQLITE_BILL_TOTAL.format(ref='NEW')}",
        V2.MSG_APPLY_EXCEEDS_BILL,
    ),
}
SQLITE_APPLICATION_TRIGGERS_V2 = {
    k: v for k, v in V2.SQLITE_TRIGGERS_NEW.items() if k.startswith("trg_ap_credit_applications_")
}

# new tables
_DISCOUNT_TAKEN = (
    f"(SELECT COALESCE(SUM({_cents('amount')}), 0) FROM bill_discounts "
    "WHERE bill_id = NEW.bill_id AND reversal_date IS NULL)"
)
SQLITE_NEW_TABLE_TRIGGERS = {
    "trg_ap_bill_discounts_no_delete": _abort(
        "trg_ap_bill_discounts_no_delete", "DELETE", "bill_discounts", "", MSG_DISCOUNT_IMMUTABLE
    ),
    "trg_ap_bill_discounts_update_guard": _abort(
        "trg_ap_bill_discounts_update_guard",
        "UPDATE",
        "bill_discounts",
        f"{SQL_DISCOUNT_FIXED_CHANGED} OR NOT ({SQL_DISCOUNT_REVERSAL_VALID} OR {SQL_DISCOUNT_REVERSAL_UNCHANGED})",
        MSG_DISCOUNT_IMMUTABLE,
    ),
    "trg_ap_bill_discounts_reverse_period": _abort(
        "trg_ap_bill_discounts_reverse_period",
        "UPDATE",
        "bill_discounts",
        f"OLD.reversal_date IS NULL AND NEW.reversal_date IS NOT NULL AND {_period_closed('NEW.reversal_date')}",
        MSG_PERIOD_CLOSED,
    ),
    "trg_ap_bill_discounts_insert_period": _abort(
        "trg_ap_bill_discounts_insert_period",
        "INSERT",
        "bill_discounts",
        _period_closed("NEW.taken_on"),
        MSG_PERIOD_CLOSED,
    ),
    "trg_ap_bill_discounts_bill_open": _abort(
        "trg_ap_bill_discounts_bill_open",
        "INSERT",
        "bill_discounts",
        f"{SQLITE_BILL_STATUS.format(ref='NEW')} NOT IN ('approved', 'partially_paid')",
        MSG_DISCOUNT_BILL_NOT_OPEN,
    ),
    "trg_ap_bill_discounts_entitlement": _abort(
        "trg_ap_bill_discounts_entitlement",
        "INSERT",
        "bill_discounts",
        f"{_DISCOUNT_TAKEN} + {_cents('NEW.amount')} > "
        f"(SELECT {_cents('discount_amount')} FROM supplier_bills WHERE id = NEW.bill_id)",
        MSG_DISCOUNT_EXCEEDS,
    ),
    "trg_ap_bill_discounts_no_overpay": _abort(
        "trg_ap_bill_discounts_no_overpay",
        "INSERT",
        "bill_discounts",
        f"{_settled('NEW', True)} + {_cents('NEW.amount')} > {SQLITE_BILL_TOTAL.format(ref='NEW')}",
        MSG_ALLOC_OVERPAY,
    ),
    "trg_ap_bill_holds_no_delete": _abort(
        "trg_ap_bill_holds_no_delete", "DELETE", "bill_holds", "", MSG_HOLD_IMMUTABLE
    ),
    "trg_ap_bill_holds_update_guard": _abort(
        "trg_ap_bill_holds_update_guard",
        "UPDATE",
        "bill_holds",
        f"{SQL_HOLD_FIXED_CHANGED} OR NOT {SQL_HOLD_RELEASE_VALID}",
        MSG_HOLD_IMMUTABLE,
    ),
    "trg_ap_duplicate_flags_update_guard": _abort(
        "trg_ap_duplicate_flags_update_guard",
        "UPDATE",
        "duplicate_flags",
        f"NOT {SQL_FLAG_REVIEW_VALID}",
        MSG_FLAG_IMMUTABLE,
    ),
    "trg_ap_period_locks_no_update": _abort(
        "trg_ap_period_locks_no_update", "UPDATE", "ap_period_locks", "", MSG_PERIOD_LOCK_IMMUTABLE
    ),
    "trg_ap_period_locks_no_delete": _abort(
        "trg_ap_period_locks_no_delete", "DELETE", "ap_period_locks", "", MSG_PERIOD_LOCK_IMMUTABLE
    ),
}


def _dialect() -> str:
    name = op.get_bind().dialect.name
    if name not in ("sqlite", "postgresql"):
        raise RuntimeError(f"Unsupported database backend for payables triggers: {name}")
    return name


def _sqlite_drop(names) -> None:
    for name in names:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _sqlite_create(triggers: dict[str, str]) -> None:
    for ddl in triggers.values():
        op.execute(ddl)


def _sqlite_legacy_alter(enabled: bool) -> None:
    """Batch rebuilds rename ``_alembic_tmp_<table>`` over a table other triggers
    reference; modern SQLite validates those references during RENAME and fails
    because the old table was just dropped. Legacy mode skips that validation
    (references are left as written, which is the final table name)."""
    op.execute(f"PRAGMA legacy_alter_table = {'ON' if enabled else 'OFF'}")


# --- upgrade --------------------------------------------------------------------------------------


def upgrade() -> None:
    dialect = _dialect()
    if dialect == "sqlite":
        _sqlite_legacy_alter(True)

    # --- discount policies (before vendors, which reference them)
    op.create_table(
        "discount_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("discount_percent", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("discount_days", sa.Integer(), nullable=False),
        sa.Column("discount_account_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "discount_percent > 0 AND discount_percent < 100",
            name=op.f("ck_discount_policies_percent_range"),
        ),
        sa.CheckConstraint(
            "discount_days >= 0", name=op.f("ck_discount_policies_days_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["discount_account_id"],
            ["accounts.id"],
            name=op.f("fk_discount_policies_discount_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discount_policies")),
        sa.UniqueConstraint("code", name="uq_discount_policies_code"),
    )

    # --- vendors: status and payment preferences
    with op.batch_alter_table("vendors") as batch:
        batch.add_column(
            sa.Column("status", sa.String(20), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("status_reason", sa.String(500), nullable=True))
        batch.add_column(sa.Column("status_changed_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("preferred_method", sa.String(20), nullable=True))
        batch.add_column(sa.Column("bank_reference", sa.String(100), nullable=True))
        batch.add_column(sa.Column("discount_policy_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_vendors_discount_policy_id_discount_policies"),
            "discount_policies",
            ["discount_policy_id"],
            ["id"],
        )
    op.execute("UPDATE vendors SET status = CASE WHEN is_active THEN 'active' ELSE 'inactive' END")
    with op.batch_alter_table("vendors") as batch:
        batch.create_check_constraint(
            op.f("ck_vendors_status_valid"),
            "status IN ('active', 'on_hold', 'suspended', 'inactive')",
        )

    # --- supplier_bills: discount snapshot + indexes
    with op.batch_alter_table("supplier_bills") as batch:
        batch.add_column(sa.Column("discount_date", sa.Date(), nullable=True))
        batch.add_column(
            sa.Column("discount_amount", sa.Numeric(18, 2), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("discount_account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_supplier_bills_discount_account_id_accounts"),
            "accounts",
            ["discount_account_id"],
            ["id"],
        )
        batch.create_check_constraint(
            op.f("ck_supplier_bills_discount_non_negative"), "discount_amount >= 0"
        )
        batch.create_index(op.f("ix_supplier_bills_vendor_id"), ["vendor_id"])
        batch.create_index(op.f("ix_supplier_bills_invoice_date"), ["invoice_date"])

    # --- vendor_payments: payable account for unapplied remainders + indexes
    with op.batch_alter_table("vendor_payments") as batch:
        batch.add_column(sa.Column("payable_account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_vendor_payments_payable_account_id_accounts"),
            "accounts",
            ["payable_account_id"],
            ["id"],
        )
        batch.create_index(op.f("ix_vendor_payments_vendor_id"), ["vendor_id"])
        batch.create_index(op.f("ix_vendor_payments_payment_date"), ["payment_date"])

    # --- payment_allocations: kind, dates, actors, unallocation stamps; the
    # (payment, bill) pair is no longer unique (advance rows can be re-applied over time).
    with op.batch_alter_table("payment_allocations") as batch:
        batch.drop_constraint("uq_payment_allocations_payment_bill", type_="unique")
        batch.add_column(
            sa.Column("kind", sa.String(20), nullable=False, server_default="original")
        )
        batch.add_column(sa.Column("allocation_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("allocated_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("unallocation_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("unallocated_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("unallocated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("unallocation_reason", sa.String(500), nullable=True))
    # Back-fill from the payment. On PostgreSQL the fmspayables02 guard still forbids any
    # UPDATE of allocations, so it is switched off for the back-fill only (SQLite's batch
    # rebuild has already dropped its triggers at this point).
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE payment_allocations DISABLE TRIGGER trg_ap_payment_allocations_guard"
        )
    op.execute(
        "UPDATE payment_allocations SET "
        "allocation_date = (SELECT payment_date FROM vendor_payments p WHERE p.id = payment_allocations.payment_id), "
        "allocated_by = (SELECT recorded_by FROM vendor_payments p WHERE p.id = payment_allocations.payment_id) "
        "WHERE allocation_date IS NULL"
    )
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE payment_allocations ENABLE TRIGGER trg_ap_payment_allocations_guard"
        )
    with op.batch_alter_table("payment_allocations") as batch:
        batch.alter_column("allocation_date", existing_type=sa.Date(), nullable=False)
        batch.alter_column("allocated_by", existing_type=sa.String(100), nullable=False)
        batch.create_check_constraint(
            op.f("ck_payment_allocations_kind_valid"), "kind IN ('original', 'advance')"
        )
        batch.create_check_constraint(
            op.f("ck_payment_allocations_unallocation_after_allocation"),
            "unallocation_date IS NULL OR unallocation_date >= allocation_date",
        )
        batch.create_index(op.f("ix_payment_allocations_payment_id"), ["payment_id"])

    # --- credit_applications: unapplication stamps; the (credit, bill) pair is no longer
    # unique because a credit can be applied, unapplied and applied again over time.
    with op.batch_alter_table("credit_applications") as batch:
        batch.drop_constraint("uq_credit_applications_credit_bill", type_="unique")
        batch.add_column(sa.Column("unapplication_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("unapplied_by", sa.String(100), nullable=True))
        batch.add_column(sa.Column("unapplied_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("unapplication_reason", sa.String(500), nullable=True))
        batch.create_check_constraint(
            op.f("ck_credit_applications_unapplication_after_application"),
            "unapplication_date IS NULL OR unapplication_date >= application_date",
        )
        batch.create_index(op.f("ix_credit_applications_credit_id"), ["credit_id"])

    op.create_index(op.f("ix_supplier_credits_vendor_id"), "supplier_credits", ["vendor_id"])

    # --- new tables
    op.create_table(
        "bill_holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("placed_by", sa.String(length=100), nullable=False),
        sa.Column(
            "placed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("released_by", sa.String(length=100), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=500), nullable=True),
        sa.CheckConstraint("kind IN ('hold', 'dispute')", name=op.f("ck_bill_holds_kind_valid")),
        sa.ForeignKeyConstraint(
            ["bill_id"], ["supplier_bills.id"], name=op.f("fk_bill_holds_bill_id_supplier_bills")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bill_holds")),
    )
    op.create_index(op.f("ix_bill_holds_bill_id"), "bill_holds", ["bill_id"])

    op.create_table(
        "duplicate_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("suspected_bill_id", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.String(length=1000), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("blocks_approval", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'cleared', 'confirmed')",
            name=op.f("ck_duplicate_flags_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_duplicate_flags_bill_id_supplier_bills"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["suspected_bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_duplicate_flags_suspected_bill_id_supplier_bills"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_duplicate_flags")),
        sa.UniqueConstraint("bill_id", "suspected_bill_id", name="uq_duplicate_flags_pair"),
    )
    op.create_index(op.f("ix_duplicate_flags_bill_id"), "duplicate_flags", ["bill_id"])

    op.create_table(
        "bill_discounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("taken_on", sa.Date(), nullable=False),
        sa.Column("taken_by", sa.String(length=100), nullable=False),
        sa.Column("discount_account_id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("reversal_date", sa.Date(), nullable=True),
        sa.Column("reversed_by", sa.String(length=100), nullable=True),
        sa.Column("reversal_journal_entry_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_bill_discounts_amount_positive")),
        sa.CheckConstraint(
            "ROUND(amount, 2) = amount", name=op.f("ck_bill_discounts_amount_whole_cents")
        ),
        sa.CheckConstraint(
            "reversal_date IS NULL OR reversal_date >= taken_on",
            name=op.f("ck_bill_discounts_reversal_after_taken"),
        ),
        sa.ForeignKeyConstraint(
            ["bill_id"],
            ["supplier_bills.id"],
            name=op.f("fk_bill_discounts_bill_id_supplier_bills"),
        ),
        sa.ForeignKeyConstraint(
            ["discount_account_id"],
            ["accounts.id"],
            name=op.f("fk_bill_discounts_discount_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_bill_discounts_journal_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["vendor_payments.id"],
            name=op.f("fk_bill_discounts_payment_id_vendor_payments"),
        ),
        sa.ForeignKeyConstraint(
            ["reversal_journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_bill_discounts_reversal_journal_entry_id_journal_entries"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bill_discounts")),
    )
    op.create_index(op.f("ix_bill_discounts_bill_id"), "bill_discounts", ["bill_id"])
    op.create_index(op.f("ix_bill_discounts_payment_id"), "bill_discounts", ["payment_id"])

    op.create_table(
        "ap_period_locks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("closed_through", sa.Date(), nullable=False),
        sa.Column("closed_by", sa.String(length=100), nullable=False),
        sa.Column(
            "closed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("validation_summary", sa.String(length=4000), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ap_period_locks")),
        sa.UniqueConstraint("closed_through", name="uq_ap_period_locks_closed_through"),
    )

    # --- rules
    if dialect == "postgresql":
        for ddl in PG_FUNCTIONS_V3.values():
            op.execute(ddl)
        for ddl in PG_TRIGGERS_NEW.values():
            op.execute(ddl)
    else:
        # Batch rebuilds dropped every trigger on the altered tables; recreate the
        # complete v3 sets explicitly (DROP IF EXISTS keeps this idempotent).
        _sqlite_drop(SQLITE_BILL_TRIGGERS_V3)
        _sqlite_create(SQLITE_BILL_TRIGGERS_V3)
        _sqlite_drop(V2.SQLITE_PAYMENT_TRIGGERS_V2)
        _sqlite_drop(SQLITE_PAYMENT_TRIGGERS_V3)
        _sqlite_create(SQLITE_PAYMENT_TRIGGERS_V3)
        _sqlite_drop(SQLITE_ALLOCATION_TRIGGERS_V2)
        _sqlite_drop(SQLITE_ALLOCATION_TRIGGERS_V3)
        _sqlite_create(SQLITE_ALLOCATION_TRIGGERS_V3)
        _sqlite_drop(SQLITE_APPLICATION_TRIGGERS_V2)
        _sqlite_drop(SQLITE_APPLICATION_TRIGGERS_V3)
        _sqlite_create(SQLITE_APPLICATION_TRIGGERS_V3)
        _sqlite_drop(["trg_ap_supplier_credits_reverse_applied"])
        _sqlite_create(SQLITE_CREDIT_TRIGGERS_NEW)
        _sqlite_create(SQLITE_NEW_TABLE_TRIGGERS)
        _sqlite_legacy_alter(False)


# --- downgrade ------------------------------------------------------------------------------------


def downgrade() -> None:
    dialect = _dialect()
    if dialect == "sqlite":
        _sqlite_legacy_alter(True)

    # Rows the fmspayables02 schema cannot represent (see module docstring). The
    # append-only triggers must be out of the way while they are removed.
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE payment_allocations DISABLE TRIGGER trg_ap_payment_allocations_guard"
        )
        op.execute(
            "ALTER TABLE credit_applications DISABLE TRIGGER trg_ap_credit_applications_guard"
        )
    else:
        _sqlite_drop(SQLITE_ALLOCATION_TRIGGERS_V3)
        _sqlite_drop(SQLITE_APPLICATION_TRIGGERS_V3)
    op.execute("DELETE FROM credit_applications WHERE unapplication_date IS NOT NULL")
    op.execute("DELETE FROM payment_allocations WHERE unallocation_date IS NOT NULL")

    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE payment_allocations ENABLE TRIGGER trg_ap_payment_allocations_guard"
        )
        op.execute(
            "ALTER TABLE credit_applications ENABLE TRIGGER trg_ap_credit_applications_guard"
        )
        for name, table in (
            ("trg_ap_vendor_payments_insert_guard", "vendor_payments"),
            ("trg_ap_bill_discounts_guard", "bill_discounts"),
            ("trg_ap_bill_holds_guard", "bill_holds"),
            ("trg_ap_duplicate_flags_guard", "duplicate_flags"),
            ("trg_ap_period_locks_guard", "ap_period_locks"),
        ):
            op.execute(f"DROP TRIGGER {name} ON {table}")
        for name in (
            "fms_ap_supplier_bills_guard",
            "fms_ap_supplier_credits_guard",
            "fms_ap_vendor_payments_guard",
            "fms_ap_payment_allocations_guard",
            "fms_ap_credit_applications_guard",
        ):
            op.execute(
                V2.PG_FUNCTIONS_V2.get(name)
                or V2.PG_FUNCTIONS_NEW[name].replace(
                    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"
                )
            )
        for name in PG_NEW_FUNCTIONS:
            signature = "(date)" if name == "fms_ap_period_closed" else "()"
            op.execute(f"DROP FUNCTION {name}{signature}")
    else:
        _sqlite_drop(SQLITE_NEW_TABLE_TRIGGERS)
        _sqlite_drop(SQLITE_CREDIT_TRIGGERS_NEW)
        op.execute(V2.SQLITE_TRIGGERS_NEW["trg_ap_supplier_credits_reverse_applied"])
        _sqlite_drop(SQLITE_PAYMENT_TRIGGERS_V3)
        _sqlite_drop(SQLITE_BILL_TRIGGERS_V3)

    op.drop_table("ap_period_locks")
    op.drop_index(op.f("ix_bill_discounts_payment_id"), table_name="bill_discounts")
    op.drop_index(op.f("ix_bill_discounts_bill_id"), table_name="bill_discounts")
    op.drop_table("bill_discounts")
    op.drop_index(op.f("ix_duplicate_flags_bill_id"), table_name="duplicate_flags")
    op.drop_table("duplicate_flags")
    op.drop_index(op.f("ix_bill_holds_bill_id"), table_name="bill_holds")
    op.drop_table("bill_holds")
    op.drop_index(op.f("ix_supplier_credits_vendor_id"), table_name="supplier_credits")

    with op.batch_alter_table("credit_applications") as batch:
        batch.drop_index(op.f("ix_credit_applications_credit_id"))
        batch.drop_constraint(
            op.f("ck_credit_applications_unapplication_after_application"), type_="check"
        )
        for column in (
            "unapplication_reason",
            "unapplied_at",
            "unapplied_by",
            "unapplication_date",
        ):
            batch.drop_column(column)
        # Closed rows were deleted above, so at most one row per pair remains.
        batch.create_unique_constraint(
            "uq_credit_applications_credit_bill", ["credit_id", "bill_id"]
        )

    with op.batch_alter_table("payment_allocations") as batch:
        batch.drop_index(op.f("ix_payment_allocations_payment_id"))
        batch.drop_constraint(
            op.f("ck_payment_allocations_unallocation_after_allocation"), type_="check"
        )
        batch.drop_constraint(op.f("ck_payment_allocations_kind_valid"), type_="check")
        for column in (
            "unallocation_reason",
            "unallocated_at",
            "unallocated_by",
            "unallocation_date",
            "allocated_by",
            "allocation_date",
            "kind",
        ):
            batch.drop_column(column)
        batch.create_unique_constraint(
            "uq_payment_allocations_payment_bill", ["payment_id", "bill_id"]
        )

    with op.batch_alter_table("vendor_payments") as batch:
        batch.drop_index(op.f("ix_vendor_payments_payment_date"))
        batch.drop_index(op.f("ix_vendor_payments_vendor_id"))
        batch.drop_constraint(
            op.f("fk_vendor_payments_payable_account_id_accounts"), type_="foreignkey"
        )
        batch.drop_column("payable_account_id")

    with op.batch_alter_table("supplier_bills") as batch:
        batch.drop_index(op.f("ix_supplier_bills_invoice_date"))
        batch.drop_index(op.f("ix_supplier_bills_vendor_id"))
        batch.drop_constraint(op.f("ck_supplier_bills_discount_non_negative"), type_="check")
        batch.drop_constraint(
            op.f("fk_supplier_bills_discount_account_id_accounts"), type_="foreignkey"
        )
        for column in ("discount_account_id", "discount_amount", "discount_date"):
            batch.drop_column(column)

    with op.batch_alter_table("vendors") as batch:
        batch.drop_constraint(op.f("ck_vendors_status_valid"), type_="check")
        batch.drop_constraint(
            op.f("fk_vendors_discount_policy_id_discount_policies"), type_="foreignkey"
        )
        for column in (
            "discount_policy_id",
            "bank_reference",
            "preferred_method",
            "status_changed_at",
            "status_changed_by",
            "status_reason",
            "status",
        ):
            batch.drop_column(column)

    op.drop_table("discount_policies")

    if dialect == "sqlite":
        # The batch rebuilds above dropped the triggers of the rebuilt tables again.
        _sqlite_drop(SQLITE_APPLICATION_TRIGGERS_V2)
        _sqlite_create(SQLITE_APPLICATION_TRIGGERS_V2)
        _sqlite_drop(SQLITE_ALLOCATION_TRIGGERS_V2)
        _sqlite_create(SQLITE_ALLOCATION_TRIGGERS_V2)
        _sqlite_drop(V2.SQLITE_PAYMENT_TRIGGERS_V2)
        _sqlite_create(V2.SQLITE_PAYMENT_TRIGGERS_V2)
        _sqlite_drop(SQLITE_BILL_TRIGGERS_V2)
        _sqlite_create(SQLITE_BILL_TRIGGERS_V2)
        _sqlite_legacy_alter(False)
