# Financial Management System (backend skeleton)

AI-Enabled Financial Management and Accounting System.
Stack follows the proposal: Python, FastAPI, SQLAlchemy 2, PostgreSQL (SQLite for local dev and tests).

What exists so far:

- Hierarchical chart of accounts: summary/posting accounts, parent links, categories, normal
  balances; `PATCH`, activate / deactivate, descendants.
- Journal entries: create / update / delete drafts with optimistic locking and provenance
  protection, reclassify (standard <-> adjusting), list / filter, post, batch-post, reverse.
  Entry types: `standard`, `adjusting`, `recurring`, `reversal`, `opening`, `closing`.
- Recurring templates: define, generate drafts on schedule (atomic `generate-due`), review, post.
- Accounting periods and fiscal years: close-check, close, reopen, lock; year-end close into
  retained earnings; opening-balance initialisation.
- Journal-processing interface for other modules (idempotent source references, atomic batches,
  idempotent draft-to-post transition, strict monetary precision).
- Posting metadata on every posted entry (`posted_at`, `posted_by`, `posting_source`,
  `correlation_id`), written by the posting transition and immutable.
- Year-end close attestations (`fiscal_year_closings`) with ledger hash and verification.
- Effective-dated account reclassifications; database-level hierarchy integrity (cycles).
- Reports (posted entries only, SQL aggregation): trial balance with roll-ups, paginated
  General Ledger detail, account balances (opening/activity/closing), hierarchy drill-down,
  range comparison; trigger-maintained daily totals with verification and rebuild.
- Reconciliation / validation checks (15) with structured findings.
- Services (`app/services/`) with explicit transaction ownership; database constraints and
  triggers for the critical guarantees; Alembic migrations.

Not implemented yet: AP/AR/cash business logic (only the GL-side contract), budgeting, tax,
auth/RBAC, user-level audit log (who/when), a scheduler process, AI components.

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
| Line amounts are whole cents (at most 2 decimals) | `CHECK ck_journal_lines_whole_cents` |
| Lines of a posted entry cannot be inserted, updated or deleted | triggers on `journal_lines` |
| Posting requires: every line has an amount on exactly one side, at least two lines, debits equal credits, entry date not in a closed period | triggers on `journal_entries` |
| A posted entry row cannot be updated (any column, incl. status) or deleted | triggers on `journal_entries` |
| A reversal can only reference a posted entry, and an entry can be reversed once | trigger + `UNIQUE (reverses_entry_id)` |
| A recurring template never generates the same occurrence twice | `UNIQUE (recurring_template_id, scheduled_for)` |
| Accounting periods never overlap; `start_date <= end_date` | trigger + `CHECK` on `accounting_periods` |
| Posting into a closed **or locked** period is refused | trigger on `journal_entries` |
| A period can only be locked from `closed`; a locked period row can never be updated or deleted | triggers on `accounting_periods` |

The same rules are validated in the application first (Pydantic schemas, the services). If
invalid data nevertheless reaches the database, the resulting `IntegrityError` is returned as
`422` with the constraint or trigger message, never as a `500`.

**Drafts vs. posted entries.** Drafts are work in progress: a draft may be unbalanced, contain
lines that have no amount yet or that have both sides filled in, have a single line or no lines
at all. The only rules applied to a draft are about representable data: amounts are
non-negative whole cents. Everything else is checked when the entry is posted. Once posted,
neither the entry row nor its lines can change (`PUT`/`DELETE` answer `409`); corrections are
made with a reversal.

## Posting

`POST /api/v1/journal-entries/{id}/post` runs `app.services.posting.post_entry`, which checks
in the application that:

- the entry is still a draft (otherwise `409`);
- every line has an amount on exactly one side;
- there are at least two lines;
- total debits equal total credits;
- every line's account is active (`POST /api/v1/accounts/{id}/deactivate` / `.../activate`).

- the entry date is not inside a closed accounting period.

