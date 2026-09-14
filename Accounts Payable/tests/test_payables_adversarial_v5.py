"""Adversarial direct-SQL changes against payment operations (SQLite and PostgreSQL).

Every attack is either refused by the database rules of ``fmspayables06`` or -- where a
database-only rule is impractical -- detected by the independent verifier, after which
the financially authoritative operations (period close, run finalisation, recovery
recording, manual completion) refuse to proceed.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.services.gateway import DurableSimulatedGateway

API = "/api/v1/payables"
D = Decimal


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class BetaOutage(JournalLedgerAdapter):
    def post(self, db, entry):
        if entry.reference.startswith("AP-PAY-") and "Beta" in entry.description:
            raise LedgerPostingRefused("ledger unavailable")
        return super().post(db, entry)


@pytest.fixture
def world(client):
    """ALPHA: completed with evidence. BETA: transfer confirmed, recording failed (reconciliation_required)."""
    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}
    vendors, bills = {}, {}
    for code, total in (("ALPHA", "100.00"), ("BETA", "200.00")):
        v = client.post(f"{API}/vendors", json={"code": code, "name": code.title(), "payable_account_id": accounts["ap"], "bank_reference": f"IBAN-{code}"}).json()
        vendors[code] = v
        b = client.post(f"{API}/bills", json={"vendor_id": v["id"], "invoice_number": f"{code}-1", "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                              "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]}).json()
        assert client.post(f"{API}/bills/{b['id']}/approve", json={"actor": "alice"}).status_code == 200
        bills[code] = b
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=DurableSimulatedGateway(), ledger=BetaOutage())
    run = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-10", "cash_account_id": accounts["cash"]}).json()
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 200
    result = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
    outcomes = {o["vendor_code"]: o for o in result["outcomes"]}
    assert outcomes["ALPHA"]["state"] == "completed" and outcomes["BETA"]["state"] == "reconciliation_required"
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=DurableSimulatedGateway())
    with Session(client.app.state.engine) as s:
        ids = {r.operation_uid: r.id for r in s.execute(text("SELECT id, operation_uid FROM payment_operations")).all()}
    return {"accounts": accounts, "vendors": vendors, "bills": bills, "run": run,
            "alpha": outcomes["ALPHA"]["operation_uid"], "beta": outcomes["BETA"]["operation_uid"],
            "alpha_id": ids[outcomes["ALPHA"]["operation_uid"]], "beta_id": ids[outcomes["BETA"]["operation_uid"]],
            "alpha_payment": outcomes["ALPHA"]["payment_id"]}


def _sql(client, sql, **params):
    with Session(client.app.state.engine) as s:
        result = s.execute(text(sql), params)
        s.commit()
        return result


def _refused(client, sql, fragment, **params):
    with pytest.raises(IntegrityError, match=fragment):
        _sql(client, sql, **params)


def _verify(client, uid):
    return client.get(f"{API}/payment-operations/{uid}/verification").json()


def _codes(verification):
    return {f["code"]: f["severity"] for f in verification["findings"]}


def _validate(client, period_end="2026-02-28"):
    return {c["code"]: c["count"] for c in client.get(f"{API}/period-end/validate?period_end={period_end}").json()["checks"]}


# --- refused by the database ---------------------------------------------------------------------------


def test_marking_an_unrecorded_operation_completed_is_refused(client, world):
    _refused(client, "UPDATE payment_operations SET state = 'completed' WHERE id = :id", "recorded vendor payment", id=world["beta_id"])
    _refused(client, "UPDATE payment_operations SET state = 'completed', accounting_status = 'recorded' WHERE id = :id",
             "recorded vendor payment", id=world["beta_id"])
    # Even with a real payment linked, a state that skips the machine is refused.
    _refused(client, "UPDATE payment_operations SET state = 'completed', payment_id = :p, accounting_status = 'recorded' WHERE id = :id",
             "invalid payment operation state transition|linked vendor payment|recorded vendor payment", id=world["beta_id"], p=world["alpha_payment"])
    assert _verify(client, world["beta"])["state"] == "reconciliation_required"


def test_failed_cannot_hide_an_accepted_transfer(client, world):
    _refused(client, "UPDATE payment_operations SET state = 'failed' WHERE id = :id", "invalid payment operation state transition", id=world["beta_id"])
    # Through a state from which 'failed' is legal (submitted): the gateway acceptance still forbids it.
    _refused(client, "UPDATE payment_operations SET state = 'failed', gateway_status = 'accepted' WHERE id = :id",
             "invalid payment operation state transition", id=world["beta_id"])
    with Session(client.app.state.engine) as s:
        s.execute(text("INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, vendor_id, amount, currency, payment_date, method, cash_account_id, "
                       "plan_json, state, version, attempts, max_attempts, created_by, correlation_id, gateway_status, gateway_confirmation_ref) "
                       "VALUES ('11111111-1111-1111-1111-111111111111', 'k-forged', 'f', :v, 5, 'XXX', '2026-02-10', 'bank_transfer', :c, '[]', 'submitted', 0, 1, 5, 't', 'c', 'accepted', 'REF-X')"),
                  {"v": world["vendors"]["ALPHA"]["id"], "c": world["accounts"]["cash"]})
        s.commit()
    _refused(client, "UPDATE payment_operations SET state = 'failed' WHERE operation_uid = '11111111-1111-1111-1111-111111111111'",
             "accepted by the gateway cannot be marked failed")


def test_replacing_or_associating_payments_is_refused(client, world):
    other = client.post(f"{API}/payments", json={"vendor_id": world["vendors"]["ALPHA"]["id"], "payment_date": "2026-02-11", "amount": "100.00",
                                                  "cash_account_id": world["accounts"]["cash"], "recorded_by": "x", "reference": "MANUAL"}).json()
    # completed: payment link is set once and frozen
    _refused(client, "UPDATE payment_operations SET payment_id = :p WHERE id = :id", "identity and instruction fields|immutable", id=world["alpha_id"], p=other["id"])
    _refused(client, "UPDATE payment_operations SET payment_id = NULL WHERE id = :id", "identity and instruction fields|immutable", id=world["alpha_id"])
    # reconciliation_required: an unrelated supplier's payment cannot be linked
    _refused(client, "UPDATE payment_operations SET payment_id = :p WHERE id = :id", "linked vendor payment must belong", id=world["beta_id"], p=other["id"])
    _refused(client, "UPDATE payment_operations SET payment_id = :p WHERE id = :id", "linked vendor payment must belong|uq_payment_operations_payment_id|UNIQUE",
             id=world["beta_id"], p=world["alpha_payment"])


def test_reusing_gateway_confirmation_references_is_refused(client, world):
    ref = client.get(f"{API}/payment-operations/{world['alpha']}").json()["gateway_confirmation_ref"]
    _refused(client, "UPDATE payment_operations SET gateway_confirmation_ref = :r WHERE id = :id", "identity and instruction fields", id=world["beta_id"], r=ref)
    with pytest.raises(IntegrityError, match="uq_payment_operations_gateway_confirmation_ref|UNIQUE"):
        _sql(client, "INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, vendor_id, amount, currency, payment_date, method, cash_account_id, "
                     "plan_json, state, version, attempts, max_attempts, created_by, correlation_id, gateway_confirmation_ref) "
                     "VALUES ('22222222-2222-2222-2222-222222222222', 'k-dup', 'f', :v, 5, 'XXX', '2026-02-10', 'bank_transfer', :c, '[]', 'prepared', 0, 0, 5, 't', 'c', :r)",
             v=world["vendors"]["ALPHA"]["id"], c=world["accounts"]["cash"], r=ref)


@pytest.mark.parametrize("sql", [
    "UPDATE payment_operations SET amount = amount + 1 WHERE id = :id",
    "UPDATE payment_operations SET vendor_id = :other WHERE id = :id",
    "UPDATE payment_operations SET cash_account_id = cash_account_id + 1 WHERE id = :id",
    "UPDATE payment_operations SET recording_status = 'pending' WHERE id = :id",
    "UPDATE payment_operations SET ledger_status = 'pending' WHERE id = :id",
    "UPDATE payment_operations SET allocation_status = 'partial' WHERE id = :id",
    "UPDATE payment_operations SET accounting_status = 'unrecorded' WHERE id = :id",
    "UPDATE payment_operations SET completion_evidence_json = '{}' WHERE id = :id",
    "UPDATE payment_operations SET completed_at = NULL WHERE id = :id",
    "UPDATE payment_operations SET state = 'reconciliation_required' WHERE id = :id",
    "DELETE FROM payment_operations WHERE id = :id",
])
def test_completed_operations_are_immutable(client, world, sql):
    _refused(client, sql, "payment operation|immutable", id=world["alpha_id"], other=world["vendors"]["BETA"]["id"])


def test_closed_period_rules_cannot_be_bypassed_by_direct_writes(client, world):
    # Fix BETA properly first so February can close.
    fixed = client.post(f"{API}/payment-operations/{world['beta']}/corrections", json={
        "actor": "cfo", "payment_date": "2026-02-10", "reason": "ledger back", "allocations": [{"bill_id": world["bills"]["BETA"]["id"], "amount": "200.00"}]})
    assert fixed.status_code == 200, fixed.text
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"}).status_code == 201
    closed = "the AP period containing this date is closed"
    _refused(client, "INSERT INTO vendor_payments (vendor_id, payment_date, amount, method, cash_account_id, recorded_by, status) "
                     "VALUES (:v, '2026-02-15', 1, 'bank_transfer', :c, 'x', 'recorded')", closed, v=world["vendors"]["ALPHA"]["id"], c=world["accounts"]["cash"])
    _refused(client, "UPDATE payment_allocations SET unallocation_date = '2026-02-20', unallocated_by = 'x', unallocation_reason = 'r' WHERE payment_id = :p",
             closed + "|cannot be modified", p=world["alpha_payment"])
    _refused(client, "INSERT INTO bill_discounts (bill_id, payment_id, amount, taken_on, taken_by, discount_account_id) VALUES (:b, :p, 1, '2026-02-20', 'x', :a)",
             closed + "|exceeds|FOREIGN KEY|foreign key", b=world["bills"]["ALPHA"]["id"], p=world["alpha_payment"], a=world["accounts"]["expense"])
    _refused(client, "DELETE FROM ap_period_locks", "append-only")
    _refused(client, "UPDATE ap_period_locks SET closed_through = '2026-01-31'", "append-only")


def test_events_and_attestations_cannot_be_deleted_or_edited(client, world):
    _refused(client, "DELETE FROM payment_operation_events WHERE operation_id = :id", "append-only", id=world["alpha_id"])
    _refused(client, "UPDATE payment_operation_events SET detail = 'x' WHERE operation_id = :id", "append-only", id=world["alpha_id"])
    _refused(client, "UPDATE payment_operation_events SET actor = 'x' WHERE operation_id = :id", "append-only", id=world["alpha_id"])
    client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"})  # fails (BETA unrecorded) -> attestation
    assert client.get(f"{API}/period-end/attestations").json()[0]["outcome"] == "failed"
    _refused(client, "UPDATE ap_close_attestations SET liability_balance = 0", "append-only")
    _refused(client, "UPDATE ap_close_attestations SET outcome = 'closed'", "append-only")
    _refused(client, "DELETE FROM ap_close_attestations", "append-only")


# --- detected by the verifier, refused by the authoritative operations ------------------------------


def test_forged_status_flags_are_detected_and_block_close_and_completion(client, world):
    _sql(client, "UPDATE payment_operations SET recording_status = 'recorded', ledger_status = 'posted', allocation_status = 'committed', "
                 "accounting_status = 'recorded' WHERE id = :id", id=world["beta_id"])
    v = _verify(client, world["beta"])
    codes = _codes(v)
    assert codes["status_flag_forged"] == "error" and codes["accounting_status_mismatch"] == "error"
    assert v["accounting_status"] == "unrecorded" and v["recorded_accounting_status"] == "recorded"
    status = client.get(f"{API}/payment-operations/{world['beta']}").json()
    assert status["checklist"]["ap_payment_recorded"] is False and status["checklist"]["gl_entry_posted"] is False
    assert status["manual_investigation_required"] is True
    checks = _validate(client)
    assert checks["unrecorded_confirmed_transfers"] == 1
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"}).status_code == 422
    # The flags do not make a completion possible: the database wants the records.
    _refused(client, "UPDATE payment_operations SET state = 'completed' WHERE id = :id", "recorded vendor payment", id=world["beta_id"])
    assert client.post(f"{API}/payment-operations/{world['beta']}/resolve", json={"actor": "cfo", "resolution": "flags say recorded", "mark_completed": True}).status_code == 422


def test_linking_a_lookalike_payment_is_detected(client, world):
    """A same-vendor, same-amount, posted payment passes the database's structural check; the verifier
    sees it is not the transfer's recording (reference, key, no transition event) and refuses completion."""
    lookalike = client.post(f"{API}/payments", json={"vendor_id": world["vendors"]["BETA"]["id"], "payment_date": "2026-02-10", "amount": "200.00",
                                                      "cash_account_id": world["accounts"]["cash"], "recorded_by": "x", "reference": "UNRELATED",
                                                      "allocations": [{"bill_id": world["bills"]["BETA"]["id"], "amount": "200.00"}]}).json()
    _sql(client, "UPDATE payment_operations SET payment_id = :p, accounting_status = 'recorded', recording_status = 'recorded', ledger_status = 'posted' WHERE id = :id",
         id=world["beta_id"], p=lookalike["id"])
    _sql(client, "UPDATE payment_operations SET state = 'completed' WHERE id = :id", id=world["beta_id"])  # structurally acceptable to the trigger
    v = _verify(client, world["beta"])
    codes = _codes(v)
    assert codes["payment_reference_mismatch"] == "error" and codes["payment_not_linked_to_operation"] == "error"
    assert codes["events_state_mismatch"] == "critical" and codes["completed_without_evidence"] == "critical"
    assert v["completion_policy_satisfied"] is False
    assert _validate(client)["unverified_completed_operations"] == 1
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"}).status_code == 422
    run_check = client.get(f"{API}/payment-runs/{world['run']['id']}/verification").json()
    assert run_check["can_complete"] is False and "manual_review" in run_check["categories"]
    assert client.get(f"{API}/verification/operations").json()[0]["operation_uid"] == world["beta"]


