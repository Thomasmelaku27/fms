"""Cash management API.

Existing endpoints (``/cash-accounts``) are kept; the new features live under
``/cash/...``. Service errors (``app.modules.cash.errors``) are translated to
HTTP responses here so the services stay reusable outside the API.
"""

from datetime import date
from decimal import Decimal
from functools import wraps

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Account, AccountType
from app.modules.cash import planning, projection, reconciliation, reports, statements
from app.modules.cash.errors import CashError
from app.modules.cash.models import CashAccount, PlannedCashFlowStatus
from app.modules.cash.schemas import (
    AutoMatchRequest,
    AutoMatchResult,
    BankStatementDetail,
    BankStatementImport,
    BankStatementImportResult,
    BankStatementOut,
    BankTransactionOut,
    CashAccountIn,
    CashAccountOut,
    CashAccountUpdate,
    CashBalance,
    CashBalancesReport,
    CashFlowReport,
    CertificationDetail,
    CertificationHistory,
    ClassificationIn,
    ClassificationOut,
    CloseRequest,
    DailyBalancesReport,
    IntegrityReport,
    LiquidityProjection,
    MatchCreate,
    MatchOut,
    PlannedCashFlowIn,
    PlannedCashFlowOut,
    PlannedCashFlowStatusUpdate,
    ReconciliationSessionCreate,
    ReconciliationSessionOut,
    ReconciliationSessionUpdate,
    ReconciliationSummary,
    ReopenRequest,
    UnmatchedItems,
)
from app.modules.cash.services import cash_balance, get_cash_account

router = APIRouter(tags=["cash"])


