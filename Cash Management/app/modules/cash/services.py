"""Shared cash services: posted-only balances, cash account lookup, locking.

Everything here reads the General Ledger as the source of truth. Only lines of
*posted* journal entries are ever counted; drafts are ignored everywhere.
"""

import warnings
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import InvalidRequestError, SAWarning
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry, JournalLine
from app.modules.cash.errors import CashNotFound
from app.modules.cash.models import CashAccount

# Two-decimal zero so that sums of cents always serialise as "0.00".
ZERO = Decimal("0.00")
CENT = Decimal("0.01")

# Namespace for PostgreSQL advisory locks taken by this module (arbitrary constant).
_ADVISORY_LOCK_NAMESPACE = 0x4341_5348  # "CASH"


def quantize(value: Decimal | int | str) -> Decimal:
    """Normalise a monetary amount to whole cents (Decimal, never float)."""
    return Decimal(value).quantize(CENT)


def get_cash_account(db: Session, cash_account_id: int) -> CashAccount:
    account = db.get(CashAccount, cash_account_id)
    if account is None:
        raise CashNotFound(f"Cash account {cash_account_id} not found")
    return account


def cash_balance(db: Session, account_id: int, as_of: date | None = None) -> Decimal:
    """Signed, posted-only balance (debits - credits) of a ledger account; no float conversion."""
    stmt = (
        select(JournalLine.debit, JournalLine.credit)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalLine.account_id == account_id, JournalEntry.status == EntryStatus.POSTED)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.entry_date <= as_of)
    rows = db.execute(stmt).all()
    # Decimal("0") (not ZERO) keeps the historical "0" representation of an empty balance
    # in the original /cash-accounts/{id}/balance endpoint.
    return sum((debit - credit for debit, credit in rows), Decimal("0"))


def lock_cash_account(db: Session, cash_account_id: int) -> None:
    """Serialise writers that reconcile or import for one cash account.

    * PostgreSQL: a transaction-scoped advisory lock keyed by the cash account, so
      two sessions matching the same bank transaction or journal line run one
      after the other and the second one sees the first one's committed matches.
    * SQLite: start the transaction in IMMEDIATE mode (if none is active yet) so a
      concurrent writer waits for the busy timeout instead of failing mid-way.
    Neither touches any row; both are released when the transaction ends.
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": _ADVISORY_LOCK_NAMESPACE, "key": cash_account_id},
        )
    elif bind.dialect.name == "sqlite":
        raw: Any = db.connection().connection.dbapi_connection
        if not getattr(raw, "in_transaction", True):
            raw.execute("BEGIN IMMEDIATE")


def snapshot_read(db: Session) -> bool:
    """Give a multi-statement report one consistent snapshot of the ledger.

    * PostgreSQL: REPEATABLE READ for the current transaction, so a posting
      committed while the report runs is either fully in or fully out.
    * SQLite: start a deferred transaction, so every statement reads the same
      database state (writers wait until the report's transaction ends).

    Best effort: only possible when nothing has been executed yet in the
    session's transaction (always the case for API requests, where the report
    is the first and only operation). Returns whether it was applied.
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        try:
            with warnings.catch_warnings():
                # SQLAlchemy warns (and ignores the option) when the connection is in use already.
                warnings.simplefilter("ignore", SAWarning)
                conn = db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        except InvalidRequestError:
            return False
        return conn.get_isolation_level().upper().replace("_", " ") == "REPEATABLE READ"
    if bind.dialect.name == "sqlite":
        # Inside a transaction every read sees the same state (the shared lock is
        # held until it ends); outside one, start a deferred transaction now.
        raw: Any = db.connection().connection.dbapi_connection
        if not getattr(raw, "in_transaction", True):
            raw.execute("BEGIN")
        return True
    return False
