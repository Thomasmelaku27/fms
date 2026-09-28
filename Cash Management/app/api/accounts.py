from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Account
from app.schemas.account import AccountCreate, AccountRead

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)) -> Account:
    if db.scalar(select(Account).where(Account.code == payload.code)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Account code {payload.code} already exists")
    account = Account(**payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.get("", response_model=list[AccountRead])
def list_accounts(db: Session = Depends(get_db)) -> list[Account]:
    return list(db.scalars(select(Account).order_by(Account.code)))


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, db: Session = Depends(get_db)) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


def _set_active(db: Session, account_id: int, is_active: bool) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    account.is_active = is_active
    db.commit()
    db.refresh(account)
    return account


@router.post("/{account_id}/activate", response_model=AccountRead)
def activate_account(account_id: int, db: Session = Depends(get_db)) -> Account:
    return _set_active(db, account_id, True)


@router.post("/{account_id}/deactivate", response_model=AccountRead)
def deactivate_account(account_id: int, db: Session = Depends(get_db)) -> Account:
    """Inactive accounts keep their history but cannot be used in newly posted entries."""
    return _set_active(db, account_id, False)
