# FMS Development Roadmap

**Project:** Financial Management System (FMS)  
**Repository:** https://github.com/Thomasmelaku27/fms  
**Status:** Expanded proposal-aligned working plan — verify implementation before marking tasks complete  
**Target stack:** Python, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL; SQLite for local development and tests

## Purpose

Build a modular Financial Management System with accurate double-entry accounting,
traceable financial workflows, and a maintainable foundation for future ERP integration.
This roadmap describes planned work, not claims of features already implemented.
It aligns with the October 8, 2026 *AI-Enabled Financial Management and Accounting System* proposal, while keeping eligible accounting application development distinct from aspirational AI/ML work.
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

### Phase 6 — Budgeting and deterministic planning (7–11 hours)
- [ ] Create annual, periodic, departmental, and project budgets, with revenue targets and spending caps.
- [ ] Implement versioned revisions, approval states, effective dates, and immutable approved versions.
- [ ] Produce budget-versus-actual variance reports using posted ledger records.
- [ ] Add user-defined what-if scenarios and transparent rule-based or arithmetic projections.
- [ ] Support rolling forecasts based on explicit formulas and synthetic sample data; label inputs and assumptions.
- [ ] Test budget ownership, period consistency, arithmetic, and permission boundaries.

**Acceptance:** approved budgets have traceable versions, calculated variances tie to verified ledger amounts, and scenarios cannot silently change approved budgets. Predictive model training is not part of this phase.

### Phase 7 — Tax records and configurable compliance support (6–10 hours)
- [ ] Define jurisdiction-configurable tax codes, rates, effective dates, and taxable transaction attributes.
- [ ] Add VAT/GST and withholding *tracking* where relevant to configured example rules.
- [ ] Create tax-period summaries, filing checklists, document references, and deadline reminders.
- [ ] Generate draft tax reports from posted transactions with full source traceability.
- [ ] Validate rounding, rate-effective-date handling, exemptions, and correction workflows.
- [ ] Document that reports require qualified tax review and the software does not determine legal obligations.

**Acceptance:** deterministic sample tax reports reconcile to source postings and configurable example rules; no automated filing or independent legal conclusions.

### Phase 8 — Financial statements, dashboard, and exports (8–12 hours)
- [ ] Expand trial balance and ledger-detail reports to balance sheet and income statement.
- [ ] Implement rule-based cash-flow statement classifications and AP/AR aging summaries.
- [ ] Add revenue, expense, and budget-versus-actual management views.
- [ ] Build a web dashboard for revenue, expenses, profit, cash, receivables, and payables.
- [ ] Provide CSV and Excel export, and PDF export where feasible; ensure consistent date filters.
- [ ] Add drill-down links from displayed figures to permitted source records.
- [ ] Test report tie-outs, empty periods, closed periods, large datasets, and authorization.

**Acceptance:** all figures are traceable to posted or reconciled records, exports match on-screen aggregates, and users see only authorized information.

### Phase 9 — Approval workflows, roles, and audit security (7–11 hours)
- [ ] Define System Administrator, Accountant, Finance Manager, AP Officer, AR Officer, Management, and read-only Auditor roles.
- [ ] Apply permissions to reading, preparing, posting, approving, reversing, and administering operations.
- [ ] Implement segregation-of-duties checks (for example, self-approval restrictions).
- [ ] Add configurable approval thresholds and multi-level approval where applicable.
- [ ] Add tamper-evident activity records for sensitive actions and configuration changes.
- [ ] Document secure session management, MFA integration points, and backup/restore procedures.
- [ ] Test authorization-denied paths and concurrency around approval transitions.

**Acceptance:** sensitive actions cannot bypass RBAC or approvals; audit events capture actor, action, target, and timestamp; read-only auditor access works as specified.

### Phase 10 — External integration contracts and migration tools (6–10 hours)
- [ ] Specify versioned import/export contracts for bank statements and payment status files.
- [ ] Support mock/sandbox payroll journal imports, POS/e-commerce settlement summaries, and ERP master-data exchange.
- [ ] Define idempotent inbound processing, request validation, retry behavior, and reconciliation reports.
- [ ] Build sample-data migration templates and dry-run validation with rollback guidance.
- [ ] Document future procurement and tax-authority connectors without assuming external APIs are available.
- [ ] Test malformed, duplicate, out-of-order, and partial import cases.

