"""Accounts payable: vendors, bills, credits, advances, holds, discounts, payment runs,
reconciliation, period end.

Layout
------
``models.py``          tables and the rules the database enforces on them
``schemas.py``         API models and amount validation (Decimal, whole cents)
``services/``          domain services (see ``services/__init__`` for the map and the
                       transaction-ownership rules): documents, credits, settlement,
                       payments, payment_runs, gateway, execution_state, orchestration,
                       recovery, diagnostics, reconciliation, reconstruction,
                       supplier_statements, validation, closing, vendors, duplicates,
                       common
``ledger_adapter.py``  the only code that writes to the ledger; builds draft journal
                       entries (bill, payment, credit, discount, reversal) and posts them
                       through ``app.services.posting.post_entry`` without committing
``integration.py``     explicit ports to other modules (ledger, audit, payment gateway,
                       accounting periods, authorization, fault injection) with
                       request / response contracts, and the data links to cash /
                       reporting / tax / budgeting
``router.py``          ``/api/v1/payables`` routes

Accounting principles
---------------------
* Every accounting effect is a dated, append-only row (document approval,
  payment, allocation, application, discount, reversal, unapplication). Rows
  are closed with an effective date and actor, never deleted or amended, so
  every report can be reproduced as of any date.
* The module never mutates ``journal_entries`` / ``journal_lines`` directly and
  never bypasses the posting service. Corrections post new (mirror) entries.
* Nothing in a payment run touches the ledger before execution is authorised
  and performed; execution goes through durable payment operations (intent
  committed before the gateway is called, money-moved states never resubmitted)
  behind a replaceable gateway port, with an AP-owned recovery worker.
* Closed AP periods refuse backdated effects in the application and the database.
"""
