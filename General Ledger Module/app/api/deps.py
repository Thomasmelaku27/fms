from collections.abc import Iterator

from fastapi import Header, Request
from sqlalchemy.orm import Session

from app.db import session_scope
from app.services.actor import PostingContext, new_correlation_id


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_posting_context(
    x_actor_ref: str | None = Header(default=None, alias="X-Actor-Ref"),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
) -> PostingContext:
    """Integration boundary for actor identity.

    Until the Security module is integrated, the actor reference is taken from
    the ``X-Actor-Ref`` header (``"anonymous"`` if absent). ``X-Correlation-Id``
    lets callers thread their own correlation id through the GL; otherwise one
    is generated per request.
    """
    return PostingContext(
        actor_ref=(x_actor_ref or "anonymous")[:100],
        source="api",
        correlation_id=(x_correlation_id or new_correlation_id())[:36],
    )
