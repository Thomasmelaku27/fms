"""Supplier statement import and reconciliation (external evidence, never posted).

CSV format (UTF-8, header required, ``kind,reference,date,amount``):

* ``kind``: ``invoice`` | ``credit`` | ``payment``
* ``reference``: the supplier's document / remittance reference (normalised like our
  invoice numbers: whitespace collapsed, upper-case)
* ``date``: ISO ``YYYY-MM-DD``
* ``amount``: positive decimal, at most two decimals (never rounded)

Protection: UTF-8 only (with or without BOM), size limit, row limit, header check,
per-row validation, duplicate rows inside the file, duplicate *imports* (same vendor
and content hash). Matching is deterministic and explained per line:

* exact: normalised reference equals an internal bill / credit number or payment
  reference of the vendor and the amount equals -> ``matched``;
  amount differs -> ``amount_discrepancy``; same amount but dates more than
  ``TIMING_DAYS`` apart -> ``timing_difference``;
* near: compact reference (alphanumerics only) matches, or same amount within
  ``TIMING_DAYS`` with a near-identical reference -> ``near_match`` with reasons;
* otherwise ``unmatched`` (an invoice we do not have, a payment we did not make).

The summary also lists internal payments in the statement period that the
statement does not show ("missing payments").
"""

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.payables.integration import Integrations
from app.modules.payables.models import (
    SETTLEMENT_BILL_STATUSES,
    StatementLineKind,
    StatementMatchStatus,
    SupplierBill,
    SupplierCredit,
    SupplierStatementImport,
    SupplierStatementLine,
    VendorPayment,
    compact_invoice_number,
    normalize_invoice_number,
)
from app.modules.payables.services.common import (
    Conflict,
    NotFound,
    ValidationFailed,
    get_vendor,
    utcnow,
)
from app.modules.payables.services.duplicates import _one_edit_apart

MAX_BYTES = 1_000_000
MAX_ROWS = 5_000
TIMING_DAYS = 7
REQUIRED_COLUMNS = ("kind", "reference", "date", "amount")
CENT = Decimal("0.01")


@dataclass
class ParsedLine:
    line_no: int
    kind: StatementLineKind
    reference: str
    normalized: str
    line_date: date
    amount: Decimal


@dataclass
class MatchResult:
    status: StatementMatchStatus
    matched_type: str | None = None
    matched_id: int | None = None
    internal_amount: Decimal | None = None
    internal_date: date | None = None
    reasons: list[str] = field(default_factory=list)


# --- parsing and validation -----------------------------------------------------------------------------


