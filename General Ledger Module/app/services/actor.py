"""Actor identity and posting context - the integration boundary towards Security/Audit.

The General Ledger does not authenticate anyone. Callers (the API layer, other
modules, batch jobs) hand in a ``PostingContext`` naming the actor reference
they are accountable for, the posting source and optionally a correlation id;
the GL records these on every posted entry and every closing attestation. The
future Security module supplies real actor references through the same
boundary (``app.api.deps.get_posting_context`` reads ``X-Actor-Ref`` /
``X-Correlation-Id`` headers today).
"""

import uuid
from dataclasses import dataclass, field, replace


def new_correlation_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class PostingContext:
    actor_ref: str = "system"
    source: str = "service"  # api | processing | batch | reversal | year_end_close | ...
    correlation_id: str = field(default_factory=new_correlation_id)

    def with_source(self, source: str) -> "PostingContext":
        return replace(self, source=source)


def system_context(source: str = "service") -> PostingContext:
    """A fresh context (new correlation id) for GL-internal operations."""
    return PostingContext(actor_ref="system", source=source)
