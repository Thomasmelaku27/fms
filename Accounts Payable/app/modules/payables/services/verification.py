"""Independent verification of payment operations from durable records.

Status flags (``recording_status``, ``ledger_status``, ``allocation_status``, even
``state``) are *claims*. This module re-derives the financial state of an operation
from what is actually in the database -- the gateway's durable transfer record, the
operation's event chain, the vendor payment, the posted journal entry and its lines,
allocations, discounts, and the payment run's items -- and reports every contradiction
as a structured :class:`Finding` with a severity and a remediation.

Four statuses are derived and kept apart:

* **delivery**    did the gateway accept the transfer? (``delivered`` / ``rejected`` /
                  ``unknown`` / ``not_sent``)
* **accounting**  is the transfer in the books? (``recorded`` / ``corrected`` /
                  ``unrecorded`` / ``inconsistent`` / ``not_applicable``)
* **settlement**  how was the cash applied? (``allocated`` / ``partial`` / ``unapplied``
                  / ``none``)
* **investigation** the operator workflow (``none`` / ``open`` / ``closed``) -- never a
                  financial fact.

Completion policy
-----------------
An operation may be (or remain) ``completed`` only when every item of
:data:`REQUIRED_EVIDENCE` is established here and no ``critical`` or ``error`` finding
exists. ``services.recovery.complete_with_evidence`` calls :func:`verify_operation`
with the candidate payment *before* it transitions, and the database trigger of
migration ``fmspayables06`` re-checks the core of it (payment of the same vendor,
amount and cash account, posted entry crediting cash). A resolution note is never
evidence.

Money is compared exactly (``Decimal`` / integer cents); there are no tolerances.
"""

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EntryStatus, JournalEntry, JournalLine
from app.modules.payables.models import (
    MONEY_MOVED_STATES,
    BillDiscount,
    OperationState,
    PaymentAllocation,
    PaymentOperation,
    PaymentOperationEvent,
    PaymentRun,
    PaymentRunItem,
    PaymentRunStatus,
    PaymentStatus,
    RunItemStatus,
    SupplierBill,
    Vendor,
    VendorPayment,
)
from app.modules.payables.services import execution_state as state

ZERO = Decimal("0.00")
MODULE_CURRENCY = "XXX"  # the module is single-currency; the placeholder code is explicit

CRITICAL, ERROR, WARNING, INFO = "critical", "error", "warning", "info"
SEVERITY_RANK = {CRITICAL: 3, ERROR: 2, WARNING: 1, INFO: 0}

REQUIRED_EVIDENCE = (
    "gateway_transfer",  # the gateway durably records an accepted transfer for the token
    "gateway_matches",  # reference, amount, currency, destination and vendor agree
    "events_intact",  # contiguous, hash-chained history with a gateway acceptance event
    "ap_payment",  # a recorded vendor payment is linked
    "payment_matches",  # vendor, amount, cash account, currency, reference
    "journal_posted",  # its journal entry is posted
    "cash_movement",  # the posted entry credits the cash account by the amount, balanced
    "allocations_valid",  # allocations <= amount, same vendor, remainder has a payable account
    "no_duplicates",  # no second payment / operation for the same transfer
    "run_consistent",  # run items and run status agree with the operation
)

# ``completion_kind`` values
KIND_AUTOMATIC = "automatic"
KIND_MANUAL_CORRECTION = "manual_correction"
CORRECTION_KINDS = (KIND_MANUAL_CORRECTION, "legacy_manual", "legacy_backfill")


@dataclass
class Finding:
    code: str
    severity: str
    operation_uid: str
    vendor_id: int | None
    amount: Decimal | None
    expected: str | None
    actual: str | None
    remediation: str
    detail: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = None if self.amount is None else f"{self.amount:.2f}"
        return d


@dataclass
class OperationVerification:
    operation_uid: str
    state: str
    vendor_id: int
    amount: Decimal
    currency: str
    run_id: int | None
    delivery_status: str
    accounting_status: str
    settlement_status: str
    investigation_status: str
    recorded_accounting_status: str  # what the row claims (``accounting_status`` column)
    evidence: dict = field(default_factory=dict)
    required_evidence: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst_severity(self) -> str | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda f: SEVERITY_RANK[f.severity]).severity

    @property
    def completion_policy_satisfied(self) -> bool:
        return all(self.required_evidence.get(k) for k in REQUIRED_EVIDENCE) and not any(
            f.severity in (CRITICAL, ERROR) for f in self.findings
        )

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in (CRITICAL, ERROR)]

    def as_dict(self) -> dict:
        return {
            "operation_uid": self.operation_uid,
            "state": self.state,
            "vendor_id": self.vendor_id,
            "amount": self.amount,
            "currency": self.currency,
            "run_id": self.run_id,
            "delivery_status": self.delivery_status,
            "accounting_status": self.accounting_status,
            "settlement_status": self.settlement_status,
            "investigation_status": self.investigation_status,
            "recorded_accounting_status": self.recorded_accounting_status,
            "completion_policy_satisfied": self.completion_policy_satisfied,
            "worst_severity": self.worst_severity,
            "required_evidence": dict(self.required_evidence),
            "evidence": self.evidence,
            "findings": [f.as_dict() for f in self.findings],
        }


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


