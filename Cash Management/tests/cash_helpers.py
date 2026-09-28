"""Helpers shared by the cash management tests.

Everything goes through the public API so that the ledger is only ever changed
by the journal endpoints and the posting service, exactly like in production.
"""

from decimal import Decimal


def account(client, code, name, type_):
    resp = client.post("/api/v1/accounts", json={"code": code, "name": name, "type": type_})
    assert resp.status_code == 201, resp.text
    return resp.json()


def cash_account(client, ledger_account_id, label="Main bank", minimum_balance=None):
    body = {"ledger_account_id": ledger_account_id, "label": label}
    if minimum_balance is not None:
        body["minimum_balance"] = str(minimum_balance)
    resp = client.post("/api/v1/cash-accounts", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def entry(client, entry_date, lines, description="entry", reference=None, post=True):
    """Create a journal entry through the API and (by default) post it via the posting service."""
    body = {"entry_date": entry_date, "description": description, "lines": lines}
    if reference is not None:
        body["reference"] = reference
    resp = client.post("/api/v1/journal-entries", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    if post:
        posted = client.post(f"/api/v1/journal-entries/{created['id']}/post")
        assert posted.status_code == 200, posted.text
        return posted.json()
    return created


def cash_in(client, entry_date, cash_ledger_id, counter_id, amount, description="receipt", **kw):
    """Money received: debit cash, credit counterpart."""
    return entry(
        client,
        entry_date,
        [
            {"account_id": cash_ledger_id, "debit": str(amount)},
            {"account_id": counter_id, "credit": str(amount)},
        ],
        description,
        **kw,
    )


def cash_out(client, entry_date, cash_ledger_id, counter_id, amount, description="payment", **kw):
    """Money paid: credit cash, debit counterpart."""
    return entry(
        client,
        entry_date,
        [
            {"account_id": counter_id, "debit": str(amount)},
            {"account_id": cash_ledger_id, "credit": str(amount)},
        ],
        description,
        **kw,
    )


def cash_line_id(posted_entry, cash_ledger_id):
    return next(l["id"] for l in posted_entry["lines"] if l["account_id"] == cash_ledger_id)


def statement_payload(cash_account_id, reference, start, end, opening, transactions):
    """Build an import payload whose closing balance foots with its transactions."""
    total = sum((Decimal(str(t["amount"])) for t in transactions), Decimal("0"))
    return {
        "cash_account_id": cash_account_id,
        "statement_reference": reference,
        "period_start": start,
        "period_end": end,
        "opening_balance": str(opening),
        "closing_balance": str(Decimal(str(opening)) + total),
        "transactions": [
            {
                "transaction_date": t["date"],
                "amount": str(t["amount"]),
                "description": t.get("description", "bank line"),
                "reference": t.get("reference"),
            }
            for t in transactions
        ],
    }


def import_statement(client, payload, expect=201):
    resp = client.post("/api/v1/cash/bank-statements", json=payload)
    assert resp.status_code == expect, resp.text
    return resp.json()


def open_session(client, cash_account_id, statement_id=None, **fields):
    body = {"cash_account_id": cash_account_id, "statement_id": statement_id, **fields}
    resp = client.post("/api/v1/cash/reconciliations", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def match(client, session_id, bank_transaction_id, journal_line_id, amount=None, expect=201):
    body = {"bank_transaction_id": bank_transaction_id, "journal_line_id": journal_line_id}
    if amount is not None:
        body["amount"] = str(amount)
    resp = client.post(f"/api/v1/cash/reconciliations/{session_id}/matches", json=body)
    assert resp.status_code == expect, resp.text
    return resp.json()


def summary(client, session_id):
    resp = client.get(f"/api/v1/cash/reconciliations/{session_id}/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def ledger_snapshot(client):
    """Every journal entry with status and lines - to prove nothing in the ledger changed."""
    return client.get("/api/v1/journal-entries").json()
