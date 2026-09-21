from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.services.posting import EntryAlreadyPosted, EntryNotPostable


def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Map database integrity errors to 422 instead of a generic 500.

    The ledger rules (CHECK constraints and triggers) are enforced in the database
    as a backstop to the Pydantic/handler validation. If input slips past the
    application checks, the database rejects it and the client still gets a
    validation-style response with the constraint/trigger message.
    """
    reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity constraint violated"
    return JSONResponse(
        status_code=422,
        content={"detail": f"Database constraint violated: {reason}"},
    )


def already_posted_handler(request: Request, exc: EntryAlreadyPosted) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def not_postable_handler(request: Request, exc: EntryNotPostable) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(EntryAlreadyPosted, already_posted_handler)  # type: ignore[arg-type]
    app.add_exception_handler(EntryNotPostable, not_postable_handler)  # type: ignore[arg-type]
