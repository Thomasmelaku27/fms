"""Accounts Receivable transactions: invoices, payments, adjustments, allocations.

Every function that changes data is atomic: it either commits everything
(document rows, allocations *and* the posted journal entry) or rolls back and
raises a ``ReceivablesError``. Ledger entries are created as drafts and posted
through ``app.services.posting.post_entry`` - the same path the journal API
uses - which also performs the final commit, so the ledger and the AR
documents can never get out of step. Posted journal history is never edited;
a void is a new reversing entry.

Concurrency
-----------
Balances are maintained with *conditional updates* executed in SQL::

    UPDATE sales_invoices
       SET amount_settled = amount_settled + :x, status = ...
     WHERE id = :id AND status IN ('issued', 'partially_paid')
       AND cents(amount_settled + :x) <= cents(total)

On PostgreSQL the statement takes the row lock and re-evaluates the condition
after a concurrent writer commits; on SQLite writers are serialised. Either way
a second caller that would over-allocate updates zero rows and is rejected. The
same pattern guards the unallocated amount of a payment/adjustment and the
draft -> issued / -> void transitions. Rows are always locked in a fixed order
(source first, then invoices by id) so concurrent allocations cannot deadlock.
Comparisons are done in whole cents so they are exact on SQLite, which stores
NUMERIC as floating point.

Idempotency
-----------
Payments require an ``idempotency_key`` (unique column). A replay returns the
original payment; the unique constraint settles the race when the same key is
sent concurrently. A key reused with a different payload is a conflict.
Adjustments and allocation batches accept an optional key with the same rules.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from typing import Any, TypeVar

from sqlalchemy import BigInteger, case, cast, func, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import Account, AccountType, JournalEntry, JournalLine
from app.modules.receivables.models import (
    OPEN_STATUSES,
    AdjustmentKind,
    AllocationRequest,
    Customer,
    CustomerAdjustment,
    CustomerPayment,
    InvoiceAllocation,
    InvoiceStatus,
    ReceivableSettings,
    SalesInvoice,
    SalesInvoiceLine,
)
from app.modules.tax.models import TaxCode
from app.services.posting import PostingError, post_entry

CENT = Decimal("0.01")
ZERO = Decimal("0")


# --- Errors ---------------------------------------------------------------------------


class ReceivablesError(Exception):
    """Base class; ``status_code`` is the HTTP status the router should use."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class NotFound(ReceivablesError):
    status_code = 404


class InvalidState(ReceivablesError):
    """The document is not in a state that allows the operation (409)."""

    status_code = 409


class IdempotencyConflict(ReceivablesError):
    """An idempotency key was reused with a different payload (409)."""

    status_code = 409


class ValidationFailed(ReceivablesError):
    status_code = 422


class OverAllocation(ValidationFailed):
    """An allocation would exceed an outstanding balance or an unallocated amount."""


F = TypeVar("F", bound=Callable[..., Any])


