"""Cash rules enforced by the database itself, transaction rollback, migration.

Like ``test_ledger_rules``, these go straight to the database (ORM and raw
SQL) to prove the cash constraints and triggers hold for application bugs and
manual SQL alike, on SQLite and PostgreSQL.
"""

from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType, EntryStatus, JournalEntry, JournalLine
from app.modules.cash import reconciliation, statements
from app.modules.cash.errors import CashConflict
from app.modules.cash.models import (
    BankStatement,
    BankTransaction,
    CashAccount,
    ReconciliationMatch,
    ReconciliationSession,
)
from app.modules.cash.schemas import (
    BankStatementImport,
    BankTransactionIn,
    CloseRequest,
    MatchCreate,
    ReopenRequest,
)
from app.services.posting import post_entry
from tests.conftest import alembic_config, migrate

CASH_TABLES = {
    "bank_statements",
    "bank_transactions",
    "reconciliation_sessions",
    "reconciliation_matches",
    "cash_flow_classifications",
    "planned_cash_flows",
}


def assert_rejected(session, action, fragment):
    with pytest.raises(IntegrityError, match=fragment):
        action()
        session.commit()
    session.rollback()


@pytest.fixture
def world(session):
    bank = Account(code="1000", name="Bank", type=AccountType.ASSET)
    other = Account(code="1010", name="Other bank", type=AccountType.ASSET)
    sales = Account(code="4000", name="Sales", type=AccountType.REVENUE)
    session.add_all([bank, other, sales])
    session.commit()
    cash = CashAccount(ledger_account_id=bank.id, label="Bank")
    other_cash = CashAccount(ledger_account_id=other.id, label="Other")
    session.add_all([cash, other_cash])
    session.commit()

    def posted(amount, debit=True, account=bank, day=5):
        cash_line = JournalLine(account_id=account.id, debit=Decimal(amount)) if debit else JournalLine(account_id=account.id, credit=Decimal(amount))
        counter = JournalLine(account_id=sales.id, credit=Decimal(amount)) if debit else JournalLine(account_id=sales.id, debit=Decimal(amount))
        entry = JournalEntry(entry_date=date(2026, 1, day), description="e", lines=[cash_line, counter])
        session.add(entry)
        session.commit()
        post_entry(session, entry)  # the posting service, never a direct status update
        return cash_line

    statement = BankStatement(
        cash_account_id=cash.id, statement_reference="S1", period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31), opening_balance=Decimal("0"), closing_balance=Decimal("60.00"),
        content_hash="x" * 64,
    )
    statement.transactions = [
        BankTransaction(cash_account_id=cash.id, line_no=1, transaction_date=date(2026, 1, 5),
                        description="in", amount=Decimal("100.00"), fingerprint="fp-in"),
        BankTransaction(cash_account_id=cash.id, line_no=2, transaction_date=date(2026, 1, 6),
                        description="out", amount=Decimal("-40.00"), fingerprint="fp-out"),
    ]
    recon = ReconciliationSession(
        cash_account_id=cash.id, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
        opening_balance=Decimal("0"), closing_balance=Decimal("60.00"),
    )
    session.add_all([statement, recon])
    session.commit()
    return {
        "bank": bank, "other": other, "sales": sales, "cash": cash, "other_cash": other_cash,
        "posted": posted, "statement": statement, "txn_in": statement.transactions[0],
        "txn_out": statement.transactions[1], "session": recon,
    }


def add_match(session, world, txn, line, amount, recon=None):
    m = ReconciliationMatch(session_id=(recon or world["session"]).id, bank_transaction_id=txn.id,
                            journal_line_id=line.id, amount=Decimal(amount))
    session.add(m)
    return m


# --- Constraints ---------------------------------------------------------------------------


def test_duplicate_transaction_fingerprint_per_cash_account_rejected(session, world):
    def dup():
        session.add(BankTransaction(statement_id=world["statement"].id, cash_account_id=world["cash"].id, line_no=3,
                                    transaction_date=date(2026, 1, 7), description="again", amount=Decimal("1.00"),
                                    fingerprint="fp-in"))
    assert_rejected(session, dup, "uq_bank_transactions_cash_account_fingerprint|UNIQUE|unique")


