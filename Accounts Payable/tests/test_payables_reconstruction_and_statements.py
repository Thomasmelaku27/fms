"""Independent historical reconstruction and supplier statement imports (SQLite and PostgreSQL)."""

import base64
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import JournalEntry
from app.modules.payables.models import SupplierStatementImport, SupplierStatementLine

API = "/api/v1/payables"
D = Decimal


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset"),
            "discount": _account(client, "4900", "revenue")}


@pytest.fixture
def vendor(client, accounts):
    return client.post(f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]}).json()


def _bill(client, vendor, accounts, number, total, invoice_date):
    r = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": number, "invoice_date": invoice_date, "due_date": invoice_date,
                                          "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    assert client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"}).status_code == 200
    return r.json()


def _pay(client, vendor, accounts, allocations, amount, date_, reference=None):
    r = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": date_, "amount": amount, "cash_account_id": accounts["cash"],
                                             "recorded_by": "bob", "reference": reference, "allocations": allocations})
    assert r.status_code == 201, r.text
    return r.json()


def _credit(client, vendor, accounts, number, total, date_):
    r = client.post(f"{API}/credits", json={"vendor_id": vendor["id"], "credit_number": number, "credit_date": date_, "total": total,
                                            "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": total}]})
    assert client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"}).status_code == 200
    return r.json()


def _recon(client, as_of, view="as_of"):
    r = client.get(f"{API}/reconstruction?as_of={as_of}&view={view}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def history(client, vendor, accounts):
    """Bills, partial/full/advance payments, credit apply+unapply, reversal, discount, correction."""
    policy = client.post(f"{API}/discount-policies", json={"code": "2/10", "description": "2/10", "discount_percent": "2.00", "discount_days": 10,
                                                            "discount_account_id": accounts["discount"]}).json()
    assert client.patch(f"{API}/vendors/{vendor['id']}", json={"discount_policy_id": policy["id"]}).status_code == 200
    b1 = _bill(client, vendor, accounts, "B1", "1000.00", "2026-01-05")   # discount 20 until 2026-01-15
    b2 = _bill(client, vendor, accounts, "B2", "400.00", "2026-01-06")
    b3 = _bill(client, vendor, accounts, "B3", "250.00", "2026-02-03")
    p1 = _pay(client, vendor, accounts, [{"bill_id": b1["id"], "amount": "980.00", "take_discount": True}], "980.00", "2026-01-12", "TRF-1")
    credit = _credit(client, vendor, accounts, "CN-1", "100.00", "2026-01-14")
    applied = client.post(f"{API}/credits/{credit['id']}/applications", json={"bill_id": b2["id"], "amount": "100.00", "actor": "bob", "application_date": "2026-01-20"}).json()
    adv = _pay(client, vendor, accounts, [], "300.00", "2026-01-25", "ADV")
    assert client.post(f"{API}/payments/{adv['id']}/applications", json={"bill_id": b2["id"], "amount": "200.00", "actor": "bob", "allocation_date": "2026-02-05"}).status_code == 200
    # March: unapply the January credit, reverse the January discounted payment, correct the advance.
    assert client.post(f"{API}/credits/{credit['id']}/applications/{applied['applications'][0]['id']}/unapply",
                       json={"actor": "carol", "reason": "wrong bill", "unapplication_date": "2026-03-02"}).status_code == 200
    assert client.post(f"{API}/payments/{p1['id']}/reverse", json={"actor": "carol", "reason": "bounced", "reversal_date": "2026-03-03"}).status_code == 200
    return {"b1": b1, "b2": b2, "b3": b3, "p1": p1, "credit": credit, "adv": adv}


# --- reconstruction ---------------------------------------------------------------------------------


def test_independent_replay_matches_production_reports_at_every_date(client, vendor, accounts, history):
    for as_of in ("2026-01-10", "2026-01-13", "2026-01-21", "2026-01-31", "2026-02-10", "2026-03-01", "2026-03-03", "2026-03-31"):
        result = _recon(client, as_of)
        assert result["findings"] == [], (as_of, result["findings"])
        assert result["limitations"][0].startswith("vendor refunds")
    jan = _recon(client, "2026-01-31")
    b1, b2, b3 = history["b1"]["id"], history["b2"]["id"], history["b3"]["id"]
    assert jan["bill_outstanding"] == {str(b1): "0.00", str(b2): "300.00"}  # b3 is February
    assert jan["vendor_bills"][str(vendor["id"])] == "300.00"
    assert jan["vendor_credits"][str(vendor["id"])] == "0.00" and jan["vendor_advances"][str(vendor["id"])] == "300.00"
    assert jan["control_balance"][str(accounts["ap"])] == "0.00" == jan["ledger_balance"][str(accounts["ap"])]
    assert _recon(client, "2026-02-28")["bill_outstanding"] == {str(b1): "0.00", str(b2): "100.00", str(b3): "250.00"}
    march = _recon(client, "2026-03-31")
    assert march["bill_outstanding"] == {str(b1): "1000.00", str(b2): "200.00", str(b3): "250.00"}
    assert march["vendor_credits"][str(vendor["id"])] == "100.00"


def test_january_view_is_stable_under_march_corrections_unless_restated(client, vendor, accounts, history):
    jan_as_of = _recon(client, "2026-01-31")
    jan_restated = _recon(client, "2026-01-31", view="restated")
    b1, b2 = history["b1"]["id"], history["b2"]["id"]
    # As of January: credit applied, discounted payment in force.
    assert jan_as_of["bill_outstanding"][str(b1)] == "0.00" and jan_as_of["bill_outstanding"][str(b2)] == "300.00"
    # Restated with March knowledge: the reversed payment and the unapplied credit never count.
    assert jan_restated["bill_outstanding"][str(b1)] == "1000.00" and jan_restated["bill_outstanding"][str(b2)] == "400.00"
    assert jan_restated["vendor_credits"][str(vendor["id"])] == "100.00"
    assert jan_restated["findings"] == [] and "restated" in jan_restated["limitations"][-1]
    # Further (future) events do not move the as-of January view.
    assert client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-04-01", "amount": "50.00", "cash_account_id": accounts["cash"],
                                                "recorded_by": "bob", "allocations": [{"bill_id": b2, "amount": "50.00"}]}).status_code == 201
    assert _recon(client, "2026-01-31")["bill_outstanding"] == jan_as_of["bill_outstanding"]
    assert client.get(f"{API}/reconstruction?as_of=2026-01-31&view=bogus").status_code == 422


