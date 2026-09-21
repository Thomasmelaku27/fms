"""Customer statements, balances, aging and overdue reports."""

from tests.receivables.conftest import API


def _scenario(ar):
    """Two customers, several invoices at different ages, payments and adjustments."""
    c2 = ar.add_customer("C2", "Beta Inc")
    a = ar.issued("A", lines=[ar.line("a", "1", "100.00", tax=True)], invoice_date="2025-10-01", due_date="2025-10-31")  # 120
    b = ar.issued("B", lines=[ar.line("b", "1", "200.00")], invoice_date="2025-12-01", due_date="2025-12-31")
    c = ar.issued("C", lines=[ar.line("c", "1", "50.00")], invoice_date="2026-01-10", due_date="2026-02-09")
    d = ar.issued("D", lines=[ar.line("d", "1", "75.00")], invoice_date="2026-01-15", due_date="2026-03-01")
    ar.invoice("DRAFT", lines=[ar.line("x", "1", "999.00")])
    voided = ar.issued("V", lines=[ar.line("v", "1", "10.00")], invoice_date="2026-01-05", due_date="2026-01-20")
    ar.void(voided["id"], void_date="2026-01-12")
    ar.payment("120.00", [(a["id"], "120.00")], key="p1", payment_date="2025-11-15")
    ar.payment("150.00", [(b["id"], "100.00")], key="p2", payment_date="2026-01-20")  # 50 on account
    ar.adjustment("credit", "10.00", invoice_id=c["id"], adjustment_date="2026-01-25")
    e = ar.issued("E", lines=[ar.line("e", "1", "300.00")], customer_id=c2["id"], invoice_date="2025-09-01", due_date="2025-09-15")
    ar.adjustment("write_off", "300.00", invoice_id=e["id"], customer_id=c2["id"], adjustment_date="2026-02-15")
    f = ar.issued("F", lines=[ar.line("f", "1", "40.00")], customer_id=c2["id"], invoice_date="2026-01-20", due_date="2026-02-19")
    return {"c2": c2, "a": a, "b": b, "c": c, "d": d, "e": e, "f": f}


