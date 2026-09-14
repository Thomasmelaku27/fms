"""Payment gateway contract and the durable *simulated* gateway.

No real banking integration exists. The contract is what a future adapter must
implement; the simulator persists its transfers in ``simulated_gateway_transfers``
on its own connection (outside the AP transaction, like an external system) so
that restarts, lost responses and recovery behave realistically.

Outcomes
--------
* ``accepted``  the transfer is (or will be) executed; ``reference`` is final
* ``rejected``  definitive refusal; nothing moved
* ``pending``   received, final outcome not yet known (look it up later)
* ``unknown``   the gateway has no record of this token (never received)

A *lost response* is modelled by :class:`GatewayTimeout`: the simulator may have
recorded the transfer before raising, so the caller must treat the outcome as
unknown and resolve it with :meth:`lookup` -- never by submitting blindly.
Submissions are idempotent by token: a repeated token returns the original
outcome; a repeated token with a different fingerprint is rejected with
``TOKEN_MISMATCH`` (the instruction changed).
"""

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy import Date, Numeric, bindparam, text
from sqlalchemy.orm import Session


class GatewayOutcome(str, enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"
    UNKNOWN = "unknown"


class GatewayTimeout(Exception):
    """The response was lost; the transfer may or may not have happened."""


@dataclass(frozen=True)
class GatewayRequest:
    token: str  # operation uid: stable across retries
    fingerprint: str
    vendor_code: str
    destination_reference: str | None
    amount: Decimal
    currency: str
    pay_date: date
    method: str
    cash_account_code: str
    bill_references: tuple[str, ...]
    correlation_id: str


@dataclass(frozen=True)
class GatewayResponse:
    outcome: GatewayOutcome
    reference: str | None = None
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class GatewayEvidence:
    """What the gateway durably knows about a token (read-only; no side effects).

    Used by ``services.verification`` as *independent* evidence of the transfer: the
    simulator reads its own table; a real adapter would read the provider's statement
    or confirmation store. ``None`` means "the gateway has no record of this token".
    """

    token: str
    status: str  # accepted | rejected | pending
    reference: str | None
    vendor_code: str
    destination_reference: str | None
    amount: Decimal
    currency: str
    pay_date: date
    error_code: str | None = None


class PaymentGatewayPort(Protocol):
    def submit(self, request: GatewayRequest, *, db: Session) -> GatewayResponse: ...

    def lookup(self, token: str, *, db: Session) -> GatewayResponse: ...

    def evidence(self, token: str, *, db: Session) -> GatewayEvidence | None: ...

    def evidence_many(self, tokens: list[str], *, db: Session) -> dict[str, GatewayEvidence]: ...

    def confirmed_transfers(self, *, db: Session, since: date | None = None) -> list[dict]: ...


@dataclass
class GatewayScript:
    """Deterministic behaviour for tests (all keyed by vendor code)."""

    reject: set[str] | None = None  # definitive rejection
    lose_response: set[str] | None = None  # transfer recorded, then the response is lost (once)
    drop: set[str] | None = None  # request never arrives: timeout, nothing recorded (once)
    pending: set[str] | None = None  # accepted only after `pending_lookups` lookups
    pending_lookups: int = 1

    def _has(self, group, code):
        return bool(group) and code in group


class DurableSimulatedGateway:
    """Simulated gateway persisting in the same database on its own connection."""

    def __init__(self, script: GatewayScript | None = None) -> None:
        self.script = script or GatewayScript()
        self.calls: list[tuple[str, str]] = []  # (operation, token)
        self._lost_once: set[str] = set()
        self._dropped_once: set[str] = set()

    # -- persistence helpers (own connection: commits independently of the AP session) -------

    def _engine(self, db: Session):
        return db.get_bind()

    def _get(self, conn, token: str):
        return conn.execute(
            text(
                "SELECT token, fingerprint, status, reference, error_code, lookups FROM simulated_gateway_transfers WHERE token = :t"
            ),
            {"t": token},
        ).first()

    # -- contract --------------------------------------------------------------------------------

    def submit(self, request: GatewayRequest, *, db: Session) -> GatewayResponse:
        self.calls.append(("submit", request.token))
        script = self.script
        if (
            script._has(script.drop, request.vendor_code)
            and request.token not in self._dropped_once
        ):
            self._dropped_once.add(request.token)
            raise GatewayTimeout("request never reached the gateway")
        with self._engine(db).begin() as conn:
            existing = self._get(conn, request.token)
            if existing is not None:
                if existing.fingerprint != request.fingerprint:
                    return GatewayResponse(
                        GatewayOutcome.REJECTED,
                        error_code="TOKEN_MISMATCH",
                        message="token already used for a different instruction",
                    )
                return self._response(existing)
            if script._has(script.reject, request.vendor_code):
                status, reference, error = "rejected", None, "REJECTED"
            elif script._has(script.pending, request.vendor_code):
                status, reference, error = "pending", None, None
            else:
                status, reference, error = (
                    "accepted",
                    f"SIM-{request.token[:8]}-{request.vendor_code}",
                    None,
                )
            conn.execute(
                text(
                    "INSERT INTO simulated_gateway_transfers (token, fingerprint, vendor_code, destination_reference, amount, currency, "
                    "pay_date, status, reference, error_code, lookups, confirmed_at) VALUES (:t, :f, :v, :d, :a, :c, :p, :s, :r, :e, 0, "
                    "CASE WHEN :confirmed = 1 THEN CURRENT_TIMESTAMP ELSE NULL END)"
                ).bindparams(bindparam("a", type_=Numeric(18, 2)), bindparam("p", type_=Date())),
                {
                    "t": request.token,
                    "f": request.fingerprint,
                    "v": request.vendor_code,
                    "d": request.destination_reference,
                    "a": request.amount,
                    "c": request.currency,
                    "p": request.pay_date,
                    "s": status,
                    "r": reference,
                    "e": error,
                    "confirmed": 1 if status == "accepted" else 0,
                },
            )
        if (
            script._has(script.lose_response, request.vendor_code)
            and request.token not in self._lost_once
        ):
            self._lost_once.add(request.token)
            raise GatewayTimeout("response lost after the gateway recorded the transfer")
        return GatewayResponse(
            GatewayOutcome(status),
            reference=reference,
            error_code=error,
            message="simulated rejection" if error == "REJECTED" else None,
        )

    def lookup(self, token: str, *, db: Session) -> GatewayResponse:
        self.calls.append(("lookup", token))
        with self._engine(db).begin() as conn:
            row = self._get(conn, token)
            if row is None:
                return GatewayResponse(
                    GatewayOutcome.UNKNOWN, message="no transfer with this token"
                )
            lookups = row.lookups + 1
            status, reference = row.status, row.reference
            if status == "pending" and lookups >= self.script.pending_lookups:
                status = "accepted"
                reference = f"SIM-{token[:8]}-LATE"
                conn.execute(
                    text(
                        "UPDATE simulated_gateway_transfers SET status = 'accepted', reference = :r, lookups = :l, "
                        "confirmed_at = CURRENT_TIMESTAMP WHERE token = :t"
                    ),
                    {"r": reference, "l": lookups, "t": token},
                )
            else:
                conn.execute(
                    text("UPDATE simulated_gateway_transfers SET lookups = :l WHERE token = :t"),
                    {"l": lookups, "t": token},
                )
            return GatewayResponse(
                GatewayOutcome(status), reference=reference, error_code=row.error_code
            )

    def evidence(self, token: str, *, db: Session) -> GatewayEvidence | None:
        """Durable record of the token without counting as a lookup (verification only)."""
        return self.evidence_many([token], db=db).get(token)

    def evidence_many(self, tokens: list[str], *, db: Session) -> dict[str, GatewayEvidence]:
        """Same as :meth:`evidence` for a batch (one query; used by batch verification)."""
        if not tokens:
            return {}
        with self._engine(db).connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT token, status, reference, vendor_code, destination_reference, amount, currency, "
                    "pay_date, error_code FROM simulated_gateway_transfers WHERE token IN :tokens"
                ).bindparams(bindparam("tokens", expanding=True)),
                {"tokens": list(tokens)},
            ).all()
        out = {}
        for row in rows:
            pay_date = (
                row.pay_date
                if isinstance(row.pay_date, date)
                else date.fromisoformat(str(row.pay_date)[:10])
            )
            out[row.token] = GatewayEvidence(
                token=row.token,
                status=row.status,
                reference=row.reference,
                vendor_code=row.vendor_code,
                destination_reference=row.destination_reference,
                amount=Decimal(str(row.amount)),
                currency=row.currency,
                pay_date=pay_date,
                error_code=row.error_code,
            )
        return out

    def confirmed_transfers(self, *, db: Session, since: date | None = None) -> list[dict]:
        with self._engine(db).begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT token, reference, amount, currency, pay_date, vendor_code FROM simulated_gateway_transfers "
                    "WHERE status = 'accepted' AND (:since IS NULL OR pay_date >= :since) ORDER BY id"
                ).bindparams(bindparam("since", type_=Date())),
                {"since": since},
            ).all()
        return [dict(r._mapping) for r in rows]

    def _response(self, row) -> GatewayResponse:
        return GatewayResponse(
            GatewayOutcome(row.status),
            reference=row.reference,
            error_code=row.error_code,
            message="simulated rejection" if row.error_code == "REJECTED" else None,
        )