All violated rules are reported together in a single `422`; on success the posted entry is
returned with `200`. The status change is a conditional update (`WHERE status = 'draft'`), so
two concurrent callers cannot both succeed, and the database triggers re-verify completeness,
balance and the period as a backstop. The inactive-account rule is application-only (it is a
chart-of-accounts policy rather than a ledger integrity rule).

## Reversals

`POST /api/v1/journal-entries/{id}/reverse` (optional body `{"reversal_date", "description"}`;
the date defaults to today) creates a **new posted entry** (`entry_type = reversal`) with every
line's debit and credit swapped and `reverses_entry_id` pointing at the original. The body is
strict: unknown fields are rejected with `422` (so a mistyped date field can never silently
produce a reversal dated today), `entry_date` is accepted only as a deprecated alias of
`reversal_date` and must agree with it, and a reversal dated before the original entry is
refused. Both entries stay in the ledger and
are immutable, so the audit history is complete; entries expose `reverses_entry_id` and
`reversed_by_entry_id`. Rules: only posted entries can be reversed (`409` for drafts), an entry
can be reversed once (`409` afterwards, `UNIQUE` in the database), and the reversal goes through
the normal posting rules - so a reversal dated in a closed period, or touching an inactive
account, is rejected with `422` and nothing is stored. Reversing a reversal is allowed (it
re-books the original amounts and keeps the chain).

## Draft editing and optimistic locking

`PUT /api/v1/journal-entries/{id}` replaces a draft's header and lines and requires the
`version` the client last saw; the entry's version increases on every successful edit (also
when only lines change). A stale version yields `409` - reload and retry. `DELETE` accepts an
optional `If-Match: "<version>"` header with the same semantics. The ORM additionally emits
every entry UPDATE as `WHERE version = <loaded>`, so a concurrent edit that slips between the
check and the write is also caught (`409`). Posting does not change the version.

## Recurring entries

`POST /api/v1/recurring-templates` defines a template (lines, `frequency` weekly / monthly /
quarterly / yearly, `start_date`, optional `end_date`). Occurrences are anchored on the start
date (monthly from the 31st clamps to month end without drifting).
`POST .../{id}/generate {"until"}` creates one **draft** per occurrence from `next_run_date` up
to `until` (`entry_type = recurring`, linked to the template with its `scheduled_for` slot) and
advances `next_run_date`; `POST /recurring-templates/generate-due {"until"}` does this for
every active template (what a scheduler would call). Repeating a call creates nothing new, a
reviewed/edited/deleted draft is never regenerated, and the database's unique (template, slot)
pair is the backstop. Generation is one transaction: if any draft cannot be created, none are.
Generated drafts are reviewed, edited (`PUT`, with `version`) and posted like any other draft.
`PATCH .../{id}` renames, pauses (`is_active`) or ends (`end_date`) a template.

## Adjusting entries

Create an entry with `"entry_type": "adjusting"` (period-end accruals, depreciation,
corrections). It follows exactly the same posting and reversal rules; the type is kept on the
posted entry, can be filtered (`GET /journal-entries?entry_type=adjusting`), and a reversal
(e.g. the accrual reversing on the first day of the next period) links back to it.

## Accounting periods

`POST /api/v1/periods` creates an open period (`409` on overlap or duplicate name).

| Transition | Endpoint | Rules |
| --- | --- | --- |
| open → closed | `POST .../close` | month-end validation must pass (below); `409` otherwise or if not open |
| closed → open | `POST .../reopen` | `409` if open or locked |
| closed → locked | `POST .../lock` | only from closed; **final** |

`GET .../close-check` runs the month-end validation without changing anything and lists what
still blocks closing: draft entries dated inside the period (they could never be posted once
it is closed - post, re-date or delete them first), earlier periods that are still open
(periods close in order), or posted activity that does not balance (cannot happen unless the
ledger was tampered with). Transitions are conditional updates (`WHERE status = expected`), so
two concurrent callers cannot both succeed; the loser gets `409` and nothing is written.

