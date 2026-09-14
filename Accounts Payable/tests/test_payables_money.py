"""Monetary validation: every unsupported input is a predictable domain error, never
``decimal.InvalidOperation``; comparisons are exact to the cent (no floats, no tolerances)."""

from decimal import Decimal, InvalidOperation

import pytest

from app.modules.payables.services import execution_state as state
from app.modules.payables.services.common import ValidationFailed
from app.modules.payables.services.execution_state import MoneyError, canonical_amount, canonical_plan, cents

D = Decimal
API = "/api/v1/payables"


@pytest.mark.parametrize(("value", "fragment"), [
    ("1e40", "exceeds the representable range"),
    ("1e100", "exceeds the representable range"),
    ("1E+17", "exceeds the representable range"),
    ("10000000000000000.00", "exceeds the representable range"),
    (D("1e40"), "exceeds the representable range"),
    ("Infinity", "not a finite"),
    ("-Infinity", "not a finite"),
    ("inf", "not a finite"),
    ("NaN", "not a finite"),
    ("sNaN", "not a finite"),
    (D("NaN"), "not a finite"),
    (D("Infinity"), "not a finite"),
    ("-0", "negative zero"),
    ("-0.00", "negative zero"),
    (D("-0"), "negative zero"),
    ("-1.00", "must not be negative"),
    ("0", "must be positive"),
    ("0.00", "must be positive"),
    ("1.005", "unsupported precision"),
    ("0.001", "unsupported precision"),
    (D("1.000001"), "unsupported precision"),
    ("1E-3", "unsupported precision"),
    ("abc", "not a monetary value"),
    ("", "not a finite"),
    ("1,000.00", "not a monetary value"),
    (1.5, "unsupported numeric type float"),
    (0.1, "unsupported numeric type float"),
    (True, "unsupported numeric type bool"),
    (None, "unsupported numeric type NoneType"),
    ([1], "unsupported numeric type list"),
])
def test_unsupported_inputs_are_domain_errors(value, fragment):
    with pytest.raises(MoneyError, match=fragment) as info:
        canonical_amount(value)
    assert isinstance(info.value, ValidationFailed) and info.value.status_code == 422
    assert not isinstance(info.value, InvalidOperation)


@pytest.mark.parametrize(("value", "expected"), [
    ("1", D("1.00")), ("1.5", D("1.50")), ("1.50", D("1.50")), (" 2.25 ", D("2.25")), ("1E+2", D("100.00")),
    ("1.5E+2", D("150.00")), (D("3"), D("3.00")), (7, D("7.00")), ("9999999999999999.99", D("9999999999999999.99")),
    ("0.01", D("0.01")), ("1.10", D("1.10")), (D("2.500"), D("2.50")),
])
def test_supported_inputs_are_exact(value, expected):
    result = canonical_amount(value)
    assert result == expected and str(result) == f"{expected:.2f}" and result.as_tuple().exponent == -2


def test_zero_only_when_allowed():
    assert canonical_amount("0", allow_zero=True) == D("0.00")
    assert canonical_amount("0E+5", allow_zero=True) == D("0.00")
    with pytest.raises(MoneyError, match="negative zero"):
        canonical_amount("-0", allow_zero=True)


def test_plan_parts_are_validated_with_their_names():
    with pytest.raises(MoneyError, match="pay for bill 7: 1E\\+40 exceeds"):
        canonical_plan([{"bill_id": 7, "pay": "1e40"}])
    with pytest.raises(MoneyError, match="credit for bill 7: not a finite"):
        canonical_plan([{"bill_id": 7, "credit": "Infinity"}])
    with pytest.raises(MoneyError, match="discount for bill 7: unsupported precision"):
        canonical_plan([{"bill_id": 7, "discount": "0.005"}])
    with pytest.raises(MoneyError, match="advance for bill 7: must not be negative"):
        canonical_plan([{"bill_id": 7, "advance": "-1"}])
    assert canonical_plan([{"bill_id": 2, "pay": "1E+1"}, {"bill_id": 1, "pay": None, "settle": 3}]) == [
        {"bill_id": 1, "settle": "3.00", "discount": "0.00", "credit": "0.00", "advance": "0.00", "pay": "0.00"},
        {"bill_id": 2, "settle": "0.00", "discount": "0.00", "credit": "0.00", "advance": "0.00", "pay": "10.00"},
    ]


def test_fingerprint_refuses_unrepresentable_amounts_instead_of_crashing():
    from datetime import date

    with pytest.raises(MoneyError):
        state.instruction_fingerprint(vendor_id=1, destination_reference=None, amount=D("1e40"), currency="XXX", method="bank_transfer",
                                      payment_date=date(2026, 1, 1), cash_account_id=1, run_id=None, plan=[])


def test_integer_cents_and_one_cent_discrepancies():
    assert cents(D("1234.56")) == 123456 and cents(D("0.01")) == 1 and cents(D("9999999999999999.99")) == 999999999999999999
    # Binary floats would call these equal at some scales; exact decimal never does.
    big = D("9999999999999999.99")
    assert big != big - D("0.01") and cents(big) - cents(big - D("0.01")) == 1
    assert D("0.1") + D("0.2") == D("0.3")
    assert sum((D("0.10") for _ in range(10)), D("0")) == D("1.00")


# --- API boundary -------------------------------------------------------------------------------


def _account(client, code, type_):
    r = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("amount", ["1e40", "1e100", "Infinity", "-Infinity", "NaN", "-0", "-1", "0", "1.005", "abc", "", None, 1.005, True, [1]])