def parse_csv(content: bytes, *, file_name: str = "statement.csv") -> tuple[list[ParsedLine], str]:
    """Validate and parse; raises ``ValidationFailed`` listing every problem found."""
    if len(content) > MAX_BYTES:
        raise ValidationFailed(
            f"{file_name}: file too large ({len(content)} bytes; limit {MAX_BYTES})"
        )
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed(
            f"{file_name}: unsupported encoding; UTF-8 required ({exc.reason} at byte {exc.start})"
        ) from exc
    if "\x00" in text_content:
        raise ValidationFailed(f"{file_name}: binary content is not a CSV statement")
    reader = csv.DictReader(io.StringIO(text_content))
    if reader.fieldnames is None:
        raise ValidationFailed(f"{file_name}: empty file")
    columns = [c.strip().lower() for c in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise ValidationFailed(
            f"{file_name}: missing columns {missing}; expected {list(REQUIRED_COLUMNS)}"
        )
    problems: list[str] = []
    lines: list[ParsedLine] = []
    seen: dict[tuple, int] = {}
    for index, row in enumerate(reader, start=2):
        if index - 1 > MAX_ROWS:
            problems.append(f"more than {MAX_ROWS} rows")
            break
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        kind_raw, reference, date_raw, amount_raw = (
            row.get("kind", ""),
            row.get("reference", ""),
            row.get("date", ""),
            row.get("amount", ""),
        )
        if not any((kind_raw, reference, date_raw, amount_raw)):
            continue  # blank line
        try:
            kind = StatementLineKind(kind_raw.lower())
        except ValueError:
            problems.append(f"row {index}: kind {kind_raw!r} must be invoice, credit or payment")
            continue
        if not reference or len(reference) > 80:
            problems.append(f"row {index}: reference is required (max 80 characters)")
            continue
        try:
            line_date = date.fromisoformat(date_raw)
        except ValueError:
            problems.append(f"row {index}: date {date_raw!r} is not ISO YYYY-MM-DD")
            continue
        try:
            amount = Decimal(amount_raw.replace(",", ""))
        except InvalidOperation:
            problems.append(f"row {index}: amount {amount_raw!r} is not a number")
            continue
        if not amount.is_finite() or amount <= 0:
            problems.append(f"row {index}: amount must be a positive number")
            continue
        if amount != amount.quantize(CENT):
            problems.append(
                f"row {index}: amount {amount} has unsupported precision (whole cents only)"
            )
            continue
        normalized = normalize_invoice_number(reference)
        key = (kind, normalized, line_date, amount)
        if key in seen:
            problems.append(
                f"row {index}: duplicate of row {seen[key]} ({kind.value} {reference} {line_date} {amount})"
            )
            continue
        seen[key] = index
        lines.append(
            ParsedLine(index, kind, reference, normalized, line_date, amount.quantize(CENT))
        )
    if problems:
        raise ValidationFailed(
            f"{file_name}: " + "; ".join(problems[:20]) + (" ..." if len(problems) > 20 else "")
        )
    if not lines:
        raise ValidationFailed(f"{file_name}: no statement lines")
    return lines, hashlib.sha256(content).hexdigest()


# --- matching ---------------------------------------------------------------------------------------------


def _internal_documents(db: Session, vendor_id: int, statement_date: date) -> dict[str, list[dict]]:
    """Internal documents as the supplier could have known them on the statement date
    (payments reversed later still appear; documents dated after it do not)."""
    bills = [
        b
        for b in db.scalars(
            select(SupplierBill).where(
                SupplierBill.vendor_id == vendor_id,
                SupplierBill.status.in_(SETTLEMENT_BILL_STATUSES),
            )
        )
        if b.invoice_date <= statement_date
    ]
    credits = [
        c
        for c in db.scalars(select(SupplierCredit).where(SupplierCredit.vendor_id == vendor_id))
        if c.status.value not in ("draft", "rejected") and c.in_force_on(statement_date)
    ]
    payments = [
        p
        for p in db.scalars(select(VendorPayment).where(VendorPayment.vendor_id == vendor_id))
        if p.in_force_on(statement_date)
    ]
    return {
        "invoice": [
            {
                "type": "bill",
                "id": b.id,
                "ref": b.invoice_number_normalized,
                "compact": compact_invoice_number(b.invoice_number),
                "amount": b.total,
                "date": b.invoice_date,
            }
            for b in bills
        ],
        "credit": [
            {
                "type": "credit",
                "id": c.id,
                "ref": c.credit_number_normalized,
                "compact": compact_invoice_number(c.credit_number),
                "amount": c.total,
                "date": c.credit_date,
            }
            for c in credits
        ],
        "payment": [
            {
                "type": "payment",
                "id": p.id,
                "ref": normalize_invoice_number(p.reference) if p.reference else f"#{p.id}",
                "compact": compact_invoice_number(p.reference or f"#{p.id}"),
                "amount": p.amount,
                "date": p.payment_date,
            }
            for p in payments
        ],
    }


def match_line(line: ParsedLine, candidates: list[dict]) -> MatchResult:
    exact = [c for c in candidates if c["ref"] == line.normalized]
    if exact:
        doc = exact[0]
        if doc["amount"] != line.amount:
            return MatchResult(
                StatementMatchStatus.AMOUNT_DISCREPANCY,
                doc["type"],
                doc["id"],
                doc["amount"],
                doc["date"],
                [
                    f"reference matches {doc['type']} {doc['id']}; statement {line.amount} vs internal {doc['amount']}"
                ],
            )
        if abs((doc["date"] - line.line_date).days) > TIMING_DAYS:
            return MatchResult(
                StatementMatchStatus.TIMING_DIFFERENCE,
                doc["type"],
                doc["id"],
                doc["amount"],
                doc["date"],
                [
                    f"reference and amount match {doc['type']} {doc['id']}; dates differ by "
                    f"{abs((doc['date'] - line.line_date).days)} days ({line.line_date} vs {doc['date']})"
                ],
            )
        return MatchResult(
            StatementMatchStatus.MATCHED,
            doc["type"],
            doc["id"],
            doc["amount"],
            doc["date"],
            [f"exact reference and amount match {doc['type']} {doc['id']}"],
        )
    compact = compact_invoice_number(line.reference)
    near = []
    for doc in candidates:
        reasons = []
        if doc["compact"] == compact:
            reasons.append(
                f"references match once punctuation and spaces are ignored ({doc['ref']})"
            )
        elif (
            doc["amount"] == line.amount
            and abs((doc["date"] - line.line_date).days) <= TIMING_DAYS
            and _one_edit_apart(doc["compact"], compact)
        ):
            reasons.append(
                f"same amount within {TIMING_DAYS} days and reference one character away ({doc['ref']})"
            )
        elif doc["amount"] == line.amount and doc["date"] == line.line_date:
            reasons.append(
                f"same amount and date as {doc['type']} {doc['id']} ({doc['ref']}); reference differs"
            )
        if reasons:
            if doc["amount"] != line.amount:
                reasons.append(
                    f"amount differs: statement {line.amount} vs internal {doc['amount']}"
                )
            near.append((doc, reasons))
    if near:
        doc, reasons = near[0]
        return MatchResult(
            StatementMatchStatus.NEAR_MATCH,
            doc["type"],
            doc["id"],
            doc["amount"],
            doc["date"],
            reasons,
        )
    what = {
        "invoice": "no internal bill with this reference (invoice we do not have)",
        "credit": "no internal credit with this reference",
        "payment": "no internal payment with this reference (payment we did not make, or reference differs)",
    }[line.kind.value]
    return MatchResult(StatementMatchStatus.UNMATCHED, reasons=[what])


def reconcile(
    db: Session, vendor_id: int, lines: list[ParsedLine], *, statement_date: date
) -> tuple[list[tuple[ParsedLine, MatchResult]], dict]:
    docs = _internal_documents(db, vendor_id, statement_date)
    results = [(line, match_line(line, docs[line.kind.value])) for line in lines]
    matched_payment_ids = {
        r.matched_id for _, r in results if r.matched_type == "payment" and r.matched_id
    }
    earliest = min(line.line_date for line in lines)
    missing_payments = [
        {
            "payment_id": p["id"],
            "date": p["date"].isoformat(),
            "amount": f"{p['amount']:.2f}",
            "reference": p["ref"],
        }
        for p in docs["payment"]
        if p["id"] not in matched_payment_ids and earliest <= p["date"] <= statement_date
    ]
    counts = {
        status.value: sum(1 for _, r in results if r.status == status)
        for status in StatementMatchStatus
    }
    summary = {
        "lines": len(lines),
        "counts": counts,
        "statement_total": {
            kind.value: f"{sum((l.amount for l in lines if l.kind == kind), Decimal('0')):.2f}"
            for kind in StatementLineKind
        },
        "missing_payments": missing_payments,
        "amount_discrepancy_total": f"{sum((abs(r.internal_amount - l.amount) for l, r in results if r.status == StatementMatchStatus.AMOUNT_DISCREPANCY), Decimal('0')):.2f}",
    }
    return results, summary


# --- workflow --------------------------------------------------------------------------------------------------


def preview(
    db: Session, vendor_id: int, content: bytes, *, file_name: str, statement_date: date
) -> dict:
    """Parse, validate and match without persisting anything."""
    get_vendor(db, vendor_id)
    lines, content_hash = parse_csv(content, file_name=file_name)
    duplicate = db.scalar(
        select(SupplierStatementImport.id).where(
            SupplierStatementImport.vendor_id == vendor_id,
            SupplierStatementImport.content_hash == content_hash,
        )
    )
    results, summary = reconcile(db, vendor_id, lines, statement_date=statement_date)
    return {
        "vendor_id": vendor_id,
        "file_name": file_name,
        "content_hash": content_hash,
        "already_imported_as": duplicate,
        "summary": summary,
        "lines": [_line_dict(line, result) for line, result in results],
    }


def import_statement(
    db: Session,
    vendor_id: int,
    content: bytes,
    *,
    file_name: str,
    statement_date: date,
    actor: str,
    integrations: Integrations,
) -> SupplierStatementImport:
    """Persist the statement and its reconciliation for review. No ledger effect."""
    get_vendor(db, vendor_id)
    lines, content_hash = parse_csv(content, file_name=file_name)
    if db.scalar(
        select(SupplierStatementImport.id).where(
            SupplierStatementImport.vendor_id == vendor_id,
            SupplierStatementImport.content_hash == content_hash,
        )
    ):
        raise Conflict("This statement was already imported for the vendor (identical content)")
    results, summary = reconcile(db, vendor_id, lines, statement_date=statement_date)
    record = SupplierStatementImport(
        vendor_id=vendor_id,
        file_name=file_name[:200],
        content_hash=content_hash,
        statement_date=statement_date,
        imported_by=actor,
        line_count=len(lines),
        summary_json=json.dumps(summary)[:4000],
        lines=[
            SupplierStatementLine(
                line_no=line.line_no,
                kind=line.kind,
                reference=line.reference,
                normalized_reference=line.normalized,
                line_date=line.line_date,
                amount=line.amount,
                match_status=result.status,
                matched_type=result.matched_type,
                matched_id=result.matched_id,
                internal_amount=result.internal_amount,
                internal_date=result.internal_date,
                explanation="; ".join(result.reasons)[:1000],
                review_status="resolved"
                if result.status == StatementMatchStatus.MATCHED
                else "open",
            )
            for line, result in results
        ],
    )
    db.add(record)
    db.flush()
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.statement.imported",
        resource=f"statement_import:{record.id}",
    )
    db.commit()
    db.refresh(record)
    return record


