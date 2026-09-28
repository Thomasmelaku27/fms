"""Concurrent and duplicate requests: exactly one writer wins, nothing is doubled.

Each worker uses its own ``Session`` (own connection) like separate API
requests would. On PostgreSQL the cash-account advisory lock serialises the
writers; on SQLite the IMMEDIATE transaction does. In both cases the loser
sees the winner's committed state and gets a clean conflict instead of
creating a duplicate.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Account, AccountType, JournalEntry, JournalLine
from app.modules.cash import reconciliation, statements
from app.modules.cash.errors import CashConflict
from app.modules.cash.models import (
    BankStatement,
    BankTransaction,
    CashAccount,
    ReconciliationCertification,
    ReconciliationMatch,
    ReconciliationReopenEvent,
    ReconciliationSession,
    ReconciliationStatus,
)
from app.modules.cash.schemas import (
    BankStatementImport,
    BankTransactionIn,
    CloseRequest,
    MatchCreate,
    ReopenRequest,
)
from app.services.posting import post_entry


@pytest.fixture
def world(engine):
    with Session(engine, expire_on_commit=False) as db:
        bank = Account(code="1000", name="Bank", type=AccountType.ASSET)
        sales = Account(code="4000", name="Sales", type=AccountType.REVENUE)
        db.add_all([bank, sales])
        db.commit()
        cash = CashAccount(ledger_account_id=bank.id, label="Bank")
        db.add(cash)
        db.commit()
        lines = []
        for _ in range(2):
            entry = JournalEntry(entry_date=date(2026, 1, 5), description="receipt", lines=[
                JournalLine(account_id=bank.id, debit=Decimal("100.00")),
                JournalLine(account_id=sales.id, credit=Decimal("100.00")),
            ])
            db.add(entry)
            db.commit()
            post_entry(db, entry)
            lines.append(entry.lines[0].id)
        statement = BankStatement(cash_account_id=cash.id, statement_reference="S1", period_start=date(2026, 1, 1),
                                  period_end=date(2026, 1, 31), opening_balance=Decimal("0"), closing_balance=Decimal("100.00"),
                                  content_hash="h" * 64)
        statement.transactions = [BankTransaction(cash_account_id=cash.id, line_no=1, transaction_date=date(2026, 1, 5),
                                                  description="deposit", amount=Decimal("100.00"), fingerprint="fp1")]
        recon = ReconciliationSession(cash_account_id=cash.id, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
                                      opening_balance=Decimal("0"), closing_balance=Decimal("100.00"))
        db.add_all([statement, recon])
        db.commit()
        return {"cash_id": cash.id, "lines": lines, "txn_id": statement.transactions[0].id, "session_id": recon.id}


def run_parallel(engine, workers):
    """Run each worker(db) in its own thread and session; return the outcomes in order."""
    outcomes = [None] * len(workers)
    barrier = threading.Barrier(len(workers))

    def runner(index, worker):
        with Session(engine, expire_on_commit=False) as db:
            try:
                barrier.wait(timeout=10)
                outcomes[index] = ("ok", worker(db))
            except Exception as exc:  # noqa: BLE001 - the outcome is what we assert on
                db.rollback()
                outcomes[index] = ("error", exc)

    threads = [threading.Thread(target=runner, args=(i, w)) for i, w in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes


def assert_conflict_or_busy(outcome):
    kind, value = outcome
    assert kind == "error"
    # Our conflict, or (SQLite only) the busy timeout if the winner held the lock for too long.
    assert isinstance(value, (CashConflict, OperationalError)), repr(value)


def test_concurrent_full_matches_of_one_bank_transaction(engine, world):
    def matcher(line_id):
        def work(db):
            session = db.get(ReconciliationSession, world["session_id"])
            return reconciliation.add_match(db, session, MatchCreate(bank_transaction_id=world["txn_id"], journal_line_id=line_id)).id
        return work

    outcomes = run_parallel(engine, [matcher(world["lines"][0]), matcher(world["lines"][1])])
    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["error", "ok"], outcomes
    assert_conflict_or_busy(next(o for o in outcomes if o[0] == "error"))
    with Session(engine) as db:
        assert db.scalar(select(func.count(ReconciliationMatch.id))) == 1
        assert db.scalar(select(func.sum(ReconciliationMatch.amount))) == Decimal("100.00")


def test_concurrent_imports_of_the_same_statement(engine, world):
    payload = BankStatementImport(
        cash_account_id=world["cash_id"], statement_reference="S2", period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
        opening_balance=Decimal("100.00"), closing_balance=Decimal("130.00"),
        transactions=[
            BankTransactionIn(transaction_date=date(2026, 2, 3), amount=Decimal("10.00"), description="a", reference="A"),
            BankTransactionIn(transaction_date=date(2026, 2, 4), amount=Decimal("20.00"), description="b", reference="B"),
        ],
    )

    def importer(db):
        r = statements.import_statement(db, payload)
        return (r.statement.id, r.created)

    outcomes = run_parallel(engine, [importer, importer, importer])
    ok = [v for k, v in outcomes if k == "ok"]
    for k, v in outcomes:
        if k == "error":
            assert isinstance(v, OperationalError), repr(v)  # SQLite busy timeout only
    assert len(ok) >= 1
    assert len({sid for sid, _ in ok}) == 1, outcomes
    assert sum(1 for _, created in ok if created) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count(BankStatement.id)).where(BankStatement.statement_reference == "S2")) == 1
        assert db.scalar(select(func.count(BankTransaction.id)).where(BankTransaction.reference.in_(["A", "B"]))) == 2


def test_concurrent_close_only_one_wins_and_certifies_once(engine, world):
    def closer(db):
        session = db.get(ReconciliationSession, world["session_id"])
        return reconciliation.close_session(db, session, CloseRequest(force=True, notes="race")).status

    outcomes = run_parallel(engine, [closer, closer])
    assert sorted(k for k, _ in outcomes) == ["error", "ok"], outcomes
    assert_conflict_or_busy(next(o for o in outcomes if o[0] == "error"))
    with Session(engine) as db:
        assert db.get(ReconciliationSession, world["session_id"]).status == ReconciliationStatus.CLOSED
        certs = db.scalars(select(ReconciliationCertification).where(
            ReconciliationCertification.session_id == world["session_id"])).all()
        assert [c.version for c in certs] == [1]


def test_concurrent_reopen_records_exactly_one_event(engine, world):
    with Session(engine, expire_on_commit=False) as db:
        reconciliation.close_session(db, db.get(ReconciliationSession, world["session_id"]), CloseRequest(force=True, notes="n"))

    def reopener(db):
        session = db.get(ReconciliationSession, world["session_id"])
        return reconciliation.reopen_session(db, session, ReopenRequest(reason="race")).status

    outcomes = run_parallel(engine, [reopener, reopener])
    assert sorted(k for k, _ in outcomes) == ["error", "ok"], outcomes
    assert_conflict_or_busy(next(o for o in outcomes if o[0] == "error"))
    with Session(engine) as db:
        assert db.get(ReconciliationSession, world["session_id"]).status == ReconciliationStatus.OPEN
        assert db.scalar(select(func.count(ReconciliationReopenEvent.id))) == 1
        assert db.get(ReconciliationSession, world["session_id"]).reopen_count == 1


def test_match_racing_with_close_is_consistent_with_the_certification(engine, world):
    """Whichever wins, the certified match list equals what is in the database."""
    def matcher(db):
        session = db.get(ReconciliationSession, world["session_id"])
        return reconciliation.add_match(db, session, MatchCreate(bank_transaction_id=world["txn_id"], journal_line_id=world["lines"][0])).id

    def closer(db):
        session = db.get(ReconciliationSession, world["session_id"])
        return reconciliation.close_session(db, session, CloseRequest(force=True, notes="race")).status

    outcomes = run_parallel(engine, [matcher, closer])
    assert outcomes[1][0] == "ok", outcomes  # the close always succeeds (forced)
    with Session(engine) as db:
        cert = db.scalar(select(ReconciliationCertification).where(ReconciliationCertification.session_id == world["session_id"]))
        live_matches = db.scalar(select(func.count(ReconciliationMatch.id)))
        if outcomes[0][0] == "ok":
            assert live_matches == 1 and cert.matches_count == 1 and cert.is_balanced is True
        else:
            assert_conflict_or_busy(outcomes[0])
            assert live_matches == 0 and cert.matches_count == 0 and cert.is_balanced is False


def test_concurrent_imports_of_overlapping_statements_never_duplicate_lines(engine, world):
    shared = BankTransactionIn(transaction_date=date(2026, 2, 5), amount=Decimal("5.00"), description="shared", reference="SH")
    weekly = BankStatementImport(cash_account_id=world["cash_id"], statement_reference="W", period_start=date(2026, 2, 1),
                                 period_end=date(2026, 2, 7), opening_balance=Decimal("100.00"), closing_balance=Decimal("105.00"),
                                 transactions=[shared])
    monthly = BankStatementImport(cash_account_id=world["cash_id"], statement_reference="M", period_start=date(2026, 2, 1),
                                  period_end=date(2026, 2, 28), opening_balance=Decimal("100.00"), closing_balance=Decimal("106.00"),
                                  transactions=[shared, BankTransactionIn(transaction_date=date(2026, 2, 20), amount=Decimal("1.00"), description="only monthly")])

    outcomes = run_parallel(engine, [lambda db: statements.import_statement(db, weekly).created,
                                     lambda db: statements.import_statement(db, monthly).created])
    for k, v in outcomes:
        assert k == "ok" or isinstance(v, (CashConflict, OperationalError)), outcomes
    with Session(engine) as db:
        assert db.scalar(select(func.count(BankTransaction.id)).where(BankTransaction.reference == "SH")) == 1
        assert db.scalar(select(func.count(BankTransaction.id)).where(BankTransaction.cash_account_id == world["cash_id"])) <= 3


def test_stale_session_object_cannot_match_after_another_session_closed_it(engine, world):
    """Mirrors the posting service test: the in-memory status is stale, the database decides."""
    with Session(engine, expire_on_commit=False) as stale:
        session_obj = stale.get(ReconciliationSession, world["session_id"])
        assert session_obj.status == ReconciliationStatus.OPEN

        with Session(engine, expire_on_commit=False) as other:
            reconciliation.close_session(other, other.get(ReconciliationSession, world["session_id"]), CloseRequest(force=True, notes="x"))

        with pytest.raises(CashConflict, match="not open"):
            reconciliation.add_match(stale, session_obj, MatchCreate(bank_transaction_id=world["txn_id"], journal_line_id=world["lines"][0]))
        with pytest.raises(CashConflict, match="not open"):
            reconciliation.close_session(stale, session_obj, CloseRequest(force=True, notes="y"))


def test_second_matcher_sees_committed_match_of_the_first(engine, world):
    """Sequential sessions (no threads): sums are always read fresh from the database."""
    with Session(engine, expire_on_commit=False) as a, Session(engine, expire_on_commit=False) as b:
        sa_ = a.get(ReconciliationSession, world["session_id"])
        sb_ = b.get(ReconciliationSession, world["session_id"])
        reconciliation.add_match(a, sa_, MatchCreate(bank_transaction_id=world["txn_id"], journal_line_id=world["lines"][0], amount=Decimal("70.00")))
        with pytest.raises(CashConflict, match="exceeds the unmatched bank amount 30.00"):
            reconciliation.add_match(b, sb_, MatchCreate(bank_transaction_id=world["txn_id"], journal_line_id=world["lines"][1], amount=Decimal("30.01")))
        reconciliation.add_match(b, sb_, MatchCreate(bank_transaction_id=world["txn_id"], journal_line_id=world["lines"][1]))
    with Session(engine) as db:
        assert db.scalar(select(func.sum(ReconciliationMatch.amount))) == Decimal("100.00")


def test_duplicate_api_requests_do_not_double_anything(client):
    """The same POST sent twice: one statement, one session, one match, one planned flow."""
    ledger = client.post("/api/v1/accounts", json={"code": "1000", "name": "Bank", "type": "asset"}).json()
    sales = client.post("/api/v1/accounts", json={"code": "4000", "name": "Sales", "type": "revenue"}).json()
    cash = client.post("/api/v1/cash-accounts", json={"ledger_account_id": ledger["id"], "label": "Bank"}).json()
    entry = client.post("/api/v1/journal-entries", json={"entry_date": "2026-01-05", "description": "sale", "lines": [
        {"account_id": ledger["id"], "debit": "10.00"}, {"account_id": sales["id"], "credit": "10.00"}]}).json()
    client.post(f"/api/v1/journal-entries/{entry['id']}/post")
    line_id = entry["lines"][0]["id"]

    statement = {"cash_account_id": cash["id"], "statement_reference": "DUP", "period_start": "2026-01-01", "period_end": "2026-01-31",
                 "opening_balance": "0", "closing_balance": "10.00",
                 "transactions": [{"transaction_date": "2026-01-05", "amount": "10.00", "description": "sale"}]}
    codes = [client.post("/api/v1/cash/bank-statements", json=statement).status_code for _ in range(2)]
    assert codes == [201, 200]
    txn_id = client.get("/api/v1/cash/bank-transactions").json()[0]["id"]
    assert len(client.get("/api/v1/cash/bank-transactions").json()) == 1

    session = {"cash_account_id": cash["id"], "period_start": "2026-01-01", "period_end": "2026-01-31", "opening_balance": "0", "closing_balance": "10.00"}
    codes = [client.post("/api/v1/cash/reconciliations", json=session).status_code for _ in range(2)]
    assert codes == [201, 409]
    sid = client.get("/api/v1/cash/reconciliations").json()[0]["id"]

    match = {"bank_transaction_id": txn_id, "journal_line_id": line_id}
    codes = [client.post(f"/api/v1/cash/reconciliations/{sid}/matches", json=match).status_code for _ in range(2)]
    assert codes == [201, 409]
    assert len(client.get(f"/api/v1/cash/reconciliations/{sid}/matches").json()) == 1

    plan = {"cash_account_id": cash["id"], "expected_date": "2026-02-01", "amount": "-5.00", "description": "fee", "source": "bank", "external_reference": "FEE-1"}
    codes = [client.post("/api/v1/cash/planned-flows", json=plan).status_code for _ in range(2)]
    assert codes == [201, 200]
    assert len(client.get("/api/v1/cash/planned-flows").json()) == 1

    codes = [client.post(f"/api/v1/cash/reconciliations/{sid}/close").status_code for _ in range(2)]
    assert codes == [200, 409]
    codes = [client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": "test"}).status_code for _ in range(2)]
    assert codes == [200, 409]