Dates inside a **closed or locked** period cannot be posted or reversed into (service `422` +
database trigger). A locked period cannot be reopened, re-dated, renamed or deleted - the API
answers `409` and direct SQL is rejected by triggers. Dates not covered by any period are
unrestricted - requiring an open period for every posting is a policy that can be added later.

## Reports

Both reports use **posted entries only**, compute in `Decimal`, and accept `from` / `to`
(inclusive entry-date filters). Reversals are ordinary posted entries and net out naturally.

- `GET /api/v1/reports/trial-balance` - debit/credit totals and debit-positive balance per
  account (all accounts listed), with report totals and `is_balanced`. Because every posted
  entry balances and lies wholly inside or outside the range, the totals always balance.
- `GET /api/v1/reports/general-ledger?account_id=` - per-account posted lines with running
  balance, `opening_balance` (net of posted lines before `from`), totals and closing balance,
  plus reversal links on each line. Omit `account_id` for all accounts.

Calculations live in `app/services/reports.py`; the routes only validate parameters.

**Exactness on SQLite.** SQLite stores `NUMERIC` as floating point, so `0.10 + 0.20 <> 0.30`.
`ck_journal_lines_whole_cents` guarantees every stored amount is a whole number of cents, and
the SQLite posting trigger sums integer cents (`CAST(ROUND(amount * 100) AS INTEGER)`) and
compares the totals exactly - no tolerance is applied. PostgreSQL `NUMERIC(18, 2)` is exact and
enforces the scale itself (values with more decimals are rounded to cents on write, which is
standard `NUMERIC` behaviour), so its trigger compares the sums directly.

## Transactions and locking

Services own their transactions explicitly: every mutating function takes ``commit=True``
(commit on success, roll back on failure - what the API uses) or ``commit=False`` (flush only;
the caller composes several steps and commits once - how reversals, the year-end close,
batch posting and ``generate-due`` are built). Failures never leave partial records.

Concurrency strategy (`app/services/locking.py`):

- **PostgreSQL**: state transitions (close / lock a period, post, run a template, close a year)
  re-read their row under `SELECT ... FOR UPDATE`; every transaction that creates, re-dates,
  posts, generates or reverses a journal into a period holds `FOR SHARE` on that period's row.
  A period close therefore waits for in-flight journals (and sees them in its validation), and
  journals that start during a close wait and then see the closed period. Unrelated writers
  run concurrently.
- **SQLite**: every transaction opens with `BEGIN IMMEDIATE` (`app/db.py`), taking the single
  write lock up front; all writers are serialised (busy timeout 5 s, surfaced as `409` if
  exceeded). Same accounting outcome, coarser granularity. Readers are serialised with
  writers too, which is acceptable for SQLite's role (development and tests).

Invariants that are also enforced by database triggers (and therefore hold for direct SQL):
posted entries/lines immutable; posting needs complete balanced lines on active posting
accounts in an open period (only the year-end `closing` entry may post into a *closed*
period); a period cannot close with drafts inside or earlier periods open; locked periods and
closed fiscal years are final; reversals only of posted, non-system entries; unique
source references; unique recurring slots; summary accounts never on journal lines; accounts
with history never change type.

## Chart of accounts

Accounts have a `type`, an optional `category` (must belong to the type), a `normal_balance`
(defaults: asset/expense debit, others credit; override for contra accounts), `is_summary`
and `parent_id`. Parents must be summary accounts of the same type; cycles are refused;
summary accounts cannot be posted to (trigger); an account with journal lines cannot change
type or become a summary account (trigger); a summary with children keeps its role. The trial
balance rolls summary rows up from their descendants and excludes them from the totals.
Existing flat accounts migrate unchanged as posting accounts.

## Fiscal years and year-end close

`POST /api/v1/fiscal-years` (optionally `generate_monthly_periods`) defines a year with a
retained-earnings account. `GET .../close-check` lists what blocks the close: period gaps,
open periods, drafts in the year, a locked year-end period, a missing/inactive/non-equity
retained-earnings account. `GET .../closing-balances` previews the closing entry in `Decimal`.
`POST .../close` posts one balanced `closing` entry dated on the year's last day (revenue and
expense balances to retained earnings) and closes the year atomically; repeating it is `409`
and never duplicates; any failure leaves the ledger unchanged. Balance-sheet accounts carry
forward by construction (the General Ledger's opening balance is the net of all prior posted
activity). Periods of a closed year cannot be reopened.