class Store:
    """Read access used by the verifier; ``Store.prefetch`` loads everything for a batch.

    Without prefetching every accessor issues its own query (fine for one operation).
    ``verify_operations`` prefetches for the whole batch: a handful of ``IN`` queries
    instead of a dozen round trips per operation. Results are identical.
    """

    def __init__(self, db: Session):
        self.db = db
        self.loaded = False
        self.vendors: dict[int, Vendor] = {}
        self.events: dict[int, list[PaymentOperationEvent]] = {}
        self.payments: dict[int, VendorPayment] = {}
        self.replacements: dict[int, VendorPayment] = {}
        self.entries: dict[int, JournalEntry] = {}
        self.lines: dict[int, list[JournalLine]] = {}
        self.allocations: dict[int, list[PaymentAllocation]] = {}
        self.bills: dict[int, SupplierBill] = {}
        self.discounts: dict[int, list[BillDiscount]] = {}
        self.payments_by_ref: dict[tuple[int, str], list[VendorPayment]] = {}
        self.ops_by_ref: dict[str, list[PaymentOperation]] = {}
        self.payments_by_key: dict[str, list[VendorPayment]] = {}
        self.ops_by_payment: dict[int, list[PaymentOperation]] = {}
        self.live_ops: dict[tuple[int, int], list[PaymentOperation]] = {}
        self.runs: dict[int, PaymentRun] = {}
        self.items: dict[tuple[int, int], list[PaymentRunItem]] = {}
        self.gateway_evidence: dict | None = None  # token -> GatewayEvidence, when prefetched

    @classmethod
    def prefetch(cls, db: Session, ops: list[PaymentOperation], *, gateway=None) -> "Store":
        store = cls(db)
        if gateway is not None and hasattr(gateway, "evidence_many"):
            store.gateway_evidence = gateway.evidence_many([o.operation_uid for o in ops], db=db)
        if not ops:
            store.loaded = True
            return store
        op_ids = [o.id for o in ops]
        for v in db.scalars(select(Vendor).where(Vendor.id.in_({o.vendor_id for o in ops}))):
            store.vendors[v.id] = v
        for e in db.scalars(
            select(PaymentOperationEvent)
            .where(PaymentOperationEvent.operation_id.in_(op_ids))
            .order_by(PaymentOperationEvent.operation_id, PaymentOperationEvent.seq)
        ):
            store.events.setdefault(e.operation_id, []).append(e)
        payment_ids = {o.payment_id for o in ops if o.payment_id is not None}
        payments = (
            list(db.scalars(select(VendorPayment).where(VendorPayment.id.in_(payment_ids))))
            if payment_ids
            else []
        )
        for r in (
            db.scalars(
                select(VendorPayment).where(
                    VendorPayment.corrects_payment_id.in_(payment_ids),
                    VendorPayment.status == PaymentStatus.RECORDED,
                )
            )
            if payment_ids
            else []
        ):
            store.replacements[r.corrects_payment_id] = r
            payments.append(r)
        refs = {o.gateway_confirmation_ref for o in ops if o.gateway_confirmation_ref}
        if refs:
            for p in db.scalars(
                select(VendorPayment).where(
                    VendorPayment.reference.in_(refs),
                    VendorPayment.status == PaymentStatus.RECORDED,
                )
            ):
                store.payments_by_ref.setdefault((p.vendor_id, p.reference), []).append(p)
                payments.append(p)
            for other in db.scalars(
                select(PaymentOperation).where(PaymentOperation.gateway_confirmation_ref.in_(refs))
            ):
                store.ops_by_ref.setdefault(other.gateway_confirmation_ref, []).append(other)
        keys = [f"op:{o.operation_uid}" for o in ops]
        for p in db.scalars(select(VendorPayment).where(VendorPayment.idempotency_key.in_(keys))):
            store.payments_by_key.setdefault(p.idempotency_key, []).append(p)
            payments.append(p)
        for p in payments:
            store.payments[p.id] = p
        all_payment_ids = set(store.payments)
        if all_payment_ids:
            for other in db.scalars(
                select(PaymentOperation).where(PaymentOperation.payment_id.in_(all_payment_ids))
            ):
                store.ops_by_payment.setdefault(other.payment_id, []).append(other)
            entry_ids = {p.journal_entry_id for p in store.payments.values() if p.journal_entry_id}
            for a in db.scalars(
                select(PaymentAllocation).where(
                    PaymentAllocation.payment_id.in_(all_payment_ids),
                    PaymentAllocation.unallocation_date.is_(None),
                )
            ):
                store.allocations.setdefault(a.payment_id, []).append(a)
            for d in db.scalars(
                select(BillDiscount).where(
                    BillDiscount.payment_id.in_(all_payment_ids),
                    BillDiscount.reversal_date.is_(None),
                )
            ):
                store.discounts.setdefault(d.payment_id, []).append(d)
                if d.journal_entry_id:
                    entry_ids.add(d.journal_entry_id)
            if entry_ids:
                for e in db.scalars(select(JournalEntry).where(JournalEntry.id.in_(entry_ids))):
                    store.entries[e.id] = e
                for line in db.scalars(
                    select(JournalLine).where(JournalLine.entry_id.in_(entry_ids))
                ):
                    store.lines.setdefault(line.entry_id, []).append(line)
            bill_ids = {a.bill_id for rows in store.allocations.values() for a in rows}
            if bill_ids:
                for b in db.scalars(select(SupplierBill).where(SupplierBill.id.in_(bill_ids))):
                    store.bills[b.id] = b
        run_ids = {o.run_id for o in ops if o.run_id is not None}
        if run_ids:
            for r in db.scalars(select(PaymentRun).where(PaymentRun.id.in_(run_ids))):
                store.runs[r.id] = r
            for other in db.scalars(
                select(PaymentOperation).where(
                    PaymentOperation.run_id.in_(run_ids),
                    PaymentOperation.state.in_(MONEY_MOVED_STATES),
                )
            ):
                store.live_ops.setdefault((other.run_id, other.vendor_id), []).append(other)
            for i in db.scalars(
                select(PaymentRunItem).where(
                    PaymentRunItem.run_id.in_(run_ids),
                    PaymentRunItem.status != RunItemStatus.EXCLUDED,
                )
            ):
                store.items.setdefault((i.run_id, i.vendor_id), []).append(i)
        store.loaded = True
        return store

    # --- accessors (prefetched dict or a query) ---------------------------------------------------

    def vendor(self, vendor_id):
        return self.vendors[vendor_id] if self.loaded else self.db.get(Vendor, vendor_id)

    def events_of(self, op):
        if self.loaded:
            return list(self.events.get(op.id, []))
        return list(
            self.db.scalars(
                select(PaymentOperationEvent)
                .where(PaymentOperationEvent.operation_id == op.id)
                .order_by(PaymentOperationEvent.seq)
            )
        )

    def payment(self, payment_id):
        if payment_id is None:
            return None
        if self.loaded and payment_id in self.payments:
            return self.payments[payment_id]
        return self.db.get(VendorPayment, payment_id)  # not cached: its rows were not prefetched

    def replacement_of(self, payment):
        if self.loaded:
            return self.replacements.get(payment.id)
        return self.db.scalar(
            select(VendorPayment).where(
                VendorPayment.corrects_payment_id == payment.id,
                VendorPayment.status == PaymentStatus.RECORDED,
            )
        )

    def entry(self, entry_id):
        if entry_id is None:
            return None
        if self.loaded and entry_id in self.entries:
            return self.entries[entry_id]
        return self.db.get(JournalEntry, entry_id)

    def lines_of(self, entry):
        if self.loaded and entry.id in self.lines:
            return list(self.lines[entry.id])
        return list(self.db.scalars(select(JournalLine).where(JournalLine.entry_id == entry.id)))

    def allocations_of(self, payment):
        if self.loaded and payment.id in self.payments:
            return list(self.allocations.get(payment.id, []))
        return list(
            self.db.scalars(
                select(PaymentAllocation).where(
                    PaymentAllocation.payment_id == payment.id,
                    PaymentAllocation.unallocation_date.is_(None),
                )
            )
        )

    def bills_of(self, allocations):
        ids = {a.bill_id for a in allocations}
        if not ids:
            return {}
        if self.loaded and ids <= set(self.bills):
            return {i: self.bills[i] for i in ids}
        return {
            b.id: b for b in self.db.scalars(select(SupplierBill).where(SupplierBill.id.in_(ids)))
        }

    def discounts_of(self, payment):
        if self.loaded and payment.id in self.payments:
            return list(self.discounts.get(payment.id, []))
        return list(
            self.db.scalars(
                select(BillDiscount).where(
                    BillDiscount.payment_id == payment.id, BillDiscount.reversal_date.is_(None)
                )
            )
        )

    def recorded_payments_with_reference(self, vendor_id, reference):
        if self.loaded:
            return list(self.payments_by_ref.get((vendor_id, reference), []))
        return list(
            self.db.scalars(
                select(VendorPayment).where(
                    VendorPayment.vendor_id == vendor_id,
                    VendorPayment.reference == reference,
                    VendorPayment.status == PaymentStatus.RECORDED,
                )
            )
        )

    def operations_with_reference(self, reference):
        if self.loaded:
            return list(self.ops_by_ref.get(reference, []))
        return list(
            self.db.scalars(
                select(PaymentOperation).where(
                    PaymentOperation.gateway_confirmation_ref == reference
                )
            )
        )

    def payments_with_key(self, key):
        if self.loaded:
            return list(self.payments_by_key.get(key, []))
        return list(
            self.db.scalars(select(VendorPayment).where(VendorPayment.idempotency_key == key))
        )

    def operations_of_payment(self, payment_id):
        if self.loaded:
            return list(self.ops_by_payment.get(payment_id, []))
        return list(
            self.db.scalars(
                select(PaymentOperation).where(PaymentOperation.payment_id == payment_id)
            )
        )

    def live_operations(self, run_id, vendor_id):
        if self.loaded:
            return list(self.live_ops.get((run_id, vendor_id), []))
        return list(
            self.db.scalars(
                select(PaymentOperation).where(
                    PaymentOperation.run_id == run_id,
                    PaymentOperation.vendor_id == vendor_id,
                    PaymentOperation.state.in_(MONEY_MOVED_STATES),
                )
            )
        )

    def run(self, run_id):
        if self.loaded:
            return self.runs.get(run_id)
        return self.db.get(PaymentRun, run_id)

    def items_of(self, run_id, vendor_id):
        if self.loaded:
            return list(self.items.get((run_id, vendor_id), []))
        return list(
            self.db.scalars(
                select(PaymentRunItem).where(
                    PaymentRunItem.run_id == run_id,
                    PaymentRunItem.vendor_id == vendor_id,
                    PaymentRunItem.status != RunItemStatus.EXCLUDED,
                )
            )
        )


