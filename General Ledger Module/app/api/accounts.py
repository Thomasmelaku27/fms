from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_posting_context
from app.models import Account
from app.schemas.account import (
    AccountCreate,
    AccountRead,
    AccountUpdate,
    ReclassificationImpactRead,
    ReclassificationRead,
    ReclassifyAccountRequest,
)
from app.services import accounts as account_service
from app.services import reports as report_service
from app.services.actor import PostingContext

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _load(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)) -> Account:
    """Create a posting or summary account, optionally under a summary parent."""
    return account_service.create_account(db, **payload.model_dump())


@router.get("", response_model=list[AccountRead])
def list_accounts(
    parent_id: int | None = Query(default=None, description="Direct children of this account"),
    db: Session = Depends(get_db),
) -> list[Account]:
    stmt = select(Account).order_by(Account.code)
    if parent_id is not None:
        stmt = stmt.where(Account.parent_id == parent_id)
    return list(db.scalars(stmt))


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, db: Session = Depends(get_db)) -> Account:
    return _load(db, account_id)


@router.get("/{account_id}/descendants", response_model=list[AccountRead])
def account_descendants(account_id: int, db: Session = Depends(get_db)) -> list[Account]:
    """Every account below this one in the hierarchy (any depth), ordered by code."""
    return sorted(account_service.descendants(db, _load(db, account_id)), key=lambda a: a.code)


@router.patch("/{account_id}", response_model=AccountRead)
def update_account(
    account_id: int, payload: AccountUpdate, db: Session = Depends(get_db)
) -> Account:
    """Partial update. 409 for unsafe changes to accounts with history; 422 for bad hierarchy."""
    return account_service.update_account(
        db, _load(db, account_id), payload.model_dump(exclude_unset=True)
    )


def _set_active(db: Session, account_id: int, is_active: bool) -> Account:
    return account_service.update_account(db, _load(db, account_id), {"is_active": is_active})


@router.post("/{account_id}/activate", response_model=AccountRead)
def activate_account(account_id: int, db: Session = Depends(get_db)) -> Account:
    return _set_active(db, account_id, True)


@router.post("/{account_id}/deactivate", response_model=AccountRead)
def deactivate_account(account_id: int, db: Session = Depends(get_db)) -> Account:
    """Inactive accounts keep their history but cannot be used in newly posted entries."""
    return _set_active(db, account_id, False)


@router.post("/{account_id}/reclassify", response_model=AccountRead)
def reclassify_account(
    account_id: int,
    payload: ReclassifyAccountRequest,
    db: Session = Depends(get_db),
    ctx: PostingContext = Depends(get_posting_context),
) -> Account:
    """Effective-dated parent/category change, recorded with reason and actor.

    Set ``change_parent`` / ``change_category`` to apply the corresponding
    field (a null value clears it). Required for accounts with journal history.
    """
    if not payload.change_parent and not payload.change_category:
        raise HTTPException(422, "set change_parent and/or change_category")
    account = _load(db, account_id)
    account_service.reclassify_account(
        db,
        account,
        effective_from=payload.effective_from,
        reason=payload.reason,
        actor_ref=ctx.actor_ref,
        parent_id=payload.parent_id if payload.change_parent else ...,
        category=payload.category if payload.change_category else ...,
    )
    return account


@router.get("/{account_id}/reclassifications", response_model=list[ReclassificationRead])
def list_reclassifications(account_id: int, db: Session = Depends(get_db)):
    return account_service.reclassifications(db, _load(db, account_id))


@router.get(
    "/{account_id}/reclassifications/impact", response_model=list[ReclassificationImpactRead]
)
def reclassification_impact(account_id: int, db: Session = Depends(get_db)):
    """How much historical balance each reclassification moved between parents."""
    return report_service.reclassification_impact(db, _load(db, account_id))
