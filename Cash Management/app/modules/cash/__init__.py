"""Cash management module.

Features
--------
* Cash accounts linked to asset ledger accounts (``/cash-accounts``), with an
  optional ``minimum_balance`` threshold for liquidity alerts.
* Bank statement import (``/cash/bank-statements``): idempotent per statement
  reference and per transaction fingerprint; never posts anything.
* Bank reconciliation (``/cash/reconciliations``): sessions per cash account
  and period, full or partial matches between bank transactions and *posted*
  journal lines, automatic suggestions, review summary with discrepancies,
  close with certified (hashed, append-only) evidence, drift detection,
  reopen with a mandatory reason and a full history.
* Reports (``/cash/reports/...``): dated balances, daily balances, cash flow by
  operating / investing / financing activity with internal transfers excluded.
* Liquidity projection (``/cash/projection``): posted balance + future-dated
  posted entries + planned cash flows over a configurable horizon, with
  threshold alerts and an itemised explanation.

Accounting boundary
-------------------
The General Ledger is the only source of truth. This module never creates,
edits or posts journal entries (the posting service is the only way to post)
and never stores balances. Draft entries, bank transactions (matched or not)
and planned cash flows are never counted as posted financial transactions.

Monetary values are ``Decimal`` end to end; nothing goes through ``float``.
"""
