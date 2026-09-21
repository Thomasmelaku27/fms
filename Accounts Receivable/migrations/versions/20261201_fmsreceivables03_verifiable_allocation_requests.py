"""Allocation requests without a verifiable fingerprint can no longer be replayed.

``fmsreceivables02`` (left untouched) created a request row with
``request_hash = NULL`` for every keyed allocation batch recorded before
request fingerprints existed, and the application treated NULL as "matches
anything" - so a *modified* request reusing such a key was answered as if it
were a retry. This migration records, once and for all, how far each of those
legacy requests can be trusted, in the new column ``fingerprint_origin``:

* ``reconstructed`` - the original payload can be rebuilt reliably, and its
  fingerprint is stored. That is the case only when exactly one allocation row
  carries the key and it belongs to the request's payment/credit: allocation
  rows are append-only (trigger since ``fmsreceivables01``), a batch was
  written all-or-nothing, two batches can never share a key *and* an invoice
  (unique constraint), and pre-upgrade requests had no effective date - so a
  single row is exactly one complete request. The fingerprint is the one the
  application computes for that request (``allocation_fingerprint``, asserted
  equal by the regression tests).
* ``unverifiable`` - anything else (several rows, which the old code could have
  merged from racing requests; rows of another source; no rows). The hash stays
  NULL and every reuse of the key is answered with 409 Conflict.
* ``request`` - rows that already have a hash computed from the request
  (written by the application since ``fmsreceivables02``).

Two CHECK constraints keep the column and the hash consistent from now on.
Payments and adjustments are not touched: their keys have always been stored
with a fingerprint, which the application keeps verifying (including the
pre-canonicalisation encoding).

Revision ID: fmsreceivables03
Revises: fmsreceivables02
Create Date: 2026-12-01 00:00:00
"""

import hashlib
import json
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "fmsreceivables03"
down_revision: str | None = "fmsreceivables02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CK_ORIGIN = "ck_allocation_requests_fingerprint_origin"
CK_HASH = "ck_allocation_requests_hash_matches_origin"


def _fingerprint(source_type: str, source_id: int, invoice_id: int, amount) -> str:
    """Self-contained copy of the application's allocation fingerprint (one line, no effective date)."""
    money = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payload = {
        "operation": "allocation",
        "source_type": source_type,
        "source_id": int(source_id),
        "allocations": [[int(invoice_id), f"{money:.2f}"]],
        "effective_date": None,
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _classify(bind) -> None:
    legacy = bind.execute(
        sa.text(
            "SELECT id, idempotency_key, payment_id, adjustment_id FROM allocation_requests "
            "WHERE request_hash IS NULL ORDER BY id"
        )
    ).all()
    for request_id, key, payment_id, adjustment_id in legacy:
        rows = bind.execute(
            sa.text(
                "SELECT invoice_id, payment_id, adjustment_id, amount FROM invoice_allocations "
                "WHERE idempotency_key = :key"
            ),
            {"key": key},
        ).all()
        reliable = (
            len(rows) == 1
            and rows[0].payment_id == payment_id
            and rows[0].adjustment_id == adjustment_id
        )
        if reliable:
            row = rows[0]
            source_type, source_id = (
                ("payment", row.payment_id) if row.payment_id is not None else ("adjustment", row.adjustment_id)
            )
            bind.execute(
                sa.text(
                    "UPDATE allocation_requests SET request_hash = :hash, "
                    "fingerprint_origin = 'reconstructed' WHERE id = :id"
                ),
                {"hash": _fingerprint(source_type, source_id, row.invoice_id, row.amount), "id": request_id},
            )
        else:
            bind.execute(
                sa.text("UPDATE allocation_requests SET fingerprint_origin = 'unverifiable' WHERE id = :id"),
                {"id": request_id},
            )


def upgrade() -> None:
    # allocation_requests carries no triggers, so SQLite's batch rebuild is safe here.
    with op.batch_alter_table("allocation_requests") as batch_op:
        batch_op.add_column(
            sa.Column("fingerprint_origin", sa.String(length=20), server_default="request", nullable=False)
        )
    _classify(op.get_bind())
    with op.batch_alter_table("allocation_requests") as batch_op:
        batch_op.create_check_constraint(
            op.f(CK_ORIGIN), "fingerprint_origin IN ('request', 'reconstructed', 'unverifiable')"
        )
        batch_op.create_check_constraint(
            op.f(CK_HASH),
            "(fingerprint_origin = 'unverifiable' AND request_hash IS NULL)"
            " OR (fingerprint_origin <> 'unverifiable' AND request_hash IS NOT NULL)",
        )


def downgrade() -> None:
    # Restore the fmsreceivables02 state exactly, so that downgrade + upgrade is
    # lossless: fingerprints this migration reconstructed are removed again and
    # are recomputed identically on the next upgrade. Warning: code written for
    # fmsreceivables02 treats a NULL fingerprint as a match, so downgrading below
    # this revision is only meant to go together with reverting that code.
    with op.batch_alter_table("allocation_requests") as batch_op:
        batch_op.drop_constraint(op.f(CK_HASH), type_="check")
        batch_op.drop_constraint(op.f(CK_ORIGIN), type_="check")
    op.execute("UPDATE allocation_requests SET request_hash = NULL WHERE fingerprint_origin = 'reconstructed'")
    with op.batch_alter_table("allocation_requests") as batch_op:
        batch_op.drop_column("fingerprint_origin")