def verify_operation(
    db: Session,
    op: PaymentOperation,
    *,
    gateway=None,
    candidate_payment_id: int | None = None,
    completion_kind: str | None = None,
    store: "Store | None" = None,
) -> OperationVerification:
    """Reconstruct the operation's financial state from durable rows and compare it with its claims.

    ``gateway`` is the payment gateway port (its ``evidence(token)`` is the independent
    transfer record); without it the gateway evidence counts as *unavailable*, which
    fails the completion policy. ``candidate_payment_id`` verifies a payment that is
    about to be linked (manual completion) in place of ``op.payment_id``.
    """
    db.flush()
    store = store or Store(db)
    vendor = store.vendor(op.vendor_id)
    money_moved = op.state in MONEY_MOVED_STATES
    kind = completion_kind or op.completion_kind
    is_correction = kind in CORRECTION_KINDS or (
        candidate_payment_id is not None and kind != KIND_AUTOMATIC
    )
    result = OperationVerification(
        operation_uid=op.operation_uid,
        state=op.state.value,
        vendor_id=op.vendor_id,
        amount=op.amount,
        currency=op.currency,
        run_id=op.run_id,
        delivery_status="unknown",
        accounting_status="not_applicable",
        settlement_status="none",
        investigation_status=op.investigation_status,
        recorded_accounting_status=op.accounting_status,
        required_evidence={k: False for k in REQUIRED_EVIDENCE},
    )
    findings = result.findings
    req = result.required_evidence

    def add(code, severity, *, expected=None, actual=None, remediation, detail=""):
        findings.append(
            Finding(
                code,
                severity,
                op.operation_uid,
                op.vendor_id,
                op.amount,
                expected,
                actual,
                remediation,
                detail,
            )
        )

    # --- 1. gateway ---------------------------------------------------------------------------------
    evidence = None
    legacy = (
        op.fingerprint == "backfill"
    )  # fmspayables05 back-fill of Turn-3 executions: no gateway store existed
    if legacy:
        req["gateway_transfer"] = req["gateway_matches"] = bool(op.gateway_confirmation_ref)
        result.delivery_status = "delivered" if op.gateway_confirmation_ref else "unknown"
        add(
            "gateway_evidence_not_applicable",
            INFO,
            remediation="back-filled Turn-3 execution: the execution reference of the paid run items is the only transfer evidence",
        )
    elif gateway is None or not hasattr(gateway, "evidence"):
        add(
            "gateway_evidence_unavailable",
            INFO if not money_moved else WARNING,
            remediation="verify with the gateway port that holds the transfer records",
            detail="no gateway evidence source was provided to the verifier",
        )
    elif store.gateway_evidence is not None:
        evidence = store.gateway_evidence.get(op.operation_uid)
    else:
        evidence = gateway.evidence(op.operation_uid, db=db)
    if evidence is not None:
        result.evidence["gateway"] = {
            "status": evidence.status,
            "reference": evidence.reference,
            "amount": f"{evidence.amount:.2f}",
            "currency": evidence.currency,
            "vendor_code": evidence.vendor_code,
            "destination_reference": evidence.destination_reference,
            "pay_date": evidence.pay_date.isoformat(),
        }
        result.delivery_status = {
            "accepted": "delivered",
            "rejected": "rejected",
            "pending": "unknown",
        }.get(evidence.status, "unknown")
        req["gateway_transfer"] = evidence.status == "accepted"
        matches = True
        if op.gateway_confirmation_ref and evidence.reference != op.gateway_confirmation_ref:
            matches = False
            add(
                "gateway_reference_mismatch",
                CRITICAL,
                expected=evidence.reference,
                actual=op.gateway_confirmation_ref,
                remediation="investigate with the gateway; the recorded confirmation does not belong to this transfer",
            )
        if evidence.amount != op.amount:
            matches = False
            add(
                "gateway_amount_mismatch",
                CRITICAL,
                expected=f"{op.amount:.2f}",
                actual=f"{evidence.amount:.2f}",
                remediation="the gateway moved a different amount than instructed; reconcile with the bank statement",
            )
        if evidence.currency != op.currency:
            matches = False
            add(
                "gateway_currency_mismatch",
                CRITICAL,
                expected=op.currency,
                actual=evidence.currency,
                remediation="currency differs from the instruction; investigate",
            )
        if (evidence.destination_reference or "") != (op.destination_reference or ""):
            matches = False
            add(
                "gateway_destination_mismatch",
                CRITICAL,
                expected=op.destination_reference,
                actual=evidence.destination_reference,
                remediation="the transfer went to a different destination than instructed; investigate immediately",
            )
        if vendor is not None and evidence.vendor_code != vendor.code:
            matches = False
            add(
                "gateway_vendor_mismatch",
                CRITICAL,
                expected=vendor.code,
                actual=evidence.vendor_code,
                remediation="the gateway record names another supplier; investigate",
            )
        req["gateway_matches"] = matches and evidence.status == "accepted"
        if money_moved and evidence.status != "accepted":
            add(
                "gateway_not_accepted",
                CRITICAL,
                expected="accepted",
                actual=evidence.status,
                remediation="the operation claims money moved but the gateway does not confirm it; do not record or complete",
            )
        if evidence.status == "accepted" and op.state in (
            OperationState.SUBMITTED,
            OperationState.CONFIRMATION_PENDING,
        ):
            add(
                "transfer_accepted_state_unaware",
                WARNING,
                expected="confirmed",
                actual=op.state.value,
                remediation="the gateway accepted the transfer but the operation has not learnt it yet; recovery's lookup will confirm and record it",
            )
        if op.state == OperationState.FAILED and evidence.status == "accepted":
            add(
                "failed_but_transfer_accepted",
                CRITICAL,
                expected="money moved: confirmed / completed",
                actual="failed",
                remediation="the gateway accepted this transfer; record the payment through the correction workflow",
            )
    elif gateway is not None and hasattr(gateway, "evidence") and not legacy:
        result.delivery_status = "not_sent" if op.state == OperationState.PREPARED else "unknown"
        if money_moved or op.gateway_status == "accepted" or op.gateway_confirmation_ref:
            add(
                "gateway_evidence_missing",
                CRITICAL,
                expected="a transfer record for the operation token",
                actual="none",
                remediation="the claimed confirmation has no transfer behind it; investigate with the gateway before any accounting",
            )
    if money_moved and not op.gateway_confirmation_ref:
        add(
            "confirmed_without_reference",
            ERROR,
            expected="a gateway confirmation reference",
            actual=None,
            remediation="obtain the confirmation reference from the gateway (lookup) before recording",
        )

    if (
        vendor is not None
        and getattr(vendor.status, "value", vendor.status) != "active"
        and money_moved
    ):
        add(
            "vendor_not_active",
            WARNING,
            expected="active",
            actual=getattr(vendor.status, "value", str(vendor.status)),
            remediation="the supplier is no longer active (suspended after authorisation?); the transfer still has to be accounted for",
        )

    # --- 2. events -----------------------------------------------------------------------------------
    events = store.events_of(op)
    intact = bool(events) or legacy
    if not events and legacy:
        add(
            "events_not_applicable",
            INFO,
            remediation="back-filled Turn-3 execution: no event history existed before fmspayables05",
        )
    elif not events:
        add(
            "events_missing",
            ERROR,
            remediation="an operation without history cannot be trusted; investigate",
        )
    prev_hash = None
    unhashed = 0
    for index, e in enumerate(events, start=1):
        if e.seq != index:
            intact = False
            add(
                "events_sequence_gap",
                CRITICAL,
                expected=f"seq {index}",
                actual=f"seq {e.seq}",
                remediation="events were deleted or renumbered; treat every status claim as unverified",
            )
            break
        if e.event_hash is None:
            unhashed += 1
        else:
            expected_hash = state.event_hash(
                prev_hash,
                op.operation_uid,
                e.seq,
                e.from_state,
                e.to_state,
                e.category,
                e.detail,
                e.actor,
            )
            if e.prev_hash != prev_hash or e.event_hash != expected_hash:
                intact = False
                add(
                    "events_hash_chain_broken",
                    CRITICAL,
                    expected=expected_hash[:16],
                    actual=(e.event_hash or "")[:16],
                    remediation="an event was edited or removed; treat every status claim as unverified",
                    detail=f"seq {e.seq}",
                )
                break
        prev_hash = e.event_hash
    if unhashed and intact:
        add(
            "events_unhashed",
            WARNING,
            remediation="events predate the hash chain; only sequence contiguity was verified",
            detail=f"{unhashed} event(s) without hash",
        )
    if events and events[-1].to_state != op.state.value and not legacy:
        intact = False
        add(
            "events_state_mismatch",
            CRITICAL,
            expected=events[-1].to_state,
            actual=op.state.value,
            remediation="the state was changed without a transition event (direct write); investigate",
        )
    has_acceptance = any(
        e.category == "gateway" and e.to_state == OperationState.CONFIRMED.value for e in events
    )
    if money_moved and not has_acceptance and not legacy:
        intact = False
        add(
            "events_no_gateway_acceptance",
            ERROR,
            remediation="no event records the gateway's acceptance; confirm with the gateway before recording",
        )
    if (
        op.state == OperationState.COMPLETED
        and not legacy
        and not any(
            e.to_state == OperationState.COMPLETED.value
            and e.from_state != OperationState.COMPLETED.value
            for e in events
        )
    ):
        intact = False
        add(
            "events_no_completion",
            ERROR,
            remediation="no event records the completion; investigate",
        )
    req["events_intact"] = intact
    result.evidence["events"] = {
        "count": len(events),
        "hash_chain_verified": intact and not unhashed,
    }

    # --- 3. AP payment, journal, allocations ---------------------------------------------------------
    payment_id = candidate_payment_id if candidate_payment_id is not None else op.payment_id
    payment = store.payment(payment_id)
    original = payment
    if payment_id is not None and payment is None:
        add(
            "payment_not_found",
            CRITICAL,
            expected=f"vendor payment {payment_id}",
            actual="missing",
            remediation="the referenced payment does not exist; investigate the link",
        )
    if op.currency != MODULE_CURRENCY:
        add(
            "currency_unsupported",
            ERROR,
            expected=MODULE_CURRENCY,
            actual=op.currency,
            remediation="the AP module is single-currency; a foreign-currency transfer cannot be recorded here",
        )
    planned_pay = sum((Decimal(i["pay"]) for i in state.plan_of(op)), ZERO)
    plan_bills = {i["bill_id"] for i in state.plan_of(op)}
    if payment is None:
        if op.state == OperationState.COMPLETED:
            add(
                "payment_missing",
                CRITICAL,
                expected="a recorded vendor payment",
                actual="none",
                remediation="completed without an AP payment: record the payment through the correction workflow and complete with evidence",
            )
            result.accounting_status = "inconsistent"
        elif money_moved:
            result.accounting_status = "unrecorded"
            add(
                "accounting_unrecorded",
                INFO,
                expected="a recorded vendor payment",
                actual="none",
                remediation="let recovery retry the recording, or record the payment through the correction workflow",
            )
    else:
        original = payment
        replacement = None
        if (
            payment.status == PaymentStatus.REVERSED
            and candidate_payment_id is None
            and op.state == OperationState.COMPLETED
        ):
            # Reverse-and-replace through the payment correction workflow: the replacement
            # is the accounting record of the transfer now; the chain is reported.
            replacement = store.replacement_of(payment)
            if replacement is not None:
                add(
                    "payment_corrected",
                    INFO,
                    expected=str(payment.id),
                    actual=str(replacement.id),
                    remediation="the original recording was reversed and replaced through the correction workflow",
                )
                payment = replacement
            else:
                add(
                    "transfer_unaccounted_after_reversal",
                    ERROR,
                    expected="a replacement payment",
                    actual="reversed, no replacement",
                    remediation="the only recording of a confirmed transfer was reversed; record the refund or the replacement",
                )
        req["ap_payment"] = payment.status == PaymentStatus.RECORDED
        result.evidence["payment"] = {
            "id": payment.id,
            "vendor_id": payment.vendor_id,
            "amount": f"{payment.amount:.2f}",
            "payment_date": payment.payment_date.isoformat(),
            "cash_account_id": payment.cash_account_id,
            "reference": payment.reference,
            "status": payment.status.value,
            "idempotency_key": payment.idempotency_key,
            "journal_entry_id": payment.journal_entry_id,
        }
        matches = True
        if payment.vendor_id != op.vendor_id:
            matches = False
            add(
                "payment_vendor_mismatch",
                CRITICAL,
                expected=str(op.vendor_id),
                actual=str(payment.vendor_id),
                remediation="the linked payment belongs to another supplier; unlink and investigate",
            )
        if payment.amount != op.amount:
            matches = False
            add(
                "payment_amount_mismatch",
                CRITICAL,
                expected=f"{op.amount:.2f}",
                actual=f"{payment.amount:.2f}",
                remediation="the recorded amount differs from the transfer; reverse and re-record the exact amount",
            )
        if payment.cash_account_id != op.cash_account_id and replacement is not None:
            add(
                "replacement_cash_account_differs",
                INFO,
                expected=str(op.cash_account_id),
                actual=str(payment.cash_account_id),
                remediation="the correction moved the recording to another cash account; the bank reconciliation of both accounts must agree",
            )
        elif payment.cash_account_id != op.cash_account_id:
            matches = False
            add(
                "payment_cash_account_mismatch",
                CRITICAL,
                expected=str(op.cash_account_id),
                actual=str(payment.cash_account_id),
                remediation="the payment credits another cash account than the transfer used",
            )
        if (
            op.gateway_confirmation_ref
            and payment.reference != op.gateway_confirmation_ref
            and replacement is None
        ):
            matches = False
            add(
                "payment_reference_mismatch",
                ERROR,
                expected=op.gateway_confirmation_ref,
                actual=payment.reference,
                remediation="the payment must carry the gateway confirmation reference so it can be traced to the transfer",
            )
        if payment.status == PaymentStatus.REVERSED:
            matches = False
            add(
                "payment_reversed",
                ERROR,
                expected="recorded",
                actual="reversed",
                remediation="a reversed payment is not evidence; if the reversal was a correction, link its replacement",
            )
        automatic_link = original.idempotency_key == f"op:{op.operation_uid}" or (
            replacement is not None and original.idempotency_key == f"op:{op.operation_uid}"
        )
        if not automatic_link and not is_correction and not legacy:
            matches = False
            add(
                "payment_not_linked_to_operation",
                ERROR,
                expected=f"idempotency key op:{op.operation_uid} or an authorised correction",
                actual=payment.idempotency_key,
                remediation="an automatically completed operation must point at the payment it recorded; investigate the link",
            )
        if payment.payment_date != op.payment_date:
            add(
                "payment_date_differs",
                INFO if is_correction else WARNING,
                expected=op.payment_date.isoformat(),
                actual=payment.payment_date.isoformat(),
                remediation="the accounting date differs from the instructed pay date (allowed for corrections dated in an open period)",
            )
        req["payment_matches"] = matches

        entry = store.entry(payment.journal_entry_id)
        if entry is None:
            add(
                "journal_missing",
                CRITICAL,
                expected="a posted journal entry",
                actual="none",
                remediation="the payment has no ledger entry; post it through the correction workflow",
            )
        else:
            lines = store.lines_of(entry)
            debits = sum((_money(line.debit) for line in lines), ZERO)
            credits = sum((_money(line.credit) for line in lines), ZERO)
            cash_credit = sum(
                (
                    _money(line.credit)
                    for line in lines
                    if line.account_id == payment.cash_account_id
                ),
                ZERO,
            )
            result.evidence["journal"] = {
                "id": entry.id,
                "status": entry.status.value,
                "entry_date": entry.entry_date.isoformat(),
                "debits": f"{debits:.2f}",
                "credits": f"{credits:.2f}",
                "cash_credit": f"{cash_credit:.2f}",
            }
            req["journal_posted"] = entry.status == EntryStatus.POSTED
            if entry.status != EntryStatus.POSTED:
                add(
                    "journal_not_posted",
                    CRITICAL,
                    expected="posted",
                    actual=entry.status.value,
                    remediation="the ledger entry is not posted; the payment is not in the books",
                )
            cash_ok = cash_credit == op.amount and debits == credits and debits == op.amount
            req["cash_movement"] = cash_ok and entry.status == EntryStatus.POSTED
            if cash_credit != op.amount:
                add(
                    "journal_cash_mismatch",
                    CRITICAL,
                    expected=f"{op.amount:.2f}",
                    actual=f"{cash_credit:.2f}",
                    remediation="the posted entry does not credit the cash account by the transferred amount",
                )
            if debits != credits:
                add(
                    "journal_unbalanced",
                    CRITICAL,
                    expected=f"{debits:.2f}",
                    actual=f"{credits:.2f}",
                    remediation="the entry does not balance; this should be impossible for a posted entry",
                )
            if entry.entry_date != payment.payment_date:
                add(
                    "journal_date_mismatch",
                    ERROR,
                    expected=payment.payment_date.isoformat(),
                    actual=entry.entry_date.isoformat(),
                    remediation="the entry is dated differently from the payment",
                )

        allocations = store.allocations_of(payment)
        allocated = sum((a.amount for a in allocations), ZERO)
        unapplied = payment.amount - allocated
        bills = store.bills_of(allocations)
        allocations_ok = True
        if allocated > payment.amount:
            allocations_ok = False
            add(
                "allocations_exceed_payment",
                CRITICAL,
                expected=f"<= {payment.amount:.2f}",
                actual=f"{allocated:.2f}",
                remediation="allocations exceed the payment; unallocate the excess",
            )
        for a in allocations:
            bill = bills.get(a.bill_id)
            if bill is None or bill.vendor_id != op.vendor_id:
                allocations_ok = False
                add(
                    "allocation_foreign_vendor",
                    CRITICAL,
                    expected=str(op.vendor_id),
                    actual=str(bill.vendor_id if bill else None),
                    remediation="an allocation settles another supplier's bill; unallocate it",
                    detail=f"allocation {a.id} bill {a.bill_id}",
                )
            elif a.bill_id not in plan_bills and plan_bills:
                add(
                    "allocation_outside_plan",
                    WARNING,
                    expected=f"bills {sorted(plan_bills)}",
                    actual=str(a.bill_id),
                    remediation="the cash settled a bill that was not in the approved plan; confirm the reason is documented",
                )
        if unapplied > 0 and payment.payable_account_id is None:
            allocations_ok = False
            add(
                "unapplied_without_payable_account",
                ERROR,
                expected="a payable account for the unapplied remainder",
                actual=None,
                remediation="the advance has no control account; this should be impossible for a posted payment",
            )
        discounts = store.discounts_of(payment)
        for d in discounts:
            d_entry = store.entry(d.journal_entry_id)
            if d_entry is None or d_entry.status != EntryStatus.POSTED:
                allocations_ok = False
                add(
                    "discount_not_posted",
                    ERROR,
                    expected="posted discount entry",
                    actual=None if d_entry is None else d_entry.status.value,
                    remediation="the discount taken with this payment is not posted",
                    detail=f"discount {d.id}",
                )
        req["allocations_valid"] = allocations_ok
        result.evidence["allocations"] = {
            "count": len(allocations),
            "allocated": f"{allocated:.2f}",
            "unapplied": f"{unapplied:.2f}",
            "planned_pay": f"{planned_pay:.2f}",
            "discounts": [f"{d.amount:.2f}" for d in discounts],
        }
        if allocations and allocated == planned_pay and unapplied == 0:
            result.settlement_status = "allocated"
        elif allocations and allocated < planned_pay:
            result.settlement_status = "partial"
        elif allocations:
            result.settlement_status = "allocated" if unapplied == 0 else "partial"
        else:
            result.settlement_status = "unapplied" if unapplied > 0 else "none"
        if op.allocation_status == "committed" and state.cents(allocated) < state.cents(
            planned_pay
        ):
            add(
                "allocation_status_forged",
                ERROR,
                expected=f"allocated {planned_pay:.2f}",
                actual=f"{allocated:.2f}",
                remediation="the operation claims the plan was fully applied but the allocations say otherwise; investigate the flag",
            )
        elif op.allocation_status == "partial" and allocated == planned_pay and planned_pay > 0:
            add(
                "allocation_status_stale",
                WARNING,
                expected="committed",
                actual="partial",
                remediation="the plan was fully applied after all; refresh the status",
            )
        # status claims vs facts
        posted = entry is not None and entry.status == EntryStatus.POSTED
        if op.ledger_status == "posted" and not posted:
            add(
                "status_flag_forged",
                ERROR,
                expected="a posted entry",
                actual=op.ledger_status,
                remediation="ledger_status claims a posting that does not exist",
                detail="ledger_status",
            )
        recorded_ok = (
            req["ap_payment"]
            and req["payment_matches"]
            and posted
            and req["cash_movement"]
            and allocations_ok
        )
        if recorded_ok:
            result.accounting_status = "corrected" if is_correction else "recorded"
        else:
            result.accounting_status = "inconsistent"
    if payment is None and op.recording_status == "recorded":
        add(
            "status_flag_forged",
            ERROR,
            expected="a linked payment",
            actual="none",
            remediation="recording_status claims a payment that does not exist",
            detail="recording_status",
        )
    if payment is None and op.ledger_status == "posted":
        add(
            "status_flag_forged",
            ERROR,
            expected="a posted entry",
            actual="none",
            remediation="ledger_status claims a posting that does not exist",
            detail="ledger_status",
        )
    if op.accounting_status in ("recorded", "corrected") and result.accounting_status not in (
        "recorded",
        "corrected",
    ):
        add(
            "accounting_status_mismatch",
            ERROR,
            expected=result.accounting_status,
            actual=op.accounting_status,
            remediation="the row claims the transfer is in the books but the records disagree",
        )
    if op.accounting_status == "unverified":
        add(
            "accounting_unverified",
            ERROR,
            expected="verified completion evidence",
            actual="unverified (historical completion)",
            remediation="record the payment through the correction workflow and complete with evidence",
        )

    # --- 4. duplicates -------------------------------------------------------------------------------
    no_duplicates = True
    if op.gateway_confirmation_ref:
        own = {p.id for p in (payment, original) if p is not None}
        twins = [
            t
            for t in store.recorded_payments_with_reference(
                op.vendor_id, op.gateway_confirmation_ref
            )
            if t.id not in own
        ]
        for twin in twins:
            no_duplicates = False
            add(
                "duplicate_payment_for_transfer",
                CRITICAL,
                expected="one payment per gateway confirmation",
                actual=f"payment {twin.id} also carries {op.gateway_confirmation_ref}",
                remediation="the same transfer is recorded twice; reverse the duplicate",
            )
        other_ops = [
            o for o in store.operations_with_reference(op.gateway_confirmation_ref) if o.id != op.id
        ]
        for other in other_ops:
            no_duplicates = False
            add(
                "duplicate_confirmation_reference",
                CRITICAL,
                actual=other.operation_uid,
                remediation="two operations claim the same gateway confirmation; investigate",
            )
    keyed = store.payments_with_key(f"op:{op.operation_uid}")
    for twin in keyed:
        if twin.id not in {p.id for p in (payment, original) if p is not None}:
            no_duplicates = False
            add(
                "duplicate_payment_for_operation",
                CRITICAL,
                actual=f"payment {twin.id}",
                remediation="a payment recorded for this operation is not the linked one; investigate before any retry",
            )
    if payment is not None:
        for other in (o for o in store.operations_of_payment(payment.id) if o.id != op.id):
            no_duplicates = False
            add(
                "duplicate_operation_for_payment",
                CRITICAL,
                actual=other.operation_uid,
                remediation="two operations point at the same payment; investigate",
            )
    if op.run_id is not None:
        for other in (o for o in store.live_operations(op.run_id, op.vendor_id) if o.id != op.id):
            if money_moved:
                no_duplicates = False
                add(
                    "duplicate_live_operation",
                    CRITICAL,
                    actual=other.operation_uid,
                    remediation="two transfers moved money for the same run and supplier; reconcile with the bank",
                )
    req["no_duplicates"] = no_duplicates

    # --- 5. payment run ------------------------------------------------------------------------------
    run_consistent = True
    if op.run_id is not None:
        run = store.run(op.run_id)
        items = store.items_of(op.run_id, op.vendor_id)
        result.evidence["run"] = {
            "status": run.status.value if run else None,
            "items": {i.status.value: sum(1 for j in items if j.status == i.status) for i in items},
        }
        if op.state == OperationState.COMPLETED:
            for item in items:
                linked = op.payment_id if candidate_payment_id is None else None
                if item.status != RunItemStatus.PAID or (
                    linked is not None and item.payment_id != linked
                ):
                    # Stale run bookkeeping (e.g. the executor died after the commit): no money
                    # is at stake, recovery re-syncs it; reported, not blocking.
                    add(
                        "run_items_inconsistent",
                        WARNING,
                        expected=f"paid with payment {linked}" if linked else "paid",
                        actual=f"{item.status.value} with payment {item.payment_id}",
                        remediation="the run item does not reflect the completed operation; re-sync the run",
                        detail=f"item {item.id}",
                    )
        else:
            paid = [i for i in items if i.status == RunItemStatus.PAID]
            if paid:
                run_consistent = False
                add(
                    "run_items_paid_without_completion",
                    ERROR,
                    expected=op.state.value,
                    actual="paid",
                    remediation="run items claim payment for an operation that is not completed; investigate",
                )
            if run is not None and run.status == PaymentRunStatus.COMPLETED and money_moved:
                run_consistent = False
                add(
                    "run_completed_with_incomplete_operation",
                    CRITICAL,
                    expected="run not completed",
                    actual="completed",
                    remediation="the run was finalised while a transfer was still unrecorded; the run status is not trustworthy",
                )
    else:
        result.evidence["run"] = None
    req["run_consistent"] = run_consistent

    if op.state == OperationState.COMPLETED and not result.completion_policy_satisfied:
        add(
            "completed_without_evidence",
            CRITICAL,
            expected="all completion evidence",
            actual=", ".join(k for k, v in result.required_evidence.items() if not v) or "findings",
            remediation="this completion is not backed by records; treat as unresolved and remediate explicitly",
        )
    return result


