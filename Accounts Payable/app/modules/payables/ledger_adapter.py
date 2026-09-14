"""Module-owned adapter between the AP workflow and the general ledger.

This is the **only** place in the payables module that creates journal rows.
It builds *draft* ``JournalEntry`` / ``JournalLine`` objects with the core models
and hands them to ``app.services.posting.post_entry`` -- the same posting path
``POST /journal-entries/{id}/post`` uses -- so every ledger rule (balance,
completeness, active accounts, the database triggers) applies to AP postings
exactly as it does to manual entries. The adapter never updates or deletes
journal rows; once posted, an entry is history and a correction is a new,
reversing entry (see the "Integration points" section of the package docstring).

Two-phase protocol
------------------
``draft_*_entry`` add an *unposted* entry to the session and flush it, so the
caller can store ``journal_entry_id`` on its own rows inside the same
transaction. ``post`` then runs the core posting service. The core service
commits the session it is given, so ``post`` hands it a second ``Session``
joined to the caller's connection with ``join_transaction_mode="create_savepoint"``:
the core's ``commit()`` releases a SAVEPOINT instead of committing the outer
transaction. The adapter therefore **never commits**; the service layer owns
the transaction boundary, and a unit of work may post several entries (e.g. a
payment correction = reversal + replacement) and still commit or roll back as
one.

Entries produced
----------------
Bill approval (``entry_date`` = invoice date, reference ``AP-BILL-<id>``)::

    Dr <line account>        line_total    (one line per bill line)
        Cr <payable account>       bill total

Vendor payment (``entry_date`` = payment date, reference ``AP-PAY-<id>``)::

    Dr <payable account>     allocated amount (one line per AP account)
    Dr <payment payable acct> unapplied remainder (advance / on-account part, if any)
        Cr <cash account>          payment amount

Early-payment discount taken with a payment (``entry_date`` = the date taken,
reference ``AP-DISC-<id>``)::

    Dr <payable account>     discount amount
        Cr <discount account>      discount amount

Supplier credit / adjustment approval (``entry_date`` = credit date,
reference ``AP-CREDIT-<id>``) -- the mirror image of a bill::

    Dr <payable account>     credit total
        Cr <line account>          line_total (one line per credit line)

Reversal of a payment or credit (``entry_date`` = reversal date, reference
``AP-PAY-REV-<id>`` / ``AP-CREDIT-REV-<id>``): every line of the original
*posted* entry with debit and credit swapped. The original entry is read, never
touched; the reversal is a new entry posted through the same service.
"""

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry, JournalLine
from app.services.posting import EntryAlreadyPosted, EntryNotPostable, post_entry

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from datetime import date

    from app.modules.payables.models import (
        BillDiscount,
        SupplierBill,
        SupplierCredit,
        VendorPayment,
    )


class LedgerPostingRefused(Exception):
    """The core posting service rejected the entry built for an AP document.

    Should not happen for documents validated by the service layer (the adapter
    mirrors the posting rules), but it is the documented failure mode: the
    caller rolls back and nothing is committed.
    """


class LedgerPort(Protocol):
    """What the AP services need from the ledger. Swap in tests or future backends."""

    def draft_bill_entry(self, db: Session, bill: "SupplierBill") -> JournalEntry: ...

    def draft_payment_entry(self, db: Session, payment: "VendorPayment") -> JournalEntry: ...

    def draft_credit_entry(self, db: Session, credit: "SupplierCredit") -> JournalEntry: ...

    def draft_discount_entry(self, db: Session, discount: "BillDiscount") -> JournalEntry: ...

    def draft_reversal_entry(
        self,
        db: Session,
        original: JournalEntry,
        *,
        entry_date: "date",
        reference: str,
        description: str,
    ) -> JournalEntry: ...

    def post(self, db: Session, entry: JournalEntry) -> JournalEntry: ...


