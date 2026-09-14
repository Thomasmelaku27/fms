# Financial Management System (backend skeleton)

AI-Enabled Financial Management and Accounting System.
Stack follows the proposal: Python, FastAPI, SQLAlchemy 2, PostgreSQL (SQLite for local dev and tests).

What exists so far:

- Chart of accounts (`Account`): create, list, get, activate / deactivate.
- Journal entries (`JournalEntry` + `JournalLine`): create as draft, list, get, post.
- Posting service (`app/services/posting.py`) reusable outside the API.
- Schema migrations with Alembic.
- Core double-entry rules enforced in the database (see below) as well as in the application.

- Accounts payable (`app/modules/payables`): vendors, supplier bills with line items,
  approval / rejection, payment schedules, partial payments with allocations, supplier credit
  notes and adjustments, payment reversal / correction, outstanding payables and aging,
  supplier statements and ledger reconciliation, payment runs executed as durable,
  recoverable payment operations, historical reconstruction, statement imports and
  attested period close.
  See [Accounts payable](#accounts-payable-appmodulespayables).

Not implemented yet: editing/deleting journal drafts via the API, reversing entries, periods,
AR beyond the starter, budgeting, tax, reporting beyond the trial balance, auth/RBAC, audit
trail, AI components.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head               # create / migrate the database (FMS_DATABASE_URL)
uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
pytest
```

The app does **not** create tables on startup; always run `alembic upgrade head` first
(and again after pulling new migrations). If you have an `fms.db` from before migrations were
introduced, delete it and run the upgrade.

### PostgreSQL

```bash
docker compose up -d db
pip install -e ".[dev,postgres]"
export FMS_DATABASE_URL=postgresql+psycopg://fms:fms@localhost:5432/fms
alembic upgrade head
uvicorn app.main:app --reload
```

## Database migrations

Alembic reads the database URL from the app settings (`FMS_DATABASE_URL` / `.env`), so the CLI
always targets the same database as the app.

```bash
alembic upgrade head                                  # apply all migrations
alembic current                                       # show applied revision
alembic downgrade -1                                  # roll back one revision
alembic revision --autogenerate -m "add something"    # new migration from model changes
```

Autogenerate only detects tables, columns, indexes and foreign keys. Triggers and trigger
functions must be written by hand in the migration (see the initial migration for the pattern:
one block of DDL per supported backend, selected with `op.get_bind().dialect.name`).

## Ledger rules enforced by the database

Per the proposal, "every journal entry must balance before it can be posted, and this rule is
enforced in the database as well as in the application". The initial migration installs, on both
SQLite and PostgreSQL:

| Rule | Mechanism |
| --- | --- |
| Line amounts are non-negative | `CHECK ck_journal_lines_non_negative` |
| A line never has both a debit and a credit | `CHECK ck_journal_lines_not_both_sides` |
| Line amounts are whole cents (at most 2 decimals) | `CHECK ck_journal_lines_whole_cents` |
| Lines of a posted entry cannot be inserted, updated or deleted | triggers on `journal_lines` |
| An entry can only be posted when every line has an amount, it has at least two lines and debits equal credits | triggers on `journal_entries` |
| A posted entry row cannot be updated (any column, incl. status) or deleted | triggers on `journal_entries` |

The same rules are validated in the application first (Pydantic schemas, the posting service).
If invalid data nevertheless reaches the database, the resulting `IntegrityError` is returned
as `422` with the constraint or trigger message, never as a `500`.

**Drafts vs. posted entries.** Drafts are work in progress: a draft may be unbalanced, contain
lines that have no amount yet (debit and credit both `0`), have a single line or no lines at
all. The only rules applied to a draft are about representable data: amounts are non-negative
whole cents and a single line never carries both a debit and a credit. Everything else is
checked when the entry is posted. Once posted, neither the entry row nor its lines can change;
corrections are made with a new (reversing) entry.

## Posting

`POST /api/v1/journal-entries/{id}/post` runs `app.services.posting.post_entry`, which checks
in the application that:

- the entry is still a draft (otherwise `409`);
- every line has an amount on exactly one side;
- there are at least two lines;
- total debits equal total credits;
- every line's account is active (`POST /api/v1/accounts/{id}/deactivate` / `.../activate`).

All violated rules are reported together in a single `422`; on success the posted entry is
returned with `200`. The status change is a conditional update (`WHERE status = 'draft'`), so
two concurrent callers cannot both succeed, and the database triggers re-verify completeness
and balance as a backstop. The inactive-account rule is application-only (it is a chart-of-
accounts policy rather than a ledger integrity rule).

**Exactness on SQLite.** SQLite stores `NUMERIC` as floating point, so `0.10 + 0.20 <> 0.30`.
`ck_journal_lines_whole_cents` guarantees every stored amount is a whole number of cents, and
the SQLite posting trigger sums integer cents (`CAST(ROUND(amount * 100) AS INTEGER)`) and
compares the totals exactly - no tolerance is applied. PostgreSQL `NUMERIC(18, 2)` is exact and
enforces the scale itself (values with more decimals are rounded to cents on write, which is
standard `NUMERIC` behaviour), so its trigger compares the sums directly.

## Tests

Tests build their databases by running the Alembic migrations. Every database-backed test runs
against SQLite, and additionally against PostgreSQL when `FMS_TEST_POSTGRES_URL` is set
(otherwise the PostgreSQL variants are skipped):

```bash
docker compose up -d db
FMS_TEST_POSTGRES_URL=postgresql+psycopg://fms:fms@localhost:5432/fms pytest
```

The suite creates a scratch database named `<dbname>_test` (here `fms_test`) on that server and
drops it afterwards; the database in the URL itself is not touched.

## Layout

```
alembic.ini      Alembic config (URL comes from app settings, not from here)
migrations/
  env.py         migration environment
  versions/      migration scripts
app/
  main.py        app factory, router wiring
  config.py      settings (env prefix FMS_)
  db.py          engine / session helpers, declarative Base, naming convention
  models/        SQLAlchemy models
  schemas/       Pydantic request/response models
  api/           route handlers, error handlers
  modules/       feature modules (payables, receivables, ...); registry.py wires their routers
    payables/    AP workflow: models, schemas, services, router, ledger_adapter, integration
tests/           pytest suite (migrated SQLite per test; PostgreSQL optional)
```


## Accounts payable (`app/modules/payables`)

Routes live under `/api/v1/payables`. Money is `Decimal` end to end (whole cents; quantities
and unit prices allow 4 decimals) and every document links to the chart of accounts. The
module is organised as `models` / `schemas` / `router` plus a `services` package split by
domain (`documents`, `credits`, `settlement`, `payments`, `payment_runs`, `gateway`,
`execution_state`, `orchestration`, `recovery`, `verification`, `diagnostics`, `reconciliation`,
`reconstruction`, `supplier_statements`, `validation`, `closing`, `vendors`, `duplicates`,
`common`) and two integration files (`ledger_adapter.py`, `integration.py`).

### Workflows

| Area | Endpoints | Rules |
| --- | --- | --- |
| Vendors | `POST/GET/PATCH /vendors`, `POST /vendors/{id}/status`, `GET /vendors/{id}/balance`, `GET /vendor-balances` | Status `active` / `on_hold` (documents yes, payments no) / `suspended` / `inactive` (nothing new; corrections still allowed), with reason and actor. Payment terms, preferred method, opaque `bank_reference`, discount policy. The balance endpoint separates **open bill balances** from the **net amount owed** (bills less unapplied credits and advances). |
| Discount policies | `POST/GET/PATCH /discount-policies` | Configurable early-payment terms (`discount_percent` within `discount_days`, credited to a revenue / contra-expense account). Snapshotted onto each bill at approval, so later edits never change approved bills. |
| Bills | `POST/GET/PUT/DELETE /bills`, `approve`, `reject`, `reopen` | Drafts are editable; approval posts **Dr lines / Cr payables**, snapshots the discount, opens a schedule and freezes the bill. Exact duplicate numbers are refused (`409`, API + DB). |
| Duplicate review | `POST /bills/{id}/duplicate-flags/{flag}/review` | Near-duplicates are *flagged*, not rejected: same number once punctuation is stripped (or same number + amount at another vendor) **blocks approval until cleared**; weaker similarities (adjacent numbers with the same amount within a week) are advisory. Every flag lists the rules that fired. |
| Holds / disputes | `POST /bills/{id}/holds`, `POST /bills/{id}/holds/{hold}/release` | Held or disputed bills are excluded from payment runs and refuse payment allocations (API + DB trigger) until released with a resolution; credits may still settle them. Holds are history: released, never deleted. |
| Schedules | `GET/PUT /bills/{id}/schedule`, `GET /payment-schedule` | Installments sum to the bill; settlements apply to the earliest installment first; proposals select by installment due date. |
| Payments | `POST /payments`, `reverse`, `correct`, `POST /payments/{id}/applications`, `.../{alloc}/unallocate` | Allocations may total **less** than the amount: the remainder is an **advance** carried on the vendor's payable account (`Dr payables / Cr cash`), later applied to bills with no ledger effect. `take_discount` on an allocation settles the bill's remaining early-payment discount (`Dr payables / Cr discount account`) when the bill is paid in full inside the window. Recorded payments are immutable in every column; corrections are explicit reversals (mirror entry, discounts reversed with it) or reverse-and-replace. |
| Credits | `POST/GET/PUT/DELETE /credits`, `approve`, `reject`, `reopen`, `applications`, `.../unapply`, `reverse` | Approval posts **Dr payables / Cr lines**. Applying settles a bill without a ledger entry; several dated applications per (credit, bill) are allowed, an identical one (same amount and date) is refused as a repeat request. **Unapplying never deletes**: the application row is closed with `unapplication_date`, actor and reason. |
| Payment runs | `POST/GET /payment-runs`, items `PATCH` / `exclude`, `review`, `send-back`, `approve`, `cancel`, `execute` | Proposal from approved, unheld bills of active vendors due by `due_through`: discounts (if paid in full inside the window), available credits and advances, then cash; every item explains itself. `draft -> reviewed -> approved -> executing -> completed / failed`, `failed -> executing` (retry), `approved -> draft` (approval invalidated by a material change), cancel from draft / reviewed / approved / failed. Approver must differ from creator and reviewer; executor from approver. **Nothing touches the ledger before execution.** |
| Reports | `GET /outstanding`, `/aging`, `/vendors/{id}/statement`, `/reconciliation` (with `pending_obligations`: confirmed transfers not in the books on that date) | All as-of capable. Statements list bills, credits, payments, discounts and every reversal with a running balance and reconcile to the open-item view. The reconciliation compares each AP control account with the subledger replay and lists **exceptions** (vendor, account, document, entry, expected vs actual, cause: `missing_entry`, `entry_not_posted`, `entry_mismatch`, `foreign_entry`, `over_settled`). |
| Period end | `GET /period-end/validate`, `POST /period-end/close`, `GET /period-end/locks` | Structured checks (unapproved documents, blocking / advisory duplicate flags, holds, unapplied credits / advances, reconciliation defects, foreign ledger entries, runs in flight, post-cutoff transactions) with severities. Closing writes an append-only AP lock; afterwards nothing dated on or before it can be written (application and DB triggers). |

### Historical integrity

Every settlement row is **dated and append-only**: payment allocations (`original` rows written
with the payment, `advance` rows applied later), credit applications, discounts, reversals. A
row is never deleted or amended; it is *closed* once with an effective date and actor
(unapplication, unallocation, reversal). Therefore `outstanding`, `aging`, `vendor balance`,
`statement` and `reconciliation` as of any date reproduce exactly what was true on that date:
a credit applied in January and unapplied in March is applied in the January and February
reports and unapplied from March on. Backdating is allowed back to the later of the
documents' dates, never before the application itself, and never into a closed AP period.

### Payment execution and recovery

Execution of a run is the one place the module talks to the outside world, so it is
deliberately *not* one transaction. Each (run, vendor) group becomes a durable **payment
operation** (`payment_operations`, uid = gateway idempotency token, canonical amount,
instruction fingerprint, `version`, attempts, lease, append-only events) driven through
explicit durability points: the intent is committed as `submitted` *before* the gateway is
called; the gateway answer is committed as its own transition; AP payment + allocations +
journal entry + `completed` are one transaction. State machine:
`prepared -> submitted -> [confirmation_pending ->] confirmed -> recording -> completed`;
`failed` only while no money moved; once it has, a recording failure becomes
`reconciliation_required` and the **only** way forward is to record again (recovery) or to
resolve manually with authorization — never to resubmit. Every transition is a
compare-and-swap on `(state, version)`; a lost race is `409`.

The gateway port (`services/gateway.py`: `PaymentGatewayPort`, `GatewayRequest` /
`GatewayResponse`, outcomes `accepted / rejected / pending / unknown`, `GatewayTimeout`)
is the only external call; the default `DurableSimulatedGateway` persists its transfers on
its own connection and honours the token like a real provider. There is no real banking
integration.

| Area | Endpoints | Rules |
| --- | --- | --- |
| Operations | `GET /payment-operations`, `GET /payment-operations/{uid}`, `GET …/verification`, `POST …/investigation`, `POST …/investigation/close`, `POST …/complete`, `POST …/corrections`, `POST …/resolve`, `POST …/confirmations` | Status view derived by the **verifier** from durable records (gateway transfer record, hash-chained events, vendor payment, posted entry, allocations, run): four statuses (delivery / accounting / settlement / investigation), checklist, findings with remediation, next steps. Investigation notes never change the financial state. **Completion requires evidence**: `complete` links a recorded payment that verifies; `corrections` records the AP payment for a confirmed transfer (no new transfer) and completes in one transaction; `resolve … mark_completed` without `payment_id` is 422. Idempotent by key; same key, different content = 409. |
| Verification | `GET /verification/operations`, `GET /payment-runs/{id}/verification` | Batch verification of every operation whose money moved (blocking findings by default); per-run categories (`recorded`, `confirmed_unrecorded`, `rejected`, `unknown_outcome`, `partial_allocation`, `manual_review`, …) and the completion decision. |
| Recovery | `GET /recovery/scan`, `POST /recovery/run` | AP-owned worker: `resume` / `reconcile_gateway` (lookup by uid) / `retry_recording` / `manual` / `ignore`, one lease per operation, same orchestration code as execution; re-syncs run items and run status. |
| Runs | as before, plus `version`, `proposal_fingerprint`, `approved_fingerprint`, `finalization_status`, `settlement_summary_json`; items `settlement_status`, `actual_*` amounts, `settlement_note` | Review freezes the proposal; approval compares it; a material change after approval sends the run back to `draft`. Approver ∉ {creator, reviewer}; executor ≠ approver. A run is `completed` only when every item is settled without cash or covered by a completed operation whose evidence verifies; otherwise `failed` with the reason (`incomplete_accounting`, `awaiting_gateway`, `partially_rejected`, `manual_review`, `not_started`). The approved plan is kept next to the actual settlement; a changed allocation is recorded with its reason, never paid again. |
| Reconstruction | `GET /reconstruction?as_of&view=as_of|restated` | Independent plain-SQL replay of every balance compared with the production reports and with closed-period attestations; itemised findings. |
| Statement imports | `POST /vendors/{id}/statement-imports[/preview]`, `GET /statement-imports[/{id}]`, `POST /statement-imports/{id}/lines/{line}/resolve` | CSV (`kind,reference,date,amount`), strict validation, deterministic matching against documents in force on the statement date, append-only results, identical re-import is `409`. |
| Period end | `POST /period-end/close` (`force`, `force_reason`, `gl_period_reference`), `GET /period-end/attestations`, `GET …/attestations/{id}/verification`, `GET /period-end/exceptions`, `POST /period-end/exceptions/{id}/resolve` | Close runs under an exclusive close lock (advisory lock on PostgreSQL) that every dated AP write shares; dated records are additionally guarded by triggers. Every attempt leaves a hashed attestation (`closed` / `forced` / `failed`) with the validation snapshot, liability and **obligations** (confirmed, unrecorded transfers — a blocker). A forced close records each accepted blocker as an immutable exception, resolved once later. Close packages are re-verified against AP rows and posted GL history (content hash, source fingerprint, replayed liability, obligations today, exceptions). |

Failure injection for tests: `ScriptedFaults` at `submit.before_commit`, `submit.after_gateway`,
`confirm.before_commit`, `lookup.after_gateway`, `recording.before_books / after_flush /
before_commit / after_commit`, `recovery.before_action`, `completion.before_commit`,
`close.before_commit`.

Money: `execution_state.canonical_amount` accepts `Decimal`, `int` and decimal strings only;
non-finite values, magnitudes beyond `NUMERIC(18,2)`, extra decimals, negatives, negative zero
and floats are `422` domain errors, never rounded. Every comparison is exact `Decimal`.

### Ledger policy

The module never writes to `journal_entries` / `journal_lines` directly and never mutates
posted history. `ledger_adapter.py` builds *draft* entries with the core models and posts them
with `app.services.posting.post_entry` on a SAVEPOINT-joined session, so the adapter never
commits and one unit of work can post several entries atomically (e.g. a correction). Reversals
are mirror images of the original posted entries. Transaction ownership is documented in
`services/__init__.py`: every public service is one unit of work that commits at the end or
rolls everything back; integration ports run inside it and never commit.

### Database-enforced AP rules

Migrations `fmspayables01`..`06` install the same rules on SQLite and PostgreSQL (identical
messages): canonical numbers are computed by the database; financial fields and lines of
non-draft documents, **recorded payments in every column** (method, reference, actor, key,
accounts) and original allocations are frozen; applications / advance allocations / discounts /
holds are closed exactly once and never deleted; settlement never exceeds a bill's total (only
rows in force count), a credit's total or a payment's unapplied remainder; advance rows need a
recorded payment on the same payable account; held bills refuse allocations; pending blocking
duplicate flags stop approval; AP period locks are append-only and refuse dated writes inside
the closed period; payment runs follow their state machine and freeze their items; payment
operations follow theirs, their identity / amount / fingerprint are frozen, **`completed`
requires a recorded payment of the same vendor / amount / cash account with a posted entry
crediting cash by the amount**, `failed` is refused once the gateway accepted, `confirmed`
needs the gateway reference, completed operations are immutable, one confirmation reference
and one payment per operation (UNIQUE); events, close attestations and close exceptions are
append-only (events hash-chained, attestations content-hashed). Violations surface as `422`
through the core `IntegrityError` handler.

### Migrations

`fmspayables01` branches from the shared `fmsmodules01` baseline (branch label `payables`);
`02`..`06` follow. `05` (payment operations, events, simulated transfers, attestations,
statement imports, run versioning) back-fills completed operations from paid run items; `06`
(completion evidence, accounting / investigation status, event hash chain, close exceptions,
run finalisation) runs a non-destructive preflight that labels historical completions without
evidence `unverified` (nothing rewritten; `scripts/ap_preflight.py` reports the same without
migrating). Both refuse to downgrade unless `FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set. `03` back-fills `payment_allocations.allocation_date /
allocated_by` from the payments and `vendors.status` from `is_active`, and rebuilds the trigger
sets (SQLite table rebuilds drop triggers, so each migration recreates complete sets; on
PostgreSQL append-only guards are disabled around back-fills). Downgrades restore the previous
revision's triggers exactly. **Irreversible on downgrade of `03`**: closed (unapplied) credit
applications and unallocated advance allocations are deleted (the older schema cannot represent
them); discounts, holds, duplicate flags, period locks, discount policies and vendor status
details are dropped. While parallel module branches exist, apply them with
`alembic upgrade heads`.

### Tests

`tests/test_payables_*.py`: API suites (bills / schedules / payments / credits / statements /
controls / history / operations / runs / execution / period end), monetary edge cases
(`test_payables_money.py`), direct-SQL rule suites (`db_rules`, `_v2`, `_v3`, `_v4`) and
adversarial tampering (`test_payables_adversarial_v5.py`), service and adapter tests, migration
tests (history, `migration_v5`, `migration_v6` populated-upgrade with preflight), failure-injection
tests at every durability point (`test_payables_failure_injection.py`, `_v5` incl. a restarted
worker process), concurrency tests with real threads and deterministic gates
(`test_payables_concurrency.py`, `_v4`, `_v5`), seeded randomized property tests
(`test_payables_properties.py`, `test_payables_lifecycle_properties.py` — lifecycles with
faults and recovery, checked against the independent reconstruction; widen with
`FMS_PROPERTY_*` / `FMS_LIFECYCLE_*`), reconstruction and statement-import tests,
benchmarks (`test_payables_performance*.py`, marker `perf`, results with statement counts and
query plans in `build/perf/`), and end-to-end scenarios (`test_payables_e2e_milestone3.py`,
`test_payables_e2e_turn4.py`, `test_payables_e2e_turn5.py` — multi-period with exact balances).
Development utilities: `scripts/ap_preflight.py`, `scripts/ap_recovery_worker.py`,
`scripts/ap_scratch_cleanup.py`.
PostgreSQL variants run when `FMS_TEST_POSTGRES_URL` is set, each session on its own uniquely
named scratch database (`tests/payables_support/dbscratch.py`; the base database is never
migrated or dropped). The handoff (features, migrations, verification, measurements,
limitations) is in [docs/accounts-payable.md](docs/accounts-payable.md).

## Parallel module work

See [PARALLEL_WORK.md](PARALLEL_WORK.md) for the shared module baseline, current limitations and integration rules.
