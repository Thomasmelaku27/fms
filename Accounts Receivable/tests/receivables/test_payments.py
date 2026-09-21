"""Customer payments: registration, allocation, partial payments, idempotency, ledger."""

from tests.receivables.conftest import API, D


def test_payment_allocated_to_one_invoice_in_full(ar):
    inv = ar.issued()
    pay = ar.payment("100.00", [(inv["id"], "100.00")], reference="CHQ-1")
    assert pay["amount"] == "100.00" and pay["amount_allocated"] == "100.00"
    assert pay["unallocated_amount"] == "0.00"
    assert [(a["invoice_id"], a["amount"]) for a in pay["allocations"]] == [(inv["id"], "100.00")]

    fresh = ar.get_invoice(inv["id"])
    assert fresh["status"] == "paid"
    assert fresh["amount_settled"] == "100.00" and fresh["balance_due"] == "0.00"
    assert ar.balance()["balance"] == "0.00"


def test_partial_payments_until_paid(ar):
    inv = ar.issued(lines=[ar.line("w", "1", "100.00")])
    ar.payment("33.33", [(inv["id"], "33.33")], key="p1")
    assert ar.get_invoice(inv["id"])["status"] == "partially_paid"
    ar.payment("33.33", [(inv["id"], "33.33")], key="p2")
    mid = ar.get_invoice(inv["id"])
    assert mid["status"] == "partially_paid" and mid["balance_due"] == "33.34"
    ar.payment("33.34", [(inv["id"], "33.34")], key="p3")
    done = ar.get_invoice(inv["id"])
    assert done["status"] == "paid" and done["amount_settled"] == "100.00" and done["balance_due"] == "0.00"
    assert ar.ledger("1200") == D("0") and ar.ledger("1000") == D("100.00")


def test_one_payment_across_several_invoices(ar):
    a = ar.issued("A", lines=[ar.line("a", "1", "40.00")])
    b = ar.issued("B", lines=[ar.line("b", "1", "60.00")])
    c = ar.issued("C", lines=[ar.line("c", "1", "10.00")])
    pay = ar.payment("120.00", [(a["id"], "40.00"), (b["id"], "50.00"), (c["id"], "10.00")])
    assert pay["amount_allocated"] == "100.00" and pay["unallocated_amount"] == "20.00"
    assert ar.get_invoice(a["id"])["status"] == "paid"
    assert ar.get_invoice(b["id"])["status"] == "partially_paid"
    assert ar.get_invoice(c["id"])["status"] == "paid"
    bal = ar.balance()
    assert bal["outstanding"] == "10.00" and bal["unapplied_credit"] == "20.00" and bal["balance"] == "-10.00"


def test_payment_on_account_then_allocated_later(ar):
    pay = ar.payment("150.00")  # nothing to allocate yet
    assert pay["unallocated_amount"] == "150.00"
    assert ar.ledger("1200") == D("-150.00")  # credit balance on the customer's account
    inv = ar.issued()
    assert ar.get_invoice(inv["id"])["status"] == "issued"  # on-account money is not auto-applied

    after = ar.allocate(pay["id"], [(inv["id"], "100.00")])
    assert after["amount_allocated"] == "100.00" and after["unallocated_amount"] == "50.00"
    assert ar.get_invoice(inv["id"])["status"] == "paid"
    assert ar.ledger("1200") == D("-50.00")  # allocation does not post anything new
    assert len(ar.journal_entries()) == 2  # invoice + payment


def test_cannot_allocate_more_than_the_invoice_balance(ar):
    inv = ar.issued()  # 100.00
    r = ar.c.post(f"{API}/receivables/payments", json={
        "customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "150.00",
        "deposit_account_id": ar.accounts["1000"], "idempotency_key": "k",
        "allocations": [{"invoice_id": inv["id"], "amount": "100.01"}]})
    assert r.status_code == 422, r.text
    assert "exceeds the outstanding balance 100.00" in r.json()["detail"]
    # nothing was recorded: no payment, no entry, invoice untouched
    assert ar.c.get(f"{API}/receivables/payments").json() == []
    assert len(ar.journal_entries()) == 1
    assert ar.get_invoice(inv["id"])["amount_settled"] == "0.00"

    ar.payment("60.00", [(inv["id"], "60.00")])
    r = ar.c.post(f"{API}/receivables/payments", json={
        "customer_id": ar.customer["id"], "payment_date": "2026-01-21", "amount": "50.00",
        "deposit_account_id": ar.accounts["1000"], "idempotency_key": "k2",
        "allocations": [{"invoice_id": inv["id"], "amount": "40.01"}]})
    assert r.status_code == 422
    assert ar.get_invoice(inv["id"])["balance_due"] == "40.00"