def test_duplicate_statement_reference_per_cash_account_rejected(session, world):
    def dup():
        session.add(BankStatement(cash_account_id=world["cash"].id, statement_reference="S1", period_start=date(2026, 2, 1),
                                  period_end=date(2026, 2, 28), opening_balance=Decimal("0"), closing_balance=Decimal("0"),
                                  content_hash="y" * 64))
    assert_rejected(session, dup, "uq_bank_statements_cash_account_reference|UNIQUE|unique")
    # ...but the same reference is fine for another cash account.
    session.add(BankStatement(cash_account_id=world["other_cash"].id, statement_reference="S1", period_start=date(2026, 2, 1),
                              period_end=date(2026, 2, 28), opening_balance=Decimal("0"), closing_balance=Decimal("0"),
                              content_hash="y" * 64))
    session.commit()


@pytest.mark.parametrize(
    ("column_values", "fragment"),
    [
        ({"amount": "0"}, "ck_bank_transactions_non_zero"),
        ({"amount": "0.001"}, "ck_bank_transactions"),  # whole_cents on SQLite, rounds to 0 -> non_zero on PostgreSQL
    ],
)
def test_bank_transaction_check_constraints(session, world, column_values, fragment):
    def bad():
        session.add(BankTransaction(statement_id=world["statement"].id, cash_account_id=world["cash"].id, line_no=9,
                                    transaction_date=date(2026, 1, 7), description="bad", fingerprint="fp-bad",
                                    amount=Decimal(column_values["amount"])))
    assert_rejected(session, bad, fragment)


def test_statement_period_order_and_session_uniqueness(session, world):
    def reversed_period():
        session.add(BankStatement(cash_account_id=world["cash"].id, statement_reference="S9", period_start=date(2026, 2, 28),
                                  period_end=date(2026, 2, 1), opening_balance=Decimal("0"), closing_balance=Decimal("0"),
                                  content_hash="z" * 64))
    assert_rejected(session, reversed_period, "ck_bank_statements_period_order")

    def same_period():
        session.add(ReconciliationSession(cash_account_id=world["cash"].id, period_start=date(2026, 1, 1),
                                          period_end=date(2026, 1, 31), opening_balance=Decimal("0"), closing_balance=Decimal("1")))
    assert_rejected(session, same_period, "uq_reconciliation_sessions_cash_account_period|UNIQUE|unique")


def test_match_amount_must_be_positive(session, world):
    line = world["posted"]("100.00")
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], line, "0"), "ck_reconciliation_matches_positive")
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], line, "-1.00"), "ck_reconciliation_matches_positive")


# --- Triggers on reconciliation_matches -------------------------------------------------------


def test_only_posted_lines_can_be_matched(session, world):
    draft = JournalEntry(entry_date=date(2026, 1, 5), description="draft", lines=[
        JournalLine(account_id=world["bank"].id, debit=Decimal("100.00")),
        JournalLine(account_id=world["sales"].id, credit=Decimal("100.00")),
    ])
    session.add(draft)
    session.commit()
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], draft.lines[0], "100.00"),
                    "only lines of posted journal entries")
    # Posting through the service makes the very same line matchable.
    post_entry(session, draft)
    add_match(session, world, world["txn_in"], draft.lines[0], "100.00")
    session.commit()
    assert session.scalar(select(ReconciliationMatch.amount)) == Decimal("100.00")


def test_line_must_belong_to_the_cash_accounts_ledger_account(session, world):
    elsewhere = world["posted"]("100.00", account=world["other"])
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], elsewhere, "100.00"),
                    "not on the ledger account")
    world["posted"]("100.00", debit=False)  # debits Sales: right direction, wrong account
    sales_debit = session.scalar(
        select(JournalLine).where(JournalLine.account_id == world["sales"].id, JournalLine.debit > 0)
    )
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], sales_debit, "100.00"),
                    "not on the ledger account")


def test_direction_is_enforced(session, world):
    debit_line = world["posted"]("40.00")
    credit_line = world["posted"]("100.00", debit=False)
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], credit_line, "100.00"),
                    "inflows match debits")
    assert_rejected(session, lambda: add_match(session, world, world["txn_out"], debit_line, "40.00"),
                    "outflows match credits")


