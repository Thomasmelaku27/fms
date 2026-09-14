"""Ledger rules enforced by the database itself (CHECK constraints and triggers).

These tests go straight to the database (ORM and raw SQL), bypassing the API and
its Pydantic validation, to prove the rules hold for application bugs and manual
SQL alike. They run against SQLite and, when configured, PostgreSQL.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Account, AccountType, EntryStatus, JournalEntry, JournalLine


@pytest.fixture
def accounts(session):
    cash = Account(code="1000", name="Cash", type=AccountType.ASSET)
    sales = Account(code="4000", name="Sales", type=AccountType.REVENUE)
    session.add_all([cash, sales])
    session.commit()
    return cash, sales


def make_entry(session, accounts, amounts, status=EntryStatus.DRAFT):
    """Create an entry with one line per (debit, credit) pair in ``amounts``."""
    cash, sales = accounts
    entry = JournalEntry(
        entry_date=date(2026, 1, 15),
        description="test",
        status=status,
        lines=[
            JournalLine(
                account_id=cash.id if Decimal(d) > 0 else sales.id,
                debit=Decimal(d),
                credit=Decimal(c),
            )
            for d, c in amounts
        ],
    )
    session.add(entry)
    session.commit()
    return entry


def post(session, entry):
    entry.status = EntryStatus.POSTED
    session.commit()


def posted_entry(session, accounts):
    entry = make_entry(session, accounts, [("100.00", "0"), ("0", "100.00")])
    post(session, entry)
    return entry


def assert_rejected(session, action, message_fragment):
    with pytest.raises(IntegrityError, match=message_fragment):
        action()
        session.commit()
    session.rollback()


# --- CHECK constraints on journal_lines -----------------------------------------


@pytest.mark.parametrize(
    ("debit", "credit", "constraint"),
    [
        ("-5.00", "0", "ck_journal_lines_non_negative"),
        ("0", "-5.00", "ck_journal_lines_non_negative"),
        ("5.00", "5.00", "ck_journal_lines_not_both_sides"),
    ],
)
def test_invalid_line_amounts_rejected(session, accounts, debit, credit, constraint):
    assert_rejected(session, lambda: make_entry(session, accounts, [(debit, credit)]), constraint)


def test_line_without_amount_allowed_in_draft_but_blocks_posting(session, accounts):
    # Balanced apart from one line that has no amount yet.
    entry = make_entry(session, accounts, [("100.00", "0"), ("0", "0"), ("0", "100.00")])
    assert session.get(JournalEntry, entry.id).status == EntryStatus.DRAFT

    assert_rejected(session, lambda: post(session, entry), "lines without an amount")

    # Completing the line (or removing it) makes the entry postable.
    session.delete(entry.lines[1])
    session.commit()
    post(session, entry)
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED


def test_valid_lines_accepted(session, accounts):
    entry = make_entry(session, accounts, [("5.00", "0"), ("0", "5.00")])
    assert len(session.get(JournalEntry, entry.id).lines) == 2


def test_check_constraints_apply_to_raw_sql(session, accounts):
    entry = make_entry(session, accounts, [("5.00", "0"), ("0", "5.00")])

    def raw_update():
        session.execute(
            text("UPDATE journal_lines SET debit = -1 WHERE entry_id = :id"), {"id": entry.id}
        )

    assert_rejected(session, raw_update, "ck_journal_lines_non_negative")


# --- Posting requires balanced lines ----------------------------------------------


def test_posting_balanced_entry_succeeds(session, accounts):
    entry = make_entry(session, accounts, [("100.00", "0"), ("0", "60.00"), ("0", "40.00")])
    post(session, entry)
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED


def test_posting_unbalanced_entry_rejected(session, accounts):
    entry = make_entry(session, accounts, [("100.00", "0"), ("0", "99.99")])
    assert_rejected(session, lambda: post(session, entry), "must have lines that balance")
    assert session.get(JournalEntry, entry.id).status == EntryStatus.DRAFT


def test_posting_entry_without_lines_rejected(session, accounts):
    entry = JournalEntry(entry_date=date(2026, 1, 15), description="empty")
    session.add(entry)
    session.commit()
    assert_rejected(session, lambda: post(session, entry), "must have lines that balance")


def test_inserting_entry_directly_as_posted_rejected(session, accounts):
    # An entry has no lines at the moment it is inserted, so it cannot balance.
    assert_rejected(
        session,
        lambda: make_entry(session, accounts, [], status=EntryStatus.POSTED),
        "must have lines that balance",
    )


def test_posting_via_raw_sql_is_checked(session, accounts):
    entry = make_entry(session, accounts, [("100.00", "0"), ("0", "1.00")])

    def raw_post():
        session.execute(
            text("UPDATE journal_entries SET status = 'posted' WHERE id = :id"), {"id": entry.id}
        )

    assert_rejected(session, raw_post, "must have lines that balance")


def test_balance_check_is_exact_to_the_cent(session, accounts):
    # 0.10 + 0.20 != 0.30 in binary floating point; the check must not be fooled
    # by that on backends that store NUMERIC as REAL (SQLite).
    entry = make_entry(session, accounts, [("0.10", "0"), ("0.20", "0"), ("0", "0.30")])
    post(session, entry)
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED

    off_by_a_cent = make_entry(session, accounts, [("0.10", "0"), ("0.20", "0"), ("0", "0.31")])
    assert_rejected(session, lambda: post(session, off_by_a_cent), "must have lines that balance")


def test_balance_check_sums_many_cent_lines_exactly(session, accounts):
    # 1000 x 0.01 is not 10.00 in floating point; the totals must still be compared exactly.
    entry = make_entry(session, accounts, [("0.01", "0")] * 1000 + [("0", "10.00")])
    post(session, entry)
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED

    off_by_a_cent = make_entry(session, accounts, [("0.01", "0")] * 1000 + [("0", "10.01")])
    assert_rejected(session, lambda: post(session, off_by_a_cent), "must have lines that balance")


def test_sub_cent_amounts_are_never_stored(session, accounts, engine):
    """A mismatch below one cent can never reach the balance check.

    SQLite does not enforce NUMERIC scale, so ck_journal_lines_whole_cents rejects
    the row. PostgreSQL rounds to the declared scale of NUMERIC(18, 2) on write,
    so the stored value is already whole cents (standard NUMERIC behaviour).
    """
    entry = make_entry(session, accounts, [("0", "100.00")])
    cash, _ = accounts
    insert = text(
        "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
        "VALUES (:entry, :account, 100.004, 0)"
    )
    params = {"entry": entry.id, "account": cash.id}

    if engine.dialect.name == "sqlite":
        assert_rejected(session, lambda: session.execute(insert, params), "whole_cents")
        assert_rejected(session, lambda: post(session, entry), "must have lines that balance")
    else:
        session.execute(insert, params)
        session.commit()
        stored = session.execute(
            text("SELECT debit FROM journal_lines WHERE entry_id = :id AND debit > 0"),
            {"id": entry.id},
        ).scalar_one()
        assert stored == Decimal("100.00")

    stored_sub_cent = session.execute(
        text("SELECT COUNT(*) FROM journal_lines WHERE ROUND(debit, 2) <> debit")
    ).scalar_one()
    assert stored_sub_cent == 0


# --- Posted history is immutable -------------------------------------------------


def test_lines_of_posted_entry_cannot_be_updated(session, accounts):
    entry = posted_entry(session, accounts)

    def change_amount():
        entry.lines[0].debit = Decimal("999.00")

    assert_rejected(session, change_amount, "posted journal entry cannot be modified")

    def raw_change_memo():
        session.execute(
            text("UPDATE journal_lines SET memo = 'tampered' WHERE entry_id = :id"),
            {"id": entry.id},
        )

    assert_rejected(session, raw_change_memo, "posted journal entry cannot be modified")


def test_lines_of_posted_entry_cannot_be_deleted(session, accounts):
    entry = posted_entry(session, accounts)

    def raw_delete():
        session.execute(text("DELETE FROM journal_lines WHERE entry_id = :id"), {"id": entry.id})

    assert_rejected(session, raw_delete, "posted journal entry cannot be modified")
    assert len(session.get(JournalEntry, entry.id).lines) == 2


def test_lines_cannot_be_added_to_posted_entry(session, accounts):
    entry = posted_entry(session, accounts)
    cash, _ = accounts

    def add_line():
        session.add(JournalLine(entry_id=entry.id, account_id=cash.id, debit=Decimal("1.00")))

    assert_rejected(session, add_line, "posted journal entry cannot be modified")


def test_line_cannot_be_moved_into_posted_entry(session, accounts):
    posted = posted_entry(session, accounts)
    draft = make_entry(session, accounts, [("1.00", "0")])

    def move_line():
        session.execute(
            text("UPDATE journal_lines SET entry_id = :posted WHERE entry_id = :draft"),
            {"posted": posted.id, "draft": draft.id},
        )

    assert_rejected(session, move_line, "posted journal entry cannot be modified")


def test_posted_entry_cannot_be_unposted_or_deleted(session, accounts):
    entry = posted_entry(session, accounts)

    def unpost():
        entry.status = EntryStatus.DRAFT

    assert_rejected(session, unpost, "cannot be modified or deleted")

    def raw_delete():
        session.execute(text("DELETE FROM journal_entries WHERE id = :id"), {"id": entry.id})

    assert_rejected(session, raw_delete, "cannot be modified or deleted")
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED


def test_posted_entry_header_cannot_be_changed(session, accounts):
    entry = posted_entry(session, accounts)

    def change_description():
        entry.description = "tampered"

    assert_rejected(session, change_description, "cannot be modified or deleted")

    for column, value in (("reference", "'INV-1'"), ("entry_date", "'2030-01-01'")):

        def raw_change():
            session.execute(
                text(f"UPDATE journal_entries SET {column} = {value} WHERE id = :id"),
                {"id": entry.id},
            )

        assert_rejected(session, raw_change, "cannot be modified or deleted")

    session.expire_all()
    fresh = session.get(JournalEntry, entry.id)
    assert fresh.description == "test"
    assert fresh.reference is None
    assert fresh.entry_date == date(2026, 1, 15)


def test_draft_may_be_unbalanced_until_posted(session, accounts):
    """Drafts can be incomplete/unbalanced; the balance rule only applies at posting."""
    # Single line, unbalanced: fine as a draft.
    entry = make_entry(session, accounts, [("100.00", "0")])
    assert session.get(JournalEntry, entry.id).status == EntryStatus.DRAFT

    # Still unbalanced after partial completion: draft is fine, posting is not.
    _, sales = accounts
    entry.lines.append(JournalLine(account_id=sales.id, credit=Decimal("60.00")))
    session.commit()
    assert_rejected(session, lambda: post(session, entry), "must have lines that balance")
    assert session.get(JournalEntry, entry.id).status == EntryStatus.DRAFT

    # Complete it, then posting succeeds and the entry becomes immutable.
    entry.lines.append(JournalLine(account_id=sales.id, credit=Decimal("40.00")))
    session.commit()
    post(session, entry)
    assert session.get(JournalEntry, entry.id).status == EntryStatus.POSTED

    def edit_after_posting():
        entry.lines[0].debit = Decimal("1.00")

    assert_rejected(session, edit_after_posting, "posted journal entry cannot be modified")


def test_draft_entries_remain_editable(session, accounts):
    entry = make_entry(session, accounts, [("100.00", "0"), ("0", "100.00")])
    cash, _ = accounts

    entry.lines[0].debit = Decimal("50.00")
    entry.lines.append(JournalLine(account_id=cash.id, debit=Decimal("50.00")))
    entry.description = "edited draft"
    entry.reference = "REF-1"
    session.commit()
    assert len(session.get(JournalEntry, entry.id).lines) == 3
    assert session.get(JournalEntry, entry.id).description == "edited draft"

    session.delete(entry)
    session.commit()
    assert session.get(JournalEntry, entry.id) is None
