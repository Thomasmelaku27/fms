"""Chart of accounts: hierarchy and safe changes.

Structural rules (service level and database triggers/CHECKs, revisions
d5b5eb8280ba and a99cf5d9c85d):

* ``parent`` must be a *summary* account of the same ``type``;
* no cycles, direct or indirect (recursive trigger check);
* ``category`` must belong to the account's ``type`` (CHECK constraint);
* ``normal_balance`` defaults from the type (asset/expense debit, else credit);
* a summary account cannot be used on journal lines;
* a summary account with children keeps its role and type;
* accounts are never deleted; deactivate them instead.

Changes to accounts **with journal history** (any line, draft or posted):

* safe, always allowed: ``name``, ``is_active``;
* effective-dated, allowed only through ``reclassify_account``: ``parent_id``
  and ``category``. Both change where historical amounts roll up, so the
  change is recorded with an effective date, a reason and an actor in
  ``account_reclassifications`` (the database refuses the change without such
  a record) and the reporting impact can be explained afterwards;
* prohibited: ``type``, ``normal_balance``, becoming a summary account - they
  would change the meaning of posted amounts.
"""

from datetime import date

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import (
    CATEGORY_TYPES,
    DEFAULT_NORMAL_BALANCE,
    Account,
    AccountCategory,
    AccountReclassification,
    AccountType,
    JournalLine,
    NormalBalance,
)
from app.services.exceptions import ConflictError, UnprocessableError


class AccountCodeTaken(ConflictError):
    pass


def has_history(db: Session, account: Account) -> bool:
    return db.scalar(select(exists().where(JournalLine.account_id == account.id)))


def ancestors(db: Session, account: Account) -> list[Account]:
    chain: list[Account] = []
    current = account.parent
    seen = {account.id}
    while current is not None and current.id not in seen:
        chain.append(current)
        seen.add(current.id)
        current = current.parent
    return chain


def descendants(db: Session, account: Account) -> list[Account]:
    found: list[Account] = []
    frontier = list(account.children)
    while frontier:
        child = frontier.pop()
        found.append(child)
        frontier.extend(child.children)
    return found


def _validate_parent(db: Session, account: Account | None, parent: Account | None, type_):
    if parent is None:
        return
    if not parent.is_summary:
        raise UnprocessableError(f"Parent account {parent.code} is not a summary account")
    if parent.type != type_:
        raise UnprocessableError(
            f"Parent account {parent.code} is of type {parent.type.value}, not {type_.value}"
        )
    if account is not None and (
        parent.id == account.id or parent.id in {a.id for a in descendants(db, account)}
    ):
        raise UnprocessableError(
            f"Account {account.code} cannot be placed under {parent.code}: hierarchy cycle"
        )


def _validate_category(category: AccountCategory | None, type_: AccountType) -> None:
    if category is not None and CATEGORY_TYPES[category] != type_:
        raise UnprocessableError(
            f"Category {category.value} belongs to {CATEGORY_TYPES[category].value} accounts, "
            f"not {type_.value}"
        )


def create_account(
    db: Session,
    *,
    code: str,
    name: str,
    type: AccountType,
    category: AccountCategory | None = None,
    normal_balance: NormalBalance | None = None,
    is_summary: bool = False,
    parent_id: int | None = None,
    commit: bool = True,
) -> Account:
    if db.scalar(select(Account).where(Account.code == code)):
        raise AccountCodeTaken(f"Account code {code} already exists")
    parent = db.get(Account, parent_id) if parent_id is not None else None
    if parent_id is not None and parent is None:
        raise UnprocessableError(f"Unknown parent account id {parent_id}")
    _validate_parent(db, None, parent, type)
    _validate_category(category, type)
    account = Account(
        code=code,
        name=name,
        type=type,
        category=category,
        normal_balance=normal_balance or DEFAULT_NORMAL_BALANCE[type],
        is_summary=is_summary,
        parent=parent,
    )
    db.add(account)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(account)
    return account


