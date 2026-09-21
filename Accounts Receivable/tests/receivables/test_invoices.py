"""Invoice creation with line items, totals, tax, lifecycle and ledger integration."""

from decimal import Decimal

from tests.receivables.conftest import API, D


# --- Creation and totals --------------------------------------------------------------


def test_create_invoice_computes_totals_from_lines(ar):
    inv = ar.invoice(
        lines=[
            ar.line("Widget", quantity="3", unit_price="33.333", tax=True),  # 99.999 -> 100.00 net
            ar.line("Service", quantity="1", unit_price="50.00"),
            ar.line("Free item", quantity="1", unit_price="0"),
        ]
    )
    assert inv["status"] == "draft"
    assert [l["net_amount"] for l in inv["lines"]] == ["100.00", "50.00", "0.00"]
    assert [l["tax_amount"] for l in inv["lines"]] == ["20.00", "0.00", "0.00"]
    assert [l["tax_rate"] for l in inv["lines"]] == ["0.200000", None, None]
    assert [l["line_no"] for l in inv["lines"]] == [1, 2, 3]
    assert (inv["subtotal"], inv["tax_total"], inv["total"]) == ("150.00", "20.00", "170.00")
    assert inv["amount_settled"] == "0.00" and inv["balance_due"] == "170.00"
    assert inv["journal_entry_id"] is None


def test_tax_is_rounded_half_up_per_line(ar):
    tax = ar.tax("T7", "0.070000")
    line = ar.line("x", quantity="1", unit_price="10.07") | {"tax_code_id": tax["id"]}
    inv = ar.invoice(lines=[line])
    # 10.07 * 0.07 = 0.7049 -> 0.70 ; 0.705 would round to 0.71
    assert inv["lines"][0]["tax_amount"] == "0.70"
    line2 = ar.line("y", quantity="1", unit_price="10.075") | {"tax_code_id": tax["id"]}
    inv2 = ar.invoice("INV-2", lines=[line2])
    assert inv2["lines"][0]["net_amount"] == "10.08"  # half-up on the net as well
    assert inv2["lines"][0]["tax_amount"] == "0.71"


def test_totals_are_exact_decimal_sums(ar):
    # 0.10 + 0.20 is not 0.30 in binary floating point.
    inv = ar.invoice(lines=[ar.line("a", "1", "0.10"), ar.line("b", "1", "0.20")])
    assert inv["total"] == "0.30"
    cents = ar.invoice("INV-2", lines=[ar.line(f"c{i}", "1", "0.01") for i in range(100)])
    assert cents["total"] == "1.00"


def test_client_supplied_total_is_rejected(ar):
    """``total`` is computed; a client still sending it gets a validation error, not silence."""
    ar.invoice(expect=422, total="999.00")


def test_invoice_validation(ar):
    r = ar.c.post(f"{API}/receivables/invoices", json={"customer_id": ar.customer["id"], "invoice_number": "X",
                                                        "invoice_date": "2026-01-10", "due_date": "2026-02-09", "lines": []})
    assert r.status_code == 422
    ar.invoice(due_date="2026-01-01", expect=422)  # due before invoice date
    ar.invoice(customer_id=999, expect=404)
    ar.invoice(lines=[ar.line(quantity="0")], expect=422)
    ar.invoice(lines=[ar.line(unit_price="-1")], expect=422)
    ar.invoice(lines=[ar.line(account=9999)], expect=422)
    ar.invoice(lines=[ar.line() | {"tax_code_id": 9999}], expect=422)
    ar.invoice("INV-1")
    ar.invoice("INV-1", expect=409)  # same number for the same customer
    other = ar.add_customer("C2", "Other")
    ar.invoice("INV-1", customer_id=other["id"])  # numbers are per customer


def test_inactive_revenue_account_or_tax_code_rejected(ar):
    ar.c.post(f"{API}/accounts/{ar.accounts['4000']}/deactivate")
    r = ar.c.post(f"{API}/receivables/invoices", json={
        "customer_id": ar.customer["id"], "invoice_number": "I", "invoice_date": "2026-01-10",
        "due_date": "2026-02-09", "lines": [ar.line()]})
    assert r.status_code == 422 and "inactive" in r.json()["detail"]