def atomic(func: F) -> F:
    """Roll the session back if the wrapped operation raises for any reason.

    Every function below either commits (directly or through ``post_entry``)
    or raises; this guarantees that a failure - expected (``ReceivablesError``)
    or not - never leaves partial work pending in the caller's session.
    """

    @wraps(func)
    def wrapper(db: Session, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(db, *args, **kwargs)
        except BaseException:
            db.rollback()
            raise

    return wrapper  # type: ignore[return-value]


# --- Money helpers -----------------------------------------------------------------------


def to_money(value: Decimal | int | str) -> Decimal:
    """Round to whole cents, half up (commercial rounding)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def cents(expr: Any) -> Any:
    """SQL expression: ``expr`` as an exact integer number of cents.

    Mirrors the technique of the ledger triggers so that comparisons are exact
    on SQLite (REAL storage) and PostgreSQL (NUMERIC) alike.
    """
    return cast(func.round(expr * 100), BigInteger)


def request_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of a request payload, used to detect idempotency-key reuse."""
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# --- Idempotency: what makes two requests "the same" ---------------------------------
#
# The same rule everywhere (payments, adjustments, allocation batches): the
# fingerprint covers every significant field of the request, amounts in a
# canonical form so 20, 20.0 and 20.00 are the same request. Replaying the
# identical request returns the original result; the same key with any other
# request is an ``IdempotencyConflict``.


def _money_text(value: Decimal | str | int) -> str:
    return f"{to_money(value):.2f}"


def _allocation_items(allocations: Iterable[Any]) -> list[list[Any]]:
    return sorted([[int(a.invoice_id), _money_text(a.amount)] for a in allocations])


def payment_fingerprint(data: Any) -> str:
    return request_fingerprint(
        {
            "operation": "payment",
            "customer_id": data.customer_id,
            "payment_date": data.payment_date,
            "amount": _money_text(data.amount),
            "deposit_account_id": data.deposit_account_id,
            "reference": data.reference,
            "allocations": _allocation_items(data.allocations),
        }
    )


def adjustment_fingerprint(data: Any) -> str:
    return request_fingerprint(
        {
            "operation": "adjustment",
            "customer_id": data.customer_id,
            "kind": AdjustmentKind(data.kind).value,
            "adjustment_date": data.adjustment_date,
            "amount": _money_text(data.amount),
            "reason": data.reason,
            "invoice_id": data.invoice_id,
        }
    )


def allocation_fingerprint(source: Any, data: Any) -> str:
    return request_fingerprint(
        {
            "operation": "allocation",
            "source_type": "payment" if isinstance(source, CustomerPayment) else "adjustment",
            "source_id": source.id,
            "allocations": _allocation_items(data.allocations),
            # Only what the client asked for: a server-resolved default must not
            # make an otherwise identical retry look like a different request.
            "effective_date": getattr(data, "effective_date", None),
        }
    )


# Legacy (v1) encodings: what was stored before amounts were canonicalised. They
# are frozen here field by field - not derived from the current schema - so a
# future schema change cannot silently stop old requests from being verified.
# v1 hashed the request exactly as received (``str(Decimal)`` keeps "100" and
# "100.00" apart), so a legacy request replays only when resent as it was sent.


def legacy_payment_fingerprint(data: Any) -> str:
    return request_fingerprint(
        {
            "customer_id": data.customer_id,
            "payment_date": data.payment_date.isoformat(),
            "amount": str(data.amount),
            "deposit_account_id": data.deposit_account_id,
            "reference": data.reference,
            "allocations": [
                {"invoice_id": a.invoice_id, "amount": str(a.amount)} for a in data.allocations
            ],
        }
    )


def legacy_adjustment_fingerprint(data: Any) -> str:
    return request_fingerprint(
        {
            "customer_id": data.customer_id,
            "kind": AdjustmentKind(data.kind).value,
            "adjustment_date": data.adjustment_date.isoformat(),
            "amount": str(data.amount),
            "reason": data.reason,
            "invoice_id": data.invoice_id,
        }
    )


def _same_request(stored: str | None, *accepted: str) -> bool:
    """True only if the stored fingerprint is one of the accepted encodings.

    A missing fingerprint can never be verified, so it never matches: the
    caller must treat the request as a conflict, not as a retry.
    """
    return stored is not None and stored in accepted


# --- Settings --------------------------------------------------------------------------------

_SETTINGS_ACCOUNT_TYPES: dict[str, tuple[AccountType, ...]] = {
    "receivable_account_id": (AccountType.ASSET,),
    "tax_account_id": (AccountType.LIABILITY,),
    "credit_account_id": (AccountType.REVENUE, AccountType.EXPENSE),
    "write_off_account_id": (AccountType.EXPENSE,),
}


def get_settings(db: Session) -> ReceivableSettings | None:
    return db.get(ReceivableSettings, 1)


def require_settings(db: Session) -> ReceivableSettings:
    settings = get_settings(db)
    if settings is None:
        raise ValidationFailed(
            "Receivables ledger accounts are not configured (PUT /receivables/settings)"
        )
    return settings


@atomic
def save_settings(db: Session, values: dict[str, int]) -> ReceivableSettings:
    for field, allowed in _SETTINGS_ACCOUNT_TYPES.items():
        account = db.get(Account, values[field])
        if account is None:
            raise ValidationFailed(f"{field}: account {values[field]} does not exist")
        if account.type not in allowed:
            names = " or ".join(t.value for t in allowed)
            raise ValidationFailed(f"{field}: account {account.code} must be of type {names}")
        if not account.is_active:
            raise ValidationFailed(f"{field}: account {account.code} is inactive")
    settings = get_settings(db)
    if settings is None:
        settings = ReceivableSettings(id=1, **values)
        db.add(settings)
    else:
        for field, value in values.items():
            setattr(settings, field, value)
    db.commit()
    db.refresh(settings)
    return settings


# --- Lookups -------------------------------------------------------------------------------


def get_customer(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFound(f"Customer {customer_id} not found")
    return customer


def get_invoice(db: Session, invoice_id: int) -> SalesInvoice:
    invoice = db.get(
        SalesInvoice,
        invoice_id,
        options=[selectinload(SalesInvoice.lines), selectinload(SalesInvoice.allocations)],
    )
    if invoice is None:
        raise NotFound(f"Invoice {invoice_id} not found")
    return invoice


def get_payment(db: Session, payment_id: int) -> CustomerPayment:
    payment = db.get(CustomerPayment, payment_id, options=[selectinload(CustomerPayment.allocations)])
    if payment is None:
        raise NotFound(f"Payment {payment_id} not found")
    return payment


def get_adjustment(db: Session, adjustment_id: int) -> CustomerAdjustment:
    adjustment = db.get(
        CustomerAdjustment, adjustment_id, options=[selectinload(CustomerAdjustment.allocations)]
    )
    if adjustment is None:
        raise NotFound(f"Adjustment {adjustment_id} not found")
    return adjustment


def _active_account(db: Session, account_id: int, what: str, *types: AccountType) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise ValidationFailed(f"{what}: account {account_id} does not exist")
    if types and account.type not in types:
        raise ValidationFailed(
            f"{what}: account {account.code} must be of type {' or '.join(t.value for t in types)}"
        )
    if not account.is_active:
        raise ValidationFailed(f"{what}: account {account.code} is inactive")
    return account


# --- Invoice lines and totals ------------------------------------------------------------------


def build_lines(db: Session, lines: Sequence[Any]) -> list[SalesInvoiceLine]:
    """Validate line input and compute net/tax/total amounts with ``Decimal``."""
    account_ids = {line.revenue_account_id for line in lines}
    accounts = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(account_ids)))}
    tax_ids = {line.tax_code_id for line in lines if line.tax_code_id is not None}
    tax_codes = (
        {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.id.in_(tax_ids)))}
        if tax_ids
        else {}
    )

    problems: list[str] = []
    result: list[SalesInvoiceLine] = []
    for position, line in enumerate(lines, start=1):
        account = accounts.get(line.revenue_account_id)
        if account is None:
            problems.append(f"line {position}: account {line.revenue_account_id} does not exist")
        elif not account.is_active:
            problems.append(f"line {position}: account {account.code} is inactive")

        tax_rate: Decimal | None = None
        if line.tax_code_id is not None:
            tax_code = tax_codes.get(line.tax_code_id)
            if tax_code is None:
                problems.append(f"line {position}: tax code {line.tax_code_id} does not exist")
            elif not tax_code.is_active:
                problems.append(f"line {position}: tax code {tax_code.code} is inactive")
            else:
                tax_rate = Decimal(tax_code.rate)

        net = to_money(Decimal(line.quantity) * Decimal(line.unit_price))
        tax = to_money(net * tax_rate) if tax_rate is not None else ZERO
        result.append(
            SalesInvoiceLine(
                line_no=position,
                description=line.description,
                quantity=Decimal(line.quantity),
                unit_price=Decimal(line.unit_price),
                revenue_account_id=line.revenue_account_id,
                tax_code_id=line.tax_code_id,
                tax_rate=tax_rate,
                net_amount=net,
                tax_amount=tax,
                line_total=net + tax,
            )
        )
    if problems:
        raise ValidationFailed("; ".join(problems))
    return result