def test_api_refuses_unrepresentable_amounts_with_422(client, amount):
    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}
    vendor = client.post(f"{API}/vendors", json={"code": "V", "name": "V", "payable_account_id": accounts["ap"]}).json()
    payment = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-02-10", "amount": amount,
                                                    "cash_account_id": accounts["cash"], "recorded_by": "x"})
    assert payment.status_code == 422, payment.text
    bill = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "B-1", "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                             "total": amount, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "1.00"}]})
    assert bill.status_code == 422, bill.text
    with_lines = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "B-2", "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                                   "total": "1.00", "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": amount}]})
    assert with_lines.status_code == 422, with_lines.text
    assert client.get(f"{API}/payments").json() == [] and client.get(f"{API}/bills").json() == []


def test_api_json_numbers_are_parsed_exactly_not_through_floats(client):
    accounts = {"ap": _account(client, "2000", "liability"), "cash": _account(client, "1000", "asset")}
    vendor = client.post(f"{API}/vendors", json={"code": "V", "name": "V", "payable_account_id": accounts["ap"]}).json()
    # A JSON number is decimal text on the wire; 0.1 arrives as exactly 0.10, never 0.1000000000000000055.
    r = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-02-10", "amount": 0.1,
                                             "cash_account_id": accounts["cash"], "recorded_by": "x"})
    assert r.status_code == 201 and r.json()["amount"] == "0.10"


def test_api_one_cent_discrepancy_is_detected_everywhere(client):
    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}
    vendor = client.post(f"{API}/vendors", json={"code": "V", "name": "V", "payable_account_id": accounts["ap"]}).json()
    # Bill lines must sum exactly to the total: one cent off is refused.
    off = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "B-1", "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                            "total": "100.00", "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "99.99"}]})
    assert off.status_code == 422 and "100.00" in off.json()["detail"]
    bill = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": "B-1", "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                             "total": "100.00", "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": "100.00"}]}).json()
    assert client.post(f"{API}/bills/{bill['id']}/approve", json={"actor": "a"}).status_code == 200
    # Allocating one cent more than the payment is refused; one cent more than the bill too.
    over = client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-02-10", "amount": "100.00", "cash_account_id": accounts["cash"],
                                                "recorded_by": "x", "allocations": [{"bill_id": bill["id"], "amount": "100.01"}]})
    assert over.status_code == 422
    assert client.post(f"{API}/payments", json={"vendor_id": vendor["id"], "payment_date": "2026-02-10", "amount": "99.99", "cash_account_id": accounts["cash"],
                                                "recorded_by": "x", "allocations": [{"bill_id": bill["id"], "amount": "99.99"}]}).status_code == 201
    outstanding = client.get(f"{API}/outstanding").json()
    assert outstanding["rows"][0]["outstanding"] == "0.01" and outstanding["total_outstanding"] == "0.01"
    assert client.get(f"{API}/vendors/{vendor['id']}/balance").json()["bills_outstanding"] == "0.01"
    recon = client.get(f"{API}/reconciliation").json()
    assert recon["total_subledger"] == "0.01" and recon["total_difference"] == "0.00"
    replay = client.get(f"{API}/reconstruction?as_of=2026-02-28").json()
    assert replay["findings"] == [] and replay["bill_outstanding"][str(bill["id"])] == "0.01"


def test_completion_evidence_is_checked_in_exact_cents_on_both_backends(client):
    """Regression found by the benchmark: SQLite stores NUMERIC as REAL, and a trigger summing
    0.10 + 0.20 in floating point refused a legitimate completion (0.30000000000000004 > 0.30).
    The evidence trigger compares integer cents, so multi-bill operations complete."""
    from app.modules.payables.integration import Integrations, get_integrations
    from app.modules.payables.services.gateway import DurableSimulatedGateway

    accounts = {"ap": _account(client, "2000", "liability"), "expense": _account(client, "6000", "expense"), "cash": _account(client, "1000", "asset")}
    vendor = client.post(f"{API}/vendors", json={"code": "V", "name": "V", "payable_account_id": accounts["ap"], "bank_reference": "IBAN"}).json()
    for number, total in (("B-1", "0.10"), ("B-2", "0.20"), ("B-3", "0.70"), ("B-4", "1.15"), ("B-5", "2.85")):
        bill = client.post(f"{API}/bills", json={"vendor_id": vendor["id"], "invoice_number": number, "invoice_date": "2026-01-10", "due_date": "2026-02-01",
                                                 "total": total, "lines": [{"description": "x", "account_id": accounts["expense"], "unit_price": total}]}).json()
        assert client.post(f"{API}/bills/{bill['id']}/approve", json={"actor": "a"}).status_code == 200
    client.app.dependency_overrides[get_integrations] = lambda: Integrations(gateway=DurableSimulatedGateway())
    run = client.post(f"{API}/payment-runs", json={"actor": "ops", "pay_date": "2026-02-10", "cash_account_id": accounts["cash"]}).json()
    assert run["total_pay_amount"] == "5.00"
    assert client.post(f"{API}/payment-runs/{run['id']}/review", json={"actor": "rev"}).status_code == 200
    assert client.post(f"{API}/payment-runs/{run['id']}/approve", json={"actor": "boss"}).status_code == 200
    result = client.post(f"{API}/payment-runs/{run['id']}/execute", json={"actor": "ops"}).json()
    assert result["run"]["status"] == "completed" and result["outcomes"][0]["state"] == "completed", result
    status = client.get(f"{API}/payment-operations/{result['outcomes'][0]['operation_uid']}").json()
    assert status["completion_policy_satisfied"] and status["verification"]["evidence"]["allocations"]["allocated"] == "5.00"