def test_matches_cannot_exceed_the_bank_transaction(session, world):
    a = world["posted"]("60.00")
    b = world["posted"]("60.00")
    add_match(session, world, world["txn_in"], a, "60.00")
    session.commit()
    assert_rejected(session, lambda: add_match(session, world, world["txn_in"], b, "40.01"),
                    "exceed the bank transaction amount")
    add_match(session, world, world["txn_in"], b, "40.00")  # exactly the remainder is fine
    session.commit()

    def bump():
        session.execute(text("UPDATE reconciliation_matches SET amount = 60.00 WHERE journal_line_id = :id"), {"id": b.id})
    assert_rejected(session, bump, "exceed the bank transaction amount")


def test_matches_cannot_exceed_the_journal_line(session, world):
    line = world["posted"]("50.00")
    session.add(BankTransaction(statement_id=world["statement"].id, cash_account_id=world["cash"].id, line_no=3,
                                transaction_date=date(2026, 1, 8), description="in2", amount=Decimal("30.00"),
                                fingerprint="fp-in2"))
    session.commit()
    second = session.scalar(select(BankTransaction).where(BankTransaction.fingerprint == "fp-in2"))
    add_match(session, world, world["txn_in"], line, "30.00")
    session.commit()
    assert_rejected(session, lambda: add_match(session, world, second, line, "20.01"), "exceed the journal line amount")

    def raw():
        session.execute(
            text("INSERT INTO reconciliation_matches (session_id, bank_transaction_id, journal_line_id, amount) "
                 "VALUES (:s, :b, :l, 20.01)"),
            {"s": world["session"].id, "b": second.id, "l": line.id},
        )
    assert_rejected(session, raw, "exceed the journal line amount")


def test_sums_in_triggers_are_exact_to_the_cent(session, world):
    """0.10 + 0.20 + 0.70 fills a 1.00 transaction exactly; one more cent is refused."""
    session.add(BankTransaction(statement_id=world["statement"].id, cash_account_id=world["cash"].id, line_no=3,
                                transaction_date=date(2026, 1, 8), description="one", amount=Decimal("1.00"),
                                fingerprint="fp-one"))
    session.commit()
    one = session.scalar(select(BankTransaction).where(BankTransaction.fingerprint == "fp-one"))
    for amount in ("0.10", "0.20", "0.70"):
        add_match(session, world, one, world["posted"](amount), amount)
        session.commit()
    assert_rejected(session, lambda: add_match(session, world, one, world["posted"]("0.01"), "0.01"),
                    "exceed the bank transaction amount")


def test_matches_of_a_closed_session_are_frozen(session, world):
    line = world["posted"]("100.00")
    other_line = world["posted"]("40.00", debit=False)
    m = add_match(session, world, world["txn_in"], line, "100.00")
    session.commit()
    # Closing is only possible through the service, which certifies first (see test_cash_certification).
    reconciliation.close_session(session, world["session"], CloseRequest(force=True, notes="freeze"))

    assert_rejected(session, lambda: add_match(session, world, world["txn_out"], other_line, "40.00"), "not open")
    assert_rejected(session, lambda: session.delete(m), "not open")

    def raw_delete():
        session.execute(text("DELETE FROM reconciliation_matches WHERE id = :id"), {"id": m.id})
    assert_rejected(session, raw_delete, "not open")

    def raw_update():
        session.execute(text("UPDATE reconciliation_matches SET amount = 50.00 WHERE id = :id"), {"id": m.id})
    assert_rejected(session, raw_update, "not open")

    reconciliation.reopen_session(session, world["session"], ReopenRequest(reason="fix a match"))
    session.delete(m)
    session.commit()
    assert session.scalar(select(ReconciliationMatch)) is None


def test_matched_bank_transactions_cannot_be_deleted_or_changed(session, world):
    line = world["posted"]("100.00")
    add_match(session, world, world["txn_in"], line, "100.00")
    session.commit()

    def delete_txn():
        session.execute(text("DELETE FROM bank_transactions WHERE id = :id"), {"id": world["txn_in"].id})
    assert_rejected(session, delete_txn, "cannot be deleted or changed")

    def change_amount():
        session.execute(text("UPDATE bank_transactions SET amount = 200.00 WHERE id = :id"), {"id": world["txn_in"].id})
    assert_rejected(session, change_amount, "cannot be deleted or changed")

    # Descriptions may still be corrected.
    session.execute(text("UPDATE bank_transactions SET description = 'fixed' WHERE id = :id"), {"id": world["txn_in"].id})
    session.commit()
    # Unmatched transactions can be deleted.
    session.execute(text("DELETE FROM bank_transactions WHERE id = :id"), {"id": world["txn_out"].id})
    session.commit()