def test_findings_name_the_discrepancy_when_the_ledger_is_touched_outside_ap(client, vendor, accounts, history):
    manual = client.post("/api/v1/journal-entries", json={"entry_date": "2026-01-20", "description": "manual", "reference": "MAN",
                                                           "lines": [{"account_id": accounts["expense"], "debit": "7.00"}, {"account_id": accounts["ap"], "credit": "7.00"}]}).json()
    assert client.post(f"/api/v1/journal-entries/{manual['id']}/post").status_code == 200
    result = _recon(client, "2026-01-31")
    findings = {f["check"]: f for f in result["findings"]}
    assert set(findings) == {"gl_control"}
    f = findings["gl_control"]
    assert (f["transaction"], f["expected"], f["actual"], f["difference"]) == ("account:2000", "0.00", "7.00", "7.00")
    assert "manual entries" in f["cause"] and f["as_of"] == "2026-01-31"
    assert _recon(client, "2026-01-19")["findings"] == []


def test_findings_for_tampered_run_settlement_and_closed_period_attestation(client, vendor, accounts, history):
    # Close January, then the attestation must keep matching the replay; a later backdated raw change is caught.
    assert client.post(f"{API}/period-end/close", json={"period_end": "2026-01-31", "actor": "cfo"}).status_code == 201
    assert [f["check"] for f in _recon(client, "2026-03-31")["findings"]] == []
    with Session(client.app.state.engine) as s:
        # Tamper with an attestation is impossible (append-only); tamper the replay input instead: a bill
        # total cannot change either (frozen). So simulate a data-integrity incident on the attestation
        # side by inserting a fake attestation row with a wrong liability.
        s.execute(text("INSERT INTO ap_close_attestations (period_end, outcome, attempted_by, forced, liability_balance, open_bill_count, "
                       "vendor_count, checks_json, exceptions_json, reconciliation_json, source_fingerprint) "
                       "VALUES ('2026-01-31', 'closed', 'x', false, 999, 1, 1, '[]', '[]', '{}', 'deadbeef')"))
        s.commit()
    findings = _recon(client, "2026-03-31")["findings"]
    assert [f["check"] for f in findings] == ["closed_period_attestation"]
    assert findings[0]["expected"] == "999.00" and findings[0]["actual"] == "300.00"