def translate_errors(func):
    """Map ``CashError`` subclasses to ``HTTPException`` with their status code."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except CashError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    return wrapper


# --- Cash accounts (existing API, extended) ---------------------------------------------


@router.post("/cash-accounts", response_model=CashAccountOut, status_code=201)
def link(data: CashAccountIn, db: Session = Depends(get_db)):
    account = db.get(Account, data.ledger_account_id)
    if account is None or account.type != AccountType.ASSET:
        raise HTTPException(422, "Active asset ledger account required")
    if not account.is_active:
        raise HTTPException(422, "Account inactive")
    if db.scalar(select(CashAccount.id).where(CashAccount.ledger_account_id == account.id)):
        raise HTTPException(409, "Already linked")
    x = CashAccount(**data.model_dump())
    db.add(x)
    db.commit()
    db.refresh(x)
    return x


@router.get("/cash-accounts", response_model=list[CashAccountOut])
def all_cash(db: Session = Depends(get_db)):
    return list(db.scalars(select(CashAccount).order_by(CashAccount.id)))


@router.get("/cash-accounts/{cash_id}", response_model=CashAccountOut)
@translate_errors
def get_cash(cash_id: int, db: Session = Depends(get_db)):
    return get_cash_account(db, cash_id)


@router.patch("/cash-accounts/{cash_id}", response_model=CashAccountOut)
@translate_errors
def update_cash(cash_id: int, data: CashAccountUpdate, db: Session = Depends(get_db)):
    x = get_cash_account(db, cash_id)
    if data.label is not None:
        x.label = data.label
    if data.clear_minimum_balance:
        x.minimum_balance = None
    elif data.minimum_balance is not None:
        x.minimum_balance = data.minimum_balance
    db.commit()
    db.refresh(x)
    return x


@router.get("/cash-accounts/{cash_id}/balance", response_model=CashBalance)
def balance(cash_id: int, as_of: date | None = None, db: Session = Depends(get_db)):
    x = db.get(CashAccount, cash_id)
    if x is None:
        raise HTTPException(404, "Cash account not found")
    return CashBalance(
        id=x.id,
        ledger_account_id=x.ledger_account_id,
        label=x.label,
        minimum_balance=x.minimum_balance,
        balance=cash_balance(db, x.ledger_account_id, as_of=as_of),
        as_of=as_of,
    )


# --- Bank statements --------------------------------------------------------------------


@router.post(
    "/cash/bank-statements",
    response_model=BankStatementImportResult,
    status_code=201,
    responses={200: {"description": "Identical statement already imported; nothing changed"}},
)
@translate_errors
def import_statement(data: BankStatementImport, response: Response, db: Session = Depends(get_db)):
    result = statements.import_statement(db, data)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return {
        "statement": result.statement,
        "created": result.created,
        "imported_count": result.imported_count,
        "skipped_duplicates": result.skipped_duplicates,
        "transactions": statements.describe_transactions(db, result.statement.transactions),
        "late_transactions": result.late_transactions,
        "warnings": result.warnings,
    }


@router.get("/cash/bank-statements", response_model=list[BankStatementOut])
def list_statements(cash_account_id: int | None = None, db: Session = Depends(get_db)):
    return statements.list_statements(db, cash_account_id)


@router.get("/cash/bank-statements/{statement_id}", response_model=BankStatementDetail)
@translate_errors
def get_statement(statement_id: int, db: Session = Depends(get_db)):
    stmt = statements.get_statement(db, statement_id)
    return BankStatementDetail(
        **BankStatementOut.model_validate(stmt).model_dump(),
        transactions=statements.describe_transactions(db, stmt.transactions),
    )


@router.delete("/cash/bank-statements/{statement_id}", status_code=204)
@translate_errors
def delete_statement(statement_id: int, db: Session = Depends(get_db)):
    statements.delete_statement(db, statement_id)
    return Response(status_code=204)


@router.get("/cash/bank-transactions", response_model=list[BankTransactionOut])
def list_transactions(
    cash_account_id: int | None = None,
    status: str | None = Query(default=None, pattern="^(unmatched|partially_matched|matched)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
):
    return statements.list_transactions(db, cash_account_id, status, date_from, date_to)


# --- Reconciliation -----------------------------------------------------------------------


@router.post("/cash/reconciliations", response_model=ReconciliationSessionOut, status_code=201)
@translate_errors
def create_reconciliation(data: ReconciliationSessionCreate, db: Session = Depends(get_db)):
    return reconciliation.create_session(db, data)


@router.get("/cash/reconciliations", response_model=list[ReconciliationSessionOut])
def list_reconciliations(cash_account_id: int | None = None, db: Session = Depends(get_db)):
    return reconciliation.list_sessions(db, cash_account_id)


@router.get("/cash/reconciliations/{session_id}", response_model=ReconciliationSessionOut)
@translate_errors
def get_reconciliation(session_id: int, db: Session = Depends(get_db)):
    return reconciliation.get_session(db, session_id)


@router.patch("/cash/reconciliations/{session_id}", response_model=ReconciliationSessionOut)
@translate_errors
def update_reconciliation(session_id: int, data: ReconciliationSessionUpdate, db: Session = Depends(get_db)):
    """Correct balances/notes of an open session (closed sessions are frozen)."""
    return reconciliation.update_session(db, reconciliation.get_session(db, session_id), data)


@router.get("/cash/reconciliations/{session_id}/summary", response_model=ReconciliationSummary)
@translate_errors
def reconciliation_summary(session_id: int, db: Session = Depends(get_db)):
    """Review: statement vs. posted ledger, discrepancies and whether the session balances."""
    return reconciliation.summary(db, reconciliation.get_session(db, session_id))


@router.get("/cash/reconciliations/{session_id}/unmatched", response_model=UnmatchedItems)
@translate_errors
def reconciliation_unmatched(session_id: int, db: Session = Depends(get_db)):
    return reconciliation.unmatched_items(db, reconciliation.get_session(db, session_id))


@router.get("/cash/reconciliations/{session_id}/matches", response_model=list[MatchOut])
@translate_errors
def reconciliation_matches(session_id: int, db: Session = Depends(get_db)):
    return reconciliation.list_matches(db, reconciliation.get_session(db, session_id))


@router.post(
    "/cash/reconciliations/{session_id}/matches", response_model=MatchOut, status_code=201
)
@translate_errors
def add_match(session_id: int, data: MatchCreate, db: Session = Depends(get_db)):
    return reconciliation.add_match(db, reconciliation.get_session(db, session_id), data)


@router.delete("/cash/reconciliations/{session_id}/matches/{match_id}", status_code=204)
@translate_errors
def remove_match(session_id: int, match_id: int, db: Session = Depends(get_db)):
    reconciliation.remove_match(db, reconciliation.get_session(db, session_id), match_id)
    return Response(status_code=204)


@router.post("/cash/reconciliations/{session_id}/auto-match", response_model=AutoMatchResult)
@translate_errors
def auto_match(
    session_id: int, data: AutoMatchRequest | None = None, db: Session = Depends(get_db)
):
    return reconciliation.auto_match(
        db, reconciliation.get_session(db, session_id), data or AutoMatchRequest()
    )


@router.post("/cash/reconciliations/{session_id}/close", response_model=ReconciliationSessionOut)
@translate_errors
def close_reconciliation(
    session_id: int, data: CloseRequest | None = None, db: Session = Depends(get_db)
):
    return reconciliation.close_session(
        db, reconciliation.get_session(db, session_id), data or CloseRequest()
    )


@router.post("/cash/reconciliations/{session_id}/reopen", response_model=ReconciliationSessionOut)
@translate_errors
def reopen_reconciliation(session_id: int, data: ReopenRequest, db: Session = Depends(get_db)):
    """Reopen a certified session. A reason is mandatory and kept in the history."""
    return reconciliation.reopen_session(db, reconciliation.get_session(db, session_id), data)


@router.post("/cash/reconciliations/{session_id}/recover", response_model=IntegrityReport)
@translate_errors
def recover_reconciliation(session_id: int, db: Session = Depends(get_db)):
    """Complete a half-done close (certification recorded, status never flipped). Deletes nothing."""
    return reconciliation.recover_session(db, reconciliation.get_session(db, session_id))


@router.get("/cash/reconciliations/{session_id}/certification", response_model=CertificationDetail)
@translate_errors
def reconciliation_certification(session_id: int, db: Session = Depends(get_db)):
    """Latest certified evidence: canonical content, hash verification and live drift."""
    return reconciliation.certification_detail(db, reconciliation.get_session(db, session_id))


@router.get("/cash/reconciliations/{session_id}/history", response_model=CertificationHistory)
@translate_errors
def reconciliation_history(session_id: int, db: Session = Depends(get_db)):
    return reconciliation.certification_history(db, reconciliation.get_session(db, session_id))


# --- Reports ------------------------------------------------------------------------------


@router.get("/cash/reports/balances", response_model=CashBalancesReport)
def balances_report(as_of: date | None = None, db: Session = Depends(get_db)):
    return reports.balances_report(db, as_of)


@router.get("/cash/reports/daily-balances", response_model=DailyBalancesReport)
@translate_errors
def daily_balances(
    start: date, end: date, cash_account_id: int | None = None, db: Session = Depends(get_db)
):
    return reports.daily_balances_report(db, start, end, cash_account_id)


@router.get("/cash/reports/cash-flow", response_model=CashFlowReport)
@translate_errors
def cash_flow(start: date, end: date, db: Session = Depends(get_db)):
    return reports.cash_flow_report(db, start, end)


# --- Liquidity projection ------------------------------------------------------------------


@router.get("/cash/projection", response_model=LiquidityProjection)
@translate_errors
def liquidity(
    as_of: date | None = None,
    horizon_days: int = Query(default=30, ge=1, le=366),
    cash_account_id: int | None = None,
    threshold: Decimal | None = Query(default=None, decimal_places=2),
    history_days: int = Query(default=30, ge=0, le=366),
    db: Session = Depends(get_db),
):
    return projection.liquidity_projection(
        db,
        as_of=as_of or date.today(),
        horizon_days=horizon_days,
        cash_account_id=cash_account_id,
        threshold=threshold,
        history_days=history_days,
    )


# --- Planned cash flows -------------------------------------------------------------------


@router.post(
    "/cash/planned-flows",
    response_model=PlannedCashFlowOut,
    status_code=201,
    responses={200: {"description": "Identical planned flow already exists"}},
)
@translate_errors
def create_planned_flow(data: PlannedCashFlowIn, response: Response, db: Session = Depends(get_db)):
    before = {f.id for f in planning.list_planned_flows(db, data.cash_account_id)}
    item = planning.create_planned_flow(db, data)
    if item.id in before:
        response.status_code = status.HTTP_200_OK
    return item


@router.get("/cash/planned-flows", response_model=list[PlannedCashFlowOut])
def list_planned_flows(
    cash_account_id: int | None = None,
    status: PlannedCashFlowStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
):
    return planning.list_planned_flows(db, cash_account_id, status, date_from, date_to)


@router.get("/cash/planned-flows/{flow_id}", response_model=PlannedCashFlowOut)
@translate_errors
def get_planned_flow(flow_id: int, db: Session = Depends(get_db)):
    return planning.get_planned_flow(db, flow_id)


@router.patch("/cash/planned-flows/{flow_id}/status", response_model=PlannedCashFlowOut)
@translate_errors
def update_planned_flow_status(
    flow_id: int, data: PlannedCashFlowStatusUpdate, db: Session = Depends(get_db)
):
    return planning.update_planned_flow_status(db, flow_id, data)


@router.delete("/cash/planned-flows/{flow_id}", status_code=204)
@translate_errors
def delete_planned_flow(flow_id: int, db: Session = Depends(get_db)):
    planning.delete_planned_flow(db, flow_id)
    return Response(status_code=204)


# --- Activity classifications ---------------------------------------------------------------


@router.put("/cash/classifications", response_model=ClassificationOut)
@translate_errors
def set_classification(data: ClassificationIn, db: Session = Depends(get_db)):
    return planning.set_classification(db, data)


@router.get("/cash/classifications", response_model=list[ClassificationOut])
def list_classifications(db: Session = Depends(get_db)):
    return planning.list_classifications(db)


@router.delete("/cash/classifications/{account_id}", status_code=204)
@translate_errors
def delete_classification(account_id: int, db: Session = Depends(get_db)):
    planning.delete_classification(db, account_id)
    return Response(status_code=204)