def test_cannot_allocate_more_than_the_payment(ar):
    a = ar.issued("A")
    b = ar.issued("B")
    r = ar.c.post(f"{API}/receivables/payments", json={
        "customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "100.00",
        "deposit_account_id": ar.accounts["1000"], "idempotency_key": "k",
        "allocations": [{"invoice_id": a["id"], "amount": "60.00"}, {"invoice_id": b["id"], "amount": "60.00"}]})
    assert r.status_code == 422 and "exceed the payment amount" in r.text

    pay = ar.payment("100.00", [(a["id"], "70.00")])
    r = ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations",
                  json={"allocations": [{"invoice_id": b["id"], "amount": "30.01"}]})
    assert r.status_code == 422 and "has only 30.00 unallocated" in r.json()["detail"]
    assert ar.get_invoice(b["id"])["amount_settled"] == "0.00"
    ar.allocate(pay["id"], [(b["id"], "30.00")])
    assert ar.c.get(f"{API}/receivables/payments/{pay['id']}").json()["unallocated_amount"] == "0.00"


def test_allocation_rejected_for_wrong_customer_draft_or_void_invoice(ar):
    other = ar.add_customer("C2", "Other")
    theirs = ar.issued("X", customer_id=other["id"])
    draft = ar.invoice("D")
    void = ar.void(ar.issued("V")["id"])
    pay = ar.payment("100.00")
    for invoice, status in ((theirs, 422), (draft, 409), (void, 409), ({"id": 9999}, 422)):
        r = ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations",
                      json={"allocations": [{"invoice_id": invoice["id"], "amount": "10.00"}]})
        assert r.status_code == status, (invoice, r.text)
    assert ar.c.get(f"{API}/receivables/payments/{pay['id']}").json()["amount_allocated"] == "0.00"
    assert ar.get_invoice(theirs["id"])["amount_settled"] == "0.00"


def test_duplicate_invoice_in_one_request_rejected(ar):
    inv = ar.issued()
    r = ar.c.post(f"{API}/receivables/payments", json={
        "customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "100.00",
        "deposit_account_id": ar.accounts["1000"], "idempotency_key": "k",
        "allocations": [{"invoice_id": inv["id"], "amount": "50.00"}, {"invoice_id": inv["id"], "amount": "50.00"}]})
    assert r.status_code == 422 and "more than once" in r.text


def test_payment_validation(ar):
    inv = ar.issued()
    base = {"customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "10.00",
            "deposit_account_id": ar.accounts["1000"], "idempotency_key": "k"}
    assert ar.c.post(f"{API}/receivables/payments", json=base | {"amount": "0"}).status_code == 422
    assert ar.c.post(f"{API}/receivables/payments", json=base | {"amount": "1.005"}).status_code == 422
    assert ar.c.post(f"{API}/receivables/payments", json={k: v for k, v in base.items() if k != "idempotency_key"}).status_code == 422
    assert ar.c.post(f"{API}/receivables/payments", json=base | {"customer_id": 999}).status_code == 404
    assert ar.c.post(f"{API}/receivables/payments", json=base | {"deposit_account_id": ar.accounts["4000"]}).status_code == 422
    assert ar.c.post(f"{API}/receivables/payments", json=base | {"deposit_account_id": 9999}).status_code == 422
    assert ar.c.post(f"{API}/receivables/payments", json=base | {"allocations": [{"invoice_id": inv["id"], "amount": "0"}]}).status_code == 422
    assert ar.c.get(f"{API}/receivables/payments").json() == []
    assert ar.c.get(f"{API}/receivables/payments/1").status_code == 404


# --- Idempotency ---------------------------------------------------------------------


def test_resent_payment_request_is_not_duplicated(ar):
    inv = ar.issued()
    body = {"customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "60.00",
            "deposit_account_id": ar.accounts["1000"], "reference": "CHQ-7", "idempotency_key": "chq-7",
            "allocations": [{"invoice_id": inv["id"], "amount": "60.00"}]}
    first = ar.c.post(f"{API}/receivables/payments", json=body)
    assert first.status_code == 201
    for _ in range(3):
        again = ar.c.post(f"{API}/receivables/payments", json=body)
        assert again.status_code == 200, again.text
        assert again.json() == first.json()

    assert len(ar.c.get(f"{API}/receivables/payments").json()) == 1
    inv_after = ar.get_invoice(inv["id"])
    assert inv_after["amount_settled"] == "60.00" and len(inv_after["allocations"]) == 1
    assert ar.ledger("1000") == D("60.00")
    assert len(ar.journal_entries()) == 2