def test_duplicate_live_operation_is_detected_and_never_recorded(client, world):
    _sql(client, "INSERT INTO payment_operations (operation_uid, idempotency_key, fingerprint, run_id, vendor_id, amount, currency, payment_date, method, cash_account_id, "
                 "plan_json, state, version, attempts, max_attempts, created_by, correlation_id, gateway_status, gateway_confirmation_ref, accounting_status) "
                 "VALUES ('33333333-3333-3333-3333-333333333333', 'k-twin', 'f', :r, :v, 200, 'XXX', '2026-02-10', 'bank_transfer', :c, '[]', 'submitted', 0, 1, 5, 't', 'c', "
                 "'accepted', 'SIM-FAKE', 'unrecorded')", r=world["run"]["id"], v=world["vendors"]["BETA"]["id"], c=world["accounts"]["cash"])
    _sql(client, "UPDATE payment_operations SET state = 'confirmed' WHERE operation_uid = '33333333-3333-3333-3333-333333333333'")
    twin = _verify(client, "33333333-3333-3333-3333-333333333333")
    assert _codes(twin)["gateway_evidence_missing"] == "critical" and _codes(twin)["duplicate_live_operation"] == "critical"
    assert _codes(_verify(client, world["beta"]))["duplicate_live_operation"] == "critical"
    # Recovery will not write the books for a transfer the gateway does not know.
    results = client.post(f"{API}/recovery/run", json={"actor": "w", "worker_id": "w"}).json()
    twin_result = [r for r in results if r["operation_uid"] == "33333333-3333-3333-3333-333333333333"][0]
    assert twin_result["after"] == "confirmed" and "escalated" in twin_result["note"] and "gateway_evidence_missing" in twin_result["note"]
    # The genuine BETA transfer (gateway evidence present) is recorded by the same worker run; the twin never is.
    assert _sql(client, "SELECT COUNT(*) FROM vendor_payments").scalar() == 2
    assert client.get(f"{API}/payment-operations/{world['beta']}").json()["state"] == "completed"
    assert client.get(f"{API}/payment-operations/33333333-3333-3333-3333-333333333333").json()["recovery_status"] == "manual"
    assert _validate(client)["unrecorded_confirmed_transfers"] == 1  # the twin stays an explicit, unresolved obligation


