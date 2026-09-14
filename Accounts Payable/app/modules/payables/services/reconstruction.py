"""Independent historical reconstruction of AP balances from durable events.

This module deliberately does **not** use the ORM balance properties or the
production report helpers. It reads the durable settlement rows with plain SQL,
replays them as of a date, and compares the result with what the production
reports say. Every difference is a structured finding (vendor, bill, transaction,
date, expected, actual, probable cause).

Two views:

* ``as_of`` (default): effective-dated -- a credit applied in January and unapplied
  in March is applied in the January view; a payment reversed in March is in force in
  the January view. A January report never changes because of a March event.
* ``restated``: the same date with all later corrections applied (reversed payments
  and unapplied applications treated as if they never happened) -- only on explicit
  request.

Covered events: approved bills, payments (partial / full / advance) and their
allocations and unallocations, credit notes and their applications / unapplications,
payment reversals and corrections, discounts and their reversals, closed periods
(the reconstruction also reports the attested liability at each close). Vendor
refunds / credit-balance settlements are not modelled by the module yet and are
therefore reported as a limitation, not silently ignored.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.payables.services import reconciliation as production
from app.modules.payables.services import settlement

ZERO = Decimal("0.00")


@dataclass
class Finding:
    check: str
    vendor_id: int | None
    bill_id: int | None
    transaction: str | None
    as_of: date
    expected: Decimal
    actual: Decimal
    cause: str

    @property
    def difference(self) -> Decimal:
        return self.actual - self.expected


@dataclass
class Reconstruction:
    as_of: date
    view: str
    bill_outstanding: dict[int, Decimal]
    vendor_bills: dict[int, Decimal]
    vendor_credits: dict[int, Decimal]
    vendor_advances: dict[int, Decimal]
    control_balance: dict[int, Decimal]
    ledger_balance: dict[int, Decimal]
    findings: list[Finding] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    # Payment operations positioned at the cutoff (fmspayables06): uid -> position dict.
    operation_positions: dict[str, dict] = field(default_factory=dict)
    obligations_total: Decimal = ZERO
    statement_imports: dict[str, int] = field(default_factory=dict)


def _dec(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else ZERO


def replay(db: Session, as_of: date, *, view: str = "as_of") -> Reconstruction:
    """Rebuild balances as of ``as_of`` from the durable rows with plain SQL."""
    if view not in ("as_of", "restated"):
        raise ValueError("view must be 'as_of' or 'restated'")
    d = as_of.isoformat()
    # Payment in force: dated on/before, and (as_of view) not reversed on/before; (restated) never reversed.
    pay_in_force = (
        "p.payment_date <= :d AND (p.status = 'recorded' OR p.reversal_date > :d)"
        if view == "as_of"
        else "p.payment_date <= :d AND p.status = 'recorded'"
    )
    alloc_in_force = (
        "a.allocation_date <= :d AND (a.unallocation_date IS NULL OR a.unallocation_date > :d)"
        if view == "as_of"
        else "a.allocation_date <= :d AND a.unallocation_date IS NULL"
    )
    app_in_force = (
        "ca.application_date <= :d AND (ca.unapplication_date IS NULL OR ca.unapplication_date > :d)"
        if view == "as_of"
        else "ca.application_date <= :d AND ca.unapplication_date IS NULL"
    )
    disc_in_force = (
        "bd.taken_on <= :d AND (bd.reversal_date IS NULL OR bd.reversal_date > :d)"
        if view == "as_of"
        else "bd.taken_on <= :d AND bd.reversal_date IS NULL"
    )
    credit_in_force = (
        "c.credit_date <= :d AND (c.status <> 'reversed' OR c.reversal_date > :d)"
        if view == "as_of"
        else "c.credit_date <= :d AND c.status <> 'reversed'"
    )

    bills = db.execute(
        text(
            "SELECT b.id, b.vendor_id, b.payable_account_id, b.total FROM supplier_bills b "
            "WHERE b.status IN ('approved', 'partially_paid', 'paid') AND b.invoice_date <= :d"
        ),
        {"d": d},
    ).all()
    paid = dict(
        db.execute(
            text(
                f"SELECT a.bill_id, SUM(a.amount) FROM payment_allocations a JOIN vendor_payments p ON p.id = a.payment_id "
                f"WHERE {pay_in_force} AND {alloc_in_force} GROUP BY a.bill_id"
            ),
            {"d": d},
        ).all()
    )
    credited = dict(
        db.execute(
            text(
                f"SELECT ca.bill_id, SUM(ca.amount) FROM credit_applications ca JOIN supplier_credits c ON c.id = ca.credit_id "
                f"WHERE {app_in_force} AND {credit_in_force} GROUP BY ca.bill_id"
            ),
            {"d": d},
        ).all()
    )
    discounted = dict(
        db.execute(
            text(
                f"SELECT bd.bill_id, SUM(bd.amount) FROM bill_discounts bd WHERE {disc_in_force} GROUP BY bd.bill_id"
            ),
            {"d": d},
        ).all()
    )

    bill_outstanding: dict[int, Decimal] = {}
    vendor_bills: dict[int, Decimal] = {}
    control: dict[int, Decimal] = {}
    for bill_id, vendor_id, account_id, total in bills:
        outstanding = (
            _dec(total)
            - _dec(paid.get(bill_id))
            - _dec(credited.get(bill_id))
            - _dec(discounted.get(bill_id))
        )
        bill_outstanding[bill_id] = outstanding
        vendor_bills[vendor_id] = vendor_bills.get(vendor_id, ZERO) + max(outstanding, ZERO)
        control[account_id] = (
            control.get(account_id, ZERO) + _dec(total) - _dec(discounted.get(bill_id))
        )

    # Credits: total in force less applications in force (remaining = unapplied credit value).
    credits = db.execute(
        text(
            f"SELECT c.id, c.vendor_id, c.payable_account_id, c.total FROM supplier_credits c "
            f"WHERE c.status IN ('approved', 'partially_applied', 'applied', 'reversed') AND {credit_in_force}"
        ),
        {"d": d},
    ).all()
    applied_by_credit = dict(
        db.execute(
            text(
                f"SELECT ca.credit_id, SUM(ca.amount) FROM credit_applications ca WHERE {app_in_force} GROUP BY ca.credit_id"
            ),
            {"d": d},
        ).all()
    )
    vendor_credits: dict[int, Decimal] = {}
    for credit_id, vendor_id, account_id, total in credits:
        remaining = _dec(total) - _dec(applied_by_credit.get(credit_id))
        vendor_credits[vendor_id] = vendor_credits.get(vendor_id, ZERO) + remaining
        control[account_id] = control.get(account_id, ZERO) - _dec(total)

    # Payments in force: cash leaves the control account(s); unapplied part is the vendor advance.
    payments = db.execute(
        text(
            f"SELECT p.id, p.vendor_id, p.amount, p.payable_account_id FROM vendor_payments p WHERE {pay_in_force}"
        ),
        {"d": d},
    ).all()
    allocated_by_payment = dict(
        db.execute(
            text(
                f"SELECT a.payment_id, SUM(a.amount) FROM payment_allocations a JOIN vendor_payments p ON p.id = a.payment_id "
                f"WHERE {pay_in_force} AND {alloc_in_force} GROUP BY a.payment_id"
            ),
            {"d": d},
        ).all()
    )
    original_by_payment_account = db.execute(
        text(
            f"SELECT a.payment_id, b.payable_account_id, SUM(a.amount) FROM payment_allocations a "
            f"JOIN vendor_payments p ON p.id = a.payment_id JOIN supplier_bills b ON b.id = a.bill_id "
            f"WHERE {pay_in_force} AND a.kind = 'original' GROUP BY a.payment_id, b.payable_account_id"
        ),
        {"d": d},
    ).all()
    vendor_advances: dict[int, Decimal] = {}
    original_totals: dict[int, Decimal] = {}
    for payment_id, account_id, amount in original_by_payment_account:
        control[account_id] = control.get(account_id, ZERO) - _dec(amount)
        original_totals[payment_id] = original_totals.get(payment_id, ZERO) + _dec(amount)
    for payment_id, vendor_id, amount, payable_account_id in payments:
        unapplied = _dec(amount) - _dec(allocated_by_payment.get(payment_id))
        vendor_advances[vendor_id] = vendor_advances.get(vendor_id, ZERO) + unapplied
        remainder = _dec(amount) - original_totals.get(payment_id, ZERO)
        if remainder > 0 and payable_account_id is not None:
            control[payable_account_id] = control.get(payable_account_id, ZERO) - remainder

    ledger = dict(
        db.execute(
            text(
                "SELECT l.account_id, SUM(l.credit - l.debit) FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
                "WHERE e.status = 'posted' AND e.entry_date <= :d AND l.account_id IN "
                "(SELECT DISTINCT payable_account_id FROM supplier_bills WHERE payable_account_id IS NOT NULL "
                " UNION SELECT DISTINCT payable_account_id FROM vendors WHERE payable_account_id IS NOT NULL) GROUP BY l.account_id"
            ),
            {"d": d},
        ).all()
    )
    positions, obligations_total = _operation_positions(db, as_of, view=view)
    statement_counts = dict(
        db.execute(
            text(
                "SELECT l.match_status, COUNT(*) FROM supplier_statement_lines l JOIN supplier_statement_imports i ON i.id = l.import_id "
                "WHERE i.statement_date <= :d GROUP BY l.match_status"
            ),
            {"d": d},
        ).all()
    )
    recon = Reconstruction(
        as_of=as_of,
        view=view,
        operation_positions=positions,
        obligations_total=obligations_total,
        statement_imports={str(k): int(v) for k, v in statement_counts.items()},
        bill_outstanding=bill_outstanding,
        vendor_bills=vendor_bills,
        vendor_credits=vendor_credits,
        vendor_advances=vendor_advances,
        control_balance={k: v for k, v in control.items() if k is not None},
        ledger_balance={k: _dec(v) for k, v in ledger.items()},
        limitations=[
            "vendor refunds / credit-balance settlements are not modelled by the module and cannot be replayed"
        ],
    )
    if view == "restated":
        recon.limitations.append(
            "restated view ignores effective dates of later corrections by request"
        )
    return recon


def _operation_positions(db: Session, as_of: date, *, view: str) -> tuple[dict[str, dict], Decimal]:
    """Payment operations as recorded at the cutoff.

    A transfer the gateway accepted with a pay date on or before the cutoff whose AP payment
    was not in force on that date (recorded later -- e.g. a correction dated in the next
    period -- or never) was an obligation then, whatever its state is today.
    """
    d = as_of.isoformat()
    positions: dict[str, dict] = {}
    total = ZERO
    replaced = {
        r[0]
        for r in db.execute(
            text(
                "SELECT DISTINCT corrects_payment_id FROM vendor_payments WHERE corrects_payment_id IS NOT NULL "
                "AND status = 'recorded' AND payment_date <= :d"
            ),
            {"d": d},
        ).all()
    }
    for row in db.execute(
        text(
            "SELECT o.operation_uid, o.vendor_id, o.amount, o.state, o.gateway_status, o.payment_id, "
            "p.payment_date AS recorded_on, p.status AS payment_status, p.reversal_date "
            "FROM payment_operations o LEFT JOIN vendor_payments p ON p.id = o.payment_id WHERE o.payment_date <= :d ORDER BY o.id"
        ),
        {"d": d},
    ).mappings():
        # "Delivered" is what AP durably knows: the gateway said accepted (gateway_status is
        # only ever set from a gateway answer) or the state is past confirmation. The gateway's
        # own record is the verifier's business (services.verification), not the replay's.
        delivered = row["gateway_status"] == "accepted" or row["state"] in (
            "confirmed",
            "recording",
            "completed",
            "reconciliation_required",
        )
        recorded_on = row["recorded_on"]
        if recorded_on is not None and not isinstance(recorded_on, date):
            recorded_on = date.fromisoformat(str(recorded_on)[:10])
        reversal = row["reversal_date"]
        if reversal is not None and not isinstance(reversal, date):
            reversal = date.fromisoformat(str(reversal)[:10])
        in_force = (
            recorded_on is not None
            and recorded_on <= as_of
            and (
                row["payment_status"] == "recorded"
                or (view == "as_of" and reversal is not None and reversal > as_of)
                or row["payment_id"] in replaced
            )
        )
        obligation = delivered and not in_force
        positions[row["operation_uid"]] = {
            "vendor_id": row["vendor_id"],
            "amount": _dec(row["amount"]),
            "current_state": row["state"],
            "delivered_by_cutoff": delivered,
            "recorded_by_cutoff": in_force,
            "obligation_at_cutoff": obligation,
        }
        if obligation:
            total += _dec(row["amount"])
    return positions, total


def compare(db: Session, as_of: date, *, view: str = "as_of") -> Reconstruction:
    """Replay independently, then compare with the production reports (as-of view only)."""
    recon = replay(db, as_of, view=view)
    if view != "as_of":
        return recon  # the production reports are effective-dated by design; nothing to compare against
    findings = recon.findings

    outstanding = production.outstanding_report(db, as_of=as_of)
    reported = {r.bill_id: r.outstanding for r in outstanding.rows}
    for bill_id, expected in recon.bill_outstanding.items():
        actual = reported.get(bill_id, ZERO)
        if max(expected, ZERO) != actual:
            findings.append(
                Finding(
                    "outstanding_report",
                    None,
                    bill_id,
                    f"bill:{bill_id}",
                    as_of,
                    max(expected, ZERO),
                    actual,
                    "outstanding report disagrees with the replay of allocations / applications / discounts",
                )
            )
    for bill_id, actual in reported.items():
        if bill_id not in recon.bill_outstanding:
            findings.append(
                Finding(
                    "outstanding_report",
                    None,
                    bill_id,
                    f"bill:{bill_id}",
                    as_of,
                    ZERO,
                    actual,
                    "reported as open but not an approved bill dated on or before the cutoff",
                )
            )
    aging = production.aging_report(db, as_of=as_of)
    if aging.totals.total != sum((max(v, ZERO) for v in recon.bill_outstanding.values()), ZERO):
        findings.append(
            Finding(
                "aging_report",
                None,
                None,
                None,
                as_of,
                sum((max(v, ZERO) for v in recon.bill_outstanding.values()), ZERO),
                aging.totals.total,
                "aging total differs from the replayed open balances",
            )
        )
    # One pass over the subledger event stream gives every vendor's statement closing balance.
    closing: dict[int, Decimal] = {}
    for event in production.vendor_events(db):
        if event["date"] <= as_of:
            closing[event["vendor_id"]] = closing.get(event["vendor_id"], ZERO) + event["amount"]
    for balance in settlement.vendor_balances(db, as_of=as_of):
        for name, mine, theirs in (
            (
                "bills_outstanding",
                recon.vendor_bills.get(balance.vendor_id, ZERO),
                balance.bills_outstanding,
            ),
            (
                "unapplied_credits",
                recon.vendor_credits.get(balance.vendor_id, ZERO),
                balance.unapplied_credits,
            ),
            (
                "unapplied_advances",
                recon.vendor_advances.get(balance.vendor_id, ZERO),
                balance.unapplied_advances,
            ),
        ):
            if mine != theirs:
                findings.append(
                    Finding(
                        f"vendor_balance.{name}",
                        balance.vendor_id,
                        None,
                        None,
                        as_of,
                        mine,
                        theirs,
                        f"vendor balance {name} differs from the replay",
                    )
                )
        expected_closing = (
            recon.vendor_bills.get(balance.vendor_id, ZERO)
            - recon.vendor_credits.get(balance.vendor_id, ZERO)
            - recon.vendor_advances.get(balance.vendor_id, ZERO)
        )
        if closing.get(balance.vendor_id, ZERO) != expected_closing:
            findings.append(
                Finding(
                    "supplier_statement",
                    balance.vendor_id,
                    None,
                    None,
                    as_of,
                    expected_closing,
                    closing.get(balance.vendor_id, ZERO),
                    "statement closing balance differs from replayed bills - credits - advances",
                )
            )
    gl = production.reconciliation_report(db, as_of=as_of)
    for row in gl.rows:
        mine = recon.control_balance.get(row.account_id, ZERO)
        if mine != row.subledger_balance:
            findings.append(
                Finding(
                    "gl_reconciliation.subledger",
                    None,
                    None,
                    f"account:{row.account_code}",
                    as_of,
                    mine,
                    row.subledger_balance,
                    "production subledger balance differs from the replay",
                )
            )
        if recon.ledger_balance.get(row.account_id, ZERO) != row.ledger_balance:
            findings.append(
                Finding(
                    "gl_reconciliation.ledger",
                    None,
                    None,
                    f"account:{row.account_code}",
                    as_of,
                    recon.ledger_balance.get(row.account_id, ZERO),
                    row.ledger_balance,
                    "ledger balance read differently",
                )
            )
        if mine != recon.ledger_balance.get(row.account_id, ZERO):
            findings.append(
                Finding(
                    "gl_control",
                    None,
                    None,
                    f"account:{row.account_code}",
                    as_of,
                    mine,
                    recon.ledger_balance.get(row.account_id, ZERO),
                    "replayed AP control balance differs from the posted ledger (manual entries or unposted documents)",
                )
            )
    # Payment operations: the production reconciliation's obligations must equal the replayed positions.
    reported_obligations = {o.operation_uid: o.amount for o in gl.pending_obligations}
    replayed_obligations = {
        uid: p["amount"]
        for uid, p in recon.operation_positions.items()
        if p["obligation_at_cutoff"]
    }
    for uid in sorted(set(reported_obligations) | set(replayed_obligations)):
        if reported_obligations.get(uid, ZERO) != replayed_obligations.get(uid, ZERO):
            findings.append(
                Finding(
                    "pending_obligations",
                    recon.operation_positions.get(uid, {}).get("vendor_id"),
                    None,
                    f"operation:{uid}",
                    as_of,
                    replayed_obligations.get(uid, ZERO),
                    reported_obligations.get(uid, ZERO),
                    "the reconciliation report's unrecorded transfers differ from the replay of payment operations at the cutoff",
                )
            )
    # Completed operations must be backed by a payment (at the cutoff or, if corrected, later).
    for uid, position in recon.operation_positions.items():
        if (
            position["current_state"] == "completed"
            and position["delivered_by_cutoff"]
            and not position["recorded_by_cutoff"]
        ):
            later = db.execute(
                text(
                    "SELECT p.payment_date FROM payment_operations o JOIN vendor_payments p ON p.id = o.payment_id WHERE o.operation_uid = :u"
                ),
                {"u": uid},
            ).scalar()
            if later is None:
                findings.append(
                    Finding(
                        "operation_completion",
                        position["vendor_id"],
                        None,
                        f"operation:{uid}",
                        as_of,
                        position["amount"],
                        ZERO,
                        "operation is completed but no vendor payment backs it (completion without evidence)",
                    )
                )
    # Statement imports: matched lines must still point at a document that exists (one query per table).
    tables = {
        "invoice": "supplier_bills",
        "bill": "supplier_bills",
        "credit": "supplier_credits",
        "payment": "vendor_payments",
    }
    matched_lines = db.execute(
        text(
            "SELECT l.id, l.matched_type, l.matched_id FROM supplier_statement_lines l JOIN supplier_statement_imports i ON i.id = l.import_id "
            "WHERE i.statement_date <= :d AND l.matched_id IS NOT NULL"
        ),
        {"d": as_of.isoformat()},
    ).all()
    existing: dict[str, set[int]] = {}
    for table in set(tables.values()):
        existing[table] = (
            {r[0] for r in db.execute(text(f"SELECT id FROM {table}")).all()}
            if matched_lines
            else set()
        )
    for line_id, matched_type, matched_id in matched_lines:
        table = tables.get(matched_type)
        if table and matched_id not in existing[table]:
            findings.append(
                Finding(
                    "statement_match_target",
                    None,
                    None,
                    f"statement_line:{line_id}",
                    as_of,
                    ZERO,
                    ZERO,
                    f"matched {matched_type} {matched_id} no longer exists",
                )
            )
    # Closed periods: the attested liability and obligations must equal the replay at that cutoff.
    import json as _json

    for period_end, liability, obligations_json in db.execute(
        text(
            "SELECT period_end, liability_balance, obligations_json FROM ap_close_attestations WHERE outcome IN ('closed', 'forced') AND period_end <= :d"
        ),
        {"d": as_of.isoformat()},
    ).all():
        cutoff = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end))
        at_cutoff = replay(db, cutoff)
        if obligations_json is not None:
            attested = sum((_dec(o["amount"]) for o in _json.loads(obligations_json)), ZERO)
            if attested != at_cutoff.obligations_total:
                findings.append(
                    Finding(
                        "closed_period_obligations",
                        None,
                        None,
                        f"attestation:{cutoff}",
                        cutoff,
                        attested,
                        at_cutoff.obligations_total,
                        "obligations attested at close differ from the replay of payment operations at that cutoff",
                    )
                )
        replayed = sum((max(v, ZERO) for v in at_cutoff.bill_outstanding.values()), ZERO)
        if _dec(liability) != replayed:
            findings.append(
                Finding(
                    "closed_period_attestation",
                    None,
                    None,
                    f"attestation:{cutoff}",
                    cutoff,
                    _dec(liability),
                    replayed,
                    "attested liability at close differs from the current replay of that cutoff (backdated change after close?)",
                )
            )
    return recon


def run_settlement_check(db: Session, run_id: int) -> list[Finding]:
    """Paid run items must be backed by allocations of a recorded payment for the planned cash."""
    findings = []
    rows = db.execute(
        text(
            "SELECT i.bill_id, i.vendor_id, i.pay_amount, i.payment_id, r.pay_date, "
            "(SELECT COALESCE(SUM(a.amount), 0) FROM payment_allocations a WHERE a.payment_id = i.payment_id AND a.bill_id = i.bill_id "
            " AND a.unallocation_date IS NULL) AS allocated "
            "FROM payment_run_items i JOIN payment_runs r ON r.id = i.run_id WHERE i.run_id = :r AND i.status = 'paid'"
        ),
        {"r": run_id},
    ).all()
    for bill_id, vendor_id, pay_amount, payment_id, pay_date, allocated in rows:
        if _dec(pay_amount) > 0 and _dec(allocated) != _dec(pay_amount):
            findings.append(
                Finding(
                    "payment_run_settlement",
                    vendor_id,
                    bill_id,
                    f"payment:{payment_id}",
                    pay_date,
                    _dec(pay_amount),
                    _dec(allocated),
                    "run item marked paid but the payment's allocation to the bill differs from the planned cash",
                )
            )
    return findings
