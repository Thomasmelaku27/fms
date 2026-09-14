"""Durable settlement history (SQLite and PostgreSQL).

Credit applications and advance allocations are never deleted: unapplying /
unallocating closes the row with an effective date, actor and reason. Reports
as of any date reconstruct the state that was in force on that date, for
ordinary and backdated operations. Closed AP periods refuse accounting effects
dated inside them.
"""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.payables.models import CreditApplication, PaymentAllocation

API = "/api/v1/payables"


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def accounts(client):
    return {
        "ap": _account(client, "2000", "liability"),
        "expense": _account(client, "6000", "expense"),
        "cash": _account(client, "1000", "asset"),
    }


@pytest.fixture
def vendor(client, accounts):
    r = client.post(f"{API}/vendors", json={"code": "ACME", "name": "Acme", "payable_account_id": accounts["ap"]})
    assert r.status_code == 201, r.text
    return r.json()


def _bill(client, vendor, accounts, number, total, invoice_date, due_date=None):
    r = client.post(f"{API}/bills", json={
        "vendor_id": vendor["id"], "invoice_number": number, "invoice_date": invoice_date,
        "due_date": due_date or invoice_date, "total": total,
        "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/bills/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _credit(client, vendor, accounts, number, total, credit_date):
    r = client.post(f"{API}/credits", json={
        "vendor_id": vendor["id"], "credit_number": number, "credit_date": credit_date, "total": total,
        "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": total}]})
    assert r.status_code == 201, r.text
    a = client.post(f"{API}/credits/{r.json()['id']}/approve", json={"actor": "alice"})
    assert a.status_code == 200, a.text
    return a.json()


def _apply(client, credit_id, bill_id, amount, on, actor="bob"):
    return client.post(f"{API}/credits/{credit_id}/applications",
                       json={"bill_id": bill_id, "amount": amount, "actor": actor, "application_date": on})


def _unapply(client, credit_id, application_id, on, actor="carol", reason="re-allocated"):
    return client.post(f"{API}/credits/{credit_id}/applications/{application_id}/unapply",
                       json={"actor": actor, "reason": reason, "unapplication_date": on})


def _outstanding(client, as_of, vendor_id=None):
    q = f"as_of={as_of}" + (f"&vendor_id={vendor_id}" if vendor_id else "")
    return client.get(f"{API}/outstanding?{q}").json()


def _balance(client, vendor, as_of):
    return client.get(f"{API}/vendors/{vendor['id']}/balance?as_of={as_of}").json()


