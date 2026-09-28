"""Bank reconciliation: sessions, full/partial matches, review, close, reopen.

The ledger is only ever changed through the journal API and the posting
service; reconciliation reads it and must never alter it.
"""

import pytest

from tests.cash_helpers import (
    account,
    cash_account,
    cash_in,
    cash_line_id,
    cash_out,
    entry,
    import_statement,
    ledger_snapshot,
    match,
    open_session,
    statement_payload,
    summary,
)


@pytest.fixture
def world(client):
    ledger = account(client, "1000", "Bank", "asset")
    other_ledger = account(client, "1010", "Petty cash", "asset")
    sales = account(client, "4000", "Sales", "revenue")
    rent = account(client, "5000", "Rent", "expense")
    ca = cash_account(client, ledger["id"])
    other = cash_account(client, other_ledger["id"], "Petty cash")
    return {
        "ledger": ledger["id"],
        "other_ledger": other_ledger["id"],
        "sales": sales["id"],
        "rent": rent["id"],
        "cash": ca["id"],
        "other_cash": other["id"],
    }


@pytest.fixture
def january(client, world):
    """Posted ledger + imported statement for January, plus an open session."""
    receipt = cash_in(client, "2026-01-05", world["ledger"], world["sales"], "100.00", "Customer A", reference="INV-1")
    payment = cash_out(client, "2026-01-10", world["ledger"], world["rent"], "40.50", "Rent")
    st = import_statement(
        client,
        statement_payload(
            world["cash"],
            "2026-01",
            "2026-01-01",
            "2026-01-31",
            "0",
            [
                {"date": "2026-01-05", "amount": "100.00", "description": "Customer A", "reference": "INV-1"},
                {"date": "2026-01-11", "amount": "-40.50", "description": "Rent"},
                {"date": "2026-01-20", "amount": "-9.99", "description": "Bank fee"},
            ],
        ),
    )
    txns = {t["description"]: t for t in st["transactions"]}
    session = open_session(client, world["cash"], st["statement"]["id"])
    return {
        "receipt_line": cash_line_id(receipt, world["ledger"]),
        "payment_line": cash_line_id(payment, world["ledger"]),
        "receipt": receipt,
        "payment": payment,
        "txns": txns,
        "statement": st["statement"],
        "session": session,
    }


# --- Sessions ------------------------------------------------------------------------


def test_session_from_statement_takes_period_and_balances(client, world, january):
    s = january["session"]
    assert s["status"] == "open"
    assert (s["period_start"], s["period_end"]) == ("2026-01-01", "2026-01-31")
    assert (s["opening_balance"], s["closing_balance"]) == ("0.00", "49.51")
    assert s["statement_id"] == january["statement"]["id"]
    assert client.get(f"/api/v1/cash/reconciliations/{s['id']}").json()["id"] == s["id"]
    assert [x["id"] for x in client.get("/api/v1/cash/reconciliations", params={"cash_account_id": world["cash"]}).json()] == [s["id"]]


def test_manual_session_requires_period_and_balances(client, world):
    resp = client.post("/api/v1/cash/reconciliations", json={"cash_account_id": world["cash"]})
    assert resp.status_code == 422
    assert "period_start is required" in resp.json()["detail"]
    manual = open_session(
        client, world["cash"], period_start="2026-02-01", period_end="2026-02-28",
        opening_balance="49.51", closing_balance="49.51",
    )
    assert manual["statement_id"] is None


def test_duplicate_session_for_same_period_is_conflict(client, world, january):
    resp = client.post(
        "/api/v1/cash/reconciliations",
        json={"cash_account_id": world["cash"], "statement_id": january["statement"]["id"]},
    )
    assert resp.status_code == 409


def test_session_statement_must_belong_to_cash_account(client, world, january):
    resp = client.post(
        "/api/v1/cash/reconciliations",
        json={"cash_account_id": world["other_cash"], "statement_id": january["statement"]["id"]},
    )
    assert resp.status_code == 422


# --- Matching -------------------------------------------------------------------------