class JournalLedgerAdapter:
    """Production implementation backed by the core journal and posting service."""

    def draft_bill_entry(self, db: Session, bill: "SupplierBill") -> JournalEntry:
        if bill.payable_account_id is None:
            raise LedgerPostingRefused("bill has no payable account")
        entry = JournalEntry(
            entry_date=bill.invoice_date,
            description=f"Supplier bill {bill.invoice_number} - {bill.vendor.name}"[:500],
            reference=f"AP-BILL-{bill.id}",
            lines=[
                *(
                    JournalLine(
                        account_id=line.account_id,
                        debit=line.line_total,
                        memo=line.description[:255],
                    )
                    for line in bill.lines
                ),
                JournalLine(
                    account_id=bill.payable_account_id,
                    credit=bill.total,
                    memo=f"AP {bill.vendor.code} {bill.invoice_number}"[:255],
                ),
            ],
        )
        db.add(entry)
        db.flush()
        return entry

    def draft_payment_entry(self, db: Session, payment: "VendorPayment") -> JournalEntry:
        per_account: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        allocated = Decimal("0")
        for allocation in payment.allocations:
            account_id = allocation.bill.payable_account_id
            if account_id is None:
                raise LedgerPostingRefused(f"bill {allocation.bill_id} has no payable account")
            per_account[account_id] += allocation.amount
            allocated += allocation.amount
        remainder = payment.amount - allocated
        if remainder < 0:
            raise LedgerPostingRefused("allocations exceed the payment amount")
        if remainder > 0:
            if payment.payable_account_id is None:
                raise LedgerPostingRefused("an unapplied remainder needs a payable account")
            per_account[payment.payable_account_id] += remainder
        label = payment.reference or f"#{payment.id}"
        entry = JournalEntry(
            entry_date=payment.payment_date,
            description=f"Vendor payment {label} - {payment.vendor.name}"[:500],
            reference=f"AP-PAY-{payment.id}",
            lines=[
                *(
                    JournalLine(account_id=account_id, debit=amount, memo="AP settlement")
                    for account_id, amount in sorted(per_account.items())
                ),
                JournalLine(
                    account_id=payment.cash_account_id,
                    credit=payment.amount,
                    memo=f"Payment to {payment.vendor.code}"[:255],
                ),
            ],
        )
        db.add(entry)
        db.flush()
        return entry

    def draft_credit_entry(self, db: Session, credit: "SupplierCredit") -> JournalEntry:
        if credit.payable_account_id is None:
            raise LedgerPostingRefused("credit has no payable account")
        label = "credit note" if credit.kind.value == "credit_note" else "adjustment"
        entry = JournalEntry(
            entry_date=credit.credit_date,
            description=f"Supplier {label} {credit.credit_number} - {credit.vendor.name}"[:500],
            reference=f"AP-CREDIT-{credit.id}",
            lines=[
                JournalLine(
                    account_id=credit.payable_account_id,
                    debit=credit.total,
                    memo=f"AP {credit.vendor.code} {credit.credit_number}"[:255],
                ),
                *(
                    JournalLine(
                        account_id=line.account_id,
                        credit=line.line_total,
                        memo=line.description[:255],
                    )
                    for line in credit.lines
                ),
            ],
        )
        db.add(entry)
        db.flush()
        return entry

    def draft_discount_entry(self, db: Session, discount: "BillDiscount") -> JournalEntry:
        bill = discount.bill
        if bill.payable_account_id is None:
            raise LedgerPostingRefused(f"bill {bill.id} has no payable account")
        entry = JournalEntry(
            entry_date=discount.taken_on,
            description=f"Early-payment discount {bill.invoice_number} - {bill.vendor.name}"[:500],
            reference=f"AP-DISC-{discount.id}",
            lines=[
                JournalLine(
                    account_id=bill.payable_account_id,
                    debit=discount.amount,
                    memo=f"AP {bill.vendor.code} {bill.invoice_number}"[:255],
                ),
                JournalLine(
                    account_id=discount.discount_account_id,
                    credit=discount.amount,
                    memo="Purchase discount taken",
                ),
            ],
        )
        db.add(entry)
        db.flush()
        return entry

    def draft_reversal_entry(
        self,
        db: Session,
        original: JournalEntry,
        *,
        entry_date: "date",
        reference: str,
        description: str,
    ) -> JournalEntry:
        """Mirror image of a posted entry. The original is only read."""
        if original.status != EntryStatus.POSTED:
            raise LedgerPostingRefused(f"journal entry {original.id} is not posted")
        entry = JournalEntry(
            entry_date=entry_date,
            description=description[:500],
            reference=reference[:100],
            lines=[
                JournalLine(
                    account_id=line.account_id,
                    debit=line.credit,
                    credit=line.debit,
                    memo=f"Reversal of entry {original.id}"[:255],
                )
                for line in original.lines
            ],
        )
        db.add(entry)
        db.flush()
        return entry

    def post(self, db: Session, entry: JournalEntry) -> JournalEntry:
        """Post through the core service inside the caller's transaction (no commit).

        ``post_entry`` validates (completeness, balance, active accounts), applies
        the conditional ``draft -> posted`` update and commits *its* session. Run
        on a session that joined our connection via a SAVEPOINT, that commit only
        releases the savepoint; the caller still decides to commit or roll back.
        """
        connection = db.connection()
        with Session(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as posting_session:
            target = posting_session.get(JournalEntry, entry.id)
            if target is None:
                raise LedgerPostingRefused(f"journal entry {entry.id} was not flushed")
            try:
                post_entry(posting_session, target)
            except (EntryNotPostable, EntryAlreadyPosted) as exc:
                raise LedgerPostingRefused(str(exc)) from exc
        db.expire(entry)  # the status changed underneath this session's copy
        return entry