# --- Transaction rollback ---------------------------------------------------------------------


def test_failed_import_leaves_nothing_behind(session, world, monkeypatch):
    """If the database rejects any line, the statement and all its lines are rolled back."""
    monkeypatch.setattr(statements, "transaction_fingerprint", lambda *a, **k: "collision")
    payload = BankStatementImport(
        cash_account_id=world["cash"].id, statement_reference="S2", period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28), opening_balance=Decimal("0"), closing_balance=Decimal("2.00"),
        transactions=[
            BankTransactionIn(transaction_date=date(2026, 2, 1), amount=Decimal("1.00"), description="a"),
            BankTransactionIn(transaction_date=date(2026, 2, 2), amount=Decimal("1.00"), description="b"),
        ],
    )
    with pytest.raises(CashConflict):
        statements.import_statement(session, payload)
    assert session.scalar(select(BankStatement).where(BankStatement.statement_reference == "S2")) is None
    assert session.scalar(select(BankTransaction).where(BankTransaction.fingerprint == "collision")) is None
    # The session is usable again afterwards.
    assert session.scalar(select(BankStatement.id).where(BankStatement.statement_reference == "S1")) is not None


def test_failed_match_is_rolled_back_by_the_service(session, world, monkeypatch):
    """When only the database catches a bad match, the service rolls back and reports a conflict."""
    draft = JournalEntry(entry_date=date(2026, 1, 5), description="draft", lines=[
        JournalLine(account_id=world["bank"].id, debit=Decimal("100.00")),
        JournalLine(account_id=world["sales"].id, credit=Decimal("100.00")),
    ])
    session.add(draft)
    session.commit()
    monkeypatch.setattr(reconciliation, "_validate_pair", lambda *a, **k: [])  # simulate an application bug
    with pytest.raises(CashConflict, match="only lines of posted journal entries"):
        reconciliation.add_match(session, world["session"], MatchCreate(bank_transaction_id=world["txn_in"].id,
                                                                        journal_line_id=draft.lines[0].id))
    assert session.scalar(select(ReconciliationMatch)) is None
    assert session.get(JournalEntry, draft.id).status == EntryStatus.DRAFT
    # The session still works: a valid match goes through.
    line = world["posted"]("100.00")
    reconciliation.add_match(session, world["session"], MatchCreate(bank_transaction_id=world["txn_in"].id, journal_line_id=line.id))
    assert session.scalar(select(ReconciliationMatch.amount)) == Decimal("100.00")


def test_multi_statement_rollback_is_atomic(session, world):
    """A failing statement in the same transaction as good rows rolls everything back."""
    line = world["posted"]("100.00")
    session.add(BankStatement(cash_account_id=world["cash"].id, statement_reference="S3", period_start=date(2026, 3, 1),
                              period_end=date(2026, 3, 31), opening_balance=Decimal("0"), closing_balance=Decimal("0"),
                              content_hash="q" * 64))
    session.flush()
    add_match(session, world, world["txn_in"], line, "100.01")  # over-match -> trigger
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    assert session.scalar(select(BankStatement).where(BankStatement.statement_reference == "S3")) is None
    assert session.scalar(select(ReconciliationMatch)) is None


# --- Migration ----------------------------------------------------------------------------------


def test_cash_migration_downgrade_and_upgrade(db_url):
    migrate(db_url)
    command.downgrade(alembic_config(db_url), "fmsmodules01")

    from app.db import make_engine

    engine = make_engine(db_url)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert not CASH_TABLES & tables
        assert "cash_accounts" in tables
        assert "minimum_balance" not in {c["name"] for c in inspect(conn).get_columns("cash_accounts")}
        if engine.dialect.name == "postgresql":
            leftover = conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'fms\\_%'")).scalars().all()
            assert not any("reconciliation" in n or "bank" in n for n in leftover)
    engine.dispose()

    migrate(db_url)
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert CASH_TABLES <= set(inspect(conn).get_table_names())
        assert "minimum_balance" in {c["name"] for c in inspect(conn).get_columns("cash_accounts")}
    engine.dispose()
