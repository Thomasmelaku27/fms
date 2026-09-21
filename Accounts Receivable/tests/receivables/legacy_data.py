"""A database as it looked before request fingerprints were kept for allocations.

``build_legacy_database(url)`` migrates to ``fmsreceivables01`` and writes rows
the way the code of that revision wrote them - in particular the payment and
adjustment ``request_hash`` values use the *original* v1 formula, reproduced
here verbatim (hash of ``model_dump(mode="json")`` without the key), so the
tests prove that requests recorded back then can still be verified.

Legacy allocation batches (keyed, no fingerprint):

* ``alloc-single``: one row, payment 1 -> INV-1, 20.00  (reconstructable)
* ``alloc-multi``:  two rows, payment 1 -> INV-2 10.00, INV-3 10.00 (not reliably)
* ``alloc-credit``: one row, credit 1 -> INV-4, 15.00   (reconstructable)
"""

import hashlib
import json
from sqlalchemy import text

from app.db import make_engine
from app.modules.receivables.schemas import AdjustmentIn, PaymentIn
from tests.conftest import migrate

PAYMENT_REQUEST = {
    "customer_id": 1, "payment_date": "2026-01-20", "amount": "200.00", "deposit_account_id": 1,
    "reference": "CHQ-1", "idempotency_key": "pay-legacy", "allocations": [],
}
ADJUSTMENT_REQUEST = {
    "customer_id": 1, "kind": "credit", "adjustment_date": "2026-01-25", "amount": "50.00",
    "reason": "goodwill", "invoice_id": None, "idempotency_key": "adj-legacy",
}
LEGACY_ALLOCATIONS = [
    # key, invoice_id, payment_id, adjustment_id, amount, allocation_date
    ("alloc-single", 1, 1, None, "20.00", "2026-01-20"),
    ("alloc-multi", 2, 1, None, "10.00", "2026-01-20"),
    ("alloc-multi", 3, 1, None, "10.00", "2026-01-20"),
    ("alloc-credit", 4, None, 1, "15.00", "2026-01-25"),
]


def v1_fingerprint(model) -> str:
    """The fingerprint formula of revision fmsreceivables01, verbatim."""
    payload = model.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


SHARED_KEY_ALLOCATIONS = [
    # what the old code could also produce: one key used for a payment batch and a credit batch
    ("shared", 1, 1, None, "20.00", "2026-01-20"),
    ("alloc-multi", 2, 1, None, "10.00", "2026-01-20"),
    ("alloc-multi", 3, 1, None, "10.00", "2026-01-20"),
    ("shared", 4, None, 1, "15.00", "2026-01-25"),
]


def build_legacy_database(url: str, revision: str = "fmsreceivables01", allocations=LEGACY_ALLOCATIONS) -> None:
    migrate(url, "fmsreceivables01")
    engine = make_engine(url)
    with engine.begin() as conn:
        for code, name, type_ in (("1000", "Bank", "asset"), ("1200", "AR", "asset"), ("2200", "Tax", "liability"),
                                  ("4000", "Sales", "revenue"), ("4900", "Returns", "revenue"), ("6100", "Bad debt", "expense")):
            conn.execute(text("INSERT INTO accounts (code, name, type, is_active) VALUES (:c, :n, :t, true)"),
                         {"c": code, "n": name, "t": type_})
        conn.execute(text("INSERT INTO receivable_settings (id, receivable_account_id, tax_account_id, credit_account_id, write_off_account_id) "
                          "VALUES (1, 2, 3, 5, 6)"))
        conn.execute(text("INSERT INTO customers (code, name, is_active) VALUES ('C1', 'Acme', true)"))
        conn.execute(text("INSERT INTO journal_entries (entry_date, description, status) VALUES ('2026-01-10', 'legacy', 'draft')"))
        # amounts are bound as exact decimal strings (the SQLite driver cannot bind Decimal)
        settled = {1: "20.00", 2: "10.00", 3: "10.00", 4: "15.00"}
        for invoice_id, amount_settled in settled.items():
            conn.execute(text(
                "INSERT INTO sales_invoices (customer_id, invoice_number, invoice_date, due_date, subtotal, tax_total, total, "
                "amount_settled, status, journal_entry_id) VALUES (1, :n, '2026-01-10', '2026-02-09', 100, 0, 100, :s, 'partially_paid', 1)"),
                {"n": f"INV-{invoice_id}", "s": amount_settled})
        conn.execute(text(
            "INSERT INTO customer_payments (customer_id, payment_date, amount, amount_allocated, deposit_account_id, reference, "
            "idempotency_key, request_hash, journal_entry_id) VALUES (1, '2026-01-20', 200.00, 40, 1, 'CHQ-1', 'pay-legacy', :h, 1)"),
            {"h": v1_fingerprint(PaymentIn(**PAYMENT_REQUEST))})
        conn.execute(text(
            "INSERT INTO customer_adjustments (customer_id, kind, adjustment_date, amount, amount_allocated, reason, "
            "idempotency_key, request_hash, journal_entry_id) VALUES (1, 'credit', '2026-01-25', 50.00, 15, 'goodwill', 'adj-legacy', :h, 1)"),
            {"h": v1_fingerprint(AdjustmentIn(**ADJUSTMENT_REQUEST))})
        for key, invoice_id, payment_id, adjustment_id, amount, allocation_date in allocations:
            conn.execute(text(
                "INSERT INTO invoice_allocations (invoice_id, payment_id, adjustment_id, amount, allocation_date, idempotency_key) "
                "VALUES (:i, :p, :a, :m, :d, :k)"),
                {"i": invoice_id, "p": payment_id, "a": adjustment_id, "m": amount, "d": allocation_date, "k": key})
    engine.dispose()
    if revision != "fmsreceivables01":
        migrate(url, revision)
