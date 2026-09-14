"""Turn-5 end-to-end, multi-period scenario (SQLite and PostgreSQL), with exact Decimal balances.

January:  three suppliers, six bills, a credit note applied, a partial payment, an advance,
          an early-payment discount taken; January closes clean.
February: a payment run (credits / advances / cash); the gateway confirms all three
          transfers; IRON's GL posting fails repeatedly; recovery gives up; the invalid
          manual completion (a note) is refused; February can only be force-closed and the
          obligation is preserved in the close package.
March:    a genuine, independently verified correction completes IRON's operation exactly
          once without another transfer; identical retries return the original result;
          the exceptions are resolved; supplier-statement reconciliation, historical replay
          at every cutoff and close-package verification agree; March closes clean.

Every balance and journal effect below is asserted as an exact Decimal string.
"""

from decimal import Decimal

from sqlalchemy import text
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


def _post(client, path, **body):
    r = client.post(f"{API}{path}", json=body)
    assert r.status_code in (200, 201), (path, r.text)
    return r.json()


def _bill(client, vendor, accounts, number, total, invoice_date, due_date):
    bill = _post(client, "/bills", vendor_id=vendor["id"], invoice_number=number, invoice_date=invoice_date, due_date=due_date, total=total,
                 lines=[{"description": number, "account_id": accounts["expense"], "unit_price": total}])
    return _post(client, f"/bills/{bill['id']}/approve", actor="alice")


def _sql(client, sql, **params):
    with Session(client.app.state.engine) as s:
        return s.execute(text(sql), params).scalar()


def _outstanding(client, as_of):
    report = client.get(f"{API}/outstanding?as_of={as_of}").json()
    return {r["invoice_number"]: r["outstanding"] for r in report["rows"]}, report["total_outstanding"]


def _balances(client, as_of):
    return {b["vendor_code"]: (b["bills_outstanding"], b["unapplied_credits"], b["unapplied_advances"]) for b in client.get(f"{API}/vendor-balances?as_of={as_of}").json()}


def _gl(client, as_of):
    recon = client.get(f"{API}/reconciliation?as_of={as_of}").json()
    return recon["total_ledger"], recon["total_subledger"], recon["total_difference"], recon["pending_obligations_total"], recon["exceptions"]


def _account_balance(client, account_id, through):
    debit = _sql(client, "SELECT COALESCE(SUM(l.debit), 0) FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
                         "WHERE e.status = 'posted' AND e.entry_date <= :d AND l.account_id = :a", d=through, a=account_id)
    credit = _sql(client, "SELECT COALESCE(SUM(l.credit), 0) FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
                          "WHERE e.status = 'posted' AND e.entry_date <= :d AND l.account_id = :a", d=through, a=account_id)
    return D(str(debit)).quantize(D("0.01")), D(str(credit)).quantize(D("0.01"))


class IronOutage(JournalLedgerAdapter):
    def post(self, db, entry):
        if entry.reference.startswith("AP-PAY-") and "Iron" in entry.description:
            raise LedgerPostingRefused("GL unavailable for this posting")
        return super().post(db, entry)


