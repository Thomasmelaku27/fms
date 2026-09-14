"""End-to-end financial scenario across suppliers and periods (SQLite and PostgreSQL).

Every stage states the expected balances in concrete amounts and checks them two
ways: through the production reports and through the independent replay
(``services.reconstruction``) plus raw SQL sums over the posted ledger.
"""

from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.modules.payables.integration import Integrations, get_integrations
from app.modules.payables.ledger_adapter import JournalLedgerAdapter, LedgerPostingRefused
from app.modules.payables.models import PaymentAllocation, PaymentOperation, VendorPayment
from app.modules.payables.services.gateway import DurableSimulatedGateway

API = "/api/v1/payables"
D = Decimal


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sql(client, sql, **params):
    with Session(client.app.state.engine) as s:
        return s.execute(text(sql), params).scalar()


def _ap_ledger(client, ap_id, as_of):
    return D(str(_sql(client, "SELECT COALESCE(SUM(l.credit - l.debit), 0) FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
                              "WHERE l.account_id = :a AND e.status = 'posted' AND e.entry_date <= :d", a=ap_id, d=as_of) or 0)).quantize(D("0.01"))


def _check(client, ap_id, as_of, expected_bills, expected_net, expected_ledger):
    """Production reports, independent replay and raw ledger sum must all agree."""
    recon = client.get(f"{API}/reconstruction?as_of={as_of}").json()
    assert recon["findings"] == [], (as_of, recon["findings"])
    assert {k: v for k, v in recon["bill_outstanding"].items() if v != "0.00"} == expected_bills, as_of
    balances = client.get(f"{API}/vendor-balances?as_of={as_of}").json()
    assert {b["vendor_code"]: b["net_owed"] for b in balances if b["net_owed"] != "0.00"} == expected_net, as_of
    assert recon["control_balance"][str(ap_id)] == recon["ledger_balance"][str(ap_id)] == expected_ledger, as_of
    assert _ap_ledger(client, ap_id, as_of) == D(expected_ledger)
    gl = client.get(f"{API}/reconciliation?as_of={as_of}").json()
    assert gl["total_difference"] == "0.00" and gl["exceptions"] == [], as_of