def test_customer_statement_with_running_balance(ar):
    _scenario(ar)
    r = ar.c.get(f"{API}/receivables/customers/{ar.customer['id']}/statement", params={"end_date": "2026-03-31"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["opening_balance"] == "0.00" and st["start_date"] is None
    rows = [(l["entry_date"], l["kind"], l["reference"], l["debit"], l["credit"], l["running_balance"]) for l in st["lines"]]
    assert rows == [
        ("2025-10-01", "invoice", "A", "120.00", "0.00", "120.00"),
        ("2025-11-15", "payment", "PAY-1", "0.00", "120.00", "0.00"),
        ("2025-12-01", "invoice", "B", "200.00", "0.00", "200.00"),
        ("2026-01-05", "invoice", "V", "10.00", "0.00", "210.00"),
        ("2026-01-10", "invoice", "C", "50.00", "0.00", "260.00"),
        ("2026-01-12", "void", "V", "0.00", "10.00", "250.00"),
        ("2026-01-15", "invoice", "D", "75.00", "0.00", "325.00"),
        ("2026-01-20", "payment", "PAY-2", "0.00", "150.00", "175.00"),
        ("2026-01-25", "credit", "ADJ-1", "0.00", "10.00", "165.00"),
    ]
    assert st["total_debits"] == "455.00" and st["total_credits"] == "290.00" and st["closing_balance"] == "165.00"
    # the statement agrees with the balance report and the ledger
    assert ar.balance(as_of="2026-03-31")["balance"] == "165.00"


def test_statement_period_carries_an_opening_balance(ar):
    _scenario(ar)
    st = ar.c.get(f"{API}/receivables/customers/{ar.customer['id']}/statement",
                  params={"start_date": "2026-01-01", "end_date": "2026-01-31"}).json()
    assert st["opening_balance"] == "200.00"
    assert [l["reference"] for l in st["lines"]] == ["V", "C", "V", "D", "PAY-2", "ADJ-1"]
    assert st["lines"][0]["running_balance"] == "210.00"
    assert st["closing_balance"] == "165.00"
    empty = ar.c.get(f"{API}/receivables/customers/{ar.customer['id']}/statement",
                     params={"start_date": "2027-01-01", "end_date": "2027-01-31"}).json()
    assert empty["lines"] == [] and empty["opening_balance"] == empty["closing_balance"] == "165.00"
    assert ar.c.get(f"{API}/receivables/customers/{ar.customer['id']}/statement",
                    params={"start_date": "2026-02-01", "end_date": "2026-01-31"}).status_code == 422
    assert ar.c.get(f"{API}/receivables/customers/999/statement").status_code == 404


def test_customer_balance_as_of(ar):
    s = _scenario(ar)
    today = ar.balance(as_of="2026-03-31")
    assert today == {
        "customer_id": ar.customer["id"], "code": "C1", "name": "Acme Ltd", "as_of": "2026-03-31",
        "open_invoices": 3, "outstanding": "215.00", "overdue": "215.00", "unapplied_credit": "50.00", "balance": "165.00"}
    earlier = ar.balance(as_of="2026-01-11")
    # A paid, B open 200, V open 10 (voided on the 12th), C open 50; nothing unapplied yet
    assert earlier["open_invoices"] == 3 and earlier["outstanding"] == "260.00"
    assert earlier["overdue"] == "200.00" and earlier["balance"] == "260.00"
    assert ar.balance(customer_id=s["c2"]["id"], as_of="2026-02-14")["outstanding"] == "340.00"
    assert ar.balance(customer_id=s["c2"]["id"], as_of="2026-02-15")["outstanding"] == "40.00"
    assert ar.c.get(f"{API}/receivables/customers/999/balance").status_code == 404


def test_aging_report_buckets_by_days_overdue(ar):
    s = _scenario(ar)
    r = ar.c.get(f"{API}/receivables/reports/aging", params={"as_of": "2026-02-20"})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["bucket_days"] == [30, 60, 90]
    assert report["bucket_labels"] == ["1-30", "31-60", "61-90", "90+"]
    rows = {row["code"]: row for row in report["rows"]}
    # C1: B due 2025-12-31 -> 51 days (31-60) 100 left; C due 02-09 -> 11 days, 40 left; D not yet due 75
    assert rows["C1"]["current"] == "75.00"
    assert rows["C1"]["buckets"] == ["40.00", "100.00", "0.00", "0.00"]
    assert rows["C1"]["total_outstanding"] == "215.00"
    assert rows["C1"]["unapplied_credit"] == "50.00" and rows["C1"]["balance"] == "165.00"
    # C2: E written off on 02-15; F due 02-19 -> 1 day overdue
    assert rows["C2"]["buckets"] == ["40.00", "0.00", "0.00", "0.00"] and rows["C2"]["current"] == "0.00"
    assert report["totals"]["total_outstanding"] == "255.00" and report["totals"]["balance"] == "205.00"
    assert report["totals"]["buckets"] == ["80.00", "100.00", "0.00", "0.00"]

    # as of an earlier date the write-off has not happened and E is 5 months overdue
    past = ar.c.get(f"{API}/receivables/reports/aging", params={"as_of": "2026-02-01", "customer_id": s["c2"]["id"]}).json()
    assert [row["code"] for row in past["rows"]] == ["C2"]
    assert past["rows"][0]["buckets"] == ["0.00", "0.00", "0.00", "300.00"] and past["rows"][0]["current"] == "40.00"

    custom = ar.c.get(f"{API}/receivables/reports/aging", params={"as_of": "2026-02-20", "buckets": [15, 45]}).json()
    assert custom["bucket_labels"] == ["1-15", "16-45", "45+"]
    assert {row["code"]: row["buckets"] for row in custom["rows"]}["C1"] == ["40.00", "0.00", "100.00"]
    assert ar.c.get(f"{API}/receivables/reports/aging", params={"buckets": [60, 30]}).status_code == 422
    assert ar.c.get(f"{API}/receivables/reports/aging", params={"customer_id": 999}).status_code == 404


def test_overdue_report_lists_open_invoices_past_due(ar):
    s = _scenario(ar)
    report = ar.c.get(f"{API}/receivables/reports/overdue", params={"as_of": "2026-02-20"}).json()
    rows = [(i["invoice_number"], i["customer_code"], i["days_overdue"], i["balance_due"], i["status"]) for i in report["invoices"]]
    assert rows == [("B", "C1", 51, "100.00", "partially_paid"), ("C", "C1", 11, "40.00", "partially_paid"), ("F", "C2", 1, "40.00", "issued")]
    assert report["total_overdue"] == "180.00"
    on_due_date = ar.c.get(f"{API}/receivables/reports/overdue", params={"as_of": "2026-02-19"}).json()
    assert [i["invoice_number"] for i in on_due_date["invoices"]] == ["B", "C"]  # F is due today, not overdue
    mine = ar.c.get(f"{API}/receivables/reports/overdue", params={"as_of": "2026-02-20", "customer_id": s["c2"]["id"]}).json()
    assert [i["invoice_number"] for i in mine["invoices"]] == ["F"]


def test_reports_ignore_drafts_and_fully_settled_invoices(ar):
    ar.invoice("DRAFT", lines=[ar.line("x", "1", "999.00")], invoice_date="2025-01-01", due_date="2025-01-02")
    paid = ar.issued("P", invoice_date="2025-01-01", due_date="2025-01-02")
    ar.payment("100.00", [(paid["id"], "100.00")], payment_date="2025-01-03")
    assert ar.c.get(f"{API}/receivables/reports/aging").json()["rows"] == []
    assert ar.c.get(f"{API}/receivables/reports/overdue").json()["invoices"] == []
    bal = ar.balance()
    assert bal["open_invoices"] == 0 and bal["balance"] == "0.00"
