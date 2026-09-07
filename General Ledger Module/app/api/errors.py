from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError

from app.money import MoneyError
from app.services.exceptions import ServiceError


def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Map database integrity errors to 422 instead of a generic 500.

    The ledger rules (CHECK constraints and triggers) are enforced in the database
    as a backstop to the Pydantic/service validation. If input slips past the
    application checks, the database rejects it and the client still gets a
    validation-style response with the constraint/trigger message.
    """
    reason = str(exc.orig).strip().splitlines()[0] if exc.orig else "integrity constraint violated"
    return JSONResponse(
        status_code=422,
        content={"detail": f"Database constraint violated: {reason}"},
    )


RETRYABLE_FRAGMENTS = (
    "database is locked",  # SQLite busy timeout exceeded
    "deadlock detected",  # PostgreSQL 40P01
    "could not serialize access",  # PostgreSQL 40001
)


def operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """Lock waits, deadlocks and serialisation failures are transient: ask the client to retry."""
    message = str(exc.orig)
    if any(fragment in message for fragment in RETRYABLE_FRAGMENTS):
        return JSONResponse(
            status_code=409,
            content={"detail": "The ledger is busy with another change; retry the request"},
        )
    raise exc


def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Domain errors carry their HTTP status (409 conflicts, 422 rule violations)."""
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


def money_error_handler(request: Request, exc: MoneyError) -> JSONResponse:
    """Invalid monetary amounts reaching a service/ORM boundary are client errors."""
    return JSONResponse(status_code=422, content={"detail": f"Invalid amount: {exc}"})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(MoneyError, money_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ServiceError, service_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(OperationalError, operational_error_handler)  # type: ignore[arg-type]
