"""Planned cash flows and activity classifications (forecast/reporting inputs).

Planned flows are *never* accounting: creating, realizing or cancelling one
does not create, change or post any journal entry.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, JournalEntry
from app.modules.cash.errors import CashConflict, CashNotFound, CashValidationError
from app.modules.cash.models import (
    CashFlowClassification,
    PlannedCashFlow,
    PlannedCashFlowStatus,
)
from app.modules.cash.schemas import ClassificationIn, PlannedCashFlowIn, PlannedCashFlowStatusUpdate
from app.modules.cash.services import get_cash_account, quantize


def create_planned_flow(db: Session, payload: PlannedCashFlowIn) -> PlannedCashFlow:
    get_cash_account(db, payload.cash_account_id)
    if payload.amount == 0:
        raise CashValidationError("amount must not be zero")
    if payload.external_reference is not None:
        existing = db.scalar(
            select(PlannedCashFlow).where(
                PlannedCashFlow.cash_account_id == payload.cash_account_id,
                PlannedCashFlow.source == payload.source,
                PlannedCashFlow.external_reference == payload.external_reference,
            )
        )
        if existing is not None:
            # Idempotent for source systems that resend the same planned item.
            same = (
                existing.expected_date == payload.expected_date
                and existing.amount == quantize(payload.amount)
                and existing.description == payload.description
            )
            if same:
                return existing
            raise CashConflict(
                f"Planned flow {payload.source}/{payload.external_reference} already exists "
                "with different data"
            )
    item = PlannedCashFlow(
        cash_account_id=payload.cash_account_id,
        expected_date=payload.expected_date,
        amount=quantize(payload.amount),
        description=payload.description,
        activity=payload.activity,
        source=payload.source,
        external_reference=payload.external_reference,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with an identical request: behave exactly like a retry.
        db.rollback()
        existing = db.scalar(
            select(PlannedCashFlow).where(
                PlannedCashFlow.cash_account_id == payload.cash_account_id,
                PlannedCashFlow.source == payload.source,
                PlannedCashFlow.external_reference == payload.external_reference,
            )
        )
        if existing is not None and (
            existing.expected_date == payload.expected_date
            and existing.amount == quantize(payload.amount)
            and existing.description == payload.description
        ):
            return existing
        raise CashConflict("Planned flow with this source and external reference already exists with different data")
    db.refresh(item)
    return item


def get_planned_flow(db: Session, flow_id: int) -> PlannedCashFlow:
    item = db.get(PlannedCashFlow, flow_id)
    if item is None:
        raise CashNotFound(f"Planned cash flow {flow_id} not found")
    return item


def list_planned_flows(
    db: Session,
    cash_account_id: int | None = None,
    status: PlannedCashFlowStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[PlannedCashFlow]:
    stmt = select(PlannedCashFlow).order_by(PlannedCashFlow.expected_date, PlannedCashFlow.id)
    if cash_account_id is not None:
        stmt = stmt.where(PlannedCashFlow.cash_account_id == cash_account_id)
    if status is not None:
        stmt = stmt.where(PlannedCashFlow.status == status)
    if date_from is not None:
        stmt = stmt.where(PlannedCashFlow.expected_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(PlannedCashFlow.expected_date <= date_to)
    return list(db.scalars(stmt))


def update_planned_flow_status(
    db: Session, flow_id: int, payload: PlannedCashFlowStatusUpdate
) -> PlannedCashFlow:
    item = get_planned_flow(db, flow_id)
    if payload.journal_entry_id is not None:
        entry = db.get(JournalEntry, payload.journal_entry_id)
        if entry is None:
            raise CashValidationError(f"Journal entry {payload.journal_entry_id} not found")
        if payload.status == PlannedCashFlowStatus.REALIZED and entry.status.value != "posted":
            raise CashValidationError(
                "A planned flow can only be realized by a posted journal entry"
            )
    item.status = payload.status
    item.journal_entry_id = payload.journal_entry_id
    db.commit()
    db.refresh(item)
    return item


def delete_planned_flow(db: Session, flow_id: int) -> None:
    item = get_planned_flow(db, flow_id)
    db.delete(item)
    db.commit()


def set_classification(db: Session, payload: ClassificationIn) -> CashFlowClassification:
    if db.get(Account, payload.account_id) is None:
        raise CashValidationError(f"Account {payload.account_id} not found")
    row = db.scalar(
        select(CashFlowClassification).where(CashFlowClassification.account_id == payload.account_id)
    )
    if row is None:
        row = CashFlowClassification(account_id=payload.account_id, activity=payload.activity)
        db.add(row)
    else:
        row.activity = payload.activity
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.scalar(
            select(CashFlowClassification).where(
                CashFlowClassification.account_id == payload.account_id
            )
        )
        if row is None:
            raise CashConflict("Concurrent classification change; retry")
        row.activity = payload.activity
        db.commit()
    db.refresh(row)
    return row


def list_classifications(db: Session) -> list[CashFlowClassification]:
    return list(db.scalars(select(CashFlowClassification).order_by(CashFlowClassification.account_id)))


def delete_classification(db: Session, account_id: int) -> None:
    row = db.scalar(
        select(CashFlowClassification).where(CashFlowClassification.account_id == account_id)
    )
    if row is None:
        raise CashNotFound(f"No classification for account {account_id}")
    db.delete(row)
    db.commit()
