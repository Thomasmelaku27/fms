"""Opening-balance initialisation.

A new ledger (or an imported set of account balances) starts with one
balanced ``opening`` journal entry dated ``as_of`` that debits/credits each
account with its opening balance. It goes through the normal posting rules
(posting accounts only, active accounts, open period) and the journal
processing contract, so:

* the same import (``source_id``) can never be applied twice: a retry with
  identical balances returns the existing entry, a retry with different
  balances is a 409 conflict;
* ``opening`` entries can never be reversed (trigger) and are distinguishable
  from ordinary journals, adjustments, reversals and year-end closes by
  ``entry_type``.

If the imported balances do not balance, the difference is posted to the
given ``balancing_account`` (typically an "opening balance equity" account);
without one, unbalanced imports are rejected.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import EntryType
from app.money import MoneyError, as_money
from app.services.actor import PostingContext, system_context
from app.services.exceptions import UnprocessableError
from app.services.journal_processing import (
    JournalRequest,
    JournalRequestLine,
    JournalResult,
    process_journal,
)

SOURCE_TYPE = "opening_balance"
OPERATION_ID = "apply"


@dataclass(frozen=True)
class OpeningBalance:
    account_id: int
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "debit", as_money(self.debit, field="debit"))
            object.__setattr__(self, "credit", as_money(self.credit, field="credit"))
        except MoneyError as exc:
            raise UnprocessableError(str(exc)) from None


def build_request(
    *,
    import_id: str,
    as_of: date,
    balances: list[OpeningBalance],
    balancing_account_id: int | None,
    description: str | None = None,
) -> JournalRequest:
    if not balances:
        raise UnprocessableError("no opening balances given")
    seen: set[int] = set()
    lines: list[JournalRequestLine] = []
    for balance in balances:
        if balance.account_id in seen:
            raise UnprocessableError(f"account {balance.account_id} appears twice")
        seen.add(balance.account_id)
        if balance.debit > 0 and balance.credit > 0:
            raise UnprocessableError(f"account {balance.account_id} has both a debit and a credit")
        if balance.debit == 0 and balance.credit == 0:
            continue  # zero balances need no line
        lines.append(JournalRequestLine(balance.account_id, balance.debit, balance.credit))

    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    difference = total_debit - total_credit
    if difference != 0:
        if balancing_account_id is None:
            raise UnprocessableError(
                f"opening balances do not balance (debits {total_debit:.2f}, credits "
                f"{total_credit:.2f}); provide a balancing account"
            )
        if balancing_account_id in seen:
            raise UnprocessableError("the balancing account cannot also carry an opening balance")
        lines.append(
            JournalRequestLine(
                balancing_account_id,
                debit=-difference if difference < 0 else Decimal("0"),
                credit=difference if difference > 0 else Decimal("0"),
                memo="opening balance difference",
            )
        )
    if not lines:
        raise UnprocessableError("all opening balances are zero")
    return JournalRequest(
        source_type=SOURCE_TYPE,
        source_id=import_id,
        operation_id=OPERATION_ID,
        entry_date=as_of,
        description=description or f"Opening balances ({import_id})",
        reference=import_id,
        entry_type=EntryType.OPENING,
        lines=tuple(lines),
        post=True,
    )


def apply_opening_balances(
    db: Session, request: JournalRequest, *, ctx: PostingContext | None = None, commit: bool = True
) -> JournalResult:
    ctx = (ctx or system_context()).with_source("opening_balance")
    return process_journal(db, request, ctx=ctx, commit=commit, allow_system_types=True)