# --- runs ---------------------------------------------------------------------------------------------

CATEGORY_RECORDED = "recorded"
CATEGORY_RECORDED_NO_CASH = "recorded_no_cash"
CATEGORY_CONFIRMED_UNRECORDED = "confirmed_unrecorded"
CATEGORY_REJECTED = "rejected"
CATEGORY_UNKNOWN = "unknown_outcome"
CATEGORY_PARTIAL = "partial_allocation"
CATEGORY_MANUAL = "manual_review"
CATEGORY_NOT_STARTED = "not_started"


@dataclass
class RunVerification:
    run_id: int
    status: str
    operations: list[OperationVerification]
    categories: dict[str, list[str]]  # category -> operation uids (or "item:<id>")
    findings: list[Finding]
    approved_total_cash: Decimal
    actual_total_cash: Decimal
    can_complete: bool

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "categories": self.categories,
            "approved_total_cash": self.approved_total_cash,
            "actual_total_cash": self.actual_total_cash,
            "can_complete": self.can_complete,
            "findings": [f.as_dict() for f in self.findings],
            "operations": [o.as_dict() for o in self.operations],
        }


def classify(verification: OperationVerification, op: PaymentOperation) -> str:
    if op.recovery_status == "manual" or op.investigation_status == "open":
        if op.state == OperationState.COMPLETED and verification.completion_policy_satisfied:
            return (
                CATEGORY_RECORDED
                if verification.settlement_status != "partial"
                else CATEGORY_PARTIAL
            )
        return CATEGORY_MANUAL
    if op.state == OperationState.COMPLETED:
        if not verification.completion_policy_satisfied:
            return CATEGORY_MANUAL
        return (
            CATEGORY_PARTIAL if verification.settlement_status == "partial" else CATEGORY_RECORDED
        )
    if op.state in MONEY_MOVED_STATES:
        return CATEGORY_CONFIRMED_UNRECORDED
    if op.state == OperationState.FAILED:
        if verification.delivery_status == "delivered":
            return CATEGORY_MANUAL  # failed but the gateway says accepted
        return CATEGORY_REJECTED
    if op.state == OperationState.PREPARED:
        return CATEGORY_NOT_STARTED
    return CATEGORY_UNKNOWN