def test_multi_period_scenario(client):
    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"),
                "cash": _account(client, "1000", "asset"), "discount": _account(client, "4900", "revenue")}
    policy = _post(client, "/discount-policies", code="2/10", description="2% within 10 days", discount_percent="2.00", discount_days=10,
                   discount_account_id=accounts["discount"])
    oak = _post(client, "/vendors", code="OAK", name="Oak Timber", payable_account_id=accounts["ap"], bank_reference="IBAN-OAK", discount_policy_id=policy["id"])
    iron = _post(client, "/vendors", code="IRON", name="Iron Works", payable_account_id=accounts["ap"], bank_reference="IBAN-IRON")
    glass = _post(client, "/vendors", code="GLASS", name="Glass Co", payable_account_id=accounts["ap"], bank_reference="IBAN-GLASS")

    # ---------------------------------------------------------------- January -----------------------------------------------
    o1 = _bill(client, oak, accounts, "OAK-1", "1000.00", "2026-01-05", "2026-02-04")
    o2 = _bill(client, oak, accounts, "OAK-2", "600.00", "2026-01-20", "2026-02-19")
    assert o2["discount_amount"] == "12.00" and o2["discount_date"] == "2026-01-30"
    i1 = _bill(client, iron, accounts, "IRON-1", "500.00", "2026-01-08", "2026-02-07")
    i2 = _bill(client, iron, accounts, "IRON-2", "450.00", "2026-01-25", "2026-02-24")
    g1 = _bill(client, glass, accounts, "GLASS-1", "800.00", "2026-01-12", "2026-02-11")
    credit = _post(client, "/credits", vendor_id=oak["id"], kind="credit_note", credit_number="CN-OAK", credit_date="2026-01-22", total="100.00",
                   lines=[{"description": "returned boards", "account_id": accounts["expense"], "unit_price": "100.00"}])
    _post(client, f"/credits/{credit['id']}/approve", actor="alice")
    _post(client, f"/credits/{credit['id']}/applications", bill_id=o2["id"], amount="100.00", actor="bob", application_date="2026-01-23")
    _post(client, "/payments", vendor_id=iron["id"], payment_date="2026-01-15", amount="200.00", cash_account_id=accounts["cash"], recorded_by="carol",
          reference="TRF-I1", allocations=[{"bill_id": i1["id"], "amount": "200.00"}])
    _post(client, "/payments", vendor_id=glass["id"], payment_date="2026-01-18", amount="300.00", cash_account_id=accounts["cash"], recorded_by="carol",
          reference="ADV-G")
    # OAK-2 paid in full inside the discount window: 500 outstanding after the credit = 488 cash + 12 discount.
    paid_o2 = _post(client, "/payments", vendor_id=oak["id"], payment_date="2026-01-28", amount="488.00", cash_account_id=accounts["cash"], recorded_by="carol",
                    reference="TRF-O2", allocations=[{"bill_id": o2["id"], "amount": "488.00", "take_discount": True}])
    assert paid_o2["discounts"][0]["amount"] == "12.00" and client.get(f"{API}/bills/{o2['id']}").json()["status"] == "paid"

    rows, total = _outstanding(client, "2026-01-31")
    assert rows == {"OAK-1": "1000.00", "IRON-1": "300.00", "IRON-2": "450.00", "GLASS-1": "800.00"} and total == "2550.00"
    assert _balances(client, "2026-01-31") == {"OAK": ("1000.00", "0.00", "0.00"), "IRON": ("750.00", "0.00", "0.00"), "GLASS": ("800.00", "0.00", "300.00")}
    assert _gl(client, "2026-01-31") == ("2250.00", "2250.00", "0.00", "0.00", [])
    assert _account_balance(client, accounts["ap"], "2026-01-31") == (D("1100.00"), D("3350.00"))  # 100 credit + 200 + 488 + 12 discount + 300 advance
    assert _account_balance(client, accounts["cash"], "2026-01-31") == (D("0.00"), D("988.00"))
    assert _account_balance(client, accounts["discount"], "2026-01-31") == (D("0.00"), D("12.00"))
    jan = client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"})
    assert jan.status_code == 201, jan.text
    jan_attestation = client.get(f"{API}/period-end/attestations").json()[-1]
    assert jan_attestation["outcome"] == "closed" and jan_attestation["liability_balance"] == "2550.00"
    replay_jan = client.get(f"{API}/reconstruction?as_of=2026-01-31").json()
    assert replay_jan["findings"] == [] and replay_jan["obligations_total"] == "0.00"
    assert replay_jan["control_balance"][str(accounts["ap"])] == "2250.00" and replay_jan["ledger_balance"][str(accounts["ap"])] == "2250.00"

    # ---------------------------------------------------------------- February ----------------------------------------------
    _bill(client, oak, accounts, "OAK-3", "310.00", "2026-02-25", "2026-03-27")
    run = _post(client, "/payment-runs", actor="ops", pay_date="2026-02-10", due_through="2026-02-28", cash_account_id=accounts["cash"])
    plan = {i["bill_id"]: (i["advance_amount"], i["credit_amount"], i["pay_amount"]) for i in run["items"]}
    assert plan == {o1["id"]: ("0.00", "0.00", "1000.00"), i1["id"]: ("0.00", "0.00", "300.00"), i2["id"]: ("0.00", "0.00", "450.00"), g1["id"]: ("300.00", "0.00", "500.00")}
    assert run["total_pay_amount"] == "2250.00"
    _post(client, f"/payment-runs/{run['id']}/review", actor="rev")
    _post(client, f"/payment-runs/{run['id']}/approve", actor="boss")

    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=DurableSimulatedGateway(), ledger=IronOutage())
    result = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
    outcomes = {o["vendor_code"]: o for o in result["outcomes"]}
    assert (outcomes["OAK"]["state"], outcomes["GLASS"]["state"], outcomes["IRON"]["state"]) == ("completed", "completed", "reconciliation_required")
    assert result["run"]["status"] == "failed" and result["run"]["finalization_status"] == "incomplete_accounting", client.get(f"{API}/payment-runs/{run['id']}/verification").json()
    iron_uid = outcomes["IRON"]["operation_uid"]
    assert _sql(client, "SELECT COUNT(*) FROM simulated_gateway_transfers WHERE status = 'accepted'") == 3
    assert _sql(client, "SELECT COALESCE(SUM(amount), 0) FROM simulated_gateway_transfers WHERE status = 'accepted'") == 2250
    for _ in range(6):
        client.post(f"{API}/recovery/run", json={"actor": "w", "worker_id": "w"})
    iron_status = client.get(f"{API}/payment-operations/{iron_uid}").json()
    assert iron_status["state"] == "reconciliation_required" and iron_status["recovery_action"] == "manual"
    assert iron_status["delivery_status"] == "delivered" and iron_status["accounting_status"] == "unrecorded"
    # After the attempt budget the run is re-finalised: IRON now needs a person, the run says so.
    assert client.get(f"{API}/payment-runs/{run['id']}").json()["finalization_status"] == "manual_review"
    items = {i["bill_id"]: i for i in client.get(f"{API}/payment-runs/{run['id']}").json()["items"]}
    assert items[g1["id"]]["settlement_status"] == "recorded" and items[g1["id"]]["actual_advance_amount"] == "300.00" and items[g1["id"]]["actual_pay_amount"] == "500.00"
    assert items[i1["id"]]["settlement_status"] == "manual_review" and items[i1["id"]]["status"] == "proposed"

    # The invalid manual completion: refused; the obligation stays explicit.
    invalid = client.post(f"{API}/payment-operations/{iron_uid}/resolve", json={"actor": "cfo", "resolution": "posted manually, trust me", "mark_completed": True})
    assert invalid.status_code == 422 and "not evidence" in invalid.json()["detail"]
    assert client.get(f"{API}/payment-operations/{iron_uid}").json()["state"] == "reconciliation_required"

    rows, total = _outstanding(client, "2026-02-28")
    assert rows == {"IRON-1": "300.00", "IRON-2": "450.00", "OAK-3": "310.00"} and total == "1060.00"
    assert _balances(client, "2026-02-28") == {"OAK": ("310.00", "0.00", "0.00"), "IRON": ("750.00", "0.00", "0.00"), "GLASS": ("0.00", "0.00", "0.00")}
    assert _gl(client, "2026-02-28") == ("1060.00", "1060.00", "0.00", "750.00", [])  # books agree; 750 of cash is out and unrecorded
    assert _account_balance(client, accounts["cash"], "2026-02-28") == (D("0.00"), D("2488.00"))  # 988 + 1000 + 500
    feb = client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"})
    assert feb.status_code == 422 and "unrecorded_confirmed_transfers" in feb.json()["detail"]
    forced = client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo", "force": True, "force_reason": "month end; IRON under investigation"})
    assert forced.status_code == 201, forced.text
    feb_attestation = client.get(f"{API}/period-end/attestations").json()[-1]
    assert feb_attestation["outcome"] == "forced" and feb_attestation["liability_balance"] == "1060.00"
    assert iron_uid in feb_attestation["obligations_json"]
    exceptions = client.get(f"{API}/period-end/exceptions?attestation_id={feb_attestation['id']}").json()
    assert {e["code"] for e in exceptions} == {"unrecorded_confirmed_transfers", "payment_runs_in_flight"} and all(e["status"] == "accepted" for e in exceptions)
    check = client.get(f"{API}/period-end/attestations/{feb_attestation['id']}/verification").json()
    assert check["content_hash_verified"] and check["obligations"][0]["current_state"] == "reconciliation_required"
    # Recording into the closed period is refused: the books of February stay as attested.
    late = client.post(f"{API}/payment-operations/{iron_uid}/corrections", json={"actor": "cfo", "payment_date": "2026-02-10", "reason": "late",
                                                                                "allocations": [{"bill_id": i1["id"], "amount": "300.00"}, {"bill_id": i2["id"], "amount": "450.00"}]})
    assert late.status_code == 422 and "closed AP period" in late.json()["detail"]

    # ---------------------------------------------------------------- March -------------------------------------------------
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=DurableSimulatedGateway())
    body = {"actor": "cfo", "payment_date": "2026-03-02", "reason": "GL restored; booking the confirmed transfer", "idempotency_key": "iron-fix",
            "allocations": [{"bill_id": i1["id"], "amount": "300.00"}, {"bill_id": i2["id"], "amount": "450.00"}]}
    fixed = client.post(f"{API}/payment-operations/{iron_uid}/corrections", json=body)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["state"] == "completed" and fixed.json()["accounting_status"] == "corrected" and fixed.json()["completion_policy_satisfied"]
    assert fixed.json()["verification"]["required_evidence"] == {k: True for k in fixed.json()["verification"]["required_evidence"]}
    for _ in range(2):
        again = client.post(f"{API}/payment-operations/{iron_uid}/corrections", json=body)
        assert again.status_code == 200 and again.json()["payment_id"] == fixed.json()["payment_id"]
    assert client.post(f"{API}/payment-operations/{iron_uid}/corrections", json={**body, "reason": "other"}).status_code == 409
    assert _sql(client, "SELECT COUNT(*) FROM simulated_gateway_transfers") == 3  # no second transfer
    assert _sql(client, "SELECT COUNT(*) FROM vendor_payments WHERE vendor_id = :v", v=iron["id"]) == 2  # TRF-I1 + the correction
    iron_payment = client.get(f"{API}/payments/{fixed.json()['payment_id']}").json()
    assert iron_payment["amount"] == "750.00" and iron_payment["payment_date"] == "2026-03-02" and iron_payment["reference"] == iron_status["gateway_confirmation_ref"]
    entry_lines = client.get(f"/api/v1/journal-entries/{iron_payment['journal_entry_id']}").json()
    assert entry_lines["status"] == "posted" and entry_lines["entry_date"] == "2026-03-02"
    assert {(line["account_id"], line["debit"], line["credit"]) for line in entry_lines["lines"]} == {(accounts["ap"], "750.00", "0.00"), (accounts["cash"], "0.00", "750.00")}
    run_after = client.get(f"{API}/payment-runs/{run['id']}").json()
    assert run_after["status"] == "completed" and run_after["finalization_status"] == "completed"
    assert {i["bill_id"]: i["settlement_status"] for i in run_after["items"]} == {o1["id"]: "recorded", i1["id"]: "recorded", i2["id"]: "recorded", g1["id"]: "recorded"}
    for exc in exceptions:
        assert client.post(f"{API}/period-end/exceptions/{exc['id']}/resolve", json={"actor": "cfo", "note": "IRON booked 2026-03-02", "reference": f"vendor_payment:{iron_payment['id']}"}).status_code == 200
    assert client.post(f"{API}/period-end/exceptions/{exceptions[0]['id']}/resolve", json={"actor": "cfo", "note": "again"}).status_code == 409

    # As recorded at the February cutoff nothing changed; March shows the correction.
    assert _gl(client, "2026-02-28")[:4] == ("1060.00", "1060.00", "0.00", "750.00")
    assert _outstanding(client, "2026-02-28")[1] == "1060.00"
    rows, total = _outstanding(client, "2026-03-31")
    assert rows == {"OAK-3": "310.00"} and total == "310.00"
    assert _balances(client, "2026-03-31") == {"OAK": ("310.00", "0.00", "0.00"), "IRON": ("0.00", "0.00", "0.00"), "GLASS": ("0.00", "0.00", "0.00")}
    assert _gl(client, "2026-03-31") == ("310.00", "310.00", "0.00", "0.00", [])
    assert _account_balance(client, accounts["ap"], "2026-03-31") == (D("3350.00"), D("3660.00"))
    assert _account_balance(client, accounts["cash"], "2026-03-31") == (D("0.00"), D("3238.00"))  # 200 + 488 + 300 + 1000 + 500 + 750
    assert _sql(client, "SELECT COUNT(*) FROM journal_entries WHERE status = 'posted'") == 14 and _sql(client, "SELECT COUNT(*) FROM journal_entries WHERE status <> 'posted'") == 0

    # Supplier statement reconciliation for IRON, historical replay at every cutoff, close-package verification.
    statement = ("kind,reference,date,amount\n"
                 "invoice,IRON-1,2026-01-08,500.00\ninvoice,IRON-2,2026-01-25,450.00\n"
                 f"payment,TRF-I1,2026-01-15,200.00\npayment,{iron_payment['reference']},2026-03-02,750.00\n")
    imported = client.post(f"{API}/vendors/{iron['id']}/statement-imports", json={"actor": "fin", "file_name": "iron-mar.csv", "statement_date": "2026-03-31", "content": statement})
    assert imported.status_code == 201 and {l["match_status"] for l in imported.json()["lines"]} == {"matched"}
    for as_of, obligations in (("2026-01-31", "0.00"), ("2026-02-28", "750.00"), ("2026-03-31", "0.00")):
        replay = client.get(f"{API}/reconstruction?as_of={as_of}").json()
        assert replay["findings"] == [], (as_of, replay["findings"])
        assert replay["obligations_total"] == obligations
    feb_check = client.get(f"{API}/period-end/attestations/{feb_attestation['id']}/verification").json()
    assert feb_check["verified"] and feb_check["obligations"][0] == {"operation_uid": iron_uid, "amount": "750.00", "attested_state": "reconciliation_required",
                                                                     "current_state": "completed", "accounting_status": "corrected", "verified": True}
    assert all(e["status"] == "resolved" for e in feb_check["exceptions"])
    assert client.get(f"{API}/verification/operations").json() == []
    mar = client.post(f"{API}/period-end/close", json={"period_end": "2026-03-31", "actor": "cfo"})
    assert mar.status_code == 201, mar.text
    mar_attestation = client.get(f"{API}/period-end/attestations").json()[-1]
    assert mar_attestation["outcome"] == "closed" and mar_attestation["liability_balance"] == "310.00" and mar_attestation["obligations_json"] == "[]"
    assert client.get(f"{API}/period-end/attestations/{mar_attestation['id']}/verification").json()["verified"]
