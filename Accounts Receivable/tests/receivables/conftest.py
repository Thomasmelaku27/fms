"""Fixtures for the Accounts Receivable tests.

``ar`` wraps the API ``client`` (SQLite and, when configured, PostgreSQL - see
the top-level conftest) with a minimal chart of accounts, the module settings,
a tax code and a customer, plus helpers that assert on status codes so the
tests read as business scenarios.
"""

from decimal import Decimal

import pytest

API = "/api/v1"

D = Decimal


class ARClient:
    def __init__(self, client):
        self.c = client
        self.accounts: dict[str, int] = {}

    # --- setup -------------------------------------------------------------------

    def account(self, code, name, type_, expect=201):
        r = self.c.post(f"{API}/accounts", json={"code": code, "name": name, "type": type_})
        assert r.status_code == expect, r.text
        self.accounts[code] = r.json()["id"]
        return r.json()["id"]

    def bootstrap(self):
        self.account("1000", "Bank", "asset")
        self.account("1200", "Accounts receivable", "asset")
        self.account("2200", "Tax payable", "liability")
        self.account("4000", "Sales", "revenue")
        self.account("4900", "Sales returns", "revenue")
        self.account("6100", "Bad debt expense", "expense")
        self.put_settings()
        self.tax_code = self.tax("VAT20", "0.200000")
        self.customer = self.add_customer("C1", "Acme Ltd")
        return self

    def put_settings(self, expect=200, **overrides):
        body = {
            "receivable_account_id": self.accounts["1200"],
            "tax_account_id": self.accounts["2200"],
            "credit_account_id": self.accounts["4900"],
            "write_off_account_id": self.accounts["6100"],
        } | overrides
        r = self.c.put(f"{API}/receivables/settings", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def tax(self, code, rate, expect=201):
        r = self.c.post(
            f"{API}/tax-codes",
            json={"code": code, "description": code, "rate": rate, "jurisdiction": "Test"},
        )
        assert r.status_code == expect, r.text
        return r.json()

    def add_customer(self, code, name, expect=201):
        r = self.c.post(f"{API}/receivables/customers", json={"code": code, "name": name})
        assert r.status_code == expect, r.text
        return r.json()

    # --- invoices ------------------------------------------------------------------

    def line(self, description="Widget", quantity="1", unit_price="100.00", tax=False, account=None):
        return {
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "revenue_account_id": account or self.accounts["4000"],
            "tax_code_id": self.tax_code["id"] if tax else None,
        }

    def invoice(self, number="INV-1", lines=None, customer_id=None, invoice_date="2026-01-10",
                due_date="2026-02-09", expect=201, **extra):
        body = {
            "customer_id": customer_id or self.customer["id"],
            "invoice_number": number,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "lines": lines if lines is not None else [self.line()],
        } | extra
        r = self.c.post(f"{API}/receivables/invoices", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def issue(self, invoice_id, expect=200):
        r = self.c.post(f"{API}/receivables/invoices/{invoice_id}/issue")
        assert r.status_code == expect, r.text
        return r.json()

    def issued(self, number="INV-1", lines=None, **kw):
        return self.issue(self.invoice(number, lines, **kw)["id"])

    def void(self, invoice_id, expect=200, **body):
        r = self.c.post(f"{API}/receivables/invoices/{invoice_id}/void", json=body or None)
        assert r.status_code == expect, r.text
        return r.json()

    def get_invoice(self, invoice_id):
        r = self.c.get(f"{API}/receivables/invoices/{invoice_id}")
        assert r.status_code == 200, r.text
        return r.json()

    # --- payments / adjustments -----------------------------------------------------

    def payment(self, amount, allocations=(), key="key-1", customer_id=None, expect=201,
                payment_date="2026-01-20", reference=None, deposit_account=None):
        body = {
            "customer_id": customer_id or self.customer["id"],
            "payment_date": payment_date,
            "amount": amount,
            "deposit_account_id": deposit_account or self.accounts["1000"],
            "reference": reference,
            "idempotency_key": key,
            "allocations": [{"invoice_id": i, "amount": a} for i, a in allocations],
        }
        r = self.c.post(f"{API}/receivables/payments", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def allocate(self, payment_id, allocations, key=None, expect=201, kind="payments", effective_date=None):
        body = {"idempotency_key": key, "effective_date": effective_date,
                "allocations": [{"invoice_id": i, "amount": a} for i, a in allocations]}
        r = self.c.post(f"{API}/receivables/{kind}/{payment_id}/allocations", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def allocate_raw(self, source_id, body, kind="payments"):
        return self.c.post(f"{API}/receivables/{kind}/{source_id}/allocations", json=body)

    def aging(self, as_of=None, **params):
        r = self.c.get(f"{API}/receivables/reports/aging", params={"as_of": as_of, **params} if as_of else params)
        assert r.status_code == 200, r.text
        return r.json()

    def overdue(self, as_of=None):
        r = self.c.get(f"{API}/receivables/reports/overdue", params={"as_of": as_of} if as_of else {})
        assert r.status_code == 200, r.text
        return r.json()

    def ledger_as_of(self, code, as_of):
        """Net debit balance of a posted ledger account at a date (trial balance)."""
        rows = self.c.get(f"{API}/reports/trial-balance", params={"as_of": as_of}).json()["rows"]
        for row in rows:
            if row["code"] == code:
                return D(row["debit"]) - D(row["credit"])
        return D("0")

    def adjustment(self, kind, amount, invoice_id=None, reason="adjustment", key=None,
                   customer_id=None, adjustment_date="2026-01-25", expect=201):
        body = {
            "customer_id": customer_id or self.customer["id"],
            "kind": kind,
            "adjustment_date": adjustment_date,
            "amount": amount,
            "reason": reason,
            "invoice_id": invoice_id,
            "idempotency_key": key,
        }
        r = self.c.post(f"{API}/receivables/adjustments", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    # --- reads ---------------------------------------------------------------------------

    def balance(self, customer_id=None, as_of=None):
        params = {"as_of": as_of} if as_of else {}
        r = self.c.get(f"{API}/receivables/customers/{customer_id or self.customer['id']}/balance", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def trial_balance(self):
        return {row["code"]: (D(row["debit"]), D(row["credit"])) for row in
                self.c.get(f"{API}/reports/trial-balance").json()["rows"]}

    def ledger(self, code):
        """Net debit balance of a ledger account from posted entries only."""
        debit, credit = self.trial_balance().get(code, (D("0"), D("0")))
        return debit - credit

    def journal_entries(self):
        return self.c.get(f"{API}/journal-entries").json()


@pytest.fixture
def ar(client):
    return ARClient(client).bootstrap()
