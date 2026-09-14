"""AP subledger reports: outstanding, aging, schedules, statements, reconciliation.

Everything here is read-only and reconstructs balances *as of a date* from the
dated settlement rows: bills and credits count from their document date,
payments from their payment date until their reversal date, allocations /
applications from their (un)application dates, discounts from the date taken
until reversed. Nothing is ever deleted on the settlement side, so the same
code run for an earlier date reproduces the earlier report exactly.

The reconciliation compares, per AP control account, the posted ledger balance
with the subledger replay and explains every difference as an exception that
names the vendor, account, document, journal entry, amounts and suspected cause.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Account, EntryStatus, JournalEntry, JournalLine
from app.modules.payables.models import (
    MONEY_MOVED_STATES,
    OperationState,
    PaymentOperation,
    OPEN_BILL_STATUSES,
    SETTLEMENT_BILL_STATUSES,
    SETTLEMENT_CREDIT_STATUSES,
    AllocationKind,
    BillDiscount,
    CreditStatus,
    PaymentAllocation,
    PaymentStatus,
    SupplierBill,
    SupplierCredit,
    Vendor,
    VendorPayment,
)
from app.modules.payables.schemas import (
    AgingBuckets,
    AgingReport,
    AgingRow,
    OutstandingReport,
    OutstandingRow,
    PendingObligation,
    ReconciliationException,
    ReconciliationReport,
    ReconciliationRow,
    ScheduleReport,
    ScheduleReportRow,
    StatementLine,
    StatementTotals,
    VendorStatement,
)
from app.modules.payables.services.common import ZERO, ValidationFailed, bill_query, get_vendor
from app.modules.payables.services.settlement import (
    installments_with_remaining,
    open_bills_as_of,
    unapplied_advances_as_of,
    unapplied_credits_as_of,
)

# --- payment schedule report ----------------------------------------------------------------------


def schedule_report(
    db: Session,
    *,
    vendor_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> ScheduleReport:
    """Upcoming installments still to be paid on approved bills, earliest first."""
    stmt = (
        bill_query()
        .options(selectinload(SupplierBill.schedule), selectinload(SupplierBill.vendor))
        .where(SupplierBill.status.in_(OPEN_BILL_STATUSES))
    )
    if vendor_id is not None:
        stmt = stmt.where(SupplierBill.vendor_id == vendor_id)
    rows: list[ScheduleReportRow] = []
    for bill in db.scalars(stmt):
        for installment in installments_with_remaining(bill.schedule, bill.amount_settled):
            if installment.amount_remaining <= 0:
                continue
            if date_from is not None and installment.due_date < date_from:
                continue
            if date_to is not None and installment.due_date > date_to:
                continue
            rows.append(
                ScheduleReportRow(
                    bill_id=bill.id,
                    vendor_id=bill.vendor_id,
                    vendor_code=bill.vendor.code,
                    invoice_number=bill.invoice_number,
                    **installment.model_dump(),
                )
            )
    rows.sort(key=lambda r: (r.due_date, r.bill_id, r.installment_no))
    return ScheduleReport(
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        total_remaining=sum((r.amount_remaining for r in rows), ZERO),
    )


# --- outstanding payables and aging ---------------------------------------------------------------


def outstanding_report(
    db: Session, *, as_of: date | None = None, vendor_id: int | None = None
) -> OutstandingReport:
    """Open bills as of a date, reconstructed from the dated settlement rows."""
    as_of = as_of or date.today()
    rows: list[OutstandingRow] = []
    for bill in open_bills_as_of(db, as_of, vendor_id):
        paid, credited, discounted = bill.settled_as_of(as_of)
        outstanding = bill.total - paid - credited - discounted
        if outstanding <= 0:
            continue
        rows.append(
            OutstandingRow(
                bill_id=bill.id,
                vendor_id=bill.vendor_id,
                vendor_code=bill.vendor.code,
                invoice_number=bill.invoice_number,
                invoice_date=bill.invoice_date,
                due_date=bill.due_date,
                status=bill.status,
                total=bill.total,
                amount_paid=paid + credited + discounted,
                outstanding=outstanding,
                days_overdue=max((as_of - bill.due_date).days, 0),
                on_hold=bill.on_hold,
            )
        )
    rows.sort(key=lambda r: (r.due_date, r.bill_id))
    return OutstandingReport(
        as_of=as_of, rows=rows, total_outstanding=sum((r.outstanding for r in rows), ZERO)
    )


BUCKETS = ("current", "days_1_30", "days_31_60", "days_61_90", "over_90")


def aging_bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "days_1_30"
    if days_overdue <= 60:
        return "days_31_60"
    if days_overdue <= 90:
        return "days_61_90"
    return "over_90"


def aging_report(db: Session, *, as_of: date | None = None) -> AgingReport:
    """Outstanding payables per vendor in overdue buckets (current, 1-30, 31-60, 61-90, 90+)."""
    outstanding = outstanding_report(db, as_of=as_of)
    per_vendor: dict[int, dict[str, Decimal]] = defaultdict(lambda: dict.fromkeys(BUCKETS, ZERO))
    for row in outstanding.rows:
        per_vendor[row.vendor_id][aging_bucket(row.days_overdue)] += row.outstanding
    names = {
        v.id: (v.code, v.name)
        for v in db.scalars(select(Vendor).where(Vendor.id.in_(per_vendor.keys())))
    }
    rows = [
        AgingRow(
            vendor_id=vendor_id,
            vendor_code=names[vendor_id][0],
            vendor_name=names[vendor_id][1],
            total=sum(buckets.values(), ZERO),
            **buckets,
        )
        for vendor_id, buckets in per_vendor.items()
    ]
    rows.sort(key=lambda r: r.vendor_code)
    totals = {bucket: sum((getattr(r, bucket) for r in rows), ZERO) for bucket in BUCKETS}
    return AgingReport(
        as_of=outstanding.as_of,
        rows=rows,
        totals=AgingBuckets(total=sum(totals.values(), ZERO), **totals),
    )


# --- the subledger event stream -------------------------------------------------------------------

TYPE_ORDER = {
    "bill": 0,
    "credit": 1,
    "credit_reversal": 2,
    "discount": 3,
    "discount_reversal": 4,
    "payment_reversal": 5,
    "payment": 6,
}


def vendor_events(db: Session, vendor_id: int | None = None) -> list[dict]:
    """Every balance-affecting AP transaction, oldest first.

    ``amount`` is signed from the company's point of view: positive increases
    what is owed (bills, reversals of payments / credits / discounts), negative
    reduces it (credits, payments, discounts). ``per_account`` splits the effect
    over AP control accounts. Dates are the ledger dates of the entries.
    """
    events: list[dict] = []

    bills_stmt = select(SupplierBill).where(SupplierBill.status.in_(SETTLEMENT_BILL_STATUSES))
    credits_stmt = select(SupplierCredit).where(
        SupplierCredit.status.in_((*SETTLEMENT_CREDIT_STATUSES, CreditStatus.REVERSED))
    )
    payments_stmt = select(VendorPayment).options(
        selectinload(VendorPayment.allocations).selectinload(PaymentAllocation.bill)
    )
    discounts_stmt = select(BillDiscount).options(selectinload(BillDiscount.bill))
    if vendor_id is not None:
        bills_stmt = bills_stmt.where(SupplierBill.vendor_id == vendor_id)
        credits_stmt = credits_stmt.where(SupplierCredit.vendor_id == vendor_id)
        payments_stmt = payments_stmt.where(VendorPayment.vendor_id == vendor_id)
        discounts_stmt = discounts_stmt.join(BillDiscount.bill).where(
            SupplierBill.vendor_id == vendor_id
        )

    for bill in db.scalars(bills_stmt):
        events.append(
            {
                "vendor_id": bill.vendor_id,
                "date": bill.invoice_date,
                "type": "bill",
                "document_id": bill.id,
                "reference": bill.invoice_number,
                "description": bill.description or "Supplier bill",
                "journal_entry_id": bill.journal_entry_id,
                "amount": bill.total,
                "per_account": {bill.payable_account_id: bill.total},
            }
        )
    for credit in db.scalars(credits_stmt):
        events.append(
            {
                "vendor_id": credit.vendor_id,
                "date": credit.credit_date,
                "type": "credit",
                "document_id": credit.id,
                "reference": credit.credit_number,
                "description": credit.description or f"Supplier {credit.kind.value}",
                "journal_entry_id": credit.journal_entry_id,
                "amount": -credit.total,
                "per_account": {credit.payable_account_id: -credit.total},
            }
        )
        if credit.status == CreditStatus.REVERSED:
            events.append(
                {
                    "vendor_id": credit.vendor_id,
                    "date": credit.reversal_date,
                    "type": "credit_reversal",
                    "document_id": credit.id,
                    "reference": credit.credit_number,
                    "description": f"Reversal: {credit.reversal_reason}",
                    "journal_entry_id": credit.reversal_journal_entry_id,
                    "amount": credit.total,
                    "per_account": {credit.payable_account_id: credit.total},
                }
            )
    for payment in db.scalars(payments_stmt):
        label = payment.reference or f"payment #{payment.id}"
        per_account: dict[int | None, Decimal] = defaultdict(lambda: ZERO)
        allocated = ZERO
        for allocation in payment.allocations:
            if allocation.kind == AllocationKind.ORIGINAL:
                per_account[allocation.bill.payable_account_id] -= allocation.amount
                allocated += allocation.amount
        if payment.amount - allocated > 0:
            per_account[payment.payable_account_id] -= payment.amount - allocated
        events.append(
            {
                "vendor_id": payment.vendor_id,
                "date": payment.payment_date,
                "type": "payment",
                "document_id": payment.id,
                "reference": label,
                "description": f"Payment ({payment.method.value})",
                "journal_entry_id": payment.journal_entry_id,
                "amount": -payment.amount,
                "per_account": dict(per_account),
            }
        )
        if payment.status == PaymentStatus.REVERSED:
            events.append(
                {
                    "vendor_id": payment.vendor_id,
                    "date": payment.reversal_date,
                    "type": "payment_reversal",
                    "document_id": payment.id,
                    "reference": label,
                    "description": f"Reversal: {payment.reversal_reason}",
                    "journal_entry_id": payment.reversal_journal_entry_id,
                    "amount": payment.amount,
                    "per_account": {k: -v for k, v in per_account.items()},
                }
            )
    for discount in db.scalars(discounts_stmt):
        account_id = discount.bill.payable_account_id
        events.append(
            {
                "vendor_id": discount.bill.vendor_id,
                "date": discount.taken_on,
                "type": "discount",
                "document_id": discount.id,
                "reference": discount.bill.invoice_number,
                "description": "Early-payment discount",
                "journal_entry_id": discount.journal_entry_id,
                "amount": -discount.amount,
                "per_account": {account_id: -discount.amount},
            }
        )
        if discount.reversal_date is not None:
            events.append(
                {
                    "vendor_id": discount.bill.vendor_id,
                    "date": discount.reversal_date,
                    "type": "discount_reversal",
                    "document_id": discount.id,
                    "reference": discount.bill.invoice_number,
                    "description": "Reversal of early-payment discount",
                    "journal_entry_id": discount.reversal_journal_entry_id,
                    "amount": discount.amount,
                    "per_account": {account_id: discount.amount},
                }
            )
    events.sort(key=lambda e: (e["date"], TYPE_ORDER[e["type"]], e["document_id"]))
    return events


# --- supplier statements --------------------------------------------------------------------------


def vendor_statement(
    db: Session, vendor_id: int, *, date_from: date, date_to: date
) -> VendorStatement:
    """Supplier statement for a period: opening balance, transactions, closing balance.

    Balances are what the company owes the vendor. The closing balance is
    reconciled against the open-item view as of ``date_to`` (outstanding bills
    minus credits and advances not yet applied); the two agree by construction
    unless data was changed behind the application's back.
    """
    if date_to < date_from:
        raise ValidationFailed("date_to precedes date_from")
    vendor = get_vendor(db, vendor_id)
    events = vendor_events(db, vendor.id)
    opening = sum((e["amount"] for e in events if e["date"] < date_from), ZERO)
    balance = opening
    lines: list[StatementLine] = []
    totals = dict.fromkeys(
        (
            "bills",
            "credits",
            "payments",
            "discounts",
            "payment_reversals",
            "credit_reversals",
            "discount_reversals",
        ),
        ZERO,
    )
    bucket = {
        "bill": "bills",
        "credit": "credits",
        "payment": "payments",
        "discount": "discounts",
        "payment_reversal": "payment_reversals",
        "credit_reversal": "credit_reversals",
        "discount_reversal": "discount_reversals",
    }
    for event in events:
        if not (date_from <= event["date"] <= date_to):
            continue
        balance += event["amount"]
        totals[bucket[event["type"]]] += abs(event["amount"])
        lines.append(
            StatementLine(
                date=event["date"],
                type=event["type"],
                document_id=event["document_id"],
                reference=event["reference"],
                description=event["description"],
                journal_entry_id=event["journal_entry_id"],
                debit=-event["amount"] if event["amount"] < 0 else ZERO,
                credit=event["amount"] if event["amount"] > 0 else ZERO,
                balance=balance,
            )
        )
    outstanding = outstanding_report(db, as_of=date_to, vendor_id=vendor.id).total_outstanding
    credits = unapplied_credits_as_of(db, date_to, vendor_id=vendor.id)
    advances = unapplied_advances_as_of(db, date_to, vendor_id=vendor.id)
    return VendorStatement(
        vendor_id=vendor.id,
        vendor_code=vendor.code,
        vendor_name=vendor.name,
        date_from=date_from,
        date_to=date_to,
        opening_balance=opening,
        lines=lines,
        totals=StatementTotals(**totals),
        closing_balance=balance,
        outstanding_bills=outstanding,
        unapplied_credits=credits,
        unapplied_advances=advances,
        difference=balance - (outstanding - credits - advances),
    )


# --- reconciliation with the ledger ---------------------------------------------------------------


def _control_accounts(db: Session, events: list[dict]) -> dict[int, Account]:
    ids = {a for e in events for a in e["per_account"] if a is not None}
    ids.update(
        db.scalars(select(Vendor.payable_account_id).where(Vendor.payable_account_id.is_not(None)))
    )
    if not ids:
        return {}
    return {
        a.id: a
        for a in db.scalars(select(Account).where(Account.id.in_(ids)).order_by(Account.code))
    }


def reconciliation_report(db: Session, *, as_of: date | None = None) -> ReconciliationReport:
    """AP control accounts: ledger balance vs. the payables subledger, with exceptions.

    The ledger side reads posted journal lines (credit - debit) up to ``as_of``;
    the subledger side replays every vendor's AP transactions in force on that
    date. Each difference is explained:

    * ``entry_mismatch``   a document's posted entry moves the control account by a
                           different amount than the document says;
    * ``entry_not_posted`` a document references a draft / missing journal entry;
    * ``missing_entry``    an approved document has no journal entry at all;
    * ``foreign_entry``    a posted entry on the control account that no AP
                           document accounts for (manual journal entry);
    * ``over_settled``     a bill settled beyond its total (should be impossible).
    """
    as_of = as_of or date.today()
    events = [e for e in vendor_events(db) if e["date"] <= as_of]
    accounts = _control_accounts(db, events)
    vendors = {v.id: v for v in db.scalars(select(Vendor))}
    exceptions: list[ReconciliationException] = []

    def exception(event, account_id, expected, actual, cause, journal_entry_id=None):
        vendor = vendors.get(event["vendor_id"]) if event else None
        account = accounts.get(account_id) if account_id is not None else None
        exceptions.append(
            ReconciliationException(
                account_id=account_id,
                account_code=account.code if account else None,
                vendor_id=vendor.id if vendor else None,
                vendor_code=vendor.code if vendor else None,
                document_type=event["type"] if event else "journal_entry",
                document_id=event["document_id"] if event else None,
                journal_entry_id=journal_entry_id
                if journal_entry_id is not None
                else (event or {}).get("journal_entry_id"),
                expected=expected,
                actual=actual,
                difference=actual - expected,
                cause=cause,
            )
        )

    # Subledger balances and the set of entries AP accounts for.
    subledger: dict[int, Decimal] = defaultdict(lambda: ZERO)
    referenced: dict[int, list[dict]] = defaultdict(list)
    for event in events:
        for account_id, amount in event["per_account"].items():
            if account_id is None:
                exception(event, None, amount, ZERO, "document posts to no payable account")
                continue
            subledger[account_id] += amount
        if event["journal_entry_id"] is None:
            exception(
                event,
                next(iter(event["per_account"]), None),
                event["amount"],
                ZERO,
                "missing_entry: approved document has no journal entry",
            )
        else:
            referenced[event["journal_entry_id"]].append(event)

    # Ledger side: posted lines on the control accounts up to as_of, grouped by entry.
    ledger: dict[int, Decimal] = defaultdict(lambda: ZERO)
    per_entry: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    entry_meta: dict[int, tuple] = {}
    if accounts:
        rows = db.execute(
            select(
                JournalLine.account_id,
                JournalLine.entry_id,
                JournalLine.debit,
                JournalLine.credit,
                JournalEntry.entry_date,
                JournalEntry.reference,
            )
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                JournalLine.account_id.in_(accounts.keys()),
                JournalEntry.status == EntryStatus.POSTED,
                JournalEntry.entry_date <= as_of,
            )
        ).all()
        for account_id, entry_id, debit, credit, entry_date, reference in rows:
            ledger[account_id] += credit - debit
            per_entry[(entry_id, account_id)] += credit - debit
            entry_meta[entry_id] = (entry_date, reference)

    # Documents whose entries are not posted / do not move the account as they should.
    entry_status = {}
    if referenced:
        entry_status = dict(
            db.execute(
                select(JournalEntry.id, JournalEntry.status).where(JournalEntry.id.in_(referenced))
            ).all()
        )
    for entry_id, docs in referenced.items():
        if entry_status.get(entry_id) != EntryStatus.POSTED:
            for event in docs:
                for account_id, amount in event["per_account"].items():
                    exception(
                        event,
                        account_id,
                        amount,
                        ZERO,
                        "entry_not_posted: journal entry is missing or still a draft",
                    )
            continue
        expected_per_account: dict[int, Decimal] = defaultdict(lambda: ZERO)
        for event in docs:
            for account_id, amount in event["per_account"].items():
                if account_id is not None:
                    expected_per_account[account_id] += amount
        for account_id, expected in expected_per_account.items():
            actual = per_entry.get((entry_id, account_id), ZERO)
            if actual != expected:
                exception(
                    docs[0],
                    account_id,
                    expected,
                    actual,
                    "entry_mismatch: posted entry moves the control account by a "
                    "different amount than the AP document",
                    journal_entry_id=entry_id,
                )
    # Posted lines on control accounts that no AP document explains.
    for (entry_id, account_id), actual in sorted(per_entry.items()):
        if entry_id not in referenced and actual != 0:
            entry_date, reference = entry_meta[entry_id]
            exception(
                None,
                account_id,
                ZERO,
                actual,
                f"foreign_entry: posted journal entry {entry_id} ({reference or 'no reference'}, "
                f"{entry_date}) on the control account was not originated by payables",
                journal_entry_id=entry_id,
            )
    # Over-settled bills (defensive; the database forbids it).
    for bill in open_bills_as_of(db, as_of):
        if bill.outstanding_as_of(as_of) < 0:
            exception(
                {
                    "vendor_id": bill.vendor_id,
                    "type": "bill",
                    "document_id": bill.id,
                    "journal_entry_id": bill.journal_entry_id,
                },
                bill.payable_account_id,
                bill.total,
                bill.total - bill.outstanding_as_of(as_of),
                "over_settled: bill settled beyond its total",
            )

    rows = [
        ReconciliationRow(
            account_id=account.id,
            account_code=account.code,
            account_name=account.name,
            ledger_balance=ledger[account.id],
            subledger_balance=subledger[account.id],
            difference=ledger[account.id] - subledger[account.id],
        )
        for account in accounts.values()
    ]
    # Confirmed transfers not yet in the books: cash has left the bank but neither the
    # subledger nor the ledger shows it. They are reconciliation obligations, not
    # differences (both sides agree), and must be reported as such.
    # Effective-dated like every other report: a transfer recorded by a correction dated after
    # ``as_of`` was still an obligation on ``as_of``.
    obligations = []
    candidates = list(
        db.scalars(
            select(PaymentOperation)
            .where(
                PaymentOperation.state.in_(MONEY_MOVED_STATES)
                | (PaymentOperation.gateway_status == "accepted"),
                PaymentOperation.payment_date <= as_of,
            )
            .order_by(PaymentOperation.id)
        )
    )
    linked_ids = {op.payment_id for op in candidates if op.payment_id is not None}
    linked = (
        {p.id: p for p in db.scalars(select(VendorPayment).where(VendorPayment.id.in_(linked_ids)))}
        if linked_ids
        else {}
    )
    replaced = (
        {
            r.corrects_payment_id
            for r in db.scalars(
                select(VendorPayment).where(
                    VendorPayment.corrects_payment_id.in_(linked_ids),
                    VendorPayment.status == PaymentStatus.RECORDED,
                    VendorPayment.payment_date <= as_of,
                )
            )
        }
        if linked_ids
        else set()
    )
    for op in candidates:
        if op.state == OperationState.COMPLETED:
            payment = linked.get(op.payment_id)
            in_force = (
                payment is not None
                and payment.payment_date <= as_of
                and (
                    payment.status == PaymentStatus.RECORDED
                    or (payment.reversal_date is not None and payment.reversal_date > as_of)
                    or payment.id in replaced
                )
            )
            if in_force:
                continue
        obligations.append(
            PendingObligation(
                operation_uid=op.operation_uid,
                vendor_id=op.vendor_id,
                vendor_code=vendors[op.vendor_id].code if op.vendor_id in vendors else None,
                amount=op.amount,
                payment_date=op.payment_date,
                state=op.state.value,
                gateway_confirmation_ref=op.gateway_confirmation_ref,
                cash_account_id=op.cash_account_id,
                accounting_status=op.accounting_status,
            )
        )
    return ReconciliationReport(
        as_of=as_of,
        rows=rows,
        exceptions=exceptions,
        total_ledger=sum((r.ledger_balance for r in rows), ZERO),
        total_subledger=sum((r.subledger_balance for r in rows), ZERO),
        total_difference=sum((r.difference for r in rows), ZERO),
        pending_obligations=obligations,
        pending_obligations_total=sum((o.amount for o in obligations), ZERO),
    )
