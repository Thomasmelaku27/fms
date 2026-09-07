from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db
from app.models import JournalEntry, RecurringTemplate
from app.schemas.journal import JournalEntryRead
from app.schemas.recurring import (
    GenerateDueRequest,
    GenerateRequest,
    RecurringTemplateCreate,
    RecurringTemplateRead,
    RecurringTemplateUpdate,
)
from app.services import recurring as recurring_service
from app.services.exceptions import ConflictError, UnprocessableError

router = APIRouter(prefix="/recurring-templates", tags=["recurring"])


def _load(db: Session, template_id: int) -> RecurringTemplate:
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recurring template not found")
    return template


@router.post("", response_model=RecurringTemplateRead, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: RecurringTemplateCreate, db: Session = Depends(get_db)
) -> RecurringTemplate:
    return recurring_service.create_template(
        db,
        name=payload.name,
        description=payload.description,
        reference=payload.reference,
        frequency=payload.frequency,
        start_date=payload.start_date,
        end_date=payload.end_date,
        lines=[line.model_dump() for line in payload.lines],
    )


@router.get("", response_model=list[RecurringTemplateRead])
def list_templates(db: Session = Depends(get_db)) -> list[RecurringTemplate]:
    stmt = (
        select(RecurringTemplate)
        .options(selectinload(RecurringTemplate.lines))
        .order_by(RecurringTemplate.name)
    )
    return list(db.scalars(stmt))


@router.get("/{template_id}", response_model=RecurringTemplateRead)
def get_template(template_id: int, db: Session = Depends(get_db)) -> RecurringTemplate:
    return _load(db, template_id)


@router.patch("/{template_id}", response_model=RecurringTemplateRead)
def update_template(
    template_id: int, payload: RecurringTemplateUpdate, db: Session = Depends(get_db)
) -> RecurringTemplate:
    """Rename, pause/resume (``is_active``) or end (``end_date``) a template."""
    template = _load(db, template_id)
    changes = payload.model_dump(exclude_unset=True)
    if "end_date" in changes and changes["end_date"] is not None:
        if changes["end_date"] < template.start_date:
            raise UnprocessableError("end_date must not be before start_date")
    if "name" in changes and changes["name"] != template.name:
        if db.scalar(select(RecurringTemplate).where(RecurringTemplate.name == changes["name"])):
            raise ConflictError(f"Recurring template '{changes['name']}' already exists")
    for field, value in changes.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


@router.post(
    "/{template_id}/generate",
    response_model=list[JournalEntryRead],
    status_code=status.HTTP_201_CREATED,
)
def generate_drafts(
    template_id: int, payload: GenerateRequest, db: Session = Depends(get_db)
) -> list[JournalEntry]:
    """Create the template's pending drafts up to ``until``; repeat calls add nothing."""
    return recurring_service.generate(db, _load(db, template_id), payload.until)


@router.post(
    "/generate-due", response_model=list[JournalEntryRead], status_code=status.HTTP_201_CREATED
)
def generate_due(payload: GenerateDueRequest, db: Session = Depends(get_db)) -> list[JournalEntry]:
    """Run every active template up to ``until`` (what a scheduler would call).

    ``atomic`` (default): all templates' drafts and cursors commit together or
    not at all, so a failure leaves nothing behind and a retry regenerates the
    same set. ``atomic=false`` keeps the per-template commits.
    """
    return recurring_service.generate_due(db, payload.until, atomic=payload.atomic)
