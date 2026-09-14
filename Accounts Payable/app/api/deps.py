from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.db import session_scope


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)
