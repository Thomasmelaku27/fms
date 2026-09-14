"""Upgrading a populated Turn-4 database (``fmspayables05``) to ``fmspayables06`` (both backends).

The Turn-4 history is written at the ``fmspayables05`` schema through raw SQL on top of the
Turn-3 seed of ``test_payables_migration_v5``: gateway transfers, a payment run with three
vendors' operations -- one completed automatically with full evidence, one
``reconciliation_required`` (transfer confirmed, nothing recorded), one *completed by a
Turn-4 manual resolution with nothing but a note* -- their events, a failed close
attestation and a supplier statement import.

After the upgrade: evidence-backed completions are labelled ``recorded`` / ``corrected``,
the note-only completion is labelled ``unverified`` with an appended event (state, payment
link and actor decisions untouched), event hash chains and attestation hashes are
back-filled and verify, the period cannot be closed until the unverified completion is
remediated through the correction workflow, and the downgrade is restricted.
"""

import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.db import make_engine
from app.main import create_app
from tests.conftest import PROJECT_ROOT, alembic_config, migrate
from tests.test_payables_migration_v5 import _post_entry, _seed_turn3

D = Decimal
API = "/api/v1/payables"


def _seed_turn4(url: str, ids: dict) -> dict:
    """Turn-4 rows at the fmspayables05 schema (no accounting_status / hashes yet)."""
    engine = make_engine(url)
    uids = {k: str(uuid.uuid4()) for k in ("auto", "stuck", "note")}
    with engine.begin() as conn:
        # Two more suppliers with one approved bill each.
        for code, ref in (("AUTO", "IBAN-A"), ("STUCK", "IBAN-S"), ("NOTE", "IBAN-N")):
            conn.execute(text("INSERT INTO vendors (code, name, is_active, payable_account_id, payment_terms_days, status, bank_reference) "
                              "VALUES (:c, :c, true, :ap, 30, 'active', :r)"), {"c": code, "ap": ids["ap"], "r": ref})
        vendor_ids = dict(conn.execute(text("SELECT code, id FROM vendors WHERE code IN ('AUTO', 'STUCK', 'NOTE')")).all())
        bills = {}
        for code, amount in (("AUTO", 100), ("STUCK", 200), ("NOTE", 300)):
            entry = _post_entry(conn, "2026-02-01", f"bill {code}", [(ids["expense"], amount, 0), (ids["ap"], 0, amount)])
            conn.execute(text("INSERT INTO supplier_bills (vendor_id, invoice_number, invoice_number_normalized, invoice_date, due_date, total, status, "
                              "payable_account_id, discount_amount, updated_at) VALUES (:v, :n, :n, '2026-02-01', '2026-02-20', :t, 'draft', :ap, 0, CURRENT_TIMESTAMP)"),
                         {"v": vendor_ids[code], "n": f"{code}-1", "t": amount, "ap": ids["ap"]})
            bill_id = conn.execute(text("SELECT MAX(id) FROM supplier_bills")).scalar()
            conn.execute(text("INSERT INTO supplier_bill_lines (bill_id, line_no, description, account_id, quantity, unit_price, line_total) "
                              "VALUES (:b, 1, 'l', :a, 1, :t, :t)"), {"b": bill_id, "a": ids["expense"], "t": amount})
            conn.execute(text("UPDATE supplier_bills SET status = 'approved', journal_entry_id = :e WHERE id = :b"), {"e": entry, "b": bill_id})
            conn.execute(text("INSERT INTO bill_payment_schedules (bill_id, installment_no, due_date, amount) VALUES (:b, 1, '2026-02-20', :t)"),
                         {"b": bill_id, "t": amount})
            bills[code] = bill_id
        # The run (executed in Turn 4) with one item per vendor.
        conn.execute(text("INSERT INTO payment_runs (reference, status, pay_date, due_through, take_discounts, use_credits, use_advances, cash_account_id, method, "
                          "created_by, attempts, executed_by, updated_at, version, reviewed_by, approved_by, approved_fingerprint) "
                          "VALUES ('RUN-T4', 'draft', '2026-02-25', '2026-02-28', true, true, true, :c, 'bank_transfer', 'ops', 1, 'ops', CURRENT_TIMESTAMP, 3, 'rev', 'boss', 'abc')"),
                     {"c": ids["cash"]})
        run_id = conn.execute(text("SELECT MAX(id) FROM payment_runs")).scalar()
        for code, amount in (("AUTO", 100), ("STUCK", 200), ("NOTE", 300)):
            conn.execute(text("INSERT INTO payment_run_items (run_id, vendor_id, bill_id, due_date, outstanding_snapshot, settle_amount, discount_amount, credit_amount, "
                              "advance_amount, pay_amount, status, explanation, destination_reference) VALUES (:r, :v, :b, '2026-02-20', :t, :t, 0, 0, 0, :t, 'proposed', 'x', :d)"),
                         {"r": run_id, "v": vendor_ids[code], "b": bills[code], "t": amount, "d": f"IBAN-{code[0]}"})
        for step in ("reviewed", "approved", "executing"):
            conn.execute(text("UPDATE payment_runs SET status = :s WHERE id = :r"), {"s": step, "r": run_id})
        # Gateway: all three transfers accepted.
        for code, key in (("AUTO", "auto"), ("STUCK", "stuck"), ("NOTE", "note")):
            amount = {"AUTO": 100, "STUCK": 200, "NOTE": 300}[code]
            conn.execute(text("INSERT INTO simulated_gateway_transfers (token, fingerprint, vendor_code, destination_reference, amount, currency, pay_date, status, reference, lookups, confirmed_at) "
                              "VALUES (:t, :f, :v, :d, :a, 'XXX', '2026-02-25', 'accepted', :r, 0, CURRENT_TIMESTAMP)"),
                         {"t": uids[key], "f": "f" * 64, "v": code, "d": f"IBAN-{code[0]}", "a": amount, "r": f"SIM-{code}"})
        # AUTO: recorded and posted; NOTE: completed by a note only; STUCK: reconciliation_required.
        auto_entry = _post_entry(conn, "2026-02-25", "payment SIM-AUTO", [(ids["ap"], 100, 0), (ids["cash"], 0, 100)])
        conn.execute(text("INSERT INTO vendor_payments (vendor_id, payment_date, amount, method, reference, cash_account_id, journal_entry_id, recorded_by, status, "
                          "idempotency_key) VALUES (:v, '2026-02-25', 100, 'bank_transfer', 'SIM-AUTO', :cash, :e, 'ops', 'recorded', :k)"),
                     {"v": vendor_ids["AUTO"], "cash": ids["cash"], "e": auto_entry, "k": f"op:{uids['auto']}"})
        auto_payment = conn.execute(text("SELECT MAX(id) FROM vendor_payments")).scalar()
        conn.execute(text("INSERT INTO payment_allocations (payment_id, bill_id, amount, kind, allocation_date, allocated_by) VALUES (:p, :b, 100, 'original', '2026-02-25', 'ops')"),
                     {"p": auto_payment, "b": bills["AUTO"]})
        conn.execute(text("UPDATE supplier_bills SET status = 'paid' WHERE id = :b"), {"b": bills["AUTO"]})

        def op(key, code, amount, state, **extra):
            plan = json.dumps([{"bill_id": bills[code], "settle": f"{amount}.00", "discount": "0.00", "credit": "0.00", "advance": "0.00", "pay": f"{amount}.00"}])
            cols = dict(uid=uids[key], key=f"run:{run_id}:vendor:{vendor_ids[code]}:approval:abc:attempt:1", run=run_id, v=vendor_ids[code],
                        d=f"IBAN-{code[0]}", a=amount, plan=plan, ref=f"SIM-{code}", state="prepared", cash=ids["cash"])
            conn.execute(text("INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, run_id, run_version, vendor_id, destination_reference, amount, currency, "
                              "payment_date, method, cash_account_id, plan_json, state, version, attempts, max_attempts, created_by, correlation_id, recording_status, ledger_status, "
                              "allocation_status, recovery_status) VALUES (:uid, :key, 'f', :run, 3, :v, :d, :a, 'XXX', '2026-02-25', 'bank_transfer', :cash, :plan, :state, 0, 1, 5, 'ops', 'c', "
                              "'pending', 'pending', 'pending', 'none')"), cols)
            op_id = conn.execute(text("SELECT MAX(id) FROM payment_operations")).scalar()
            seq = 0
            events = [(None, "prepared", "transition", "intent prepared", "ops"), ("prepared", "submitted", "transition", "handing to the gateway", "ops"),
                      ("submitted", "confirmed", "gateway", f"gateway accepted, reference SIM-{code}", "ops")]
            for frm, to, cat, detail, actor in events:
                seq += 1
                conn.execute(text("INSERT INTO payment_operation_events (operation_id, seq, from_state, to_state, category, detail, actor) VALUES (:o, :s, :f, :t, :c, :d, :a)"),
                             {"o": op_id, "s": seq, "f": frm, "t": to, "c": cat, "d": detail, "a": actor})
            conn.execute(text("UPDATE payment_operations SET state = 'submitted' WHERE id = :id"), {"id": op_id})
            conn.execute(text("UPDATE payment_operations SET state = 'confirmed', gateway_status = 'accepted', gateway_confirmation_ref = :r WHERE id = :id"),
                         {"id": op_id, "r": f"SIM-{code}"})
            for to, cat, detail, actor, values in extra.get("steps", []):
                seq += 1
                conn.execute(text("INSERT INTO payment_operation_events (operation_id, seq, from_state, to_state, category, detail, actor) VALUES (:o, :s, :f, :t, :c, :d, :a)"),
                             {"o": op_id, "s": seq, "f": None, "t": to, "c": cat, "d": detail, "a": actor})
                conn.execute(text(f"UPDATE payment_operations SET state = :t{', ' + values if values else ''} WHERE id = :id"), {"t": to, "id": op_id, **extra.get("params", {})})
            return op_id

        ops = {}
        ops["auto"] = op("auto", "AUTO", 100, "completed", steps=[
            ("recording", "transition", "recording", "ops", None),
            ("completed", "transition", "AP payment, allocations and journal entries recorded", "ops",
             "payment_id = :p, recording_status = 'recorded', ledger_status = 'posted', allocation_status = 'committed'")],
            params={"p": auto_payment})
        ops["stuck"] = op("stuck", "STUCK", 200, "reconciliation_required", steps=[
            ("recording", "transition", "recording", "ops", None),
            ("reconciliation_required", "error", "recording failed (ledger): outage", "ops",
             "last_error_category = 'ledger', last_error = 'outage', recovery_status = 'manual', attempts = 5")])
        ops["note"] = op("note", "NOTE", 300, "completed", steps=[
            ("recording", "transition", "recording", "ops", None),
            ("reconciliation_required", "error", "recording failed (ledger): outage", "ops", "last_error_category = 'ledger', recovery_status = 'manual'"),
            ("completed", "recovery", "manually resolved: booked by hand (no reference)", "cfo", "recovery_status = 'resolved', recovery_note = 'booked by hand'")])
        conn.execute(text("UPDATE payment_run_items SET status = 'paid', payment_id = :p, execution_reference = 'SIM-AUTO' WHERE run_id = :r AND vendor_id = :v"),
                     {"p": auto_payment, "r": run_id, "v": vendor_ids["AUTO"]})
        conn.execute(text("UPDATE payment_run_items SET status = 'paid', execution_reference = 'SIM-NOTE' WHERE run_id = :r AND vendor_id = :v"),
                     {"r": run_id, "v": vendor_ids["NOTE"]})
        conn.execute(text("UPDATE payment_runs SET status = 'failed' WHERE id = :r"), {"r": run_id})
        # A failed close attempt for February and a statement import.
        conn.execute(text("INSERT INTO ap_close_attestations (period_end, outcome, attempted_by, forced, liability_balance, open_bill_count, vendor_count, checks_json, "
                          "exceptions_json, reconciliation_json, source_fingerprint) VALUES ('2026-02-28', 'failed', 'cfo', false, 700, 3, 3, '[]', '[]', '{}', 'fp')"))
        conn.execute(text("INSERT INTO supplier_statement_imports (vendor_id, file_name, content_hash, statement_date, imported_by, line_count, summary_json) "
                          "VALUES (:v, 'auto.csv', 'h', '2026-02-28', 'fin', 1, '{}')"), {"v": vendor_ids["AUTO"]})
        imp = conn.execute(text("SELECT MAX(id) FROM supplier_statement_imports")).scalar()
        conn.execute(text("INSERT INTO supplier_statement_lines (import_id, line_no, kind, reference, normalized_reference, line_date, amount, match_status, explanation) "
                          "VALUES (:i, 1, 'invoice', 'AUTO-1', 'AUTO-1', '2026-02-01', 100, 'matched', 'ok')"), {"i": imp})
    engine.dispose()
    return {"uids": uids, "ops": ops, "run": run_id, "vendors": vendor_ids, "bills": bills, "auto_payment": auto_payment}