def test_full_match_defaults_to_common_amount(client, world, january):
    sid = january["session"]["id"]
    m = match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"])
    assert m["amount"] == "100.00"

    txn = next(t for t in client.get("/api/v1/cash/bank-transactions").json() if t["description"] == "Customer A")
    assert (txn["status"], txn["matched_amount"], txn["remaining_amount"]) == ("matched", "100.00", "0.00")
    assert [x["id"] for x in client.get(f"/api/v1/cash/reconciliations/{sid}/matches").json()] == [m["id"]]


def test_partial_reconciliation_bank_transaction_against_two_ledger_lines(client, world, january):
    """A single bank deposit settles two separately posted receipts."""
    sid = january["session"]["id"]
    second = cash_in(client, "2026-01-05", world["ledger"], world["sales"], "60.00", "Customer A part 2")
    first = cash_in(client, "2026-01-04", world["ledger"], world["sales"], "40.00", "Customer A part 1")
    deposit = import_statement(
        client,
        statement_payload(
            world["cash"], "2026-01b", "2026-01-01", "2026-01-31", "49.51",
            [{"date": "2026-01-06", "amount": "100.00", "description": "Batch deposit"}],
        ),
    )["transactions"][0]

    m1 = match(client, sid, deposit["id"], cash_line_id(first, world["ledger"]))
    assert m1["amount"] == "40.00"
    txn = next(t for t in client.get("/api/v1/cash/bank-transactions").json() if t["id"] == deposit["id"])
    assert (txn["status"], txn["remaining_amount"]) == ("partially_matched", "60.00")

    m2 = match(client, sid, deposit["id"], cash_line_id(second, world["ledger"]))
    assert m2["amount"] == "60.00"
    txn = next(t for t in client.get("/api/v1/cash/bank-transactions").json() if t["id"] == deposit["id"])
    assert txn["status"] == "matched"


def test_partial_reconciliation_ledger_line_against_two_bank_transactions(client, world, january):
    """One posted payment of 40.50 appears on the statement as 30.00 + 10.50."""
    sid = january["session"]["id"]
    split = import_statement(
        client,
        statement_payload(
            world["cash"], "2026-01c", "2026-01-01", "2026-01-31", "49.51",
            [
                {"date": "2026-01-10", "amount": "-30.00", "description": "Rent 1/2"},
                {"date": "2026-01-10", "amount": "-10.50", "description": "Rent 2/2"},
            ],
        ),
    )["transactions"]
    match(client, sid, split[0]["id"], january["payment_line"], "30.00")
    unmatched = client.get(f"/api/v1/cash/reconciliations/{sid}/unmatched").json()
    line = next(l for l in unmatched["ledger_lines"] if l["journal_line_id"] == january["payment_line"])
    assert (line["status"], line["matched_amount"], line["remaining_amount"]) == ("partially_matched", "30.00", "10.50")
    assert line["signed_amount"] == "-40.50"  # credit to cash = money out

    match(client, sid, split[1]["id"], january["payment_line"])
    unmatched = client.get(f"/api/v1/cash/reconciliations/{sid}/unmatched").json()
    assert all(l["journal_line_id"] != january["payment_line"] for l in unmatched["ledger_lines"])


def test_explicit_amount_may_not_exceed_either_side(client, world, january):
    sid = january["session"]["id"]
    resp = match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"], "100.01", expect=409)
    assert "exceeds" in resp["detail"]
    match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"], "70.00")
    # Remaining 30.00 on both sides; the same pair cannot be matched twice, another line can.
    match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"], "30.00", expect=409)
    other = cash_in(client, "2026-01-05", world["ledger"], world["sales"], "30.00", "Customer A tail")
    resp = match(client, sid, january["txns"]["Customer A"]["id"], cash_line_id(other, world["ledger"]), "30.01", expect=409)
    assert "unmatched bank amount 30.00" in resp["detail"]
    match(client, sid, january["txns"]["Customer A"]["id"], cash_line_id(other, world["ledger"]))


