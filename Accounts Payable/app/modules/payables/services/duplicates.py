"""Explainable near-duplicate detection for supplier bills.

Exact duplicates (same vendor, same canonical invoice number) are *rejected* by
the API and the database (see ``models.normalize_invoice_number``). This module
handles the grey zone: bills that look like a duplicate but are not provably
one. Each suspicion is stored as a :class:`DuplicateFlag` with a score and the
rules that fired, blocks approval while pending, and is resolved by a reviewer
who either clears it or confirms the duplicate. Legitimate invoices are never
auto-rejected.

Rules (weights add up). A pair scoring ``ADVISORY`` (40) or more is flagged for
review; a pair scoring ``BLOCKING`` (60) or more additionally blocks approval
until a reviewer clears it. Amount and date coincidences alone never reach the
advisory level -- recurring charges of the same amount are normal -- some
similarity of the invoice numbers is required:

* ``compact_number``    same vendor, invoice numbers identical once punctuation and
                        spaces are stripped ("INV-001" vs "INV001")                   60
* ``cross_vendor``      same compact number *and* total at a different vendor         60
* ``near_number``       same vendor and total, invoice numbers one edit apart         30
* ``same_total_window`` same vendor and total, invoice dates within 7 days            15
* ``same_total_date``   ... and the very same invoice date (adds to the window rule)  10
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.payables.models import (
    BillStatus,
    DuplicateFlag,
    DuplicateFlagStatus,
    SupplierBill,
    compact_invoice_number,
)

ADVISORY = 40
BLOCKING = 60
WINDOW_DAYS = 7


@dataclass(frozen=True)
class Suspicion:
    suspected_bill_id: int
    score: int
    reasons: tuple[str, ...]

    @property
    def blocks_approval(self) -> bool:
        return self.score >= BLOCKING


def _one_edit_apart(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by one substitution, insertion or deletion."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = edits = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] != longer[j]:
            edits += 1
            if edits > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return True


def detect(db: Session, bill: SupplierBill) -> list[Suspicion]:
    """Score every other bill that could be a duplicate of ``bill``."""
    compact = compact_invoice_number(bill.invoice_number)
    low, high = (
        bill.invoice_date - timedelta(days=WINDOW_DAYS),
        bill.invoice_date + timedelta(days=WINDOW_DAYS),
    )
    candidates = db.scalars(
        select(SupplierBill).where(
            SupplierBill.id != bill.id,
            SupplierBill.status != BillStatus.REJECTED,
            (
                (SupplierBill.vendor_id == bill.vendor_id)
                & (
                    (SupplierBill.total == bill.total)
                    & SupplierBill.invoice_date.between(low, high)
                )
            )
            | (SupplierBill.total == bill.total)
            | (SupplierBill.vendor_id == bill.vendor_id),
        )
    )
    suspicions = []
    for other in candidates:
        score = 0
        reasons: list[str] = []
        other_compact = compact_invoice_number(other.invoice_number)
        same_vendor = other.vendor_id == bill.vendor_id
        same_total = other.total == bill.total
        if same_vendor and other_compact == compact:
            score += 60
            reasons.append(
                f"compact_number: '{other.invoice_number}' matches '{bill.invoice_number}' "
                "once punctuation and spaces are ignored"
            )
        if same_vendor and same_total and low <= other.invoice_date <= high:
            score += 15
            reasons.append(
                f"same_total_window: same vendor and total {bill.total} within "
                f"{WINDOW_DAYS} days ({other.invoice_date} vs {bill.invoice_date})"
            )
            if other.invoice_date == bill.invoice_date:
                score += 10
                reasons.append("same_total_date: identical invoice date")
        if not same_vendor and same_total and other_compact == compact:
            score += 60
            reasons.append(
                f"cross_vendor: number '{other.invoice_number}' and total {bill.total} "
                f"also recorded for vendor {other.vendor_id}"
            )
        if (
            same_vendor
            and same_total
            and other_compact != compact
            and _one_edit_apart(other_compact, compact)
        ):
            score += 30
            reasons.append(
                f"near_number: '{other.invoice_number}' is one character away from "
                f"'{bill.invoice_number}' with the same total"
            )
        if score >= ADVISORY:
            suspicions.append(Suspicion(other.id, min(score, 100), tuple(reasons)))
    suspicions.sort(key=lambda s: (-s.score, s.suspected_bill_id))
    return suspicions


def refresh_flags(db: Session, bill: SupplierBill) -> list[DuplicateFlag]:
    """Recompute the pending flags of a draft (reviewed flags are kept as they are)."""
    reviewed = {
        f.suspected_bill_id: f
        for f in bill.duplicate_flags
        if f.status != DuplicateFlagStatus.PENDING
    }
    for flag in list(bill.duplicate_flags):
        if flag.status == DuplicateFlagStatus.PENDING:
            bill.duplicate_flags.remove(flag)
    db.flush()
    for suspicion in detect(db, bill):
        if suspicion.suspected_bill_id in reviewed:
            continue
        bill.duplicate_flags.append(
            DuplicateFlag(
                suspected_bill_id=suspicion.suspected_bill_id,
                score=suspicion.score,
                blocks_approval=suspicion.blocks_approval,
                reasons="\n".join(suspicion.reasons)[:1000],
                status=DuplicateFlagStatus.PENDING,
            )
        )
    db.flush()
    return bill.duplicate_flags
