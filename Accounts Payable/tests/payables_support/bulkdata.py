"""Bulk, consistent AP data for benchmarks (direct inserts; posted journal entries included).

Per vendor: ``bills_per_vendor`` approved bills (one with an early-payment discount
snapshot), a partial payment, a full payment, an advance with one application and one
unallocated application, one credit applied then unapplied on every third vendor, a hold
on every tenth vendor, and a completed payment operation for every executed run item.
Amounts are Decimals with two places; every bill / payment / credit has a posted entry
that moves the AP control account by exactly its amount, so the data reconciles.
"""

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

D = Decimal


def _exec(session: Session, sql: str, rows: list[dict]) -> None:
    if not rows:
        return
    if session.get_bind().dialect.name == "sqlite":
        # pysqlite cannot bind Decimal / date directly in raw SQL.
        rows = [
            {
                k: (
                    str(v)
                    if isinstance(v, Decimal)
                    else v.isoformat()
                    if isinstance(v, date)
                    else v
                )
                for k, v in row.items()
            }
            for row in rows
        ]
    session.execute(text(sql), rows)


def generate(
    session: Session,
    *,
    vendors: int,
    bills_per_vendor: int,
    start: date = date(2025, 7, 1),
    seed_accounts=None,
) -> dict:
    """Return ids and counts. Uses the session's connection; commits at the end."""
    if seed_accounts is None:
        session.execute(
            text(
                "INSERT INTO accounts (code, name, type, is_active) VALUES ('2000', 'AP', 'liability', true), "
                "('6000', 'Expense', 'expense', true), ('1000', 'Cash', 'asset', true), ('4900', 'Disc', 'revenue', true)"
            )
        )
        acct = dict(session.execute(text("SELECT code, id FROM accounts")).all())
        ap, expense, cash, disc = acct["2000"], acct["6000"], acct["1000"], acct["4900"]
    else:
        ap, expense, cash, disc = seed_accounts
    dialect = session.get_bind().dialect.name

    def next_id(table):
        return (
            session.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")).scalar() or 0
        ) + 1

    # Vendors
    vendor_rows = [
        {"code": f"S{i:05d}", "name": f"Supplier {i}", "ap": ap, "status": "active"}
        for i in range(vendors)
    ]
    _exec(
        session,
        "INSERT INTO vendors (code, name, is_active, payable_account_id, payment_terms_days, status, bank_reference) "
        "VALUES (:code, :name, true, :ap, 30, :status, :code)",
        vendor_rows,
    )
    vendor_ids = [r[0] for r in session.execute(text("SELECT id FROM vendors ORDER BY id")).all()]

    entry_id = next_id("journal_entries")
    entries, lines, bills, bill_lines, schedules = [], [], [], [], []
    payments, allocations, credits, credit_lines, applications, holds = [], [], [], [], [], []
    bill_id = next_id("supplier_bills")
    payment_id = next_id("vendor_payments")
    credit_id = next_id("supplier_credits")

    def entry(d, desc, debit_acc, credit_acc, amount):
        nonlocal entry_id
        eid = entry_id
        entry_id += 1
        entries.append({"id": eid, "d": d, "desc": desc})
        lines.append({"e": eid, "a": debit_acc, "dr": amount, "cr": D("0")})
        lines.append({"e": eid, "a": credit_acc, "dr": D("0"), "cr": amount})
        return eid

    counts = {
        "bills": 0,
        "payments": 0,
        "credits": 0,
        "applications": 0,
        "discounts": 0,
        "holds": 0,
    }
    for v_index, vendor in enumerate(vendor_ids):
        vendor_bills = []
        for n in range(bills_per_vendor):
            inv = start + timedelta(days=(v_index * 7 + n * 11) % 240)
            total = D(1000 + (v_index * 37 + n * 53) % 90000) / 100
            e = entry(inv, f"bill S{v_index}-{n}", expense, ap, total)
            discount = (total * D("0.02")).quantize(D("0.01")) if n == 0 else D("0")
            bills.append(
                {
                    "id": bill_id,
                    "v": vendor,
                    "n": f"S{v_index:05d}-{n:03d}",
                    "inv": inv,
                    "due": inv + timedelta(days=30),
                    "t": total,
                    "status": "approved",
                    "ap": ap,
                    "e": e,
                    "disc": discount,
                    "dd": inv + timedelta(days=10) if n == 0 else None,
                    "da": disc if n == 0 else None,
                }
            )
            bill_lines.append({"b": bill_id, "a": expense, "t": total})
            schedules.append({"b": bill_id, "due": inv + timedelta(days=30), "t": total})
            vendor_bills.append((bill_id, total, inv, discount))
            bill_id += 1
            counts["bills"] += 1
        # Payments: partial on bill 0 (with discount? no: partial), full on bill 1, advance 300 with applications to bill 2.
        b0, t0, inv0, _ = vendor_bills[0]
        half = (t0 / 2).quantize(D("0.01"))
        pay_day = inv0 + timedelta(days=5)
        e = entry(pay_day, "partial", ap, cash, half)
        payments.append(
            {
                "id": payment_id,
                "v": vendor,
                "d": pay_day,
                "a": half,
                "ref": f"P{payment_id}",
                "cash": cash,
                "e": e,
                "pa": None,
            }
        )
        allocations.append(
            {"p": payment_id, "b": b0, "a": half, "k": "original", "d": pay_day, "ud": None}
        )
        bills[-bills_per_vendor]["status"] = "partially_paid"
        payment_id += 1
        counts["payments"] += 1
        if bills_per_vendor > 1:
            b1, t1, inv1, _ = vendor_bills[1]
            pay_day = inv1 + timedelta(days=20)
            e = entry(pay_day, "full", ap, cash, t1)
            payments.append(
                {
                    "id": payment_id,
                    "v": vendor,
                    "d": pay_day,
                    "a": t1,
                    "ref": f"P{payment_id}",
                    "cash": cash,
                    "e": e,
                    "pa": None,
                }
            )
            allocations.append(
                {"p": payment_id, "b": b1, "a": t1, "k": "original", "d": pay_day, "ud": None}
            )
            bills[-bills_per_vendor + 1]["status"] = "paid"
            payment_id += 1
            counts["payments"] += 1
        if bills_per_vendor > 2:
            b2, t2, inv2, _ = vendor_bills[2]
            adv_day = inv2 + timedelta(days=1)
            e = entry(adv_day, "advance", ap, cash, D("300.00"))
            payments.append(
                {
                    "id": payment_id,
                    "v": vendor,
                    "d": adv_day,
                    "a": D("300.00"),
                    "ref": f"ADV{payment_id}",
                    "cash": cash,
                    "e": e,
                    "pa": ap,
                }
            )
            applied = min(D("200.00"), t2)
            allocations.append(
                {
                    "p": payment_id,
                    "b": b2,
                    "a": applied,
                    "k": "advance",
                    "d": adv_day + timedelta(days=3),
                    "ud": None,
                }
            )
            if t2 - applied >= D("50.00"):
                allocations.append(
                    {
                        "p": payment_id,
                        "b": b2,
                        "a": D("50.00"),
                        "k": "advance",
                        "d": adv_day + timedelta(days=4),
                        "ud": adv_day + timedelta(days=20),
                    }
                )
            bills[-bills_per_vendor + 2]["status"] = "partially_paid" if applied < t2 else "paid"
            payment_id += 1
            counts["payments"] += 1
        if v_index % 3 == 0 and bills_per_vendor > 3:
            b3, t3, inv3, _ = vendor_bills[3]
            cd = inv3 + timedelta(days=2)
            amount = min(D("40.00"), t3)
            e = entry(cd, "credit", ap, expense, amount)
            credits.append(
                {
                    "id": credit_id,
                    "v": vendor,
                    "n": f"CN{credit_id}",
                    "d": cd,
                    "t": amount,
                    "ap": ap,
                    "e": e,
                    "status": "approved",
                }
            )
            credit_lines.append({"c": credit_id, "a": expense, "t": amount})
            applications.append(
                {
                    "c": credit_id,
                    "b": b3,
                    "a": amount,
                    "d": cd + timedelta(days=1),
                    "ud": cd + timedelta(days=30),
                }
            )
            credit_id += 1
            counts["credits"] += 1
            counts["applications"] += 1
        if v_index % 10 == 0 and bills_per_vendor > 4:
            holds.append({"b": vendor_bills[4][0]})
            counts["holds"] += 1

    _exec(
        session,
        "INSERT INTO journal_entries (id, entry_date, description, reference, status) VALUES (:id, :d, :desc, NULL, 'draft')",
        entries,
    )
    _exec(
        session,
        "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (:e, :a, :dr, :cr)",
        lines,
    )
    session.execute(text("UPDATE journal_entries SET status = 'posted' WHERE status = 'draft'"))
    _exec(
        session,
        "INSERT INTO supplier_bills (id, vendor_id, invoice_number, invoice_number_normalized, invoice_date, due_date, total, status, payable_account_id, "
        "journal_entry_id, discount_amount, discount_date, discount_account_id, approved_by, updated_at) "
        "VALUES (:id, :v, :n, :n, :inv, :due, :t, 'draft', :ap, NULL, :disc, :dd, :da, 'bulk', CURRENT_TIMESTAMP)",
        bills,
    )
    _exec(
        session,
        "INSERT INTO supplier_bill_lines (bill_id, line_no, description, account_id, quantity, unit_price, line_total) VALUES (:b, 1, 'l', :a, 1, :t, :t)",
        bill_lines,
    )
    # approve: set entry + status through the allowed transitions
    _exec(
        session,
        "UPDATE supplier_bills SET status = 'approved', journal_entry_id = :e WHERE id = :id",
        [{"id": b["id"], "e": b["e"]} for b in bills],
    )
    _exec(
        session,
        "INSERT INTO bill_payment_schedules (bill_id, installment_no, due_date, amount) VALUES (:b, 1, :due, :t)",
        schedules,
    )
    # Every second cash payment without an advance remainder is the recording of a completed
    # payment operation: it carries the operation's idempotency key (``op:<uid>``).
    linked = [p for p in payments if p["ref"].startswith("P") and p["pa"] is None][::2]
    for p in payments:
        p["uid"] = str(uuid.uuid4())
        p["key"] = None
    for p in linked:
        p["key"] = f"op:{p['uid']}"
    _exec(
        session,
        "INSERT INTO vendor_payments (id, vendor_id, payment_date, amount, method, reference, cash_account_id, journal_entry_id, recorded_by, status, payable_account_id, idempotency_key) "
        "VALUES (:id, :v, :d, :a, 'bank_transfer', :ref, :cash, :e, 'bulk', 'recorded', :pa, :key)",
        payments,
    )
    _exec(
        session,
        "INSERT INTO payment_allocations (payment_id, bill_id, amount, kind, allocation_date, allocated_by, unallocation_date, unallocated_by) "
        "VALUES (:p, :b, :a, :k, :d, 'bulk', :ud, :ub)",
        [{**a, "ub": None if a["ud"] is None else "bulk"} for a in allocations],
    )
    _exec(
        session,
        "INSERT INTO supplier_credits (id, vendor_id, kind, credit_number, credit_number_normalized, credit_date, total, status, payable_account_id, journal_entry_id, "
        "approved_by, created_at, updated_at) VALUES (:id, :v, 'credit_note', :n, :n, :d, :t, 'draft', :ap, NULL, 'bulk', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        credits,
    )
    _exec(
        session,
        "INSERT INTO supplier_credit_lines (credit_id, line_no, description, account_id, quantity, unit_price, line_total) VALUES (:c, 1, 'l', :a, 1, :t, :t)",
        credit_lines,
    )
    _exec(
        session,
        "UPDATE supplier_credits SET status = 'approved', journal_entry_id = :e WHERE id = :id",
        [{"id": c["id"], "e": c["e"]} for c in credits],
    )
    _exec(
        session,
        "INSERT INTO credit_applications (credit_id, bill_id, amount, application_date, applied_by, unapplication_date, unapplied_by) "
        "VALUES (:c, :b, :a, :d, 'bulk', :ud, 'bulk')",
        applications,
    )
    _exec(
        session,
        "INSERT INTO bill_holds (bill_id, kind, reason, placed_by) VALUES (:b, 'hold', 'bulk hold', 'bulk')",
        holds,
    )
    # Bill statuses follow the settlements.
    _exec(
        session,
        "UPDATE supplier_bills SET status = :status WHERE id = :id AND :status <> 'approved'",
        [{"id": b["id"], "status": b["status"]} for b in bills],
    )
    # Completed payment operations (back-fill style) for the full payments.
    vendor_index = {v: i for i, v in enumerate(vendor_ids)}
    ops = [
        {
            "uid": p["uid"],
            "key": f"bulk:{p['id']}",
            "v": p["v"],
            "a": p["a"],
            "d": p["d"],
            "cash": cash,
            "plan": json.dumps([]),
            "ref": p["ref"],
            "p": p["id"],
            "corr": f"bulk-{p['id']}",
        }
        for p in linked
    ]
    _exec(
        session,
        "INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, vendor_id, amount, currency, payment_date, method, cash_account_id, "
        "plan_json, state, version, attempts, max_attempts, gateway_submission_ref, gateway_confirmation_ref, gateway_status, payment_id, recording_status, "
        "ledger_status, allocation_status, recovery_status, created_by, correlation_id, accounting_status, completion_kind, destination_reference) "
        "VALUES (:uid, :key, 'bulk', :v, :a, 'XXX', :d, 'bank_transfer', :cash, "
        ":plan, 'completed', 0, 1, 5, :ref, :ref, 'accepted', :p, 'recorded', 'posted', 'committed', 'none', 'bulk', :corr, 'recorded', 'automatic', :dest)",
        [{**o, "dest": f"S{vendor_index[o['v']]:05d}"} for o in ops],
    )
    # A few incomplete operations for the recovery scan.
    stuck = [
        {
            "uid": str(uuid.uuid4()),
            "key": f"stuck:{i}",
            "v": vendor_ids[i],
            "cash": cash,
            "corr": f"stuck-{i}",
            "state": s,
        }
        for i, s in enumerate(
            ["prepared", "submitted", "confirmation_pending", "reconciliation_required"]
            * max(1, vendors // 400)
        )
    ]
    _exec(
        session,
        "INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, vendor_id, amount, currency, payment_date, method, cash_account_id, "
        "plan_json, state, version, attempts, max_attempts, recording_status, ledger_status, allocation_status, recovery_status, created_by, correlation_id, destination_reference) "
        "VALUES (:uid, :key, 'bulk', :v, 10, 'XXX', '2026-03-01', 'bank_transfer', :cash, '[]', 'prepared', 0, 1, 5, 'pending', 'pending', 'pending', 'none', 'bulk', :corr, :dest)",
        [{**r, "dest": f"S{vendor_index[r['v']]:05d}"} for r in stuck],
    )
    for row in stuck:
        path = {
            "prepared": (),
            "submitted": ("submitted",),
            "confirmation_pending": ("submitted", "confirmation_pending"),
            "reconciliation_required": ("submitted", "confirmed", "reconciliation_required"),
        }[row["state"]]
        for step in path:
            if step == "confirmed":
                session.execute(
                    text(
                        "UPDATE payment_operations SET state = 'confirmed', gateway_status = 'accepted', "
                        "gateway_confirmation_ref = :r, accounting_status = 'unrecorded' WHERE operation_uid = :u"
                    ),
                    {"r": f"SIM-{row['uid'][:8]}", "u": row["uid"]},
                )
            else:
                session.execute(
                    text("UPDATE payment_operations SET state = :s WHERE operation_uid = :u"),
                    {"s": step, "u": row["uid"]},
                )
    session.commit()
    # Gateway transfers (durable evidence) for every confirmed / completed operation, event chains,
    # statement imports and close attestations (Turn-5 verification / reconstruction benchmarks).
    _turn5_evidence(session, ops, stuck, vendor_ids, bills, ap, cash)
    if dialect == "postgresql":
        # Rows were inserted with explicit ids: move the sequences past them.
        for table in (
            "journal_entries",
            "supplier_bills",
            "supplier_credits",
            "vendor_payments",
            "supplier_statement_imports",
        ):
            session.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1))"
                )
            )
        session.commit()
    counts.update(
        vendors=vendors,
        operations=len(ops) + len(stuck),
        gateway_transfers=session.execute(
            text("SELECT COUNT(*) FROM simulated_gateway_transfers")
        ).scalar(),
        operation_events=session.execute(
            text("SELECT COUNT(*) FROM payment_operation_events")
        ).scalar(),
        statement_lines=session.execute(
            text("SELECT COUNT(*) FROM supplier_statement_lines")
        ).scalar(),
        close_attestations=session.execute(
            text("SELECT COUNT(*) FROM ap_close_attestations")
        ).scalar(),
        journal_entries=len(entries),
        dialect=dialect,
    )
    return {
        "accounts": {"ap": ap, "expense": expense, "cash": cash, "disc": disc},
        "vendor_ids": vendor_ids,
        "counts": counts,
    }