def test_direction_debit_credit_is_enforced(client, world, january):
    sid = january["session"]["id"]
    # Bank inflow vs. a credit (payment) line, and bank outflow vs. a debit (receipt) line.
    resp = match(client, sid, january["txns"]["Customer A"]["id"], january["payment_line"], expect=422)
    assert "inflow can only be matched with a debit" in resp["detail"]
    resp = match(client, sid, january["txns"]["Rent"]["id"], january["receipt_line"], expect=422)
    assert "outflow can only be matched with a credit" in resp["detail"]


def test_draft_entries_cannot_be_reconciled_until_posted(client, world, january):
    sid = january["session"]["id"]
    draft = entry(
        client, "2026-01-20",
        [{"account_id": world["rent"], "debit": "9.99"}, {"account_id": world["ledger"], "credit": "9.99"}],
        "Bank fee", post=False,
    )
    line = cash_line_id(draft, world["ledger"])
    resp = match(client, sid, january["txns"]["Bank fee"]["id"], line, expect=422)
    assert "draft" in resp["detail"]
    assert summary(client, sid)["unmatched_bank_count"] == 3

    # Post through the posting service; now the same line reconciles.
    assert client.post(f"/api/v1/journal-entries/{draft['id']}/post").status_code == 200
    match(client, sid, january["txns"]["Bank fee"]["id"], line)
    assert summary(client, sid)["unmatched_bank_count"] == 2


def test_line_must_be_on_the_cash_accounts_ledger_account(client, world, january):
    sid = january["session"]["id"]
    elsewhere = cash_in(client, "2026-01-05", world["other_ledger"], world["sales"], "100.00", "petty")
    resp = match(client, sid, january["txns"]["Customer A"]["id"], cash_line_id(elsewhere, world["other_ledger"]), expect=422)
    assert "is not on ledger account" in resp["detail"]
    # The revenue line of a valid entry is not a cash line either.
    revenue_line = next(l["id"] for l in january["receipt"]["lines"] if l["account_id"] == world["sales"])
    match(client, sid, january["txns"]["Customer A"]["id"], revenue_line, expect=422)


def test_bank_transaction_of_other_account_or_unknown_ids(client, world, january):
    sid = january["session"]["id"]
    other = import_statement(
        client,
        statement_payload(world["other_cash"], "P1", "2026-01-01", "2026-01-31", "0",
                          [{"date": "2026-01-05", "amount": "5.00", "description": "x"}]),
    )["transactions"][0]
    match(client, sid, other["id"], january["receipt_line"], expect=422)
    match(client, sid, 9999, january["receipt_line"], expect=404)
    match(client, sid, january["txns"]["Customer A"]["id"], 9999, expect=404)
    assert client.post("/api/v1/cash/reconciliations/9999/matches", json={"bank_transaction_id": 1, "journal_line_id": 1}).status_code == 404


def test_unmatch(client, world, january):
    sid = january["session"]["id"]
    m = match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"])
    assert client.delete(f"/api/v1/cash/reconciliations/{sid}/matches/{m['id']}").status_code == 204
    assert client.delete(f"/api/v1/cash/reconciliations/{sid}/matches/{m['id']}").status_code == 404
    assert summary(client, sid)["unmatched_bank_count"] == 3


# --- Review -----------------------------------------------------------------------------


def test_summary_reports_discrepancies_and_balances_when_everything_is_matched(client, world, january):
    sid = january["session"]["id"]
    before = summary(client, sid)
    assert before["is_balanced"] is False
    assert before["bank_transactions_count"] == 3
    assert before["bank_transactions_total"] == "49.51"
    assert before["unmatched_bank_count"] == 3
    assert before["cleared_balance"] == "0.00"
    assert before["difference"] == "49.51"
    assert before["ledger_balance_at_period_end"] == "59.50"  # 100 - 40.50, fee not yet booked
    assert before["outstanding_ledger_count"] == 2
    assert before["outstanding_ledger_total"] == "59.50"
    kinds = {d["kind"] for d in before["discrepancies"]}
    assert kinds == {"unmatched_bank_transaction", "outstanding_ledger_line", "difference"}
    # adjusted bank = 49.51 + 59.50 ; adjusted ledger = 59.50 + 49.51 -> no unexplained difference
    assert before["unexplained_difference"] == "0.00"

    match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"])
    match(client, sid, january["txns"]["Rent"]["id"], january["payment_line"])
    mid = summary(client, sid)
    assert mid["matched_bank_total"] == "59.50"
    assert mid["unmatched_bank_total"] == "-9.99"
    assert mid["difference"] == "-9.99"
    assert [d["kind"] for d in mid["discrepancies"]] == ["unmatched_bank_transaction", "difference"]
    assert mid["discrepancies"][0]["bank_transaction_id"] == january["txns"]["Bank fee"]["id"]

    fee = cash_out(client, "2026-01-20", world["ledger"], world["rent"], "9.99", "Bank fee")
    match(client, sid, january["txns"]["Bank fee"]["id"], cash_line_id(fee, world["ledger"]))
    done = summary(client, sid)
    assert done["is_balanced"] is True
    assert done["difference"] == "0.00"
    assert done["discrepancies"] == []
    assert done["cleared_balance"] == done["session"]["closing_balance"] == "49.51"
    assert done["ledger_balance_at_period_end"] == "49.51"