def update_account(db: Session, account: Account, changes: dict, *, commit: bool = True) -> Account:
    """Apply a partial update (``changes`` = fields explicitly given by the caller)."""
    new_type = changes.get("type", account.type)
    new_summary = changes.get("is_summary", account.is_summary)
    history = has_history(db, account)
    if ("type" in changes and new_type != account.type) or (new_summary and not account.is_summary):
        if history:
            raise ConflictError(
                f"Account {account.code} has journal lines; its type cannot change and it "
                "cannot become a summary account"
            )
    if (
        history
        and "normal_balance" in changes
        and changes["normal_balance"] != account.normal_balance
    ):
        raise ConflictError(
            f"Account {account.code} has journal lines; its normal balance cannot change"
        )
    if history and (
        ("parent_id" in changes and changes["parent_id"] != account.parent_id)
        or ("category" in changes and changes["category"] != account.category)
    ):
        raise ConflictError(
            f"Account {account.code} has journal lines; move or recategorise it with an "
            "effective-dated reclassification (POST /accounts/{id}/reclassify)"
        )
    if account.children and (new_type != account.type or not new_summary):
        raise ConflictError(
            f"Account {account.code} has child accounts; it must stay a summary account of "
            "the same type"
        )
    if "parent_id" in changes:
        parent_id = changes["parent_id"]
        parent = db.get(Account, parent_id) if parent_id is not None else None
        if parent_id is not None and parent is None:
            raise UnprocessableError(f"Unknown parent account id {parent_id}")
        _validate_parent(db, account, parent, new_type)
    elif account.parent is not None and new_type != account.parent.type:
        raise UnprocessableError(
            f"Account {account.code} cannot become {new_type.value} under parent "
            f"{account.parent.code} ({account.parent.type.value})"
        )
    _validate_category(changes.get("category", account.category), new_type)

    for field, value in changes.items():
        setattr(account, field, value)
    if "type" in changes and "normal_balance" not in changes:
        account.normal_balance = DEFAULT_NORMAL_BALANCE[new_type]
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(account)
    return account


def reclassify_account(
    db: Session,
    account: Account,
    *,
    effective_from: date,
    reason: str,
    actor_ref: str,
    parent_id: int | None = ...,
    category: AccountCategory | None = ...,
    commit: bool = True,
) -> AccountReclassification:
    """Effective-dated parent/category change of an account (with or without history).

    Records the change first, then applies it; the database trigger accepts
    the account update only because the matching record exists in the same
    transaction. Raises 422 if nothing would change.
    """
    new_parent_id = account.parent_id if parent_id is ... else parent_id
    new_category = account.category if category is ... else category
    if new_parent_id == account.parent_id and new_category == account.category:
        raise UnprocessableError("the reclassification changes neither parent nor category")
    parent = db.get(Account, new_parent_id) if new_parent_id is not None else None
    if new_parent_id is not None and parent is None:
        raise UnprocessableError(f"Unknown parent account id {new_parent_id}")
    _validate_parent(db, account, parent, account.type)
    _validate_category(new_category, account.type)

    record = AccountReclassification(
        account_id=account.id,
        old_parent_id=account.parent_id,
        new_parent_id=new_parent_id,
        old_category=account.category.value if account.category else None,
        new_category=new_category.value if new_category else None,
        effective_from=effective_from,
        reason=reason,
        actor_ref=actor_ref,
    )
    db.add(record)
    try:
        db.flush()
        account.parent = parent
        account.category = new_category
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:
        if commit:
            db.rollback()
        raise
    db.refresh(record)
    db.refresh(account)
    return record


def reclassifications(db: Session, account: Account) -> list[AccountReclassification]:
    return db.scalars(
        select(AccountReclassification)
        .where(AccountReclassification.account_id == account.id)
        .order_by(AccountReclassification.effective_from, AccountReclassification.id)
    ).all()