def _turn5_evidence(session, ops, stuck, vendor_ids, bills, ap, cash):
    import hashlib
    import importlib.util
    from pathlib import Path

    from app.modules.payables.services.execution_state import event_hash

    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "20261201_fmspayables06_completion_evidence.py"
    )
    spec = importlib.util.spec_from_file_location(migration.stem, migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    index = {v: i for i, v in enumerate(vendor_ids)}
    transfers = [
        {"t": o["uid"], "v": f"S{index[o['v']]:05d}", "a": o["a"], "d": o["d"], "r": o["ref"]}
        for o in ops
    ] + [
        {
            "t": r["uid"],
            "v": f"S{index[r['v']]:05d}",
            "a": 10,
            "d": date(2026, 3, 1),
            "r": f"SIM-{r['uid'][:8]}",
        }
        for r in stuck
        if r["state"] == "reconciliation_required"
    ]
    _exec(
        session,
        "INSERT INTO simulated_gateway_transfers (token, fingerprint, vendor_code, destination_reference, amount, currency, pay_date, status, reference, lookups, confirmed_at) "
        "VALUES (:t, 'bulk', :v, :v, :a, 'XXX', :d, 'accepted', :r, 0, CURRENT_TIMESTAMP)",
        transfers,
    )
    op_ids = dict(session.execute(text("SELECT operation_uid, id FROM payment_operations")).all())
    events = []
    for o in ops + stuck:
        uid = o["uid"]
        if o in ops:
            steps = [
                (None, "prepared", "transition", "intent prepared"),
                ("prepared", "submitted", "transition", "handing to the gateway"),
                ("submitted", "confirmed", "gateway", f"gateway accepted, reference {o['ref']}"),
                ("confirmed", "recording", "transition", "recording"),
                (
                    "recording",
                    "completed",
                    "transition",
                    "AP payment, allocations and journal entries recorded",
                ),
            ]
        else:
            steps = [(None, "prepared", "transition", "intent prepared")]
            chain = {
                "prepared": [],
                "submitted": [("prepared", "submitted", "transition", "handing")],
                "confirmation_pending": [
                    ("prepared", "submitted", "transition", "handing"),
                    ("submitted", "confirmation_pending", "gateway", "pending"),
                ],
                "reconciliation_required": [
                    ("prepared", "submitted", "transition", "handing"),
                    ("submitted", "confirmed", "gateway", "gateway accepted"),
                    ("confirmed", "recording", "transition", "recording"),
                    (
                        "recording",
                        "reconciliation_required",
                        "error",
                        "recording failed (ledger): outage",
                    ),
                ],
            }
            steps += chain[o["state"]]
        prev = None
        for seq, (frm, to, cat, detail) in enumerate(steps, start=1):
            h = event_hash(prev, uid, seq, frm, to, cat, detail, "bulk")
            events.append(
                {
                    "o": op_ids[uid],
                    "s": seq,
                    "f": frm,
                    "t": to,
                    "c": cat,
                    "d": detail,
                    "p": prev,
                    "h": h,
                }
            )
            prev = h
    _exec(
        session,
        "INSERT INTO payment_operation_events (operation_id, seq, from_state, to_state, category, detail, actor, prev_hash, event_hash) "
        "VALUES (:o, :s, :f, :t, :c, :d, 'bulk', :p, :h)",
        events,
    )
    # Statement imports: one per ten vendors, five invoice lines each (matched).
    by_vendor: dict[int, list] = {}
    for b in bills:
        by_vendor.setdefault(b["v"], []).append(b)
    imports, lines = [], []
    import_id = (
        session.execute(
            text("SELECT COALESCE(MAX(id), 0) FROM supplier_statement_imports")
        ).scalar()
        or 0
    ) + 1
    for n, vendor_id in enumerate(vendor_ids[::10]):
        vendor_bills = by_vendor.get(vendor_id, [])[:5]
        if not vendor_bills:
            continue
        imports.append(
            {
                "id": import_id,
                "v": vendor_id,
                "h": hashlib.sha256(f"bulk-{vendor_id}".encode()).hexdigest(),
                "n": len(vendor_bills),
            }
        )
        for line_no, b in enumerate(vendor_bills, start=1):
            lines.append(
                {
                    "i": import_id,
                    "n": line_no,
                    "r": b["n"],
                    "d": b["inv"],
                    "a": b["t"],
                    "m": b["id"],
                }
            )
        import_id += 1
    _exec(
        session,
        "INSERT INTO supplier_statement_imports (id, vendor_id, file_name, content_hash, statement_date, imported_by, line_count, summary_json) "
        "VALUES (:id, :v, 'bulk.csv', :h, '2026-03-31', 'bulk', :n, '{}')",
        imports,
    )
    _exec(
        session,
        "INSERT INTO supplier_statement_lines (import_id, line_no, kind, reference, normalized_reference, line_date, amount, match_status, matched_type, matched_id, "
        "internal_amount, internal_date, explanation) VALUES (:i, :n, 'invoice', :r, :r, :d, :a, 'matched', 'bill', :m, :a, :d, 'bulk')",
        lines,
    )
    # Close attestations for the three months before the data's end, hashed like the service does.
    from app.modules.payables.services import reconstruction

    session.flush()
    for period_end in (date(2025, 9, 30), date(2025, 10, 31), date(2025, 11, 30)):
        replay = reconstruction.replay(session, period_end)
        liability = sum((max(v, D("0")) for v in replay.bill_outstanding.values()), D("0.00"))
        row = {
            "period_end": period_end,
            "outcome": "closed",
            "attempted_by": "bulk",
            "forced": False,
            "force_reason": None,
            "liability_balance": liability,
            "open_bill_count": sum(1 for v in replay.bill_outstanding.values() if v > 0),
            "vendor_count": len(replay.vendor_bills),
            "checks_json": "[]",
            "exceptions_json": "[]",
            "reconciliation_json": "{}",
            "source_fingerprint": "bulk",
            "gl_period_reference": None,
            "lock_id": None,
            "obligations_json": "[]",
        }
        session.execute(
            text(
                "INSERT INTO ap_close_attestations (period_end, outcome, attempted_by, forced, liability_balance, open_bill_count, vendor_count, checks_json, "
                "exceptions_json, reconciliation_json, source_fingerprint, obligations_json, content_hash) VALUES (:period_end, :outcome, :attempted_by, :forced, "
                ":liability_balance, :open_bill_count, :vendor_count, :checks_json, :exceptions_json, :reconciliation_json, :source_fingerprint, :obligations_json, :h)"
            ),
            {
                **row,
                "liability_balance": f"{liability:.2f}"
                if session.get_bind().dialect.name == "sqlite"
                else liability,
                "h": module.attestation_content_hash(row),
            },
        )
    session.commit()