def test_summary_flags_partial_matches_and_unexplained_differences(client, world, january):
    sid = january["session"]["id"]
    match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"], "70.00")
    s = summary(client, sid)
    assert s["partially_matched_bank_count"] == 1
    kinds = [d["kind"] for d in s["discrepancies"]]
    assert "partially_matched_bank_transaction" in kinds and "partially_matched_ledger_line" in kinds
    partial = next(d for d in s["discrepancies"] if d["kind"] == "partially_matched_bank_transaction")
    assert partial["amount"] == "30.00"

    # A session whose statement balances disagree with its transactions shows an unexplained difference.
    wrong = open_session(
        client, world["cash"], period_start="2026-02-01", period_end="2026-02-28",
        opening_balance="49.51", closing_balance="1000.00",
    )
    s2 = summary(client, wrong["id"])
    assert s2["unexplained_difference"] == "950.49"
    kinds2 = [d["kind"] for d in s2["discrepancies"]]
    assert "unexplained_difference" in kinds2 and "difference" in kinds2
    # January's leftovers are reported as prior-period items, not as this period's.
    assert kinds2.count("unmatched_prior_bank_transaction") == 3
    assert s2["bank_transactions_count"] == 0 and s2["unmatched_bank_count"] == 0


def test_opening_balance_mismatch_with_previous_closed_session(client, world, january):
    sid = january["session"]["id"]
    resp = client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "initial"})
    assert resp.status_code == 200
    feb = open_session(
        client, world["cash"], period_start="2026-02-01", period_end="2026-02-28",
        opening_balance="50.00", closing_balance="50.00",
    )
    d = next(d for d in summary(client, feb["id"])["discrepancies"] if d["kind"] == "opening_balance_mismatch")
    assert d["amount"] == "0.49"
    assert f"#{sid}" in d["message"]


# --- Auto-match -------------------------------------------------------------------------


def test_auto_match_dry_run_then_apply(client, world, january):
    sid = january["session"]["id"]
    dry = client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match", json={"dry_run": True}).json()
    assert dry["dry_run"] is True and dry["applied_count"] == 0
    assert {(s["bank_transaction_id"], s["journal_line_id"], s["applied"]) for s in dry["suggestions"]} == {
        (january["txns"]["Customer A"]["id"], january["receipt_line"], False),
        (january["txns"]["Rent"]["id"], january["payment_line"], False),
    }
    assert all("same amount" in s["reason"] for s in dry["suggestions"])
    assert client.get(f"/api/v1/cash/reconciliations/{sid}/matches").json() == []

    applied = client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match").json()
    assert applied["applied_count"] == 2
    assert all(s["applied"] for s in applied["suggestions"])
    assert summary(client, sid)["unmatched_bank_count"] == 1  # the fee has no ledger line yet

    # Idempotent: nothing left to suggest.
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match").json()["applied_count"] == 0


