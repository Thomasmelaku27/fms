"""Explicit cross-module integration points of the payables module.

Everything the AP workflow needs from outside its own package goes through the
ports collected in :class:`Integrations`. The router builds the production set
in :func:`get_integrations`; tests (and future modules) override that single
FastAPI dependency instead of monkey-patching internals.

Transaction ownership
---------------------
Ports are called *inside* the service's unit of work, on the service's
``Session``. They must never commit or roll back; the service commits once at
the end or rolls everything back. The ledger port posts through a SAVEPOINT-
joined session for exactly that reason (see ``ledger_adapter``). The gateway
port is the one exception by nature: it talks to an external system and is
invoked *between* transactions (the intent is committed as ``submitted`` first),
with the operation uid as idempotency token so a retry cannot pay twice.

Ports
-----
``ledger``     (implemented: :class:`~app.modules.payables.ledger_adapter.JournalLedgerAdapter`)
    Creates and posts journal entries for approved bills and credits, recorded
    payments, discounts and reversals through the core posting service, inside
    the caller's transaction. The only write path into the ledger. The
    reconciliation report *reads* posted journal lines of the AP control
    accounts (read-only, like the cash and reporting modules do).

``audit``      (default: :class:`NullAudit`, a no-op)
    Receives one call per business event inside the same transaction as the AP
    change. Actions (``payables.<object>.<event>``):
    bill: approved, rejected, reopened, held, released, flag_reviewed;
    credit: approved, rejected, reopened, applied, unapplied, reversed;
    payment: recorded, reversed, advance_applied, advance_unallocated;
    discount: taken; run: created, reviewed, approved, sent_back, cancelled,
    executed, item_excluded; period: closed; vendor: status_changed.
    The audit module's ``record_event(db, actor=, action=, resource=)`` already
    has this shape; wiring it is a one-line change in :func:`get_integrations`
    once the audit branch lands. Implementations must not commit.

``gateway``    (default: ``services.gateway.DurableSimulatedGateway``)
    Sends one payment instruction per (run, vendor) operation and answers status
    lookups by operation id. Contract in ``services/gateway.py``
    (``GatewayRequest`` -> ``GatewayResponse``; outcomes accepted / rejected /
    pending / unknown; ``GatewayTimeout`` for lost responses). The token is the
    operation uid and is stable across retries; a repeated token must return the
    original outcome. No real banking integration exists; the simulator persists
    its transfers in the database so restarts and recovery are realistic.

``faults``     (default: :class:`NullFaults`)
    Deterministic failure injection at named points of the orchestration and the
    period close (tests only).

``authorization`` (default: :class:`PermissiveAuthorization`)
    Hook for the Security module: ``authorize(actor, action, resource)`` guards
    forced period closes, run approval / execution and manual operation
    resolution. Maker-checker separation itself is enforced in AP.

``periods``    (default: :class:`APOnlyPeriods`)
    Tells the AP services whether a date falls in a closed accounting period.
    The default consults only the module-owned ``ap_period_locks`` table; the
    General Ledger's period module (developed separately) can be wired here to
    add its own answer (``is_closed``) and to be notified when AP closes a
    period (``notify_closed``). Nothing in this module modifies GL periods.

Integration points that are data links rather than calls (no code to wire):

* ``cash``       ``vendor_payments.cash_account_id`` is a ledger ``accounts.id``;
                 the cash module's ``cash_accounts.ledger_account_id`` refers to the
                 same rows, so cash balances already reflect posted AP payments and
                 their reversals.
* ``reporting``  AP postings are ordinary posted entries; the trial balance sees
                 them without any AP-specific code. Outstanding / aging, supplier
                 statements, vendor balances and the AP control-account
                 reconciliation are module-owned.
* ``tax``        bill lines carry no tax code yet. Adding ``tax_code_id`` to
                 ``supplier_bill_lines`` (FK ``tax_codes.id``) and a tax line to the
                 approval entry is the intended extension.
* ``budgeting``  budget-vs-actual can join ``supplier_bill_lines.account_id``
                 (committed spend) with posted ledger lines (actual spend).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPort


# --- audit ------------------------------------------------------------------------------------


class AuditPort(Protocol):
    def record(self, db: Session, *, actor: str, action: str, resource: str) -> None: ...


class NullAudit:
    """Default until the audit module is wired: events are not persisted."""

    def record(self, db: Session, *, actor: str, action: str, resource: str) -> None:
        return None


# --- payment gateway (replaces the turn-3 ``PaymentExecutionPort``) ----------------------------------
#
# ``gateway``  (default: :class:`~app.modules.payables.services.gateway.DurableSimulatedGateway`)
#     Contract: ``submit(GatewayRequest) -> GatewayResponse`` (idempotent by token),
#     ``lookup(token)`` (status by operation id), ``confirmed_transfers(since)``.
#     Outcomes: accepted / rejected / pending / unknown; a lost response raises
#     ``GatewayTimeout`` and is resolved by lookup, never by blind resubmission.
#     The simulator persists in ``simulated_gateway_transfers`` on its own connection.


# --- fault injection -----------------------------------------------------------------------------------


class FaultPort(Protocol):
    def check(self, point: str) -> None: ...


class NullFaults:
    def check(self, point: str) -> None:
        return None


class ScriptedFaults:
    """Raise the configured exception the first ``times`` the named point is reached.

    Points are documented in ``services/orchestration.py`` and ``services/closing.py``.
    """

    def __init__(self, faults: dict[str, Exception | type[Exception]], times: int = 1) -> None:
        self.faults = dict(faults)
        self.remaining = {point: times for point in faults}
        self.hits: list[str] = []

    def check(self, point: str) -> None:
        self.hits.append(point)
        if self.remaining.get(point, 0) > 0:
            self.remaining[point] -= 1
            fault = self.faults[point]
            raise fault if isinstance(fault, Exception) else fault(f"injected fault at {point}")


# --- authorization ---------------------------------------------------------------------------------------


class AuthorizationPort(Protocol):
    """Future Security module hook: may ``actor`` perform ``action`` on ``resource``?"""

    def authorize(self, actor: str, action: str, resource: str) -> bool: ...


class PermissiveAuthorization:
    """Default until the Security module is wired: every named actor is allowed.

    Maker-checker separation (creator / reviewer / approver / executor) is enforced by
    the AP services themselves and does not depend on this port.
    """

    def authorize(self, actor: str, action: str, resource: str) -> bool:
        return bool(actor)


# --- accounting periods -----------------------------------------------------------------------


class PeriodPort(Protocol):
    def is_closed(self, db: Session, on: date) -> bool: ...

    def notify_closed(self, db: Session, *, closed_through: date, summary: dict) -> None: ...


class APOnlyPeriods:
    """Default: only AP's own period locks decide. Replace to consult the GL periods."""

    def is_closed(self, db: Session, on: date) -> bool:
        from app.modules.payables.models import APPeriodLock

        latest = db.scalar(
            select(APPeriodLock.closed_through).order_by(APPeriodLock.closed_through.desc())
        )
        return latest is not None and on <= latest

    def notify_closed(self, db: Session, *, closed_through: date, summary: dict) -> None:
        return None


# --- the bundle --------------------------------------------------------------------------------


def _default_gateway():
    from app.modules.payables.services.gateway import DurableSimulatedGateway

    return DurableSimulatedGateway()


@dataclass(frozen=True)
class Integrations:
    ledger: LedgerPort = field(default_factory=JournalLedgerAdapter)
    audit: AuditPort = field(default_factory=NullAudit)
    gateway: object = field(default_factory=_default_gateway)  # PaymentGatewayPort
    periods: PeriodPort = field(default_factory=APOnlyPeriods)
    faults: FaultPort = field(default_factory=NullFaults)
    authorization: AuthorizationPort = field(default_factory=PermissiveAuthorization)


def get_integrations() -> Integrations:
    """FastAPI dependency providing the production integrations."""
    return Integrations()