def test_turn4_history_is_labelled_not_rewritten_and_remediated_explicitly(db_url):
    migrate(db_url, "fmspayables04")
    ids = _seed_turn3(db_url)
    migrate(db_url, "fmspayables05")
    t4 = _seed_turn4(db_url, ids)

    # --- the non-destructive preflight, before migrating, names the note-only completion
    preflight = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "ap_preflight.py"), "--url", db_url, "--json"],
                               capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120)
    assert preflight.returncode == 1, preflight.stderr
    report = json.loads(preflight.stdout)
    by_uid = {r["operation_uid"]: r for r in report["completed_operations"]}
    assert by_uid[t4["uids"]["auto"]]["evidence"] is True
    assert by_uid[t4["uids"]["note"]]["evidence"] is False and "no AP payment linked" in by_uid[t4["uids"]["note"]]["problems"]
    assert report["verifier"] is None  # not migrated yet: the verifier needs the new columns

    migrate(db_url)
    engine = make_engine(db_url)
    with engine.connect() as conn:
        rows = {r.operation_uid: r for r in conn.execute(text(
            "SELECT operation_uid, state, accounting_status, completion_kind, payment_id, recovery_status, recovery_note FROM payment_operations")).all()}
        auto, stuck, note = rows[t4["uids"]["auto"]], rows[t4["uids"]["stuck"]], rows[t4["uids"]["note"]]
        assert (auto.state, auto.accounting_status, auto.completion_kind) == ("completed", "recorded", "automatic")
        assert (stuck.state, stuck.accounting_status, stuck.completion_kind) == ("reconciliation_required", "unrecorded", None)
        # Labelled, not rewritten: still completed, still no payment, the actor's decision preserved, one event appended.
        assert (note.state, note.accounting_status, note.completion_kind) == ("completed", "unverified", "legacy_manual")
        assert note.payment_id is None and note.recovery_status == "resolved" and note.recovery_note == "booked by hand"
        events = conn.execute(text("SELECT seq, category, detail, actor, prev_hash, event_hash FROM payment_operation_events e JOIN payment_operations o ON o.id = e.operation_id "
                                   "WHERE o.operation_uid = :u ORDER BY seq"), {"u": t4["uids"]["note"]}).all()
        assert [e.category for e in events] == ["transition", "transition", "gateway", "transition", "error", "recovery", "verification"]
        assert events[-1].actor == "migration:fmspayables06" and "lacks accounting evidence" in events[-1].detail
        assert all(e.event_hash for e in events) and events[0].prev_hash is None and events[1].prev_hash == events[0].event_hash
        legacy = conn.execute(text("SELECT accounting_status, completion_kind FROM payment_operations WHERE fingerprint = 'backfill'")).first()
        assert legacy == ("recorded", "legacy_backfill")
        assert conn.execute(text("SELECT content_hash FROM ap_close_attestations")).scalar()
    engine.dispose()

    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as client:
        # The verifier agrees with the labels and explains the unverified one.
        note_status = client.get(f"{API}/payment-operations/{t4['uids']['note']}").json()
        assert note_status["state"] == "completed" and note_status["accounting_status"] == "inconsistent"
        assert note_status["recorded_accounting_status"] == "unverified" and note_status["completion_policy_satisfied"] is False
        codes = {f["code"] for f in note_status["verification"]["findings"]}
        assert {"payment_missing", "accounting_unverified", "completed_without_evidence"} <= codes
        assert note_status["checklist"]["reconciles"] is False
        auto_status = client.get(f"{API}/payment-operations/{t4['uids']['auto']}").json()
        assert auto_status["completion_policy_satisfied"] is True and auto_status["verification"]["evidence"]["events"]["hash_chain_verified"]
        # The historical (failed) attestation verifies as a record (hash) -- its numbers are informational.
        attestation_id = client.get(f"{API}/period-end/attestations").json()[0]["id"]
        close_check = client.get(f"{API}/period-end/attestations/{attestation_id}/verification").json()
        assert close_check["content_hash_verified"] is True
        # February cannot close: one unverified completion, one unrecorded transfer.
        validation = client.get(f"{API}/period-end/validate?period_end=2026-02-28").json()
        checks = {c["code"]: c["count"] for c in validation["checks"]}
        assert checks["unverified_completed_operations"] == 1 and checks["unrecorded_confirmed_transfers"] == 1
        # Both the unrecorded transfer and the note-only completion are obligations: cash out, nothing posted.
        assert client.get(f"{API}/reconciliation?as_of=2026-02-28").json()["pending_obligations_total"] == "500.00"
        # A note still does not remediate; the correction workflow does not apply to a completed row ...
        assert client.post(f"{API}/payment-operations/{t4['uids']['note']}/resolve", json={"actor": "cfo", "resolution": "again", "mark_completed": True}).status_code == 422
        assert client.post(f"{API}/payment-operations/{t4['uids']['note']}/corrections", json={"actor": "cfo", "payment_date": "2026-03-02", "reason": "x",
                                                                                               "allocations": [{"bill_id": t4["bills"]["NOTE"], "amount": "300.00"}]}).status_code == 409
        # ... the explicit remediation for STUCK works: correction workflow records, verifies, completes.
        fixed = client.post(f"{API}/payment-operations/{t4['uids']['stuck']}/corrections", json={
            "actor": "cfo", "payment_date": "2026-03-02", "reason": "ledger outage; booking the confirmed transfer",
            "allocations": [{"bill_id": t4["bills"]["STUCK"], "amount": "200.00"}], "idempotency_key": "stuck-fix"})
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["state"] == "completed" and fixed.json()["accounting_status"] == "corrected"
        march = client.get(f"{API}/reconciliation?as_of=2026-03-31").json()
        assert [o["operation_uid"] for o in march["pending_obligations"]] == [t4["uids"]["note"]]  # STUCK resolved; NOTE still not
        # As recorded at the February cutoff, STUCK was still an obligation: the March correction does not rewrite February.
        assert client.get(f"{API}/reconciliation?as_of=2026-02-28").json()["pending_obligations_total"] == "500.00"
        replay = client.get(f"{API}/reconstruction?as_of=2026-03-31").json()
        assert [(f["check"], f["transaction"]) for f in replay["findings"]] == [("operation_completion", f"operation:{t4['uids']['note']}")]
        # Everything else from Turn 3 / Turn 4 still reads the same.
        assert client.get(f"{API}/statement-imports").json()[0]["lines"][0]["match_status"] == "matched"
        assert client.get(f"{API}/payment-runs/{t4['run']}").json()["status"] == "failed"
        assert client.get(f"{API}/vendors/{ids['vendor']}/balance?as_of=2026-01-31").json()["bills_outstanding"] == "200.00"
    app.state.engine.dispose()

    with pytest.raises(RuntimeError, match="FMS_ALLOW_DESTRUCTIVE_DOWNGRADE"):
        command.downgrade(alembic_config(db_url), "fmspayables05")
    os.environ["FMS_ALLOW_DESTRUCTIVE_DOWNGRADE"] = "1"
    try:
        command.downgrade(alembic_config(db_url), "fmspayables05")
    finally:
        del os.environ["FMS_ALLOW_DESTRUCTIVE_DOWNGRADE"]
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM payment_operations")).scalar() == 4
        assert conn.execute(text("SELECT COUNT(*) FROM vendor_payments")).scalar() == 5
    engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM payment_operations WHERE accounting_status = 'unverified'")).scalar() == 1
    engine.dispose()
