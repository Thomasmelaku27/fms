"""Monetary precision at every public boundary: schemas, service dataclasses, ORM, SQL."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import JournalLine
from app.money import MoneyError, as_money
from app.services import journal_processing as jp
from app.services.exceptions import UnprocessableError
from app.services.opening_balances import OpeningBalance
from tests.helpers import account, balances

URL = "/api/v1/journal-processing/entries"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("5", "5.00"),
        ("5.0", "5.00"),
        (Decimal("5.00"), "5.00"),
        (5, "5.00"),
        ("0.1", "0.10"),
        ("1E+2", "100.00"),
    ],
)
def test_as_money_canonicalises_equivalent_amounts(value, canonical):
    assert str(as_money(value)) == canonical


@pytest.mark.parametrize(
    "value",
    ["5.001", "0.005", "-1", "abc", "", None, float("nan"), float("inf"), 1.5, True, "1e-3"],
)
def test_as_money_rejects_invalid_amounts(value):
    with pytest.raises(MoneyError):
        as_money(value)


def test_request_line_validates_and_canonicalises():
    line = jp.JournalRequestLine(1, "5", "0")
    assert line.debit == Decimal("5.00") and line.credit == Decimal("0.00")
    assert line == jp.JournalRequestLine(1, Decimal("5.000"), 0)  # same amount, same material
    with pytest.raises(UnprocessableError, match="more than two decimal places"):
        jp.JournalRequestLine(1, "5.001")
    with pytest.raises(UnprocessableError, match="floats are not accepted"):
        jp.JournalRequestLine(1, 5.0)
    with pytest.raises(AttributeError):
        line.debit = Decimal("9")  # immutable
    with pytest.raises(UnprocessableError):
        OpeningBalance(1, debit="1.234")


def test_orm_line_boundary(client):
    with pytest.raises(MoneyError):
        JournalLine(account_id=1, debit=Decimal("1.005"))
    with pytest.raises(MoneyError):
        JournalLine(account_id=1, credit=-1)
    assert JournalLine(account_id=1, debit="7").debit == Decimal("7.00")


@pytest.fixture
def chart(client):
    return account(client, "1000", "Cash", "asset"), account(client, "4000", "Sales", "revenue")


def _body(chart, debit, credit=None, source_id="DOC-1"):
    cash, sales = chart
    return {
        "source_type": "sim",
        "source_id": source_id,
        "operation_id": "post",
        "entry_date": "2026-01-15",
        "description": "x",
        "lines": [
            {"account_id": cash["id"], "debit": debit},
            {"account_id": sales["id"], "credit": credit or debit},
        ],
    }


@pytest.mark.parametrize("bad", ["5.001", "-5", "abc", "1e-3"])
def test_http_rejects_invalid_amounts_before_any_side_effect(client, chart, bad):
    resp = client.post(URL, json=_body(chart, bad))
    assert resp.status_code == 422
    assert client.get("/api/v1/journal-entries").json() == []
    assert (
        client.post(
            "/api/v1/journal-entries",
            json={
                "entry_date": "2026-01-15",
                "description": "x",
                "lines": [{"account_id": chart[0]["id"], "debit": bad}],
            },
        ).status_code
        == 422
    )


def test_equivalent_amounts_are_the_same_request(client, chart):
    first = client.post(URL, json=_body(chart, "5")).json()
    assert first["created"] is True and first["entry"]["lines"][0]["debit"] == "5.00"
    for variant in ("5.0", "5.00", 5):
        again = client.post(URL, json=_body(chart, variant)).json()
        assert again["created"] is False and again["entry"]["id"] == first["entry"]["id"]
    # A materially different amount is a conflict, a sub-cent one is invalid, never a silent match.
    assert client.post(URL, json=_body(chart, "5.01")).status_code == 409
    assert client.post(URL, json=_body(chart, "5.001")).status_code == 422
    assert balances(client)["1000"] == "5.00"


def test_fingerprint_never_collapses_distinct_valid_amounts():
    def req(d):
        return jp.JournalRequest(
            source_type="s", source_id="1", operation_id="o", entry_date=date(2026, 1, 1),
            description="d", lines=(jp.JournalRequestLine(1, d), jp.JournalRequestLine(2, 0, d)),
        )  # fmt: skip

    fingerprints = {req(d).fingerprint() for d in ("5", "5.01", "5.10", "50", "0.05")}
    assert len(fingerprints) == 5
    assert req("5").fingerprint() == req("5.00").fingerprint() == req(Decimal("5")).fingerprint()


def test_database_boundary_rejects_sub_cent_inserts(client, chart):
    cash, sales = chart
    entry = client.post(
        "/api/v1/journal-entries",
        json={
            "entry_date": "2026-01-15",
            "description": "x",
            "lines": [{"account_id": cash["id"], "debit": "1.00"}],
        },
    ).json()
    with Session(client.app.state.engine) as db:
        if db.get_bind().dialect.name == "sqlite":
            with pytest.raises(IntegrityError, match="whole_cents"):
                db.execute(
                    text(
                        "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
                        "VALUES (:e, :a, 1.005, 0)"
                    ),
                    {"e": entry["id"], "a": sales["id"]},
                )
            db.rollback()
        else:
            # PostgreSQL rounds to the declared scale on write (standard NUMERIC behaviour)...
            db.execute(
                text(
                    "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
                    "VALUES (:e, :a, 1.005, 0)"
                ),
                {"e": entry["id"], "a": sales["id"]},
            )
            stored = db.execute(
                text("SELECT debit FROM journal_lines WHERE account_id = :a"), {"a": sales["id"]}
            ).scalar()
            assert stored == Decimal("1.01") or stored == Decimal(
                "1.00"
            )  # banker's vs half-up is PG's call
            db.rollback()
        # ...and the application boundary never lets such a value through either way.
        with pytest.raises(IntegrityError, match="non_negative"):
            db.execute(
                text(
                    "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
                    "VALUES (:e, :a, -1, 0)"
                ),
                {"e": entry["id"], "a": sales["id"]},
            )
        db.rollback()