# --- supplier statement import ------------------------------------------------------------------------


CSV = """kind,reference,date,amount
invoice,B1,2026-01-05,1000.00
invoice,b2,2026-01-06,400.00
invoice,B3,2026-02-03,250.00
invoice,B-9,2026-02-10,75.00
credit,CN-1,2026-01-14,100.00
payment,TRF-1,2026-01-12,980.00
payment,ADV,2026-02-02,300.00
"""


def test_preview_validation_rejects_bad_files(client, vendor, history):
    def preview(content=None, content_base64=None, name="s.csv"):
        body = {"actor": "fin", "file_name": name, "statement_date": "2026-02-28"}
        if content is not None:
            body["content"] = content
        if content_base64 is not None:
            body["content_base64"] = content_base64
        return client.post(f"{API}/vendors/{vendor['id']}/statement-imports/preview", json=body)

    assert preview("reference,amount\nx,1\n").status_code == 422 and "missing columns" in preview("reference,amount\nx,1\n").json()["detail"]
    assert "not ISO" in preview("kind,reference,date,amount\ninvoice,B1,05/01/2026,10\n").json()["detail"]
    assert "unsupported precision" in preview("kind,reference,date,amount\ninvoice,B1,2026-01-05,10.005\n").json()["detail"]
    assert "not a number" in preview("kind,reference,date,amount\ninvoice,B1,2026-01-05,ten\n").json()["detail"]
    assert "positive" in preview("kind,reference,date,amount\ninvoice,B1,2026-01-05,-5\n").json()["detail"]
    assert "duplicate of row" in preview("kind,reference,date,amount\ninvoice,B1,2026-01-05,10\ninvoice,B1,2026-01-05,10\n").json()["detail"]
    assert "must be invoice, credit or payment" in preview("kind,reference,date,amount\nrefund,B1,2026-01-05,10\n").json()["detail"]
    assert "no statement lines" in preview("kind,reference,date,amount\n").json()["detail"]
    latin1 = base64.b64encode("kind,reference,date,amount\ninvoice,B\xe9,2026-01-05,10\n".encode("latin-1")).decode()
    assert "unsupported encoding" in preview(content_base64=latin1).json()["detail"]
    binary = base64.b64encode(b"kind,reference,date,amount\n\x00\x01").decode()
    assert "binary content" in preview(content_base64=binary).json()["detail"]
    huge = "kind,reference,date,amount\n" + "\n".join(f"invoice,X{i},2026-01-05,1" for i in range(5001))
    assert "more than 5000 rows" in preview(huge).json()["detail"]
    too_big = "kind,reference,date,amount\n" + ("invoice,X,2026-01-05,1\n" * 60000)
    assert "file too large" in preview(too_big).json()["detail"]
    assert preview(content="x", content_base64="eA==").status_code == 422  # exactly one of the two
    assert client.post(f"{API}/vendors/999/statement-imports/preview", json={"actor": "fin", "file_name": "s.csv", "statement_date": "2026-02-28", "content": CSV}).status_code == 404
    assert _count(client, SupplierStatementImport) == 0


def _count(client, model):
    with Session(client.app.state.engine) as s:
        return s.scalar(select(func.count()).select_from(model))


