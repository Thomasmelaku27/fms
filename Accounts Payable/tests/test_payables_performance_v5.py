"""Turn-5 benchmarks at scale: operations, evidence, verification, recovery, finalisation, closing.

Data: ``tests/payables_support/bulkdata.py`` (thousands of suppliers, bills, payments,
allocations, payment operations with gateway transfers and hash-chained events, statement
lines, close attestations). Results go to ``build/perf/<backend>-v5.json`` with, for every
measurement, wall time (integer nanoseconds, reported as exact Decimal seconds), the number
of SQL statements issued, and -- for the key queries -- the database's query plan. A
regression section compares with the previous v5 file and with the Turn-4 numbers where
the measurement existed. No float arithmetic is used anywhere in the report.

``FMS_PERF_VENDORS`` (default 2000), ``FMS_PERF_BILLS`` (default 5),
``FMS_PERF_RUN_VENDORS`` (default 150, suppliers paid in the benchmark run),
``FMS_PERF_WORKERS`` (default 4, concurrent recovery workers; PostgreSQL only).
Run with ``-m perf``.
"""

import json
import os
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.modules.payables import services
from app.modules.payables.integration import Integrations
from app.modules.payables.models import PaymentMethod, PaymentRun
from app.modules.payables.schemas import PaymentRunCreateIn
from app.modules.payables.services import execution_state as state
from app.modules.payables.services import orchestration, recovery, verification
from app.modules.payables.services.gateway import DurableSimulatedGateway
from tests.payables_support import bulkdata

pytestmark = pytest.mark.perf

VENDORS = int(os.environ.get("FMS_PERF_VENDORS", "2000"))
BILLS = int(os.environ.get("FMS_PERF_BILLS", "5"))
RUN_VENDORS = int(os.environ.get("FMS_PERF_RUN_VENDORS", "150"))
WORKERS = int(os.environ.get("FMS_PERF_WORKERS", "4"))
RESULTS_DIR = Path(__file__).resolve().parents[1] / "build" / "perf"
NS = Decimal(1_000_000_000)


class Meter:
    """Wall time in integer nanoseconds and SQL statement counts, per measurement."""

    def __init__(self, engine):
        self.engine = engine
        self.statements = 0
        self.results: dict[str, dict] = {}
        event.listen(engine, "before_cursor_execute", self._count)

    def _count(self, *args, **kwargs):
        self.statements += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self._count)

    def timed(self, name, fn):
        before = self.statements
        started = time.perf_counter_ns()
        value = fn()
        elapsed = time.perf_counter_ns() - started
        self.results[name] = {
            "seconds": str((Decimal(elapsed) / NS).quantize(Decimal("0.000001"))),
            "nanoseconds": elapsed,
            "statements": self.statements - before,
        }
        return value


def _plan(session, sql, params):
    dialect = session.get_bind().dialect.name
    prefix = "EXPLAIN (FORMAT TEXT) " if dialect == "postgresql" else "EXPLAIN QUERY PLAN "
    rows = session.execute(text(prefix + sql), params).all()
    return [" ".join(str(c) for c in row) for row in rows]


def _confirmed_operations(session, world, vendor_ids, gateway):
    """Prepare and submit one operation per vendor (the gateway accepts); stop before recording."""
    uids = []
    for vendor_id in vendor_ids:
        bill = session.execute(text("SELECT id FROM supplier_bills WHERE vendor_id = :v AND status IN ('approved', 'partially_paid') ORDER BY id LIMIT 1"), {"v": vendor_id}).first()
        if bill is None:
            continue
        amount = Decimal("5.00")  # every open bulk bill has at least this much outstanding
        op, _ = state.prepare(session, idempotency_key=f"perf:{vendor_id}", vendor_id=vendor_id, destination_reference=f"S{vendor_id}", amount=amount,
                              currency="XXX", method=PaymentMethod.BANK_TRANSFER, payment_date=date(2026, 4, 30), cash_account_id=world["accounts"]["cash"], run_id=None,
                              run_version=None, plan=[{"bill_id": bill.id, "settle": amount, "pay": amount}], actor="perf")
        session.commit()
        orchestration.submit(session, op, integrations=Integrations(gateway=gateway), actor="perf")
        uids.append(op.operation_uid)
    return uids


