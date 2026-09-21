"""Credit adjustments (credit notes) and write-offs."""

from tests.receivables.conftest import API, D


def test_write_off_reduces_invoice_and_posts_bad_debt(ar):
    inv = ar.issued()  # 100.00
    ar.payment("95.00", [(inv["id"], "95.00")])
    adj = ar.adjustment("write_off", "5.00", invoice_id=inv["id"], reason="small balance", adjustment_date="2026-03-01")
    assert adj["kind"] == "write_off" and adj["amount_allocated"] == "5.00" and adj["unallocated_amount"] == "0.00"
    assert [(a["invoice_id"], a["amount"], a["adjustment_id"]) for a in adj["allocations"]] == [(inv["id"], "5.00", adj["id"])]

    fresh = ar.get_invoice(inv["id"])
    assert fresh["status"] == "paid" and fresh["balance_due"] == "0.00"
    entry = ar.c.get(f"{API}/journal-entries/{adj['journal_entry_id']}").json()
    assert entry["status"] == "posted" and entry["entry_date"] == "2026-03-01"
    assert {(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]} == {
        (ar.accounts["6100"], "5.00", "0.00"), (ar.accounts["1200"], "0.00", "5.00")}
    assert ar.ledger("6100") == D("5.00") and ar.ledger("1200") == D("0")


def test_partial_write_off_keeps_invoice_open(ar):
    inv = ar.issued()
    ar.adjustment("write_off", "30.00", invoice_id=inv["id"])
    fresh = ar.get_invoice(inv["id"])
    assert fresh["status"] == "partially_paid" and fresh["balance_due"] == "70.00"


def test_write_off_limits(ar):
    inv = ar.issued()
    r = ar.c.post(f"{API}/receivables/adjustments", json={
        "customer_id": ar.customer["id"], "kind": "write_off", "adjustment_date": "2026-02-01",
        "amount": "100.01", "reason": "too much", "invoice_id": inv["id"]})
    assert r.status_code == 422 and "exceeds the outstanding balance" in r.json()["detail"]
    ar.adjustment("write_off", "1.00", reason="no invoice", expect=422)
    draft = ar.invoice("D")
    ar.adjustment("write_off", "1.00", invoice_id=draft["id"], expect=409)
    other = ar.add_customer("C2", "Other")
    ar.adjustment("write_off", "1.00", invoice_id=inv["id"], customer_id=other["id"], expect=422)
    assert ar.c.get(f"{API}/receivables/adjustments").json() == []
    assert len(ar.journal_entries()) == 1
    assert ar.get_invoice(inv["id"])["amount_settled"] == "0.00"
    # a write-off cannot be re-allocated
    wo = ar.adjustment("write_off", "1.00", invoice_id=inv["id"])
    ar.allocate(wo["id"], [(inv["id"], "1.00")], kind="adjustments", expect=409)


def test_credit_note_against_invoice(ar):
    inv = ar.issued(lines=[ar.line("w", "1", "100.00", tax=True)])  # 120
    adj = ar.adjustment("credit", "20.00", invoice_id=inv["id"], reason="damaged goods")
    assert adj["amount_allocated"] == "20.00"
    fresh = ar.get_invoice(inv["id"])
    assert fresh["status"] == "partially_paid" and fresh["balance_due"] == "100.00"
    assert ar.ledger("4900") == D("20.00") and ar.ledger("1200") == D("100.00")


def test_credit_note_on_account_then_allocated(ar):
    credit = ar.adjustment("credit", "50.00", reason="goodwill")
    assert credit["amount_allocated"] == "0.00" and credit["unallocated_amount"] == "50.00"
    bal = ar.balance()
    assert bal["unapplied_credit"] == "50.00" and bal["balance"] == "-50.00"

    a = ar.issued("A", lines=[ar.line("a", "1", "30.00")])
    b = ar.issued("B", lines=[ar.line("b", "1", "30.00")])
    after = ar.allocate(credit["id"], [(a["id"], "30.00"), (b["id"], "20.00")], key="c-1", kind="adjustments")
    assert after["amount_allocated"] == "50.00"
    assert ar.get_invoice(a["id"])["status"] == "paid"
    assert ar.get_invoice(b["id"])["balance_due"] == "10.00"
    # replay is harmless
    replay = ar.allocate(credit["id"], [(a["id"], "30.00"), (b["id"], "20.00")], key="c-1", kind="adjustments", expect=200)
    assert replay["amount_allocated"] == "50.00"
    ar.allocate(credit["id"], [(b["id"], "0.01")], kind="adjustments", expect=422)
    assert ar.ledger("1200") == D("10.00") and ar.balance()["balance"] == "10.00"


def test_credit_note_larger_than_invoice_balance_rejected(ar):
    inv = ar.issued()
    r = ar.c.post(f"{API}/receivables/adjustments", json={
        "customer_id": ar.customer["id"], "kind": "credit", "adjustment_date": "2026-02-01",
        "amount": "100.01", "reason": "x", "invoice_id": inv["id"]})
    assert r.status_code == 422
    paid = ar.issued("P")
    ar.payment("100.00", [(paid["id"], "100.00")])
    ar.adjustment("credit", "1.00", invoice_id=paid["id"], expect=409)  # already paid: use on-account credit
    assert ar.c.get(f"{API}/receivables/adjustments").json() == []


def test_adjustment_idempotency(ar):
    inv = ar.issued()
    first = ar.adjustment("credit", "10.00", invoice_id=inv["id"], key="cn-1")
    again = ar.adjustment("credit", "10.00", invoice_id=inv["id"], key="cn-1", expect=200)
    assert again["id"] == first["id"]
    assert ar.get_invoice(inv["id"])["amount_settled"] == "10.00"
    ar.adjustment("credit", "11.00", invoice_id=inv["id"], key="cn-1", expect=409)
    assert len(ar.c.get(f"{API}/receivables/adjustments").json()) == 1
    assert ar.c.get(f"{API}/receivables/adjustments/{first['id']}").json()["reason"] == "adjustment"
    assert ar.c.get(f"{API}/receivables/adjustments/999").status_code == 404


def test_adjustment_fails_atomically_when_posting_is_refused(ar):
    inv = ar.issued()
    ar.c.post(f"{API}/accounts/{ar.accounts['6100']}/deactivate")
    ar.adjustment("write_off", "5.00", invoice_id=inv["id"], expect=422)
    assert ar.c.get(f"{API}/receivables/adjustments").json() == []
    assert ar.get_invoice(inv["id"])["amount_settled"] == "0.00"
    assert len(ar.journal_entries()) == 1


def test_settings_validation(ar):
    ar.put_settings(expect=422, receivable_account_id=ar.accounts["4000"])  # must be an asset
    ar.put_settings(expect=422, tax_account_id=ar.accounts["1000"])        # must be a liability
    ar.put_settings(expect=422, write_off_account_id=ar.accounts["4000"])  # must be an expense
    ar.put_settings(expect=422, credit_account_id=9999)
    ar.c.post(f"{API}/accounts/{ar.accounts['6100']}/deactivate")
    ar.put_settings(expect=422)
    assert ar.c.get(f"{API}/receivables/settings").json()["write_off_account_id"] == ar.accounts["6100"]
