"""Shared helpers for the General Ledger test suites."""

import threading
from contextlib import contextmanager

import pytest

from app.services import hooks

# Raw-SQL posting used by trigger tests: a posted row must carry posting metadata (CHECK).
POST_SQL = (
    "UPDATE journal_entries SET status = 'posted', posted_at = CURRENT_TIMESTAMP, "
    "posted_by = 'test', posting_source = 'service', correlation_id = 'test' WHERE id = :id"
)


def mark_posted(entry) -> None:
    """ORM equivalent of POST_SQL for tests that flip the status themselves."""
    from datetime import datetime, timezone

    from app.models import EntryStatus

    entry.status = EntryStatus.POSTED
    entry.posted_at = datetime.now(timezone.utc)
    entry.posted_by = "test"
    entry.posting_source = "service"
    entry.correlation_id = "test"


def account(client, code, name, type_, **extra):
    resp = client.post(
        "/api/v1/accounts", json={"code": code, "name": name, "type": type_, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def draft(client, lines, entry_date="2026-01-15", description="Entry", **extra):
    resp = client.post(
        "/api/v1/journal-entries",
        json={"entry_date": entry_date, "description": description, "lines": lines, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def post(client, entry_id, expected=200):
    resp = client.post(f"/api/v1/journal-entries/{entry_id}/post")
    assert resp.status_code == expected, resp.text
    return resp.json()


def trial_balance(client, **params):
    resp = client.get("/api/v1/reports/trial-balance", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def balances(client, **params):
    """{code: balance} for posting accounts, plus totals - a compact fingerprint of the ledger."""
    report = trial_balance(client, **params)
    return {
        **{row["code"]: row["balance"] for row in report["rows"] if not row["is_summary"]},
        "__total_debit__": report["total_debit"],
        "__total_credit__": report["total_credit"],
    }


@pytest.fixture(autouse=True)
def clear_hooks():
    hooks.clear()
    yield
    hooks.clear()


class Gate:
    """Pauses a service at a checkpoint until the test releases it.

    ``reached`` is set when the service arrives at the checkpoint; the service
    then blocks until ``release`` is set (or the timeout expires).
    """

    def __init__(self, name: str, timeout: float = 15.0) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.timeout = timeout
        hooks.install(name, self)

    def __call__(self) -> None:
        self.reached.set()
        if not self.release.wait(self.timeout):
            raise TimeoutError("gate was never released")

    def wait_reached(self) -> None:
        assert self.reached.wait(self.timeout), "service never reached the checkpoint"


class Worker(threading.Thread):
    """Runs ``fn`` in a thread and records its result or exception."""

    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__(daemon=True)
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            self.error = exc

    def finish(self, timeout: float = 20.0):
        self.join(timeout)
        assert not self.is_alive(), "worker did not finish"
        return self


@contextmanager
def failing_checkpoint(name: str, exc: Exception | None = None):
    def boom() -> None:
        raise exc or RuntimeError(f"injected failure at {name}")

    hooks.install(name, boom)
    try:
        yield
    finally:
        hooks.clear()
