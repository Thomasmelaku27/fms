# FMS Development Roadmap

**Project:** Financial Management System (FMS)  
**Repository:** https://github.com/Thomasmelaku27/fms  
**Status:** Working plan — update as implementation and tests are verified  
**Target stack:** Python, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL; SQLite for local development and tests

## Purpose

Build a modular Financial Management System with accurate double-entry accounting,
traceable financial workflows, and a maintainable foundation for future ERP integration.
This roadmap describes planned work, not claims of features already implemented.
Development and demonstration should use synthetic data only.

## Documented baseline (verify against code before marking complete)

The current README describes:
- Chart-of-accounts creation, listing, retrieval, activation, and deactivation.
- Journal-entry draft creation, retrieval, listing, and posting.
- A reusable posting service with balance and account-state validation.
- Alembic migrations, SQLite/PostgreSQL database constraints and triggers, and pytest tests.

The README also identifies these gaps: draft editing/deletion endpoints, reversing
entries, accounting periods, AP/AR, budgeting, tax, reporting, authorization/RBAC,
audit trails, and AI components. Items below are plans until tested and merged.

## Planned phases and acceptance criteria

### Phase 0 — Repository readiness and developer experience (3–5 hours)
- [ ] Verify ownership, authorization to publish, and independent project eligibility.
- [ ] Check the full Git history for credentials, private data, and restricted code.
- [ ] Confirm root `LICENSE`, `README.md`, `.gitignore`, and sanitized `.env.example`.
- [ ] Fix broken documentation references, including `PARALLEL_WORK.md` if missing.
- [ ] Document local SQLite and optional PostgreSQL setup; verify a clean installation.
- [ ] Add CI checks for unit tests, formatting/linting, and migration health.

**Acceptance:** clean clone can be configured and tested; documented setup commands
work; repository and history contain no exposed secrets or real financial data.

### Phase 1 — General Ledger completion (6–8 hours)
- [ ] Implement draft journal entry edit and delete APIs with validation.
- [ ] Implement reversal entries referencing the original posted entry.
- [ ] Ensure posted entries and lines remain immutable across all update paths.
- [ ] Implement accounting periods and posting restrictions for closed periods.
- [ ] Add idempotency and concurrency tests around posting and reversals.
- [ ] Test failures for unbalanced entries, inactive accounts, invalid amounts,
      and insufficient journal lines in SQLite and PostgreSQL.

**Acceptance:** drafts can be maintained safely; posted entries cannot be edited;
reversals preserve traceability; posting validation has automated tests.

### Phase 2 — Accounts Payable (5–7 hours)
- [ ] Add vendor entities and validated invoice records.
- [ ] Implement invoice state transitions and approval controls.
- [ ] Record partial/full payments and prevent overpayment.
- [ ] Integrate approved invoice and payment events with General Ledger postings.
- [ ] Add payable aging and reconciliation tests.

**Acceptance:** valid invoices progress through documented states; payable balances
reconcile to ledger postings; duplicate payment attempts are rejected.

### Phase 3 — Accounts Receivable (5–7 hours)
- [ ] Add customer entities and receivable invoices.
- [ ] Implement invoice status, due dates, and payment application.
- [ ] Support partial payments, outstanding balances, and credit adjustments.
- [ ] Integrate relevant transactions with General Ledger.
- [ ] Test aging, duplicate settlement prevention, and reconciliation.

**Acceptance:** receivable amounts and statuses are consistent with payments and
ledger entries, including partial settlements.

### Phase 4 — Cash Management (5–7 hours)
- [ ] Define cash and bank-account records using synthetic examples.
- [ ] Implement statement import validation and safe transaction matching.
- [ ] Build bank reconciliation workflows with clear exceptions.
- [ ] Add a cash-position report derived from reconciled records.
- [ ] Test unmatched, duplicate, and conflicting transactions.

**Acceptance:** reconciliations are reproducible; exceptions are visible; cash
balances trace back to ledger records.

### Phase 5 — Reporting, access control, and integration hardening (6–9 hours)
- [ ] Implement trial balance and transaction-detail reporting.
- [ ] Add basic financial statement generation after ledger totals are validated.
- [ ] Add authentication/role permissions for sensitive state-changing actions.
- [ ] Add an append-only audit trail for critical accounting events.
- [ ] Run cross-module integration tests and migration upgrade tests.
- [ ] Improve API examples, validation errors, and deployment documentation.

**Acceptance:** authorized actions are enforced, key accounting events are auditable,
and integrated workflows pass deterministic tests.

## Execution workflow

1. Open a GitHub issue with the business goal, affected module, acceptance criteria,
   expected data invariants, and test plan.
2. Create a scoped feature branch (for example `feat/gl-reversals`).
3. Establish a reproducible baseline: inspect `git status`, run migrations, and
   run relevant tests before making changes.
4. Implement small, reviewable changes with application-level and database-level
   safeguards where appropriate.
5. Validate negative cases, concurrency where applicable, regressions, and both
   SQLite and PostgreSQL behavior when database-specific rules are affected.
6. Review the diff for unrelated changes, secrets, synthetic-only fixtures,
   backward compatibility, and documentation changes.
7. Merge only after acceptance criteria pass; preserve the real commit history.
8. Update this roadmap and relevant issues with verified outcomes.

## Module boundaries for parallel work

- **General Ledger:** accounts, journals, periods, accounting invariants, postings.
- **Accounts Payable:** vendors, bills, approvals, disbursements; GL through defined interfaces.
- **Accounts Receivable:** customers, invoices, receipts; GL through defined interfaces.
- **Cash Management:** bank accounts, matching, reconciliation, cash reporting.
- **Shared platform:** settings, migrations, common types, auth, observability, CI.

Agree on shared models, API contracts, ownership of migration revisions, and GL
integration behavior before parallel implementation. Avoid simultaneous conflicting
schema changes to the same tables or migrations.

## Scope and safeguards

- Use fictitious vendors, customers, accounts, and transactions in all examples/tests.
- Never commit credentials, `.env` files, bank statements, or personal/customer records.
- Preserve existing legitimate commits rather than replacing the repository history.
- Do not represent planned features as already implemented.
- Any future AI/ML functionality mentioned in the broader FMS concept is **outside
  this CC-CLI development roadmap**; the supplied guideline excludes AI model
  building, evaluation/benchmarking, and inference/serving infrastructure.
- Confirm code provenance and CC-CLI eligibility independently; adding documentation
  or a license does not change whether pre-existing code is eligible.

## Estimated work

| Phase | Estimated effort |
| --- | ---: |
| Repository readiness | 3–5 hours |
| General Ledger completion | 6–8 hours |
| Accounts Payable | 5–7 hours |
| Accounts Receivable | 5–7 hours |
| Cash Management | 5–7 hours |
| Reporting, security, and integration | 6–9 hours |
| **Total proposed work** | **30–43 hours** |

These are planning estimates, not hours worked, guaranteed deliverables, or
statements about program selection. Re-estimate after auditing the codebase.