def test_state_changed_without_event_is_detected(client, world):
    _sql(client, "UPDATE payment_operations SET state = 'recording' WHERE id = :id", id=world["beta_id"])
    codes = _codes(_verify(client, world["beta"]))
    assert codes["events_state_mismatch"] == "critical"


def test_falsified_allocation_completeness_is_detected(client, world):
    # BETA recorded through the correction workflow but with a partial allocation (100 of 200 planned)
    fixed = client.post(f"{API}/payment-operations/{world['beta']}/corrections", json={
        "actor": "cfo", "payment_date": "2026-02-10", "reason": "partial", "allocations": [{"bill_id": world["bills"]["BETA"]["id"], "amount": "100.00"}]})
    assert fixed.status_code == 200 and fixed.json()["settlement_status"] == "partial"
    assert client.get(f"{API}/payment-operations/{world['beta']}").json()["verification"]["evidence"]["allocations"]["unapplied"] == "100.00"
    # The completed row is frozen, so the flag cannot be forged after the fact ...
    _refused(client, "UPDATE payment_operations SET allocation_status = 'committed' WHERE id = :id", "immutable", id=world["beta_id"])
    # ... and the flag on the correction itself is truthful.
    assert client.get(f"{API}/payment-operations/{world['beta']}").json()["verification"]["findings"] == [] or \
        "allocation_status_forged" not in _codes(_verify(client, world["beta"]))