def apply_totals(invoice: SalesInvoice, lines: list[SalesInvoiceLine]) -> None:
    invoice.lines = lines
    invoice.subtotal = sum((line.net_amount for line in lines), ZERO)
    invoice.tax_total = sum((line.tax_amount for line in lines), ZERO)
    invoice.total = invoice.subtotal + invoice.tax_total


# --- Invoices ----------------------------------------------------------------------------------


@atomic
def create_invoice(db: Session, data: Any) -> SalesInvoice:
    customer = get_customer(db, data.customer_id)
    if not customer.is_active:
        raise ValidationFailed(f"Customer {customer.code} is inactive")
    if db.scalar(
        select(SalesInvoice.id).where(
            SalesInvoice.customer_id == customer.id,
            SalesInvoice.invoice_number == data.invoice_number,
        )
    ):
        raise InvalidState(f"Invoice number {data.invoice_number} already exists for this customer")

    invoice = SalesInvoice(
        customer_id=customer.id,
        invoice_number=data.invoice_number,
        invoice_date=data.invoice_date,
        due_date=data.due_date,
        notes=data.notes,
        status=InvoiceStatus.DRAFT,
    )
    apply_totals(invoice, build_lines(db, data.lines))
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise InvalidState(f"Invoice number {data.invoice_number} already exists for this customer") from exc
    return get_invoice(db, invoice.id)


