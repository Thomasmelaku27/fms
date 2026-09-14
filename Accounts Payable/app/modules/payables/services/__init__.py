"""Accounts payable services, split by domain.

======================  ==============================================================
``common``              errors, lookups, account / vendor policy, row locks, conditional
                        status transitions, accounting-period checks
``vendors``             vendor master data, supplier status changes, discount policies
``duplicates``          explainable near-duplicate detection (flags for review)
``documents``           bill drafting, approval / rejection / reopen, holds and disputes,
                        duplicate-flag review
``credits``             credit notes / adjustments: drafting, approval, dated application
                        and unapplication, reversal
``settlement``          derived settlement state, schedules, advances (unapplied
                        payments), discounts, vendor balances
``payments``            recording payments (idempotent), reversal, correction
``payment_runs``        proposals, versioned review / approval (maker-checker), revalidation,
                        execution through durable payment operations
``gateway``             payment gateway contract + durable simulated gateway (no real transfers)
``execution_state``     durable payment intents: canonical money, fingerprints, CAS transitions,
                        events, worker leases
``orchestration``       drives an operation: submit -> confirm -> record, explicit commit points,
                        fault-injection hooks; never resubmits after money moved
``recovery``            AP-owned recovery worker: decides and performs resume / gateway
                        reconciliation / re-recording / manual escalation
``diagnostics``         payment status view (checklist, guidance), paginated operation lists
``reconciliation``      outstanding, aging, statements, subledger event stream, ledger
                        reconciliation with itemised exceptions
``reconstruction``      independent replay of balances from durable rows (plain SQL) compared
                        with the production reports; as-of and restated views
``supplier_statements`` CSV statement import, validation, deterministic matching, review
``verification``        independent verification of payment operations from durable records
                        (gateway record, events, payment, posted entry, allocations, run);
                        completion policy; run categorisation
``validation``          AP period-end validation checks
``closing``             period close under the close lock, attestations, accepted exceptions
                        and their resolution, forced close, close-package verification
======================  ==============================================================

Transaction ownership
---------------------
Every public service function is one unit of work on the caller's ``Session``:
it commits on success and raises a :class:`~.common.PayablesError` (or lets an
``IntegrityError`` propagate) on failure, in which case nothing has been
committed. Integration ports (ledger, audit, periods) run *inside* that unit
of work and never commit; the ledger adapter posts through a SAVEPOINT-joined
session so one unit of work can post several journal entries atomically.
Functions named ``_*_uncommitted`` are building blocks that write without
committing; only other services call them. The deliberate exceptions are the
payment orchestration (``orchestration`` / ``recovery`` / ``payment_runs.execute_run``),
which commit at every durability point of an operation because the gateway call
in between cannot be rolled back (documented step by step in ``orchestration``),
and ``closing.close_period``, which commits a *failed* attestation before raising.

This package keeps the public names of the former ``services`` module importable
from here, so ``from app.modules.payables import services; services.create_bill(...)``
keeps working.
"""

from app.modules.payables.services import (  # noqa: F401  (re-exported modules)
    closing,
    common,
    credits,
    diagnostics,
    documents,
    duplicates,
    execution_state,
    gateway,
    orchestration,
    payment_runs,
    payments,
    reconciliation,
    reconstruction,
    recovery,
    settlement,
    supplier_statements,
    validation,
    vendors,
    verification,
)
from app.modules.payables.services.common import (  # noqa: F401
    Conflict,
    DuplicateCredit,
    DuplicateInvoice,
    NotFound,
    PayablesError,
    PeriodClosed,
    ValidationFailed,
    get_bill,
    get_credit,
    get_payment,
    get_vendor,
    require_cash_account,
    require_payable_account,
)
from app.modules.payables.services.credits import (  # noqa: F401
    apply_credit,
    approve_credit,
    create_credit,
    credit_approval_problems,
    delete_credit,
    list_credits,
    reject_credit,
    reopen_credit,
    reverse_credit,
    unapply_credit,
    update_credit,
)
from app.modules.payables.services.documents import (  # noqa: F401
    approval_problems,
    approve_bill,
    create_bill,
    delete_bill,
    list_bills,
    place_hold,
    reject_bill,
    release_hold,
    reopen_bill,
    review_duplicate_flag,
    update_bill,
)
from app.modules.payables.services.payment_runs import (  # noqa: F401
    approve_run,
    cancel_run,
    create_run,
    exclude_item,
    execute_run,
    get_run,
    invalidate_approval,
    list_runs,
    proposal_fingerprint,
    revalidate_run,
    review_run,
    send_back_run,
    update_item,
)
from app.modules.payables.services.payments import (  # noqa: F401
    correct_payment,
    list_payments,
    record_payment,
    reverse_payment,
)
from app.modules.payables.services.reconciliation import (  # noqa: F401
    aging_bucket,
    aging_report,
    outstanding_report,
    reconciliation_report,
    schedule_report,
    vendor_events,
    vendor_statement,
)
from app.modules.payables.services.settlement import (  # noqa: F401
    apply_advance,
    get_schedule,
    set_schedule,
    settlement_status,
    unallocate_advance,
    vendor_balance,
    vendor_balances,
)
from app.modules.payables.services.closing import close_period, list_attestations  # noqa: F401
from app.modules.payables.services.validation import list_period_locks, validate_period  # noqa: F401
from app.modules.payables.services.vendors import (  # noqa: F401
    change_vendor_status,
    create_discount_policy,
    create_vendor,
    get_discount_policy,
    list_discount_policies,
    list_vendors,
    update_discount_policy,
    update_vendor,
)

# Backwards-compatible aliases for names tests and callers used on the old module.
_installments_with_remaining = settlement.installments_with_remaining
_sync_bill_status = settlement.sync_bill_status
_sync_credit_status = settlement.sync_credit_status