def test_auto_match_respects_date_tolerance_and_reports_ambiguity(client, world, january):
    sid = january["session"]["id"]
    # Rent: ledger 2026-01-10, bank 2026-01-11 -> outside a zero-day tolerance.
    strict = client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match", json={"date_tolerance_days": 0, "dry_run": True}).json()
    assert [s["bank_transaction_id"] for s in strict["suggestions"]] == [january["txns"]["Customer A"]["id"]]

    # Two identical candidates for the deposit -> ambiguous, unless a reference breaks the tie.
    cash_in(client, "2026-01-05", world["ledger"], world["sales"], "100.00", "Customer A again")
    result = client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match", json={"dry_run": True}).json()
    # The bank line carries reference INV-1 and only the first receipt has that reference.
    chosen = next(s for s in result["suggestions"] if s["bank_transaction_id"] == january["txns"]["Customer A"]["id"])
    assert chosen["journal_line_id"] == january["receipt_line"]
    assert "INV-1" in chosen["reason"]

    cash_in(client, "2026-01-05", world["ledger"], world["sales"], "100.00", "Customer A third", reference="INV-1")
    result = client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match", json={"dry_run": True}).json()
    assert [a["bank_transaction_id"] for a in result["ambiguous"]] == [january["txns"]["Customer A"]["id"]]
    assert len(result["ambiguous"][0]["candidate_journal_line_ids"]) == 2


# --- Close / reopen -----------------------------------------------------------------------


def test_close_requires_balance_unless_forced_with_notes(client, world, january):
    sid = january["session"]["id"]
    resp = client.post(f"/api/v1/cash/reconciliations/{sid}/close")
    assert resp.status_code == 409
    assert "not balanced" in resp.json()["detail"]
    resp = client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True})
    assert resp.status_code == 422
    resp = client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "fee booked in Feb"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "closed" and body["closed_at"] is not None and body["notes"] == "fee booked in Feb"
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "x"}).status_code == 409


def test_closed_session_is_immutable_until_reopened(client, world, january):
    sid = january["session"]["id"]
    m = match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"])
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "partial"}).status_code == 200

    assert match(client, sid, january["txns"]["Rent"]["id"], january["payment_line"], expect=409)["detail"].endswith("is not open")
    assert client.delete(f"/api/v1/cash/reconciliations/{sid}/matches/{m['id']}").status_code == 409
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match").status_code == 409
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": "test"}).status_code == 200

    reopened = client.get(f"/api/v1/cash/reconciliations/{sid}").json()
    assert reopened["status"] == "open" and reopened["closed_at"] is None and reopened["reopen_count"] == 1
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": "test"}).status_code == 409
    match(client, sid, january["txns"]["Rent"]["id"], january["payment_line"])
    assert client.delete(f"/api/v1/cash/reconciliations/{sid}/matches/{m['id']}").status_code == 204


def test_balanced_session_closes_without_force(client, world, january):
    sid = january["session"]["id"]
    client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match")
    fee = cash_out(client, "2026-01-20", world["ledger"], world["rent"], "9.99", "Bank fee")
    match(client, sid, january["txns"]["Bank fee"]["id"], cash_line_id(fee, world["ledger"]))
    assert summary(client, sid)["is_balanced"] is True
    assert client.post(f"/api/v1/cash/reconciliations/{sid}/close").json()["status"] == "closed"


# --- Accounting boundary ---------------------------------------------------------------------


def test_reconciliation_never_changes_the_ledger(client, world, january):
    sid = january["session"]["id"]
    before = ledger_snapshot(client)
    balance = client.get(f"/api/v1/cash-accounts/{world['cash']}/balance").json()["balance"]

    client.post(f"/api/v1/cash/reconciliations/{sid}/auto-match")
    match(client, sid, january["txns"]["Customer A"]["id"], january["receipt_line"], expect=409)
    client.post(f"/api/v1/cash/reconciliations/{sid}/close", json={"force": True, "notes": "n"})
    client.post(f"/api/v1/cash/reconciliations/{sid}/reopen", json={"reason": "test"})

    assert ledger_snapshot(client) == before
    assert client.get(f"/api/v1/cash-accounts/{world['cash']}/balance").json()["balance"] == balance == "59.50"
    assert client.get("/api/v1/reports/trial-balance").json()["total_debit"] == "100.00"  # cash 59.50 + rent 40.50