@atomic
def update_invoice(db: Session, invoice_id: int, data: Any) -> SalesInvoice:
    """Replace header and lines of a *draft*."""
    invoice = get_invoice(db, invoice_id)
    if invoice.status != InvoiceStatus.DRAFT:
        raise InvalidState(f"Invoice {invoice_id} is {invoice.status.value}; only drafts can be edited")
    duplicate = db.scalar(
        select(SalesInvoice.id).where(
            SalesInvoice.customer_id == invoice.customer_id,
            SalesInvoice.invoice_number == data.invoice_number,
            SalesInvoice.id != invoice.id,
        )
    )
    if duplicate:
        raise InvalidState(f"Invoice number {data.invoice_number} already exists for this customer")

    new_lines = build_lines(db, data.lines)
    invoice.invoice_number = data.invoice_number
    invoice.invoice_date = data.invoice_date
    invoice.due_date = data.due_date
    invoice.notes = data.notes
    invoice.lines.clear()  # delete-orphan; flushed before the replacements are inserted
    db.flush()            # so (invoice_id, line_no) cannot collide
    apply_totals(invoice, new_lines)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise InvalidState(f"Invoice {invoice_id} could not be updated: {exc.orig}") from exc
    return get_invoice(db, invoice.id)


def _transition(db: Session, invoice: SalesInvoice, expected: InvoiceStatus, **values: Any) -> None:
    """Conditional status update; raises if another caller changed the invoice first."""
    result = db.execute(
        update(SalesInvoice)
        .where(SalesInvoice.id == invoice.id, SalesInvoice.status == expected)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise InvalidState(
            f"Invoice {invoice.id} is no longer {expected.value} (changed by another request)"
        )


def _post(db: Session, entry: JournalEntry) -> JournalEntry:
    """Flush pending work and post ``entry`` (which commits the whole transaction)."""
    db.flush()
    try:
        return post_entry(db, entry)
    except PostingError as exc:
        db.rollback()
        raise ValidationFailed(f"Ledger posting failed: {exc}") from exc
    except IntegrityError as exc:
        db.rollback()
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity error"
        raise ValidationFailed(f"Ledger posting failed: {reason}") from exc


@atomic
def issue_invoice(db: Session, invoice_id: int) -> SalesInvoice:
    """draft -> issued: posts ``Dr Receivables / Cr Revenue (+ Cr Tax)`` atomically."""
    invoice = get_invoice(db, invoice_id)
    if invoice.status != InvoiceStatus.DRAFT:
        raise InvalidState(f"Invoice {invoice_id} is already {invoice.status.value}")
    if not invoice.lines:
        raise ValidationFailed("An invoice needs at least one line before it can be issued")
    if invoice.total <= ZERO:
        raise ValidationFailed("An invoice total must be greater than zero to be issued")
    if not invoice.customer.is_active:
        raise ValidationFailed(f"Customer {invoice.customer.code} is inactive")
    settings = require_settings(db)

    lines = [
        JournalLine(
            account_id=settings.receivable_account_id,
            debit=invoice.total,
            memo=f"Invoice {invoice.invoice_number}",
        )
    ]
    for line in invoice.lines:
        if line.net_amount > ZERO:
            lines.append(
                JournalLine(account_id=line.revenue_account_id, credit=line.net_amount, memo=line.description)
            )
    tax_by_code: dict[int | None, Decimal] = {}
    for line in invoice.lines:
        if line.tax_amount > ZERO:
            tax_by_code[line.tax_code_id] = tax_by_code.get(line.tax_code_id, ZERO) + line.tax_amount
    if tax_by_code:
        codes = {t.id: t.code for t in db.scalars(select(TaxCode).where(TaxCode.id.in_(tax_by_code)))}
        for tax_code_id, amount in tax_by_code.items():
            memo = f"Tax {codes.get(tax_code_id, tax_code_id)} on invoice {invoice.invoice_number}"
            lines.append(JournalLine(account_id=settings.tax_account_id, credit=amount, memo=memo))

    entry = JournalEntry(
        entry_date=invoice.invoice_date,
        description=f"Sales invoice {invoice.invoice_number} - {invoice.customer.name}",
        reference=f"AR-INV-{invoice.id}",
        lines=lines,
    )
    db.add(entry)
    db.flush()
    _transition(db, invoice, InvoiceStatus.DRAFT, status=InvoiceStatus.ISSUED, journal_entry_id=entry.id)
    _post(db, entry)
    db.expire(invoice)
    return get_invoice(db, invoice.id)


@atomic
def void_invoice(db: Session, invoice_id: int, void_date: date | None = None, reason: str | None = None) -> SalesInvoice:
    """Void a draft (no ledger effect) or an issued invoice with nothing settled (reversing entry)."""
    invoice = get_invoice(db, invoice_id)
    void_date = void_date or date.today()
    if invoice.status == InvoiceStatus.VOID:
        raise InvalidState(f"Invoice {invoice_id} is already void")
    if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.ISSUED) or invoice.amount_settled != ZERO:
        raise InvalidState(
            f"Invoice {invoice_id} is {invoice.status.value} with {invoice.amount_settled:.2f} settled; "
            "reverse the allocations (credit note) before voiding"
        )
    if void_date < invoice.invoice_date:
        raise ValidationFailed("void_date precedes the invoice date")

    if invoice.status == InvoiceStatus.DRAFT:
        _transition(db, invoice, InvoiceStatus.DRAFT, status=InvoiceStatus.VOID, void_date=void_date)
        db.commit()
        db.expire(invoice)
        return get_invoice(db, invoice.id)

    original = db.get(JournalEntry, invoice.journal_entry_id)
    reversal = JournalEntry(
        entry_date=void_date,
        description=f"Void sales invoice {invoice.invoice_number}" + (f" - {reason}" if reason else ""),
        reference=f"AR-VOID-{invoice.id}",
        lines=[
            JournalLine(account_id=line.account_id, debit=line.credit, credit=line.debit, memo=line.memo)
            for line in original.lines
        ],
    )
    db.add(reversal)
    db.flush()
    _transition(
        db,
        invoice,
        InvoiceStatus.ISSUED,
        status=InvoiceStatus.VOID,
        void_date=void_date,
        void_journal_entry_id=reversal.id,
    )
    _post(db, reversal)
    db.expire(invoice)
    return get_invoice(db, invoice.id)