def test_turn5_benchmarks_at_scale(session, engine):
    meter = Meter(engine)
    results = {"backend": engine.dialect.name, "vendors": VENDORS, "bills_per_vendor": BILLS, "run_vendors": RUN_VENDORS, "workers": WORKERS}
    try:
        world = meter.timed("generate", lambda: bulkdata.generate(session, vendors=VENDORS, bills_per_vendor=BILLS))
        results["counts"] = world["counts"]
        session.expire_all()
        gateway = DurableSimulatedGateway()
        as_of = date(2026, 4, 30)
        vendor_id = world["vendor_ids"][0]

        scan = meter.timed("recovery_scan", lambda: recovery.scan(session, limit=5000))
        results["incomplete_operations"] = len(scan)
        verified = meter.timed("verify_all_money_moved_operations", lambda: verification.verify_operations(session, gateway=gateway, limit=100000))
        results["verified_operations"] = len(verified)
        results["verified_with_blocking_findings"] = sum(1 for v in verified if v.blocking_findings)
        uid = session.execute(text("SELECT operation_uid FROM payment_operations WHERE state = 'completed' LIMIT 1")).scalar()
        meter.timed("payment_status_lookup_with_verification", lambda: services.diagnostics.operation_status(session, uid, gateway=gateway))
        meter.timed("operation_listing_page", lambda: services.diagnostics.list_operations(session, page=3, page_size=50))
        meter.timed("unverified_completions_scan", lambda: verification.unverified_completions(session, gateway=gateway, as_of=as_of))

        # Payment run: proposal for a subset of suppliers, approval, execution through the simulated
        # gateway (one transfer per supplier), finalisation with verification.
        run_vendors = world["vendor_ids"][:RUN_VENDORS]
        run = meter.timed("payment_run_proposal", lambda: services.create_run(
            session, PaymentRunCreateIn(actor="ops", pay_date=as_of, due_through=as_of, cash_account_id=world["accounts"]["cash"], vendor_ids=run_vendors,
                                        use_credits=False, use_advances=False),
            integrations=Integrations()))
        results["proposal_items"] = len(run.items)
        services.review_run(session, run.id, actor="rev", integrations=Integrations())
        services.approve_run(session, run.id, actor="boss", integrations=Integrations())
        run, outcomes = meter.timed("payment_run_execution", lambda: services.execute_run(session, run.id, actor="ops", integrations=Integrations(gateway=gateway)))
        results["run_status"] = run.status.value
        results["run_outcomes"] = {s: sum(1 for o in outcomes if o["state"] == s) for s in {o["state"] for o in outcomes}}
        run_check = meter.timed("payment_run_verification", lambda: verification.verify_run(session, session.get(PaymentRun, run.id), gateway=gateway))
        results["run_categories"] = {k: len(v) for k, v in run_check.categories.items()}
        with Session(engine) as s:
            r = s.get(PaymentRun, run.id)
            if r.status.value == "completed":
                services.payment_runs._cas(s, r, r.status, status=r.status)  # no-op version bump keeps the CAS honest
            s.commit()

        # Concurrent recording: N confirmed transfers, W recovery workers at once (PostgreSQL).
        concurrent_vendors = world["vendor_ids"][RUN_VENDORS:RUN_VENDORS + 200]
        uids = meter.timed("prepare_and_submit_200_operations", lambda: _confirmed_operations(session, world, concurrent_vendors, gateway))
        results["concurrent_operations"] = len(uids)
        if engine.dialect.name == "postgresql" and WORKERS > 1:
            def worker(name):
                with Session(engine, expire_on_commit=False) as s:
                    for _ in range(20):
                        if not recovery.process(s, integrations=Integrations(gateway=DurableSimulatedGateway()), worker_id=name, actor=name, limit=50):
                            break

            def run_workers():
                threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(WORKERS)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=600)

            meter.timed("concurrent_recording_by_workers", run_workers)
        else:
            meter.timed("sequential_recording_by_one_worker", lambda: [recovery.process(session, integrations=Integrations(gateway=gateway), worker_id="w", actor="w", limit=50) for _ in range(10)])
        with Session(engine) as s:
            completed = s.execute(text("SELECT COUNT(*) FROM payment_operations WHERE idempotency_key LIKE 'perf:%' AND state = 'completed'")).scalar()
            payments = s.execute(text("SELECT COUNT(*) FROM vendor_payments WHERE idempotency_key LIKE 'op:%'")).scalar()
        results["concurrent_completed"] = completed
        results["operation_payments"] = payments
        assert completed == len(uids), "every confirmed transfer must be recorded exactly once"

        csv = "kind,reference,date,amount\n" + "\n".join(
            f"invoice,{r[0]},{r[1]},{Decimal(str(r[2])):.2f}" for r in session.execute(text(
                "SELECT invoice_number, invoice_date, total FROM supplier_bills WHERE vendor_id = :v"), {"v": vendor_id}).all())
        meter.timed("supplier_statement_matching", lambda: services.supplier_statements.preview(session, vendor_id, csv.encode(), file_name="p.csv", statement_date=as_of))
        meter.timed("ap_to_gl_reconciliation", lambda: services.reconciliation_report(session, as_of=as_of))
        validation = meter.timed("period_end_validation", lambda: services.validate_period(session, as_of, gateway=gateway))
        results["validation_status"] = validation.status
        replay = meter.timed("historical_reconstruction_replay", lambda: services.reconstruction.replay(session, as_of))
        results["replayed_operation_positions"] = len(replay.operation_positions)
        compare = meter.timed("historical_reconstruction_compare", lambda: services.reconstruction.compare(session, as_of))
        results["reconstruction_findings"] = [f.check for f in compare.findings][:20]
        attestation_id = session.execute(text("SELECT MAX(id) FROM ap_close_attestations")).scalar()
        meter.timed("close_package_verification", lambda: services.closing.verify_close(session, attestation_id, gateway=gateway))

        results["measurements"] = meter.results
        results["query_plans"] = {
            "unrecorded_transfers": _plan(session, "SELECT id FROM payment_operations WHERE state IN ('confirmed', 'recording', 'reconciliation_required') AND payment_date <= :d ORDER BY id", {"d": as_of}),
            "operation_events_by_operation": _plan(session, "SELECT seq, event_hash FROM payment_operation_events WHERE operation_id = :o ORDER BY seq", {"o": 1}),
            "payments_by_vendor_and_reference": _plan(session, "SELECT id FROM vendor_payments WHERE vendor_id = :v AND reference = :r AND status = 'recorded'", {"v": vendor_id, "r": "x"}),
            "journal_lines_by_entry": _plan(session, "SELECT account_id, debit, credit FROM journal_lines WHERE entry_id = :e", {"e": 1}),
            "operation_positions_join": _plan(session, "SELECT o.operation_uid FROM payment_operations o LEFT JOIN vendor_payments p ON p.id = o.payment_id WHERE o.payment_date <= :d ORDER BY o.id", {"d": as_of}),
            "gateway_transfer_by_token": _plan(session, "SELECT status FROM simulated_gateway_transfers WHERE token = :t", {"t": uid}),
        }
    finally:
        meter.close()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{engine.dialect.name}-v5.json"
    previous = json.loads(path.read_text()) if path.exists() else None
    turn4 = RESULTS_DIR / f"{engine.dialect.name}-v4.json"
    baseline = json.loads(turn4.read_text()) if turn4.exists() else {}
    regressions = {}
    for name, m in results["measurements"].items():
        entry = {}
        if previous and name in previous.get("measurements", {}):
            old = Decimal(previous["measurements"][name]["seconds"])
            entry["previous_seconds"] = str(old)
            entry["ratio_vs_previous"] = str((Decimal(m["seconds"]) / old).quantize(Decimal("0.01"))) if old else None
        turn4_name = {"payment_run_proposal": "payment_run_proposal", "ap_to_gl_reconciliation": "ap_to_gl_reconciliation", "period_end_validation": "period_end_validation",
                      "historical_reconstruction_replay": "historical_reconstruction_replay", "historical_reconstruction_compare": "historical_reconstruction_compare",
                      "supplier_statement_matching": "supplier_statement_reconciliation", "recovery_scan": "recovery_scan", "operation_listing_page": "operation_listing_page"}.get(name)
        if turn4_name and turn4_name in baseline:
            entry["turn4_seconds"] = str(Decimal(str(baseline[turn4_name])).quantize(Decimal("0.001")))
        if entry:
            regressions[name] = entry
    results["regressions"] = regressions
    path.write_text(json.dumps(results, indent=2, default=str))
    # Loose ceilings: these are measurements, not SLOs; a 10x blow-up is a regression worth failing on.
    seconds = {k: Decimal(v["seconds"]) for k, v in results["measurements"].items()}
    assert seconds["recovery_scan"] < Decimal("5")
    assert seconds["payment_status_lookup_with_verification"] < Decimal("2")
    assert seconds["verify_all_money_moved_operations"] < Decimal("600")
    assert completed == len(uids)