## Monetary precision

All amounts are `Decimal` with at most two decimal places. `app/money.py::as_money` is the
single canonicalisation used at every boundary - Pydantic schemas, service dataclasses
(`JournalRequestLine`, `OpeningBalance`), the ORM (`JournalLine.debit/credit` validators) - and
the database `CHECK (ROUND(x, 2) = x)` backs it. Equivalent inputs (`5`, `"5.0"`, `Decimal("5.00")`)
become the same canonical value; sub-cent, negative, float or non-numeric inputs are rejected
before any idempotency lookup or write. SQL aggregation converts each stored amount to integer
cents per row (`cents_sum`), so SQLite's REAL storage never introduces rounding.

## Posting metadata and actor identity

The conditional UPDATE that posts an entry also writes `posted_at`, `posted_by`,
`posting_source` (`api`, `batch`, `processing`, `reversal`, `year_end_close`,
`opening_balance`, `service`, `legacy`) and `correlation_id`. Posted rows are immutable, and
a CHECK constraint guarantees the metadata exists exactly when `status = 'posted'`. Actor
identity enters through `app/services/actor.py::PostingContext`; the API fills it from the
`X-Actor-Ref` and `X-Correlation-Id` headers (`"anonymous"` / generated id by default) until
the Security module is integrated. Entries posted before revision `a99cf5d9c85d` carry
`posted_by = posting_source = 'legacy'`.

## Opening balances and the journal-processing contract

`POST /api/v1/opening-balances` posts one `opening` entry from imported balances
(`import_id` is the idempotency key; a `balancing_account_id` absorbs any difference).

`POST /api/v1/journal-processing/entries` / `.../batches` is how AP, AR and cash modules will
post: each request carries `source_type`, `source_id`, `operation_id`. The *content* of the
event - `entry_date`, `entry_type` and the multiset of canonical `(account_id, debit, credit)`
lines - is immutable and is what the fingerprint covers; description, reference and memos are
not material. `post` is *workflow state*, not content: an identical retry returns the existing
entry (`created: false`); a retry with `post: true` on an existing draft posts it now (the
idempotent draft-to-post transition, refused with `409` if the draft was edited in the GL
since); the same key with a different fingerprint is `409`; batches are all-or-nothing; with
`commit=False` a batch never commits or rolls back the caller's own pending work; concurrent
identical requests resolve to the same entry (the loser retries from a savepoint).

`POST /api/v1/journal-entries/batch-post` posts existing drafts atomically (all already
posted: idempotent no-op; a mix: `409`).

## Year-end close: authorisation, attestation, recovery, corrections

A `closing` journal may enter a *closed* period only while a `fiscal_year_closings` record for
its year is `in_progress` and names that very entry, and a year can only become `closed`
together with its record (triggers on both backends). Neither the API (the type is reserved)
nor direct SQL can therefore forge closing journals. The record is completed in the same
transaction with the closing totals, covered periods, reconciliation verdict, a SHA-256 over
the year's posted lines, actor and correlation id; completed records are immutable.
`GET /fiscal-years/{id}/closing` returns it, `GET .../closing/verify` reproduces the close
from posted history and compares.

- **Interrupted close**: nothing survives a failure (no entry, no record, year still open).
  Run `close-check` and `reconciliation`, then close again. A stray `in_progress` record can
  only come from manual SQL; reconciliation reports it; delete it and re-run.
- **Corrections after close**: never rewrite posted history. Post an `adjusting` entry in the
  next open year against retained earnings (prior-period adjustment) or reverse the original
  into an open period. Periods of a closed year cannot be reopened.
- Period close (reversible, month-end validation), period lock (final, per period) and
  fiscal-year close (final, posts the closing entry) remain distinct operations.