# --- Allocation core -------------------------------------------------------------------------------


def _lock_source(db: Session, source: CustomerPayment | CustomerAdjustment, amount: Decimal) -> None:
    """Reserve ``amount`` of the source's unallocated balance (conditional update)."""
    model = type(source)
    result = db.execute(
        update(model)
        .where(
            model.id == source.id,
            cents(model.amount_allocated + amount) <= cents(model.amount),
        )
        .values(amount_allocated=func.round(model.amount_allocated + amount, 2))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        label = "Payment" if model is CustomerPayment else "Adjustment"
        fresh = db.get(model, source.id)
        available = fresh.unallocated_amount if fresh is not None else ZERO
        raise OverAllocation(
            f"{label} {source.id} has only {available:.2f} unallocated; cannot allocate {amount:.2f}"
        )


def _settle_invoice(db: Session, invoice_id: int, customer_id: int, amount: Decimal) -> None:
    """Add ``amount`` to the invoice's settled total if it fits; otherwise fail the transaction."""
    new_settled = SalesInvoice.amount_settled + amount
    result = db.execute(
        update(SalesInvoice)
        .where(
            SalesInvoice.id == invoice_id,
            SalesInvoice.customer_id == customer_id,
            SalesInvoice.status.in_(OPEN_STATUSES),
            cents(new_settled) <= cents(SalesInvoice.total),
        )
        .values(
            amount_settled=func.round(new_settled, 2),
            status=case(
                (cents(new_settled) == cents(SalesInvoice.total), literal(InvoiceStatus.PAID.value)),
                else_=literal(InvoiceStatus.PARTIALLY_PAID.value),
            ),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        invoice = db.get(SalesInvoice, invoice_id)
        if invoice is None or invoice.customer_id != customer_id:
            raise ValidationFailed(f"Invoice {invoice_id} does not belong to this customer")
        if invoice.status not in OPEN_STATUSES:
            raise InvalidState(f"Invoice {invoice.invoice_number} is {invoice.status.value} and cannot be settled")
        raise OverAllocation(
            f"Allocation of {amount:.2f} exceeds the outstanding balance "
            f"{invoice.balance_due:.2f} of invoice {invoice.invoice_number}"
        )


def _allocate(
    db: Session,
    source: CustomerPayment | CustomerAdjustment,
    allocations: Iterable[Any],
    allocation_date: date,
    idempotency_key: str | None,
    effective_date: date | None = None,
) -> list[InvoiceAllocation]:
    """Apply ``allocations`` (objects with ``invoice_id``/``amount``) from ``source``.

    ``allocation_date`` is the settlement date of the documents; ``effective_date``
    is when the allocation enters the books for "as of" reporting (``None`` =
    with the documents, used when the source is recorded together with its
    allocations). Neither can precede the invoice.

    Does not commit. Locks the source row first, then the invoices in id order.
    """
    items = sorted(((a.invoice_id, to_money(a.amount)) for a in allocations), key=lambda x: x[0])
    if not items:
        return []
    total = sum((amount for _, amount in items), ZERO)
    _lock_source(db, source, total)

    # An allocation takes effect once both documents exist (for "as of" reports).
    invoice_dates = dict(
        db.execute(
            select(SalesInvoice.id, SalesInvoice.invoice_date).where(
                SalesInvoice.id.in_([invoice_id for invoice_id, _ in items])
            )
        ).all()
    )
    rows: list[InvoiceAllocation] = []
    for invoice_id, amount in items:
        _settle_invoice(db, invoice_id, source.customer_id, amount)
        settled_on = max(allocation_date, invoice_dates[invoice_id])
        row = InvoiceAllocation(
            invoice_id=invoice_id,
            payment_id=source.id if isinstance(source, CustomerPayment) else None,
            adjustment_id=source.id if isinstance(source, CustomerAdjustment) else None,
            amount=amount,
            allocation_date=settled_on,
            effective_date=max(effective_date, settled_on) if effective_date else settled_on,
            idempotency_key=idempotency_key,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _source_date(source: CustomerPayment | CustomerAdjustment) -> date:
    return source.payment_date if isinstance(source, CustomerPayment) else source.adjustment_date


def _resolve_effective_date(db: Session, source: CustomerPayment | CustomerAdjustment, data: Any) -> date:
    """The date an allocation batch enters the books.

    Defaults to today, so applying an old payment now cannot rewrite an earlier
    period's aging or statement. An explicit date is accepted (deliberate
    backdating) as long as both documents already existed on it.
    """
    earliest = _source_date(source)
    invoice_dates = db.execute(
        select(SalesInvoice.invoice_date).where(
            SalesInvoice.id.in_([a.invoice_id for a in data.allocations])
        )
    ).scalars()
    for invoice_date in invoice_dates:
        earliest = max(earliest, invoice_date)

    requested = getattr(data, "effective_date", None)
    if requested is None:
        return max(date.today(), earliest)
    if requested < earliest:
        raise ValidationFailed(
            f"effective_date {requested.isoformat()} precedes {earliest.isoformat()}, "
            "the date from which the payment/credit and every invoice exist"
        )
    return requested


def _legacy_rows_exist(db: Session, key: str) -> bool:
    return db.scalar(select(InvoiceAllocation.id).where(InvoiceAllocation.idempotency_key == key).limit(1)) is not None


def _request_record(db: Session, key: str | None) -> AllocationRequest | None:
    if key is None:
        return None
    return db.scalar(select(AllocationRequest).where(AllocationRequest.idempotency_key == key))


def _replay(
    db: Session, record: AllocationRequest, source: CustomerPayment | CustomerAdjustment, data: Any
) -> list[InvoiceAllocation]:
    """Rows of the request recorded under this key, or a conflict if it differed.

    A request whose original payload cannot be verified (a pre-upgrade batch
    that could not be reconstructed reliably, ``request_hash IS NULL``) is
    never treated as identical to anything: every reuse of its key conflicts.
    """
    if record.request_hash is None:
        raise IdempotencyConflict(
            f"Idempotency key {record.idempotency_key!r} belongs to an allocation request recorded "
            "before request fingerprints were kept; it cannot be verified against this request. "
            "Check the payment's allocations and use a new idempotency key if more is needed"
        )
    source_type = "payment" if isinstance(source, CustomerPayment) else "adjustment"
    same_source = record.source_type == source_type and record.source_id == source.id
    if not same_source or not _same_request(record.request_hash, allocation_fingerprint(source, data)):
        raise IdempotencyConflict(
            f"Idempotency key {record.idempotency_key!r} was already used for a different "
            "allocation request"
        )
    return list(
        db.scalars(
            select(InvoiceAllocation)
            .where(InvoiceAllocation.idempotency_key == record.idempotency_key)
            .order_by(InvoiceAllocation.id)
        )
    )


def _allocate_existing(
    db: Session,
    source: CustomerPayment | CustomerAdjustment,
    data: Any,
    allocation_date: date,
) -> tuple[list[InvoiceAllocation], bool]:
    """Allocate from an already-recorded source; returns (rows, created)."""
    key = data.idempotency_key
    # Rows first, then the record: a concurrent twin committing in between can only
    # turn "no record" into "record" (a replay), never make a new key look legacy.
    legacy_rows = key is not None and _legacy_rows_exist(db, key)
    record = _request_record(db, key)
    if record is not None:
        return _replay(db, record, source, data), False
    if legacy_rows:
        # Recorded before request rows existed and never given one (a key the old
        # code used for both a payment and a credit batch): nothing to verify against.
        raise IdempotencyConflict(
            f"Idempotency key {key!r} was used by allocations recorded before request fingerprints "
            "were kept and cannot be verified against this request; use a new idempotency key"
        )

    effective_date = _resolve_effective_date(db, source, data)
    if key is not None:
        db.add(
            AllocationRequest(
                idempotency_key=key,
                payment_id=source.id if isinstance(source, CustomerPayment) else None,
                adjustment_id=source.id if isinstance(source, CustomerAdjustment) else None,
                request_hash=allocation_fingerprint(source, data),
                fingerprint_origin="request",
                effective_date=effective_date,
            )
        )
        try:
            db.flush()  # unique key: a concurrent twin loses here and replays below
        except IntegrityError as exc:
            db.rollback()
            record = _request_record(db, key)
            if record is None:
                reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity error"
                raise ValidationFailed(f"Allocation rejected by the database: {reason}") from exc
            return _replay(db, record, source, data), False

    try:
        rows = _allocate(db, source, data.allocations, allocation_date, key, effective_date)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity error"
        raise ValidationFailed(f"Allocation rejected by the database: {reason}") from exc
    return rows, True


# --- Payments ------------------------------------------------------------------------------------


def _existing_payment(db: Session, data: Any) -> CustomerPayment | None:
    payment = db.scalar(
        select(CustomerPayment)
        .where(CustomerPayment.idempotency_key == data.idempotency_key)
        .options(selectinload(CustomerPayment.allocations))
    )
    if payment is None:
        return None
    if not _same_request(payment.request_hash, payment_fingerprint(data), legacy_payment_fingerprint(data)):
        raise IdempotencyConflict(
            f"Idempotency key {data.idempotency_key!r} was already used for a different payment"
        )
    return payment


@atomic
def register_payment(db: Session, data: Any) -> tuple[CustomerPayment, bool]:
    """Record a customer payment and optionally allocate it to invoices.

    Returns ``(payment, created)``; ``created`` is False when the request was a
    replay of an earlier one with the same idempotency key.
    """
    existing = _existing_payment(db, data)
    if existing is not None:
        return existing, False

    customer = get_customer(db, data.customer_id)
    deposit = _active_account(db, data.deposit_account_id, "deposit_account_id", AccountType.ASSET)
    settings = require_settings(db)
    amount = to_money(data.amount)

    entry = JournalEntry(
        entry_date=data.payment_date,
        description=f"Payment from {customer.name}" + (f" ({data.reference})" if data.reference else ""),
        reference=data.reference,
        lines=[
            JournalLine(account_id=deposit.id, debit=amount, memo="Customer payment"),
            JournalLine(account_id=settings.receivable_account_id, credit=amount, memo=f"Customer {customer.code}"),
        ],
    )
    payment = CustomerPayment(
        customer_id=customer.id,
        payment_date=data.payment_date,
        amount=amount,
        deposit_account_id=deposit.id,
        reference=data.reference,
        idempotency_key=data.idempotency_key,
        request_hash=payment_fingerprint(data),
    )
    db.add(entry)
    db.flush()
    payment.journal_entry_id = entry.id
    db.add(payment)
    try:
        db.flush()  # unique idempotency_key: loses the race -> IntegrityError
    except IntegrityError as exc:
        db.rollback()
        existing = _existing_payment(db, data)
        if existing is not None:
            return existing, False
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity error"
        raise ValidationFailed(f"Payment rejected by the database: {reason}") from exc

    _allocate(db, payment, data.allocations, data.payment_date, None)
    _post(db, entry)
    db.expire(payment)
    return get_payment(db, payment.id), True


@atomic
def allocate_payment(db: Session, payment_id: int, data: Any) -> tuple[CustomerPayment, bool]:
    payment = get_payment(db, payment_id)
    _, created = _allocate_existing(db, payment, data, payment.payment_date)
    db.expire(payment)
    return get_payment(db, payment_id), created


# --- Adjustments -----------------------------------------------------------------------------------


def _existing_adjustment(db: Session, data: Any) -> CustomerAdjustment | None:
    if data.idempotency_key is None:
        return None
    adjustment = db.scalar(
        select(CustomerAdjustment)
        .where(CustomerAdjustment.idempotency_key == data.idempotency_key)
        .options(selectinload(CustomerAdjustment.allocations))
    )
    if adjustment is None:
        return None
    if not _same_request(adjustment.request_hash, adjustment_fingerprint(data), legacy_adjustment_fingerprint(data)):
        raise IdempotencyConflict(
            f"Idempotency key {data.idempotency_key!r} was already used for a different adjustment"
        )
    return adjustment


@atomic
def create_adjustment(db: Session, data: Any) -> tuple[CustomerAdjustment, bool]:
    """Record a credit adjustment or a write-off; posts ``Dr credit/write-off account / Cr receivables``."""
    existing = _existing_adjustment(db, data)
    if existing is not None:
        return existing, False

    customer = get_customer(db, data.customer_id)
    settings = require_settings(db)
    amount = to_money(data.amount)
    kind = AdjustmentKind(data.kind)
    if kind == AdjustmentKind.WRITE_OFF:
        debit_account_id = settings.write_off_account_id
        label = "Write-off"
    else:
        debit_account_id = settings.credit_account_id
        label = "Credit adjustment"

    target: SalesInvoice | None = None
    if data.invoice_id is not None:
        target = get_invoice(db, data.invoice_id)
        if target.customer_id != customer.id:
            raise ValidationFailed(f"Invoice {data.invoice_id} does not belong to customer {customer.code}")
        if target.status not in OPEN_STATUSES:
            raise InvalidState(f"Invoice {target.invoice_number} is {target.status.value} and cannot be adjusted")
        if amount > target.balance_due:
            raise OverAllocation(
                f"{label} of {amount:.2f} exceeds the outstanding balance "
                f"{target.balance_due:.2f} of invoice {target.invoice_number}"
            )

    entry = JournalEntry(
        entry_date=data.adjustment_date,
        description=f"{label} for {customer.name}: {data.reason}",
        reference=f"AR-{kind.value.upper()}" + (f"-INV-{target.id}" if target else ""),
        lines=[
            JournalLine(account_id=debit_account_id, debit=amount, memo=data.reason),
            JournalLine(account_id=settings.receivable_account_id, credit=amount, memo=f"Customer {customer.code}"),
        ],
    )
    db.add(entry)
    db.flush()
    adjustment = CustomerAdjustment(
        customer_id=customer.id,
        kind=kind,
        adjustment_date=data.adjustment_date,
        amount=amount,
        reason=data.reason,
        idempotency_key=data.idempotency_key,
        request_hash=adjustment_fingerprint(data) if data.idempotency_key else None,
        journal_entry_id=entry.id,
    )
    db.add(adjustment)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = _existing_adjustment(db, data)
        if existing is not None:
            return existing, False
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity error"
        raise ValidationFailed(f"Adjustment rejected by the database: {reason}") from exc

    if target is not None:
        _allocate(db, adjustment, [_Alloc(target.id, amount)], data.adjustment_date, None)
    _post(db, entry)
    db.expire(adjustment)
    return get_adjustment(db, adjustment.id), True


class _Alloc:
    __slots__ = ("invoice_id", "amount")

    def __init__(self, invoice_id: int, amount: Decimal) -> None:
        self.invoice_id = invoice_id
        self.amount = amount


@atomic
def allocate_adjustment(db: Session, adjustment_id: int, data: Any) -> tuple[CustomerAdjustment, bool]:
    adjustment = get_adjustment(db, adjustment_id)
    if adjustment.kind != AdjustmentKind.CREDIT:
        raise InvalidState("Only credit adjustments can be allocated; a write-off is applied on creation")
    _, created = _allocate_existing(db, adjustment, data, adjustment.adjustment_date)
    db.expire(adjustment)
    return get_adjustment(db, adjustment_id), created


# --- Queries used by the router ------------------------------------------------------------------------


def list_invoices(
    db: Session,
    customer_id: int | None = None,
    status: InvoiceStatus | None = None,
    open_only: bool = False,
) -> list[SalesInvoice]:
    stmt = select(SalesInvoice).options(
        selectinload(SalesInvoice.lines), selectinload(SalesInvoice.allocations)
    )
    if customer_id is not None:
        stmt = stmt.where(SalesInvoice.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(SalesInvoice.status == status)
    if open_only:
        stmt = stmt.where(SalesInvoice.status.in_(OPEN_STATUSES))
    return list(db.scalars(stmt.order_by(SalesInvoice.id)))


def list_payments(db: Session, customer_id: int | None = None) -> list[CustomerPayment]:
    stmt = select(CustomerPayment).options(selectinload(CustomerPayment.allocations))
    if customer_id is not None:
        stmt = stmt.where(CustomerPayment.customer_id == customer_id)
    return list(db.scalars(stmt.order_by(CustomerPayment.id)))


def list_adjustments(db: Session, customer_id: int | None = None) -> list[CustomerAdjustment]:
    stmt = select(CustomerAdjustment).options(selectinload(CustomerAdjustment.allocations))
    if customer_id is not None:
        stmt = stmt.where(CustomerAdjustment.customer_id == customer_id)
    return list(db.scalars(stmt.order_by(CustomerAdjustment.id)))