def _count(client, model):
    with Session(client.app.state.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


# --- credit application lifecycle ------------------------------------------------------------------


def test_unapplying_keeps_the_application_and_reports_reconstruct_every_month(client, vendor, accounts):
    """Applied in January, unapplied in March: January and February see it applied, April does not."""
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    credit = _credit(client, vendor, accounts, "CN-1", "40.00", "2026-01-10")
    applied = _apply(client, credit["id"], bill["id"], "40.00", "2026-01-15")
    assert applied.status_code == 200, applied.text
    application = applied.json()["applications"][0]
    assert (application["application_date"], application["applied_by"], application["is_active"]) == ("2026-01-15", "bob", True)
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "partially_paid"

    unapplied = _unapply(client, credit["id"], application["id"], "2026-03-10", reason="credit belongs to B2")
    assert unapplied.status_code == 200, unapplied.text
    row = unapplied.json()["applications"][0]
    assert row["id"] == application["id"]  # same row, now closed
    assert (row["unapplication_date"], row["unapplied_by"], row["unapplication_reason"], row["is_active"]) == (
        "2026-03-10", "carol", "credit belongs to B2", False)
    assert row["unapplied_at"] is not None
    assert unapplied.json()["status"] == "approved" and unapplied.json()["remaining"] == "40.00"
    assert client.get(f"{API}/bills/{bill['id']}").json()["status"] == "approved"
    assert _count(client, CreditApplication) == 1

    expectations = {
        "2026-01-14": ("100.00", "40.00"),  # before the application: credit approved, not applied
        "2026-01-31": ("60.00", "0.00"),    # January: applied
        "2026-02-28": ("60.00", "0.00"),    # February: still applied
        "2026-03-09": ("60.00", "0.00"),    # day before the unapplication
        "2026-03-10": ("100.00", "40.00"),  # unapplied from this day
        "2026-04-30": ("100.00", "40.00"),  # April: credit available again
    }
    for as_of, (outstanding, unapplied_credit) in expectations.items():
        report = _outstanding(client, as_of)
        assert report["total_outstanding"] == outstanding, as_of
        balance = _balance(client, vendor, as_of)
        assert (balance["bills_outstanding"], balance["unapplied_credits"]) == (outstanding, unapplied_credit), as_of
        assert balance["net_owed"] == "60.00", as_of  # the vendor is owed 60 net throughout
        statement = client.get(f"{API}/vendors/{vendor['id']}/statement?date_from=2026-01-01&date_to={as_of}").json()
        assert statement["closing_balance"] == "60.00" and statement["difference"] == "0.00", as_of


def test_reapplying_after_unapplication_and_repeat_requests(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    credit = _credit(client, vendor, accounts, "CN-1", "40.00", "2026-01-10")
    first = _apply(client, credit["id"], bill["id"], "30.00", "2026-01-15").json()["applications"][0]
    # Several dated applications of one credit to one bill are fine; the totals are capped.
    again = _apply(client, credit["id"], bill["id"], "10.00", "2026-01-16")
    assert again.status_code == 200 and again.json()["remaining"] == "0.00"
    assert _apply(client, credit["id"], bill["id"], "0.01", "2026-01-17").status_code == 422
    second_id = again.json()["applications"][1]["id"]
    assert _unapply(client, credit["id"], second_id, "2026-02-01").status_code == 200
    assert _unapply(client, credit["id"], first["id"], "2026-02-01").status_code == 200
    assert _unapply(client, credit["id"], first["id"], "2026-02-02").status_code == 409  # repeat
    second = _apply(client, credit["id"], bill["id"], "25.00", "2026-02-05")
    assert second.status_code == 200, second.text
    rows = second.json()["applications"]
    assert [(r["amount"], r["is_active"]) for r in rows] == [("30.00", False), ("10.00", False), ("25.00", True)]
    assert _count(client, CreditApplication) == 3
    assert _outstanding(client, "2026-01-20")["total_outstanding"] == "60.00"
    assert _outstanding(client, "2026-02-03")["total_outstanding"] == "100.00"
    assert _outstanding(client, "2026-02-28")["total_outstanding"] == "75.00"


def test_backdated_application_and_unapplication_rules(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    credit = _credit(client, vendor, accounts, "CN-1", "40.00", "2026-01-10")
    # Backdating before the credit (or the bill) is refused.
    early = _apply(client, credit["id"], bill["id"], "40.00", "2026-01-09")
    assert early.status_code == 422 and "cannot precede the credit or the bill" in early.json()["detail"]
    # A backdated application on the credit date itself is fine.
    applied = _apply(client, credit["id"], bill["id"], "40.00", "2026-01-10")
    assert applied.status_code == 200, applied.text
    app_id = applied.json()["applications"][0]["id"]
    assert _outstanding(client, "2026-01-10")["total_outstanding"] == "60.00"
    # Unapplication cannot be dated before the application.
    bad = _unapply(client, credit["id"], app_id, "2026-01-09")
    assert bad.status_code == 422 and "cannot precede the application date" in bad.json()["detail"]
    # Backdated unapplication (same day as the application) is allowed and wipes the whole window.
    assert _unapply(client, credit["id"], app_id, "2026-01-10").status_code == 200
    assert _outstanding(client, "2026-01-10")["total_outstanding"] == "100.00"
    assert _outstanding(client, "2026-01-31")["total_outstanding"] == "100.00"
    assert client.get(f"{API}/credits/{credit['id']}").json()["remaining"] == "40.00"


def test_unapply_default_date_is_today_and_validation(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    credit = _credit(client, vendor, accounts, "CN-1", "40.00", "2026-01-10")
    app_id = _apply(client, credit["id"], bill["id"], "40.00", "2026-01-15").json()["applications"][0]["id"]
    r = client.post(f"{API}/credits/{credit['id']}/applications/{app_id}/unapply", json={"actor": "carol"})
    assert r.status_code == 200
    assert r.json()["applications"][0]["unapplication_date"] == date.today().isoformat()
    assert client.post(f"{API}/credits/{credit['id']}/applications/999/unapply", json={"actor": "carol"}).status_code == 404
    assert client.post(f"{API}/credits/{credit['id']}/applications/{app_id}/unapply", json={}).status_code == 422


# --- advance allocation lifecycle -------------------------------------------------------------------------


def test_advance_allocation_and_unallocation_are_dated_and_reconstructible(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    advance = client.post(f"{API}/payments", json={
        "vendor_id": vendor["id"], "payment_date": "2026-01-08", "amount": "70.00",
        "cash_account_id": accounts["cash"], "recorded_by": "bob", "allocations": []})
    assert advance.status_code == 201, advance.text
    payment = advance.json()
    assert (payment["unapplied_amount"], payment["allocated_total"], payment["payable_account_id"]) == ("70.00", "0.00", accounts["ap"])
    assert _balance(client, vendor, "2026-01-08")["unapplied_advances"] == "70.00"
    assert _outstanding(client, "2026-01-08")["total_outstanding"] == "100.00"  # the bill is untouched

    applied = client.post(f"{API}/payments/{payment['id']}/applications",
                          json={"bill_id": bill["id"], "amount": "50.00", "actor": "bob", "allocation_date": "2026-01-20"})
    assert applied.status_code == 200, applied.text
    allocation = applied.json()["allocations"][0]
    assert (allocation["kind"], allocation["allocation_date"], allocation["allocated_by"], allocation["is_active"]) == ("advance", "2026-01-20", "bob", True)
    assert applied.json()["unapplied_amount"] == "20.00"
    assert client.get(f"{API}/bills/{bill['id']}").json()["outstanding"] == "50.00"

    unallocated = client.post(f"{API}/payments/{payment['id']}/applications/{allocation['id']}/unallocate",
                              json={"actor": "carol", "reason": "wrong invoice", "unallocation_date": "2026-03-03"})
    assert unallocated.status_code == 200, unallocated.text
    row = unallocated.json()["allocations"][0]
    assert (row["unallocation_date"], row["unallocated_by"], row["unallocation_reason"], row["is_active"]) == ("2026-03-03", "carol", "wrong invoice", False)
    assert unallocated.json()["unapplied_amount"] == "70.00"
    assert client.get(f"{API}/bills/{bill['id']}").json()["outstanding"] == "100.00"
    assert _count(client, PaymentAllocation) == 1

    for as_of, (outstanding, advances) in {
        "2026-01-19": ("100.00", "70.00"), "2026-01-31": ("50.00", "20.00"),
        "2026-02-28": ("50.00", "20.00"), "2026-03-03": ("100.00", "70.00"), "2026-04-30": ("100.00", "70.00"),
    }.items():
        balance = _balance(client, vendor, as_of)
        assert (balance["bills_outstanding"], balance["unapplied_advances"], balance["net_owed"]) == (outstanding, advances, "30.00"), as_of
        statement = client.get(f"{API}/vendors/{vendor['id']}/statement?date_from=2026-01-01&date_to={as_of}").json()
        assert statement["closing_balance"] == "30.00" and statement["difference"] == "0.00", as_of

    # Repeat unallocation is a conflict; original allocations can never be unallocated.
    assert client.post(f"{API}/payments/{payment['id']}/applications/{allocation['id']}/unallocate",
                       json={"actor": "carol"}).status_code == 409
    direct = client.post(f"{API}/payments", json={
        "vendor_id": vendor["id"], "payment_date": "2026-03-05", "amount": "10.00", "cash_account_id": accounts["cash"],
        "recorded_by": "bob", "allocations": [{"bill_id": bill["id"], "amount": "10.00"}]}).json()
    original = direct["allocations"][0]
    r = client.post(f"{API}/payments/{direct['id']}/applications/{original['id']}/unallocate", json={"actor": "carol"})
    assert r.status_code == 409 and "Original allocations cannot be unallocated" in r.json()["detail"]


def test_reversal_cannot_predate_a_later_advance_allocation(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    payment = client.post(f"{API}/payments", json={
        "vendor_id": vendor["id"], "payment_date": "2026-01-08", "amount": "70.00",
        "cash_account_id": accounts["cash"], "recorded_by": "bob"}).json()
    assert client.post(f"{API}/payments/{payment['id']}/applications",
                       json={"bill_id": bill["id"], "amount": "50.00", "actor": "bob", "allocation_date": "2026-02-10"}).status_code == 200
    early = client.post(f"{API}/payments/{payment['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-02-01"})
    assert early.status_code == 422 and "latest advance allocation" in early.json()["detail"]
    ok = client.post(f"{API}/payments/{payment['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-02-10"})
    assert ok.status_code == 200
    assert client.get(f"{API}/bills/{bill['id']}").json()["outstanding"] == "100.00"
    assert _outstanding(client, "2026-02-09")["total_outstanding"] == "100.00"  # allocation dated the 10th
    assert _balance(client, vendor, "2026-02-09")["unapplied_advances"] == "70.00"
    assert _balance(client, vendor, "2026-02-10")["unapplied_advances"] == "0.00"


# --- closed periods ---------------------------------------------------------------------------------------


def _close(client, period_end, actor="cfo"):
    return client.post(f"{API}/period-end/close", json={"period_end": period_end, "actor": actor, "note": "month end"})


def test_closed_period_refuses_backdated_settlement_changes(client, vendor, accounts):
    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    credit = _credit(client, vendor, accounts, "CN-1", "40.00", "2026-01-10")
    app_id = _apply(client, credit["id"], bill["id"], "40.00", "2026-01-15").json()["applications"][0]["id"]
    payment = client.post(f"{API}/payments", json={
        "vendor_id": vendor["id"], "payment_date": "2026-01-20", "amount": "60.00", "cash_account_id": accounts["cash"],
        "recorded_by": "bob", "allocations": [{"bill_id": bill["id"], "amount": "60.00"}]}).json()
    january_outstanding = _outstanding(client, "2026-01-31")["total_outstanding"]

    closed = _close(client, "2026-01-31")
    assert closed.status_code == 201, closed.text
    assert closed.json()["closed_through"] == "2026-01-31"

    # Anything that would change January is refused ...
    jan = "2026-01-25"
    refused = [
        _unapply(client, credit["id"], app_id, jan),
        client.post(f"{API}/payments/{payment['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": jan}),
        client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": jan, "amount": "1.00",
                                             "cash_account_id": accounts["cash"], "recorded_by": "bob"}),
    ]
    for response in refused:
        assert response.status_code == 422, response.text
        assert "closed AP period" in response.json()["detail"]
    # ... including approving documents dated in January and applying credits with January dates.
    draft = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "LATE", "invoice_date": "2026-01-28",
                                              "total": "5.00", "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "5.00"}]}).json()
    r = client.post(f"{API}/bills/{draft['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 422 and "closed AP period" in r.json()["detail"]
    credit2 = client.post(f"{API}/credits", json={"vendor_id": vendor["id"], "credit_number": "CN-2", "credit_date": "2026-01-30", "total": "5.00",
                                                  "lines": [{"description": "c", "account_id": accounts["expense"], "unit_price": "5.00"}]}).json()
    r = client.post(f"{API}/credits/{credit2['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 422 and "closed AP period" in r.json()["detail"]

    # The same operations dated after the close are fine, and January is unchanged.
    assert _unapply(client, credit["id"], app_id, "2026-02-01").status_code == 200
    assert client.post(f"{API}/payments/{payment['id']}/reverse", json={"actor": "c", "reason": "r", "reversal_date": "2026-02-02"}).status_code == 200
    assert _outstanding(client, "2026-01-31")["total_outstanding"] == january_outstanding == "0.00"
    assert _outstanding(client, "2026-02-02")["total_outstanding"] == "100.00"
    # Re-dating the late documents into February lets them through.
    assert client.put(f"{API}/bills/{draft['id']}", json={"invoice_date": "2026-02-01", "due_date": "2026-02-01"}).status_code == 200
    assert client.post(f"{API}/bills/{draft['id']}/approve", json={"actor": "alice"}).status_code == 200
    assert client.get(f"{API}/period-end/locks").json()[0]["closed_by"] == "cfo"
    assert _close(client, "2026-01-15").status_code == 409  # already closed through the 31st


def test_closed_period_is_enforced_by_the_database_too(client, vendor, accounts):
    """Raw SQL dated inside the closed period is refused by the triggers (both backends)."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    bill = _bill(client, vendor, accounts, "B1", "100.00", "2026-01-05")
    credit = _credit(client, vendor, accounts, "CN-1", "40.00", "2026-01-10")
    assert _close(client, "2026-01-31").status_code == 201
    with Session(client.app.state.engine) as session:
        with pytest.raises(IntegrityError, match="closed"):
            session.execute(text(
                "INSERT INTO credit_applications (credit_id, bill_id, amount, application_date, applied_by) "
                "VALUES (:c, :b, 10, '2026-01-20', 'sql')"), {"c": credit["id"], "b": bill["id"]})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError, match="closed"):
            session.execute(text(
                "INSERT INTO vendor_payments (vendor_id, payment_date, amount, method, cash_account_id, recorded_by, status) "
                "VALUES (:v, '2026-01-30', 5, 'cash', :cash, 'sql', 'recorded')"), {"v": vendor["id"], "cash": accounts["cash"]})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError, match="append-only"):
            session.execute(text("DELETE FROM ap_period_locks"))
            session.commit()
        session.rollback()
