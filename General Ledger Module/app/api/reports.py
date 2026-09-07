from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Account
from app.schemas.report import (
    AccountBalancesRead,
    BalanceComparisonRead,
    GeneralLedgerRead,
    HierarchyRead,
    TrialBalanceRead,
)
from app.services import reports as report_service

router = APIRouter(prefix="/reports", tags=["reports"])

FromDate = Query(default=None, alias="from", description="Earliest entry date (inclusive)")
ToDate = Query(default=None, alias="to", description="Latest entry date (inclusive)")


def _check_range(date_from: date | None, date_to: date | None) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "'from' must not be after 'to'")


@router.get("/trial-balance", response_model=TrialBalanceRead)
def trial_balance(
    date_from: date | None = FromDate, date_to: date | None = ToDate, db: Session = Depends(get_db)
) -> report_service.TrialBalance:
    """Debit/credit totals per account from posted entries only."""
    _check_range(date_from, date_to)
    return report_service.trial_balance(db, date_from, date_to)


@router.get("/general-ledger", response_model=GeneralLedgerRead)
def general_ledger(
    account_id: int | None = Query(default=None),
    date_from: date | None = FromDate,
    date_to: date | None = ToDate,
    limit: int | None = Query(default=None, ge=1, le=5000, description="Lines per page"),
    after: str | None = Query(default=None, description="next_cursor of the previous page"),
    db: Session = Depends(get_db),
) -> report_service.GeneralLedger:
    """Posted lines per account with running balances; drafts are never included.

    With ``limit`` the lines are paginated in the deterministic order
    (account code, entry date, entry id, line id); pass ``next_cursor`` back as
    ``after``. Totals and balances always cover the whole range.
    """
    _check_range(date_from, date_to)
    if account_id is not None and db.get(Account, account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    try:
        return report_service.general_ledger(db, account_id, date_from, date_to, limit, after)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


Source = Query(default="aggregate", pattern="^(aggregate|lines)$")


def _ids(account_ids: str | None) -> list[int] | None:
    if not account_ids:
        return None
    try:
        return [int(x) for x in account_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "account_ids must be a comma-separated list of ids") from None


@router.get("/account-balances", response_model=AccountBalancesRead)
def account_balances(
    date_from: date | None = FromDate,
    date_to: date | None = ToDate,
    account_ids: str | None = Query(default=None, description="Comma-separated account ids"),
    source: str = Source,
    db: Session = Depends(get_db),
):
    """Opening, activity and closing balance per account (summaries rolled up).

    ``source=aggregate`` (default) reads the trigger-maintained daily totals;
    ``source=lines`` reads the journal lines directly.
    """
    _check_range(date_from, date_to)
    return report_service.account_balances(db, date_from, date_to, _ids(account_ids), source)


@router.get("/account-balances/compare", response_model=BalanceComparisonRead)
def compare_balances(
    date_from: date | None = FromDate,
    date_to: date | None = ToDate,
    compare_from: date | None = Query(default=None),
    compare_to: date | None = Query(default=None),
    account_ids: str | None = Query(default=None),
    source: str = Source,
    db: Session = Depends(get_db),
):
    """Balances for two date ranges side by side, with closing-balance differences by code."""
    _check_range(date_from, date_to)
    _check_range(compare_from, compare_to)
    ids = _ids(account_ids)
    current = report_service.account_balances(db, date_from, date_to, ids, source)
    comparison = report_service.account_balances(db, compare_from, compare_to, ids, source)
    other = {row.code: row.closing_balance for row in comparison.rows}
    return BalanceComparisonRead(
        current=current,
        comparison=comparison,
        differences={
            row.code: row.closing_balance - other.get(row.code, 0) for row in current.rows
        },
    )


@router.get("/hierarchy", response_model=HierarchyRead)
def hierarchy(
    date_from: date | None = FromDate,
    date_to: date | None = ToDate,
    root_id: int | None = Query(default=None, description="Start from this account"),
    source: str = Source,
    db: Session = Depends(get_db),
):
    """Account tree with own and rolled-up totals for drill-down (no double counting)."""
    _check_range(date_from, date_to)
    if root_id is not None and db.get(Account, root_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return HierarchyRead(
        date_from=date_from,
        date_to=date_to,
        source=source,
        roots=report_service.hierarchy(db, date_from, date_to, root_id, source),
    )