def test_list_and_filter_invoices(ar):
    a = ar.invoice("A")
    b = ar.issued("B")
    assert [i["invoice_number"] for i in ar.c.get(f"{API}/receivables/invoices").json()] == ["A", "B"]
    assert [i["id"] for i in ar.c.get(f"{API}/receivables/invoices", params={"status": "draft"}).json()] == [a["id"]]
    assert [i["id"] for i in ar.c.get(f"{API}/receivables/invoices", params={"open_only": True}).json()] == [b["id"]]
    assert ar.c.get(f"{API}/receivables/invoices/999").status_code == 404


# --- Draft editing --------------------------------------------------------------------


def test_draft_can_be_updated_and_totals_recomputed(ar):
    inv = ar.invoice(lines=[ar.line("a", "1", "10.00")])
    r = ar.c.put(f"{API}/receivables/invoices/{inv['id']}", json={
        "invoice_number": "INV-1b", "invoice_date": "2026-01-11", "due_date": "2026-02-10",
        "lines": [ar.line("b", "2", "25.00", tax=True)]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice_number"] == "INV-1b"
    assert [l["description"] for l in body["lines"]] == ["b"]
    assert (body["subtotal"], body["tax_total"], body["total"]) == ("50.00", "10.00", "60.00")


def test_issued_invoice_cannot_be_edited(ar):
    inv = ar.issued()
    r = ar.c.put(f"{API}/receivables/invoices/{inv['id']}", json={
        "invoice_number": "INV-1", "invoice_date": "2026-01-10", "due_date": "2026-02-09",
        "lines": [ar.line("b", "1", "1.00")]})
    assert r.status_code == 409
    assert ar.get_invoice(inv["id"])["total"] == "100.00"


# --- Issuing and the ledger ------------------------------------------------------------


def test_draft_does_not_touch_the_ledger(ar):
    ar.invoice(lines=[ar.line(tax=True)])
    assert ar.journal_entries() == []
    assert ar.trial_balance() == {}


def test_issue_posts_receivable_revenue_and_tax(ar):
    inv = ar.invoice(lines=[ar.line("Widget", "2", "100.00", tax=True), ar.line("Service", "1", "50.00")])
    issued = ar.issue(inv["id"])
    assert issued["status"] == "issued"
    assert issued["journal_entry_id"] is not None

    entry = ar.c.get(f"{API}/journal-entries/{issued['journal_entry_id']}").json()
    assert entry["status"] == "posted"
    assert entry["entry_date"] == "2026-01-10"
    by_account = {}
    for line in entry["lines"]:
        d, c = by_account.get(line["account_id"], (D(0), D(0)))
        by_account[line["account_id"]] = (d + D(line["debit"]), c + D(line["credit"]))
    assert by_account[ar.accounts["1200"]] == (D("290.00"), D(0))
    assert by_account[ar.accounts["4000"]] == (D(0), D("250.00"))
    assert by_account[ar.accounts["2200"]] == (D(0), D("40.00"))

    assert ar.ledger("1200") == D("290.00")
    assert ar.ledger("4000") == D("-250.00")
    assert ar.ledger("2200") == D("-40.00")
    assert ar.balance()["balance"] == "290.00"


def test_issue_requires_settings(ar, client):
    """Without configured ledger accounts nothing is posted and the draft is unchanged."""
    app = client.app
    with app.state.session_factory() as db:
        from app.modules.receivables.models import ReceivableSettings
        db.delete(db.get(ReceivableSettings, 1))
        db.commit()
    inv = ar.invoice()
    r = ar.c.post(f"{API}/receivables/invoices/{inv['id']}/issue")
    assert r.status_code == 422 and "not configured" in r.json()["detail"]
    assert ar.get_invoice(inv["id"])["status"] == "draft"
    assert ar.journal_entries() == []


def test_issue_twice_is_conflict(ar):
    inv = ar.issued()
    ar.issue(inv["id"], expect=409)
    assert len(ar.journal_entries()) == 1


def test_zero_total_invoice_cannot_be_issued(ar):
    inv = ar.invoice(lines=[ar.line(unit_price="0")])
    r = ar.c.post(f"{API}/receivables/invoices/{inv['id']}/issue")
    assert r.status_code == 422
    assert ar.get_invoice(inv["id"])["status"] == "draft"


def test_issue_fails_atomically_when_posting_is_refused(ar):
    """An inactive revenue account makes posting fail: invoice stays draft, no entry survives."""
    inv = ar.invoice()
    ar.c.post(f"{API}/accounts/{ar.accounts['4000']}/deactivate")
    r = ar.c.post(f"{API}/receivables/invoices/{inv['id']}/issue")
    assert r.status_code == 422, r.text
    assert "inactive accounts: 4000" in r.json()["detail"]
    fresh = ar.get_invoice(inv["id"])
    assert fresh["status"] == "draft" and fresh["journal_entry_id"] is None
    assert ar.journal_entries() == []
    assert ar.trial_balance() == {}

    ar.c.post(f"{API}/accounts/{ar.accounts['4000']}/activate")
    assert ar.issue(inv["id"])["status"] == "issued"


def test_tax_rate_is_snapshotted_on_the_line(ar, client):
    inv = ar.invoice(lines=[ar.line(tax=True)])
    with client.app.state.session_factory() as db:
        from app.modules.tax.models import TaxCode
        code = db.get(TaxCode, ar.tax_code["id"])
        code.rate = Decimal("0.5")
        db.commit()
    issued = ar.issue(inv["id"])
    assert issued["tax_total"] == "20.00"  # rate at the time the line was saved
    assert ar.ledger("2200") == D("-20.00")


# --- Void -----------------------------------------------------------------------------


def test_void_draft_has_no_ledger_effect(ar):
    inv = ar.invoice()
    voided = ar.void(inv["id"], void_date="2026-01-15")
    assert voided["status"] == "void" and voided["void_date"] == "2026-01-15"
    assert voided["journal_entry_id"] is None and voided["void_journal_entry_id"] is None
    assert ar.journal_entries() == []
    ar.void(inv["id"], expect=409)
    ar.issue(inv["id"], expect=409)


def test_void_issued_invoice_posts_reversal_and_keeps_history(ar):
    inv = ar.issued(lines=[ar.line("Widget", "1", "100.00", tax=True)])
    assert ar.ledger("1200") == D("120.00")
    voided = ar.void(inv["id"], void_date="2026-01-20", reason="duplicate")
    assert voided["status"] == "void"
    assert voided["void_journal_entry_id"] not in (None, voided["journal_entry_id"])

    entries = ar.journal_entries()
    assert [e["status"] for e in entries] == ["posted", "posted"]  # original untouched, reversal added
    reversal = ar.c.get(f"{API}/journal-entries/{voided['void_journal_entry_id']}").json()
    assert reversal["entry_date"] == "2026-01-20"
    assert "duplicate" in reversal["description"]
    assert ar.ledger("1200") == D("0") and ar.ledger("4000") == D("0") and ar.ledger("2200") == D("0")
    assert ar.balance()["balance"] == "0.00"
    assert ar.balance(as_of="2026-01-15")["balance"] == "120.00"  # it was outstanding then


def test_void_date_cannot_precede_invoice_date(ar):
    inv = ar.issued()
    ar.void(inv["id"], expect=422, void_date="2026-01-01")
    assert ar.get_invoice(inv["id"])["status"] == "issued"


def test_invoice_with_settlements_cannot_be_voided(ar):
    inv = ar.issued()
    ar.payment("40.00", [(inv["id"], "40.00")])
    r = ar.c.post(f"{API}/receivables/invoices/{inv['id']}/void")
    assert r.status_code == 409
    assert "credit note" in r.json()["detail"]
    paid = ar.issued("INV-2")
    ar.payment("100.00", [(paid["id"], "100.00")], key="k2")
    ar.void(paid["id"], expect=409)