## Account hierarchy changes

Safe for any account: `name`, `is_active`. For accounts **with journal lines**: `parent_id`
and `category` change only through `POST /accounts/{id}/reclassify` (effective date, reason,
actor), which writes an `account_reclassifications` row the database trigger requires;
`GET .../reclassifications/impact` reports how much history each move carried between
parents. `type`, `normal_balance` and becoming a summary account are prohibited once an
account has lines. Direct or indirect cycles, category/type mismatches and a summary-with-
children losing its role are refused by triggers/CHECKs on both backends.

## Aggregates: `account_daily_totals`

Source of truth: `journal_lines` of posted entries. A trigger adds an entry's lines to the
per-account, per-day cents totals in the posting transaction; posted entries are immutable, so
the table only grows and needs no invalidation. `POST /balances/verify` recomputes every
account/day from the lines and logs the outcome; `POST /balances/rebuild` recomputes the table
in one transaction (exclusive table lock on PostgreSQL) and logs what it replaced. Journals
are never modified. Reports read the aggregate by default (`source=aggregate`) or the lines
(`source=lines`); reconciliation compares both.

## Reconciliation

`GET /api/v1/reconciliation?from=&to=` runs 15 GL integrity checks (unbalanced posted
entries, invalid posted lines, duplicate source references / operations, unmatched references,
posting-date violations, posting metadata, period and fiscal-period state, closing consistency
against the attestation, hierarchy integrity, orphaned records, line-level vs. aggregate vs.
trial-balance balances, daily-total drift, drafts stranded in closed periods) and returns
structured findings with entry ids, account ids and amounts.

## Pagination

`GET /journal-entries/page?limit=&after=` and `GET /reports/general-ledger?limit=&after=`
are keyset-paginated in deterministic orders (`(entry_date, id)` and
`(account code, entry_date, entry id, line id)`); running balances of a General Ledger page
are computed in SQL from everything before the cursor, so pages are independent.

## Tests

Tests build their databases by running the Alembic migrations. Every database-backed test runs
against SQLite, and additionally against PostgreSQL when `FMS_TEST_POSTGRES_URL` is set
(otherwise the PostgreSQL variants are skipped):

```bash
docker compose up -d db
FMS_TEST_POSTGRES_URL=postgresql+psycopg://fms:fms@localhost:5432/fms pytest
```

Every PostgreSQL test gets its own freshly created database (`fms_test_<hex>`) that is dropped
when the test ends. The database named in the URL is only used to run `CREATE DATABASE` /
`DROP DATABASE` (the role needs `CREATEDB`); its schema and data are never touched or reset.
Nothing outside the `fms_test_*` names is ever dropped. If a run is killed hard, leftover
`fms_test_*` databases can be listed with `\l fms_test_*` in `psql` and dropped manually.

To exercise the live API against PostgreSQL without touching a working database, create a
scratch database the same way (`CREATE DATABASE fms_live_x`), point `FMS_DATABASE_URL` at it,
run `alembic upgrade head`, and drop it afterwards.

`tests/test_mutation_balance.py` is a mutation test: it disables the posting service's balance
check (`app/services/posting.py::balance_problem`) for one request and asserts both that the
regular tests would fail and that the database trigger still refuses the unbalanced posting.

Concurrency tests (`tests/test_period_concurrency.py`, and the concurrent cases in
`test_journal_processing.py`, `test_batch_post.py`, `test_fiscal_years.py`) run genuinely
competing sessions in threads, pausing one of them at a service checkpoint
(`app/services/hooks.py`) while the other races it. `tests/test_invariants_random.py` drives
seeded random workloads and checks the accounting invariants after every few steps;
`tests/test_high_volume.py` prints throughput figures (run with `-s`).

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
  services/      domain services (posting, journal, recurring, periods, fiscal_years,
                 accounts, journal_processing, opening_balances, reports, reconciliation,
                 locking, hooks)
  api/           route handlers, error handlers
tests/           pytest suite (migrated SQLite per test; PostgreSQL optional)
```