def test_idempotency_key_reused_with_different_payload_is_conflict(ar):
    ar.payment("60.00", key="same")
    r = ar.c.post(f"{API}/receivables/payments", json={
        "customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "61.00",
        "deposit_account_id": ar.accounts["1000"], "idempotency_key": "same"})
    assert r.status_code == 409 and "different payment" in r.json()["detail"]
    assert len(ar.c.get(f"{API}/receivables/payments").json()) == 1


def test_resent_allocation_request_is_not_applied_twice(ar):
    inv = ar.issued()
    pay = ar.payment("100.00")
    body = {"idempotency_key": "alloc-1", "allocations": [{"invoice_id": inv["id"], "amount": "40.00"}]}
    first = ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations", json=body)
    assert first.status_code == 201
    again = ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations", json=body)
    assert again.status_code == 200
    assert again.json()["amount_allocated"] == "40.00"
    assert ar.get_invoice(inv["id"])["amount_settled"] == "40.00"
    assert len(ar.get_invoice(inv["id"])["allocations"]) == 1

    # Without a key a repeated request is a new allocation (and still bounded by the balances).
    no_key = {"allocations": [{"invoice_id": inv["id"], "amount": "30.00"}]}
    assert ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations", json=no_key).status_code == 201
    assert ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations", json=no_key).status_code == 201
    assert ar.c.post(f"{API}/receivables/payments/{pay['id']}/allocations", json=no_key).status_code == 422
    assert ar.get_invoice(inv["id"])["status"] == "paid"


# --- Ledger integration ----------------------------------------------------------------


def test_payment_posts_bank_against_receivables(ar):
    inv = ar.issued()
    pay = ar.payment("100.00", [(inv["id"], "100.00")], payment_date="2026-01-22", reference="TRF-9")
    entry = ar.c.get(f"{API}/journal-entries/{pay['journal_entry_id']}").json()
    assert entry["status"] == "posted" and entry["entry_date"] == "2026-01-22" and entry["reference"] == "TRF-9"
    sides = {(l["account_id"], l["debit"], l["credit"]) for l in entry["lines"]}
    assert sides == {(ar.accounts["1000"], "100.00", "0.00"), (ar.accounts["1200"], "0.00", "100.00")}
    assert ar.ledger("1200") == D("0") and ar.ledger("1000") == D("100.00")


def test_payment_fails_atomically_when_posting_is_refused(ar):
    inv = ar.issued()
    ar.c.post(f"{API}/accounts/{ar.accounts['1000']}/deactivate")
    r = ar.c.post(f"{API}/receivables/payments", json={
        "customer_id": ar.customer["id"], "payment_date": "2026-01-20", "amount": "100.00",
        "deposit_account_id": ar.accounts["1000"], "idempotency_key": "k",
        "allocations": [{"invoice_id": inv["id"], "amount": "100.00"}]})
    assert r.status_code == 422 and "inactive" in r.json()["detail"]
    assert ar.c.get(f"{API}/receivables/payments").json() == []
    assert ar.get_invoice(inv["id"])["status"] == "issued"
    assert ar.get_invoice(inv["id"])["allocations"] == []
    assert len(ar.journal_entries()) == 1


def test_customer_balance_matches_posted_receivables_account(ar):
    """balance == outstanding - unapplied credit == the customer's AR control account."""
    a = ar.issued("A", lines=[ar.line("a", "1", "100.00", tax=True)])   # 120
    b = ar.issued("B", lines=[ar.line("b", "3", "10.00")])               # 30
    ar.invoice("draft-ignored")
    ar.payment("50.00", [(a["id"], "50.00")], key="p1")
    ar.payment("100.00", [(b["id"], "30.00")], key="p2")                  # 70 on account
    ar.adjustment("credit", "5.00", invoice_id=a["id"])
    ar.adjustment("write_off", "2.00", invoice_id=a["id"], reason="rounding")
    bal = ar.balance()
    assert bal["outstanding"] == "63.00"        # 120 - 50 - 5 - 2
    assert bal["unapplied_credit"] == "70.00"
    assert bal["balance"] == "-7.00"
    assert ar.ledger("1200") == D("-7.00")