def get_import(db: Session, import_id: int) -> SupplierStatementImport:
    record = db.get(SupplierStatementImport, import_id)
    if record is None:
        raise NotFound("Statement import not found")
    return record


def list_imports(db: Session, *, vendor_id: int | None = None) -> list[SupplierStatementImport]:
    stmt = select(SupplierStatementImport).order_by(SupplierStatementImport.id.desc())
    if vendor_id is not None:
        stmt = stmt.where(SupplierStatementImport.vendor_id == vendor_id)
    return list(db.scalars(stmt))


def resolve_line(
    db: Session, import_id: int, line_id: int, *, actor: str, note: str, integrations: Integrations
) -> SupplierStatementImport:
    record = get_import(db, import_id)
    line = db.get(SupplierStatementLine, line_id)
    if line is None or line.import_id != record.id:
        raise NotFound("Statement line not found")
    line.review_status = "resolved"
    line.resolution_note = note[:500]
    line.reviewed_by = actor
    line.reviewed_at = utcnow()
    integrations.audit.record(
        db,
        actor=actor,
        action="payables.statement.line_resolved",
        resource=f"statement_line:{line.id}",
    )
    db.commit()
    db.refresh(record)
    return record


def _line_dict(line: ParsedLine, result: MatchResult) -> dict:
    return {
        "line_no": line.line_no,
        "kind": line.kind.value,
        "reference": line.reference,
        "normalized_reference": line.normalized,
        "date": line.line_date,
        "amount": line.amount,
        "match_status": result.status.value,
        "matched_type": result.matched_type,
        "matched_id": result.matched_id,
        "internal_amount": result.internal_amount,
        "internal_date": result.internal_date,
        "explanation": "; ".join(result.reasons),
    }