def test_reliable_accounts_payable_end_to_end(client):
    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"),
                "cash": _account(client, "1000", "asset"), "cash2": _account(client, "1010", "asset"), "discount": _account(client, "4900", "revenue")}
    ap = accounts["ap"]

    # --- 1. Suppliers and terms ----------------------------------------------------------------------
    policy = client.post(f"{API}/discount-policies", json={"code": "2/10", "description": "2% within 10 days", "discount_percent": "2.00",
                                                            "discount_days": 10, "discount_account_id": accounts["discount"]}).json()
    vendors = {}
    for code, name, terms, pol in (("OAK", "Oak Timber", 30, policy["id"]), ("IRON", "Iron Works", 45, None), ("GLASS", "Glass Co", 14, None)):
        r = client.post(f"{API}/vendors", json={"code": code, "name": name, "payable_account_id": ap, "payment_terms_days": terms,
                                                 "discount_policy_id": pol, "bank_reference": f"IBAN-{code}"})
        assert r.status_code == 201, r.text
        vendors[code] = r.json()
    oak, iron, glass = vendors["OAK"], vendors["IRON"], vendors["GLASS"]

    def bill(vendor, number, lines, invoice_date, due=None):
        total = sum((D(q) * D(p) for _, q, p in lines), D("0"))
        r = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": number, "invoice_date": invoice_date, "due_date": due,
                                              "total": str(total), "lines": [{"description": d, "account_id": accounts["expense"], "quantity": q, "unit_price": p} for d, q, p in lines]})
        assert r.status_code == 201, r.text
        return r.json()

    def approve(b):
        r = client.post(f"{API}/bills/{b['id']}/approve", json={"actor": "alice"})
        assert r.status_code == 200, r.text
        return r.json()

    # --- 2. Bills: validation, duplicates, approval ------------------------------------------------------
    o1 = bill(oak, "OAK-1001", [("planks", "40", "30.00")], "2026-01-05")                  # 1,200.00, discount 24 until 01-15
    assert client.post(f"{API}/bills", json={"vendor_id": oak["id"], "invoice_number": " oak-1001 ", "invoice_date": "2026-01-05",
                                             "total": "1.00"}).status_code == 409                                    # exact duplicate
    suspect = bill(oak, "OAK 1001", [("planks", "40", "30.00")], "2026-01-05")             # near duplicate -> blocking flag
    assert suspect["duplicate_flags"][0]["blocks_approval"] and client.post(f"{API}/bills/{suspect['id']}/approve", json={"actor": "alice"}).status_code == 422
    flag = suspect["duplicate_flags"][0]
    assert client.post(f"{API}/bills/{suspect['id']}/duplicate-flags/{flag['id']}/review", json={"decision": "confirmed", "actor": "rev"}).status_code == 200
    assert client.post(f"{API}/bills/{suspect['id']}/reject", json={"actor": "alice", "reason": "duplicate"}).status_code == 200
    o2 = bill(oak, "OAK-1002", [("beams", "10", "80.00")], "2026-01-20")                   # 800.00, due 02-19
    i1 = bill(iron, "IR-77", [("steel", "3", "500.00")], "2026-01-08")                     # 1,500.00, due 02-22
    i2 = bill(iron, "IR-78", [("bolts", "1000", "0.45")], "2026-01-25")                    # 450.00, due 03-11
    g1 = bill(glass, "GL-5", [("panes", "4", "125.00")], "2026-02-02")                     # 500.00, due 02-16
    for b in (o1, o2, i1, i2, g1):
        assert approve(b)["status"] == "approved"
    assert o1["due_date"] == "2026-02-04" and i1["due_date"] == "2026-02-22" and g1["due_date"] == "2026-02-16"
    assert client.get(f"{API}/bills/{o1['id']}").json()["discount_amount"] == "24.00"
    _check(client, ap, "2026-02-02", {str(o1["id"]): "1200.00", str(o2["id"]): "800.00", str(i1["id"]): "1500.00", str(i2["id"]): "450.00", str(g1["id"]): "500.00"},
           {"OAK": "2000.00", "IRON": "1950.00", "GLASS": "500.00"}, "4450.00")

    # --- 3. Partial payment, advance, credit apply / unapply, hold / dispute ------------------------------
    p_partial = client.post(f"{API}/payments", json={"vendor_id": oak["id"], "payment_date": "2026-01-12", "amount": "700.00", "cash_account_id": accounts["cash"],
                                                     "recorded_by": "bob", "reference": "TRF-OAK-1", "allocations": [{"bill_id": o1["id"], "amount": "700.00"}]})
    assert p_partial.status_code == 201
    advance = client.post(f"{API}/payments", json={"vendor_id": iron["id"], "payment_date": "2026-01-15", "amount": "1000.00", "cash_account_id": accounts["cash"],
                                                   "recorded_by": "bob", "reference": "ADV-IRON"}).json()
    assert advance["unapplied_amount"] == "1000.00"
    assert client.post(f"{API}/payments/{advance['id']}/applications", json={"bill_id": i1["id"], "amount": "600.00", "actor": "bob", "allocation_date": "2026-01-18"}).status_code == 200
    credit = client.post(f"{API}/credits", json={"vendor_id": oak["id"], "credit_number": "CN-OAK-9", "credit_date": "2026-01-22", "total": "100.00",
                                                 "lines": [("x", "1", "100.00") and {"description": "warped planks", "account_id": accounts["expense"], "unit_price": "100.00"}]}).json()
    assert client.post(f"{API}/credits/{credit['id']}/approve", json={"actor": "alice"}).status_code == 200
    applied = client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": o1["id"], "amount": "100.00", "actor": "bob", "application_date": "2026-01-23"}).json()
    assert client.post(f"{API}/bills/{i2['id']}/holds", json={"kind": "dispute", "reason": "short delivery of 200 bolts", "actor": "alice"}).status_code == 200
    held_g1 = client.post(f"{API}/bills/{g1['id']}/holds", json={"kind": "hold", "reason": "awaiting goods receipt", "actor": "alice"}).json()
    _check(client, ap, "2026-01-31", {str(o1["id"]): "400.00", str(o2["id"]): "800.00", str(i1["id"]): "900.00", str(i2["id"]): "450.00"},
           {"OAK": "1200.00", "IRON": "950.00"}, "2150.00")   # 4450 - 700 - 1000 advance - 100 credit (- 500 glass not yet dated)
    # Unapply the credit in February (belongs to OAK-1002) and re-apply it there.
    assert client.post(f"{API}/credits/{credit['id']}/applications/{applied['applications'][0]['id']}/unapply",
                       json={"actor": "carol", "reason": "belongs to OAK-1002", "unapplication_date": "2026-02-03"}).status_code == 200
    assert client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": o2["id"], "amount": "100.00", "actor": "carol", "application_date": "2026-02-03"}).status_code == 200
    _check(client, ap, "2026-01-31", {str(o1["id"]): "400.00", str(o2["id"]): "800.00", str(i1["id"]): "900.00", str(i2["id"]): "450.00"},
           {"OAK": "1200.00", "IRON": "950.00"}, "2150.00")   # January unchanged by the February correction
    _check(client, ap, "2026-02-05", {str(o1["id"]): "500.00", str(o2["id"]): "700.00", str(i1["id"]): "900.00", str(i2["id"]): "450.00", str(g1["id"]): "500.00"},
           {"OAK": "1200.00", "IRON": "950.00", "GLASS": "500.00"}, "2650.00")

    # --- 4. Payment proposal, independent review and approval ------------------------------------------------
    run = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-20", "due_through": "2026-02-28", "cash_account_id": accounts["cash"],
                                                   "description": "February run"}).json()
    items = {i["bill_id"]: i for i in run["items"]}
    assert set(items) == {o1["id"], o2["id"], i1["id"]}             # i2 disputed, g1 held, i2 due in March anyway
    assert items[o1["id"]]["pay_amount"] == "500.00" and items[o1["id"]]["discount_amount"] == "0.00"
    assert items[o2["id"]]["pay_amount"] == "700.00"
    assert (items[i1["id"]]["advance_amount"], items[i1["id"]]["pay_amount"]) == ("400.00", "500.00")   # remaining advance first
    assert run["total_pay_amount"] == "1700.00"
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 409        # not reviewed
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "rev"}).status_code == 422         # four eyes
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "ops"}).status_code == 422         # creator
    approved = client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).json()
    assert approved["status"] == "approved" and approved["approved_fingerprint"]
    assert client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "boss"}).status_code == 422        # approver may not execute

    # --- 5. Execution: OAK paid; IRON's ledger posting fails after the gateway confirmed ---------------------------
    class IronLedgerOutage(JournalLedgerAdapter):
        def post(self, db, entry):
            if entry.reference.startswith("AP-PAY-") and "Iron" in entry.description:
                raise LedgerPostingRefused("ledger unavailable for this posting")
            return super().post(db, entry)

    gateway = DurableSimulatedGateway()
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=gateway, ledger=IronLedgerOutage())
    result = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
    assert result["run"]["status"] == "failed"
    outcomes = {o["vendor_code"]: o for o in result["outcomes"]}
    assert outcomes["OAK"]["success"] and outcomes["OAK"]["amount"] == "1200.00"
    assert outcomes["IRON"]["state"] == "reconciliation_required" and "recording pending recovery" in outcomes["IRON"]["error"]
    assert _sql(client, "SELECT COUNT(*) FROM simulated_gateway_transfers WHERE status = 'accepted'") == 2   # both transfers happened
    assert _sql(client, "SELECT COUNT(*) FROM vendor_payments WHERE reference LIKE 'SIM-%'") == 1               # only OAK recorded
    assert _sql(client, "SELECT COUNT(*) FROM journal_entries WHERE status = 'draft'") == 0
    iron_status = client.get(f"{API}/payment-operations/{outcomes['IRON']['operation_uid']}").json()
    assert iron_status["checklist"]["gateway_confirmed"] and not iron_status["checklist"]["ap_payment_recorded"]
    assert iron_status["failure_category"] == "ledger" and iron_status["recovery_safe"]

    # --- 6. Worker restart and recovery; repeated submission with the original key; no second transfer ------------
    fresh_gateway = DurableSimulatedGateway()          # a new process: no memory, same durable store
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=fresh_gateway)
    retry = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()   # same run, same operation key
    assert retry["run"]["status"] == "completed"
    iron_retry = {o["vendor_code"]: o for o in retry["outcomes"]}["IRON"]
    assert iron_retry["success"] and iron_retry["operation_uid"] == outcomes["IRON"]["operation_uid"]
    assert fresh_gateway.calls == []                                                                  # never asked the bank again
    assert _sql(client, "SELECT COUNT(*) FROM simulated_gateway_transfers") == 2
    assert _sql(client, "SELECT COUNT(*) FROM payment_operations") == 2
    assert client.get(f"{API}/recovery/scan").json() == []
    iron_payment = [p for p in client.get(f"{API}/payments?vendor_id={iron['id']}").json() if p["reference"] and p["reference"].startswith("SIM-")][0]
    assert iron_payment["amount"] == "500.00" and iron_payment["idempotency_key"] == f"op:{outcomes['IRON']['operation_uid']}"
    assert client.get(f"{API}/bills/{i1['id']}").json()["status"] == "paid"
    _check(client, ap, "2026-02-20", {str(i2["id"]): "450.00", str(g1["id"]): "500.00"}, {"IRON": "450.00", "GLASS": "500.00"}, "950.00")

    # --- 7. Supplier statement import and matching (external evidence, nothing posted) ---------------------------
    oak_payments = client.get(f"{API}/payments?vendor_id={oak['id']}").json()
    sim_ref = [p["reference"] for p in oak_payments if p["reference"].startswith("SIM-")][0]
    csv = ("kind,reference,date,amount\n"
           "invoice,OAK-1001,2026-01-05,1200.00\ninvoice,OAK-1002,2026-01-20,800.00\ninvoice,OAK-1003,2026-02-25,310.00\n"
           f"credit,CN-OAK-9,2026-01-22,100.00\npayment,TRF-OAK-1,2026-01-12,700.00\npayment,{sim_ref},2026-02-20,1200.00\n")
    before = _sql(client, "SELECT COUNT(*) FROM journal_entries")
    imported = client.post(f"{API}/vendors/{oak['id']}/statement-imports", json={"actor": "fin", "file_name": "oak-feb.csv", "statement_date": "2026-02-28", "content": csv})
    assert imported.status_code == 201, imported.text
    statuses = {l["reference"]: l["match_status"] for l in imported.json()["lines"]}
    assert statuses == {"OAK-1001": "matched", "OAK-1002": "matched", "OAK-1003": "unmatched", "CN-OAK-9": "matched", "TRF-OAK-1": "matched", sim_ref: "matched"}
    assert _sql(client, "SELECT COUNT(*) FROM journal_entries") == before

    # --- 8. Payment correction (OAK's run payment left the wrong bank account) ------------------------------------------
    oak_run_payment = [p for p in oak_payments if p["reference"] == sim_ref][0]
    corrected = client.post(f"{API}/payments/{oak_run_payment['id']}/correct", json={
        "actor": "carol", "reason": "wrong bank account", "reversal_date": "2026-02-25",
        "replacement": {"vendor_id": oak["id"], "payment_date": "2026-02-25", "amount": "1200.00", "cash_account_id": accounts["cash2"], "recorded_by": "carol",
                        "reference": "TRF-FIX", "allocations": [{"bill_id": o1["id"], "amount": "500.00"}, {"bill_id": o2["id"], "amount": "700.00"}]}})
    assert corrected.status_code == 200, corrected.text
    _check(client, ap, "2026-02-24", {str(i2["id"]): "450.00", str(g1["id"]): "500.00"}, {"IRON": "450.00", "GLASS": "500.00"}, "950.00")
    _check(client, ap, "2026-02-28", {str(i2["id"]): "450.00", str(g1["id"]): "500.00"}, {"IRON": "450.00", "GLASS": "500.00"}, "950.00")

    # --- 9. Period-end: January closes; the disputed / held bills are February warnings ---------------------------------
    jan = client.get(f"{API}/period-end/validate?period_end=2026-01-31").json()
    assert jan["can_close"], [c for c in jan["checks"] if c["severity"] == "blocker" and c["count"]]
    closed = client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo", "note": "January"})
    assert closed.status_code == 201
    attestation = client.get(f"{API}/period-end/attestations?period_end=2026-01-31").json()[0]
    assert attestation["outcome"] == "closed" and attestation["liability_balance"] == "2550.00"    # Jan 31 bills: 400+800+900+450
    assert client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": i2["id"], "amount": "1.00", "actor": "x", "application_date": "2026-01-30"}).status_code == 422
    # Resolve the dispute and pay IRON's last bill in March; release GLASS and pay it.
    holds = client.get(f"{API}/bills/{i2['id']}").json()["holds"]
    assert client.post(f"{API}/bills/{i2['id']}/holds/{holds[0]['id']}/release", json={"actor": "alice", "resolution": "200 bolts delivered"}).status_code == 200
    assert client.post(f"{API}/bills/{g1['id']}/holds/{held_g1['holds'][0]['id']}/release", json={"actor": "alice", "resolution": "received"}).status_code == 200
    run2 = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-03-12", "due_through": "2026-03-31", "cash_account_id": accounts["cash"]}).json()
    assert {i["bill_id"]: i["pay_amount"] for i in run2["items"]} == {i2["id"]: "450.00", g1["id"]: "500.00"}
    assert client.post(f"{API}/payment-runs/{run2['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run2['id']}/approve", json={"actor": "boss"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run2['id']}/execute", json={"actor": "ops"}).json()["run"]["status"] == "completed"
    _check(client, ap, "2026-03-31", {}, {}, "0.00")
    feb = client.post(f"{API}/period-end/close", json={"period_end": "2026-02-28", "actor": "cfo"})
    assert feb.status_code == 201, (feb.text, [(f["code"], f["actual"]) for v in client.get(f"{API}/verification/operations").json() for f in v["findings"]], client.get(f"{API}/verification/operations").json()[0]["required_evidence"])

    # --- 10. Historical statements and final reconciliation -----------------------------------------------------------
    oak_statement = client.get(f"{API}/vendors/{oak['id']}/statement?date_from=2026-01-01&date_to=2026-01-31").json()
    assert oak_statement["closing_balance"] == "1200.00" and oak_statement["difference"] == "0.00"
    assert [(l["type"], l["credit"] if l["type"] == "bill" else l["debit"]) for l in oak_statement["lines"]] == [
        ("bill", "1200.00"), ("payment", "700.00"), ("bill", "800.00"), ("credit", "100.00")]
    oak_q1 = client.get(f"{API}/vendors/{oak['id']}/statement?date_from=2026-01-01&date_to=2026-03-31").json()
    assert oak_q1["closing_balance"] == "0.00" and oak_q1["totals"]["payment_reversals"] == "1200.00"
    for as_of in ("2026-01-31", "2026-02-28", "2026-03-31"):
        assert client.get(f"{API}/reconstruction?as_of={as_of}").json()["findings"] == []
    restated_jan = client.get(f"{API}/reconstruction?as_of=2026-01-31&view=restated").json()
    assert restated_jan["vendor_credits"][str(oak["id"])] == "100.00"   # with later knowledge: credit was not OAK-1001's
    with Session(client.app.state.engine) as s:
        assert s.scalar(select(func.count()).select_from(VendorPayment)) == 7      # partial, advance, oak run, iron run, replacement, iron2, glass
        assert s.scalar(select(func.count()).select_from(PaymentOperation)) == 4   # oak, iron, iron2, glass
        assert all(o.state.value == "completed" for o in s.scalars(select(PaymentOperation)))
        assert s.scalar(select(func.count()).select_from(PaymentAllocation)) == 1 + 1 + 2 + 1 + 2 + 1 + 1 + 1
        assert all(e.status.value == "posted" for e in s.scalars(select(JournalEntry)))
    final = client.get(f"{API}/reconciliation?as_of=2026-03-31").json()
    assert final["total_ledger"] == final["total_subledger"] == "0.00" and final["exceptions"] == []
    assert _sql(client, "SELECT COUNT(*) FROM simulated_gateway_transfers WHERE status = 'accepted'") == 4
    assert client.get(f"{API}/period-end/validate?period_end=2026-03-31").json()["status"] in ("ok", "warnings")