@pytest.mark.parametrize("tamper", ["delete_event", "edit_event", "edit_attestation"])
def test_bypassed_append_only_guards_are_detected_by_hashes(client, world, tamper):
    """If the triggers are disabled (superuser, restored dump, ...), the hash chains still tell."""
    client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"})  # failed attempt -> attestation
    engine = client.app.state.engine
    pg = engine.dialect.name == "postgresql"
    with Session(engine) as s:
        if tamper in ("delete_event", "edit_event"):
            s.execute(text("ALTER TABLE payment_operation_events DISABLE TRIGGER trg_ap_payment_operation_events_guard" if pg
                           else "DROP TRIGGER trg_ap_payment_operation_events_no_" + ("delete" if tamper == "delete_event" else "update")))
            if tamper == "delete_event":
                s.execute(text("DELETE FROM payment_operation_events WHERE operation_id = :id AND seq = 2"), {"id": world["alpha_id"]})
            else:
                s.execute(text("UPDATE payment_operation_events SET detail = 'rewritten' WHERE operation_id = :id AND seq = 3"), {"id": world["alpha_id"]})
            if pg:
                s.execute(text("ALTER TABLE payment_operation_events ENABLE TRIGGER trg_ap_payment_operation_events_guard"))
        else:
            s.execute(text("ALTER TABLE ap_close_attestations DISABLE TRIGGER trg_ap_close_attestations_guard" if pg
                           else "DROP TRIGGER trg_ap_close_attestations_no_update"))
            s.execute(text("UPDATE ap_close_attestations SET liability_balance = liability_balance - 1"))
            if pg:
                s.execute(text("ALTER TABLE ap_close_attestations ENABLE TRIGGER trg_ap_close_attestations_guard"))
        s.commit()
    if tamper == "edit_attestation":
        attestation = client.get(f"{API}/period-end/attestations").json()[0]
        check = client.get(f"{API}/period-end/attestations/{attestation['id']}/verification").json()
        assert check["content_hash_verified"] is False and check["verified"] is False
        assert {f["code"] for f in check["findings"]} >= {"attestation_content_modified"}
    else:
        v = _verify(client, world["alpha"])
        codes = _codes(v)
        assert ("events_sequence_gap" in codes) if tamper == "delete_event" else (codes["events_hash_chain_broken"] == "critical")
        assert v["completion_policy_satisfied"] is False and v["required_evidence"]["events_intact"] is False
        assert _validate(client)["unverified_completed_operations"] == 1
        assert client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo", "force": True, "force_reason": "x"}).status_code == 201
        # A forced close preserves the obligation and the accepted exceptions instead of erasing them.
        attestation = client.get(f"{API}/period-end/attestations").json()[-1]
        assert attestation["outcome"] == "forced"
        exceptions = client.get(f"{API}/period-end/exceptions?attestation_id={attestation['id']}").json()
        assert {e["code"] for e in exceptions} >= {"unverified_completed_operations", "unrecorded_confirmed_transfers"}
        check = client.get(f"{API}/period-end/attestations/{attestation['id']}/verification").json()
        assert [o["current_state"] for o in check["obligations"]] == ["reconciliation_required"]
        assert {f["code"] for f in check["findings"]} >= {"obligation_outstanding", "exception_unresolved"}