**Acceptance:** synthetic third-party fixtures pass repeatable import/export contract tests; failures do not introduce duplicate ledger transactions.

### Phase 11 — Frontend, operations, documentation, and releases (7–12 hours)
- [ ] Establish accessible React/Next.js + TypeScript UI conventions, where a frontend is added.
- [ ] Add user-facing workflow screens and role-aware navigation for implemented modules.
- [ ] Build Docker-based local deployment, environment-variable templates, and health checks.
- [ ] Verify database migrations, backup and restoration drills, and failure recovery.
- [ ] Add observability for slow queries, failed imports, and application errors (without logging secrets).
- [ ] Prepare installation, user, API, release, and troubleshooting documentation.
- [ ] Track measurable baselines: invoice approval time, reconciliation effort, reporting turnaround, and uptime.

**Acceptance:** an independent developer can provision a clean synthetic-data environment, exercise supported workflows, and reproduce test and recovery procedures.

## Long-term product vision — separate from CC-CLI eligible scope

The October 8, 2026 proposal also describes an assistive AI layer with transaction classification, invoice/document OCR and extraction, statistical or ML anomaly/fraud-risk alerts, customer payment prediction, cash-flow forecasts, draft budget recommendations, and a natural-language financial assistant. It proposes confidence scores, review queues, permissions, human overrides, and read-only source-grounded answers. The proposal additionally mentions model tracking, retraining, and model evaluation.

**These are proposal concepts, not implemented features or commitments for a CC-CLI coding session.** CC-CLI's supplied guideline excludes AI model development, training, fine-tuning, evaluation/benchmarking, and inference/serving infrastructure. Do not perform these activities as part of the submitted CC-CLI project without written clarification or approval from the program. Keep potential AI code and real/regulated financial data outside CC-CLI tasks. If pursued independently in a separate eligible context, retain the deterministic ledger and human approval boundary.

### Future AI concept backlog (not part of CC-CLI scope)
- [ ] AI-assisted invoice and receipt extraction with explicit reviewer confirmation.
- [ ] Suggested account/expense categorization with confidence explanations.
- [ ] Anomaly and fraud-risk *flags* for investigators, never automatic allegations.
- [ ] Receivables late-payment risk and cash-flow prediction for review.
- [ ] Natural-language assistant restricted to read-only, user-authorized verified records.
- [ ] Model governance, drift measurement, and model lifecycle management.

### Proposal-wide timeline (not a CC-CLI time estimate)

The source proposal presents an **indicative 25-week / eight-phase** implementation: requirements (2 weeks); architecture and UI/UX (2); core accounting modules (6); budgeting and tax (3); AI components (5); integration (2); testing/security (3); deployment/training (2). This assumes a broader project and is **not** an estimate of CC-CLI hours or a guarantee of completion. The engineering hours for the non-AI tasks above are independently estimated below and must be refined after repository inspection.

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
- Any AI/ML capabilities in the proposal are documented solely under the separate
  long-term product vision, not as CC-CLI deliverables. The supplied guideline excludes
  AI model building, evaluation/benchmarking, and inference/serving infrastructure.
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
| Budgeting and deterministic planning | 7–11 hours |
| Tax and compliance support | 6–10 hours |
| Financial statements, dashboard, exports | 8–12 hours |
| Approvals, RBAC, audit security | 7–11 hours |
| Integration contracts and migration | 6–10 hours |
| Frontend, operations, and documentation | 7–12 hours |
| **Total proposed non-AI development** | **71–109 hours** |

These are planning estimates, not hours worked, guaranteed deliverables, or
statements about program selection. Re-estimate after auditing the codebase.

## Proposal reference

*AI-Enabled Financial Management and Accounting System*, project proposal v1.0, October 8, 2026 (19 pages). The proposal is a conceptual scope document; the repository README, tests, and merged code are authoritative for actual implementation status. The proposal expressly excludes payroll calculation, inventory/procurement as full ERP modules, independent tax-law determinations, and fully autonomous payments/postings.