def test_preview_and_import_match_deterministically_and_explain(client, vendor, accounts, history):
    preview = client.post(f"{API}/vendors/{vendor['id']}/statement-imports/preview",
                          json={"actor": "fin", "file_name": "acme-feb.csv", "statement_date": "2026-02-28", "content": CSV})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["already_imported_as"] is None
    by_ref = {line["reference"]: line for line in body["lines"]}
    assert by_ref["B1"]["match_status"] == "matched" and by_ref["B1"]["matched_type"] == "bill"
    assert by_ref["b2"]["match_status"] == "matched"  # normalised reference
    assert by_ref["B3"]["match_status"] == "matched"
    assert by_ref["B-9"]["match_status"] == "unmatched" and "invoice we do not have" in by_ref["B-9"]["explanation"]
    assert by_ref["CN-1"]["match_status"] == "matched" and by_ref["CN-1"]["matched_type"] == "credit"
    assert by_ref["TRF-1"]["match_status"] == "matched" and by_ref["TRF-1"]["matched_type"] == "payment"
    assert by_ref["ADV"]["match_status"] == "timing_difference" and "dates differ by 8 days" in by_ref["ADV"]["explanation"]
    assert body["summary"]["counts"] == {"matched": 5, "near_match": 0, "amount_discrepancy": 0, "timing_difference": 1, "unmatched": 1}
    assert body["summary"]["missing_payments"] == []  # every internal payment in the window appears on the statement
    entries_before = _count(client, JournalEntry)

    imported = client.post(f"{API}/vendors/{vendor['id']}/statement-imports",
                           json={"actor": "fin", "file_name": "acme-feb.csv", "statement_date": "2026-02-28", "content": CSV})
    assert imported.status_code == 201, imported.text
    record = imported.json()
    assert record["line_count"] == 7 and record["content_hash"] == body["content_hash"]
    assert [l["review_status"] for l in record["lines"] if l["match_status"] == "matched"] == ["resolved"] * 5
    open_lines = [l for l in record["lines"] if l["review_status"] == "open"]
    assert {l["reference"] for l in open_lines} == {"B-9", "ADV"}
    assert _count(client, JournalEntry) == entries_before  # nothing posted from a statement
    duplicate = client.post(f"{API}/vendors/{vendor['id']}/statement-imports",
                            json={"actor": "fin", "file_name": "again.csv", "statement_date": "2026-02-28", "content": CSV})
    assert duplicate.status_code == 409
    assert client.post(f"{API}/vendors/{vendor['id']}/statement-imports/preview",
                       json={"actor": "fin", "file_name": "x", "statement_date": "2026-02-28", "content": CSV}).json()["already_imported_as"] == record["id"]
    # Review resolution.
    b9 = [l for l in open_lines if l["reference"] == "B-9"][0]
    resolved = client.post(f"{API}/statement-imports/{record['id']}/lines/{b9['id']}/resolve", json={"actor": "fin", "note": "requested a copy of B-9"})
    assert resolved.status_code == 200
    line = [l for l in resolved.json()["lines"] if l["id"] == b9["id"]][0]
    assert (line["review_status"], line["resolution_note"], line["reviewed_by"]) == ("resolved", "requested a copy of B-9", "fin")
    assert client.get(f"{API}/statement-imports?vendor_id={vendor['id']}").json()[0]["id"] == record["id"]
    assert client.get(f"{API}/statement-imports/{record['id']}").status_code == 200
    assert _count(client, SupplierStatementLine) == 7


def test_amount_discrepancy_near_match_and_missing_payment(client, vendor, accounts, history):
    csv = ("kind,reference,date,amount\n"
           "invoice,B1,2026-01-05,1100.00\n"      # amount discrepancy
           "invoice,B 2,2026-01-06,400.00\n"      # near match: compact reference
           "invoice,B7,2026-02-03,250.00\n"       # near match: one edit away from B3, same amount & date
           "credit,CN-1,2026-01-14,100.00\n")
    body = client.post(f"{API}/vendors/{vendor['id']}/statement-imports/preview",
                       json={"actor": "fin", "file_name": "s.csv", "statement_date": "2026-02-28", "content": csv}).json()
    by_ref = {line["reference"]: line for line in body["lines"]}
    assert by_ref["B1"]["match_status"] == "amount_discrepancy" and "statement 1100.00 vs internal 1000.00" in by_ref["B1"]["explanation"]
    assert by_ref["B 2"]["match_status"] == "near_match" and "punctuation and spaces" in by_ref["B 2"]["explanation"]
    assert by_ref["B7"]["match_status"] == "near_match" and "one character away" in by_ref["B7"]["explanation"]
    assert body["summary"]["amount_discrepancy_total"] == "100.00"
    missing = {m["reference"] for m in body["summary"]["missing_payments"]}
    assert missing == {"TRF-1", "ADV"}  # internal payments in the window absent from the statement