def verify_run(db: Session, run: PaymentRun, *, gateway=None) -> RunVerification:
    """Every operation of the run verified, categorised, and the run-level completion decision."""
    ops = list(
        db.scalars(
            select(PaymentOperation)
            .where(PaymentOperation.run_id == run.id)
            .order_by(PaymentOperation.id)
        )
    )
    store = Store.prefetch(db, ops, gateway=gateway)
    verifications = [verify_operation(db, op, gateway=gateway, store=store) for op in ops]
    categories: dict[str, list[str]] = {}
    findings: list[Finding] = []
    live_by_vendor: dict[int, tuple[PaymentOperation, OperationVerification]] = {}
    for op, v in zip(ops, verifications):
        findings.extend(v.findings)
        category = classify(v, op)
        categories.setdefault(category, []).append(op.operation_uid)
        # the newest non-failed operation per vendor decides that vendor's outcome
        current = live_by_vendor.get(op.vendor_id)
        if (
            current is None
            or op.state != OperationState.FAILED
            or current[0].state == OperationState.FAILED
        ):
            live_by_vendor[op.vendor_id] = (op, v)
    items = [i for i in run.items if i.status != RunItemStatus.EXCLUDED]
    vendors_with_cash = {
        vendor_id
        for vendor_id in {i.vendor_id for i in items}
        if sum((i.pay_amount for i in items if i.vendor_id == vendor_id), ZERO) > 0
    }
    can_complete = True
    for vendor_id in {i.vendor_id for i in items}:
        vendor_items = [i for i in items if i.vendor_id == vendor_id]
        if vendor_id not in vendors_with_cash:
            if all(i.status == RunItemStatus.PAID for i in vendor_items):
                categories.setdefault(CATEGORY_RECORDED_NO_CASH, []).extend(
                    f"item:{i.id}" for i in vendor_items
                )
            else:
                can_complete = False
                categories.setdefault(CATEGORY_NOT_STARTED, []).extend(
                    f"item:{i.id}" for i in vendor_items if i.status != RunItemStatus.PAID
                )
            continue
        live = live_by_vendor.get(vendor_id)
        if live is None:
            can_complete = False
            categories.setdefault(CATEGORY_NOT_STARTED, []).extend(
                f"item:{i.id}" for i in vendor_items
            )
            continue
        op, v = live
        if not (op.state == OperationState.COMPLETED and v.completion_policy_satisfied):
            can_complete = False
    approved_cash = sum((i.pay_amount for i in items), ZERO)
    actual_cash = sum(
        (
            op.amount
            for op, v in live_by_vendor.values()
            if op.state == OperationState.COMPLETED and v.completion_policy_satisfied
        ),
        ZERO,
    )
    return RunVerification(
        run_id=run.id,
        status=run.status.value,
        operations=verifications,
        categories=categories,
        findings=findings,
        approved_total_cash=approved_cash,
        actual_total_cash=actual_cash,
        can_complete=can_complete,
    )


# --- scans ----------------------------------------------------------------------------------------------


def verify_operations(
    db: Session,
    *,
    gateway=None,
    states: tuple[OperationState, ...] | None = None,
    as_of: date | None = None,
    limit: int = 1000,
) -> list[OperationVerification]:
    """Verify many operations (default: every operation whose money moved, completed included)."""
    stmt = select(PaymentOperation).order_by(PaymentOperation.id).limit(limit)
    stmt = stmt.where(PaymentOperation.state.in_(states or MONEY_MOVED_STATES))
    if as_of is not None:
        stmt = stmt.where(PaymentOperation.payment_date <= as_of)
    ops = list(db.scalars(stmt))
    results = []
    for start in range(0, len(ops), 500):
        batch = ops[start : start + 500]
        store = Store.prefetch(db, batch, gateway=gateway)
        results.extend(verify_operation(db, op, gateway=gateway, store=store) for op in batch)
    return results


def unverified_completions(
    db: Session, *, gateway=None, as_of: date | None = None
) -> list[OperationVerification]:
    """Completed operations whose completion evidence does not hold (closing blocker / preflight)."""
    return [
        v
        for v in verify_operations(
            db, gateway=gateway, states=(OperationState.COMPLETED,), as_of=as_of
        )
        if not v.completion_policy_satisfied
    ]


def unrecorded_transfers(db: Session, *, as_of: date | None = None) -> list[PaymentOperation]:
    """Operations whose transfer was confirmed but which are not completed (outstanding obligations)."""
    stmt = (
        select(PaymentOperation)
        .where(
            PaymentOperation.state.in_(
                (
                    OperationState.CONFIRMED,
                    OperationState.RECORDING,
                    OperationState.RECONCILIATION_REQUIRED,
                )
            )
        )
        .order_by(PaymentOperation.id)
    )
    if as_of is not None:
        stmt = stmt.where(PaymentOperation.payment_date <= as_of)
    return list(db.scalars(stmt))


def count_by_state(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(PaymentOperation.state, func.count()).group_by(PaymentOperation.state)
    ).all()
    return {s.value if hasattr(s, "value") else str(s): n for s, n in rows}
