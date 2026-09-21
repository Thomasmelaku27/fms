# Financial Management System (backend skeleton)

AI-Enabled Financial Management and Accounting System.
Stack follows the proposal: Python, FastAPI, SQLAlchemy 2, PostgreSQL (SQLite for local dev and tests).

What exists so far:

- Chart of accounts (`Account`): create, list, get, activate / deactivate.
- Journal entries (`JournalEntry` + `JournalLine`): create as draft, list, get, post.
- Posting service (`app/services/posting.py`) reusable outside the API.
- Schema migrations with Alembic.
- Core double-entry rules enforced in the database (see below) as well as in the application.

Not implemented yet: editing/deleting drafts via the API, reversing entries, periods,
auth/RBAC, AI components. Business modules (AP/AR, budgeting, tax, reporting, audit) are
described at the end of this file.

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

Disposable test resources are created fresh, never reused, and only removed when they can be
proven to belong to the current run:

- PostgreSQL: a scratch database named `fms_test_<run tag>` (unique per process and run) is
  created on that server - `CREATE DATABASE` fails loudly rather than replacing an existing
  database - and is stamped with the run tag in its own `fms_test_owner` schema. The per-test
  reset of the `public` schema and the final `DROP DATABASE` both re-read that stamp first and
  refuse to touch a database that is unstamped or stamped by another run, leaving it in place
  with a warning. The database named in the URL is only used to issue `CREATE`/`DROP DATABASE`;
  nothing in it is modified.
- SQLite: a file named after the run tag inside the test's own pytest `tmp_path`. An existing
  file is never opened or deleted; pytest manages the retention of its tmp directories.

`tests/test_db_safety.py` covers these guarantees.

To run the PostgreSQL variants without an existing server, let the suite create and dispose of a
local cluster (needs `initdb`/`pg_ctl` on `PATH`):

```bash
python -m tests.scratch_postgres -- python -m pytest
```

The cluster lives in a fresh `mkdtemp` directory stamped with a random token. Its identity
(path, inode, token, postmaster PID) is kept only in that process's memory - never read back
from a file - and is re-verified through file descriptors immediately before the server is
stopped and before the directory is deleted; if anything does not match, nothing is touched
and the reason is printed. SIGTERM/SIGHUP/SIGINT still lead to the verified cleanup; after a
SIGKILL the cluster can no longer be verified and is left for manual removal. `--capture`
sends the command's output to a `mkstemp` file carrying a token header, prints its tail at the
end and deletes it only after the same kind of check. `tests/test_scratch_postgres.py` covers
these rules.

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
tests/           pytest suite (migrated SQLite per test; PostgreSQL optional)
```


## Business modules

The application includes basic Accounts Payable, Accounts Receivable, budgeting, cash accounts, reporting, tax codes, review suggestions, and audit event modules under `app/modules/`. These capabilities are initial implementations and are not a production-ready financial control system. Financial changes must use the existing ledger posting workflow, and posted journal history must not be edited directly.

### Accounts Receivable (`app/modules/receivables/`)

The AR module covers the full customer-side cycle. All amounts are `Decimal` (whole cents);
every accounting effect goes through `app.services.posting.post_entry`, and posted journal
history is never edited (a void is a new reversing entry).

| Step | Endpoint | Ledger effect |
| --- | --- | --- |
| Configure the accounts the module posts to (once) | `PUT /api/v1/receivables/settings` | - |
| Create / edit a draft invoice with line items | `POST`, `PUT /api/v1/receivables/invoices[/{id}]` | none (drafts never reach the ledger) |
| Issue an invoice | `POST /api/v1/receivables/invoices/{id}/issue` | Dr Receivables / Cr Revenue per line, Cr Tax payable |
| Void a draft, or an issued invoice with nothing settled | `POST /api/v1/receivables/invoices/{id}/void` | reversing entry (issued invoices only) |
| Register a payment, optionally allocated to invoices | `POST /api/v1/receivables/payments` | Dr deposit account / Cr Receivables |
| Allocate an existing payment or credit to invoices | `POST /api/v1/receivables/{payments,adjustments}/{id}/allocations` | none (settlement only) |
| Credit adjustment (credit note) or write-off | `POST /api/v1/receivables/adjustments` | Dr credit / write-off account, Cr Receivables |
| Balance, statement, aging, overdue | `GET .../customers/{id}/balance`, `.../statement`, `.../reports/aging`, `.../reports/overdue` | - |

Invoice line amounts are computed by the server: `net = round(quantity * unit_price, 2)`,
`tax = round(net * rate, 2)` using the tax code's rate *at the time the line is saved*
(rate changes never alter existing invoices). The invoice `total` is the sum of its lines and
is no longer accepted from the client.

**Lifecycle.** `draft -> issued -> partially_paid -> paid`; `draft` and `issued` (with nothing
settled) can become `void`. `balance_due = total - amount_settled`, where `amount_settled` is
the sum of the invoice's allocations from payments, credit adjustments and write-offs. Partial
payments are allowed; an allocation can never exceed the invoice's outstanding balance or the
unallocated remainder of its payment/credit. Money that is not allocated stays on the
customer's account as unapplied credit and can be allocated later. A customer's `balance`
(outstanding invoices minus unapplied credit) always equals its share of the posted
receivables control account.

**Duplicate and concurrent requests.** `POST /payments` requires an `idempotency_key`;
adjustments and allocation batches accept one. The rule is the same for all three:

* the key is stored in a unique column (payments, adjustments) or in the
  `allocation_requests` table (allocation batches), so concurrent submissions are settled by
  the database - exactly one is applied;
* a `request_hash` covers **every significant field** of the request - customer, dates,
  amount, accounts, reference, the invoices and amounts being allocated, and an explicit
  effective date - with amounts normalised, so `20`, `20.0` and `20.00` are the same request;
* retrying the identical request replays the original result with `200` (the first call
  returns `201`) and changes nothing; the same key with *any* other request is a `409`
  conflict. Allocating 20.00 under a key and then 25.00 under the same key is rejected, and
  the original 20.00 stays as it was;
* a request that fails does not consume its key: the corrected retry may reuse it.

Keys are scoped per operation, so a payment, an adjustment and an allocation batch may each
use the same key value.

**Requests recorded before an upgrade.** A key is only treated as a retry when the original
request can be *verified*; a missing fingerprint never matches anything.

* Payments and adjustments have always stored a fingerprint. Those recorded before amounts were
  canonicalised (the "v1" encoding, frozen in `legacy_payment_fingerprint` /
  `legacy_adjustment_fingerprint`) still replay when the request is resent exactly as it was
  sent; any change - including respelling `200.00` as `200`, which v1 cannot prove equivalent -
  is a `409`.
* Allocation batches recorded before `fmsreceivables02` had no fingerprint. Migration
  `fmsreceivables03` classifies each of them once (`allocation_requests.fingerprint_origin`):
  a batch consisting of exactly one (append-only) allocation row of the request's own source
  is rebuilt and gets its fingerprint (`reconstructed`, replayable when resent identically);
  every other batch is `unverifiable` and any reuse of its key - even one that may well be the
  original - is answered with `409`. CHECK constraints keep `request_hash` present exactly when
  the request is verifiable.

**Keys shared by a payment and a credit batch (controlled revision of `fmsreceivables02`).**
Old code could use one allocation key for a payment batch *and* a credit batch. The original
`fmsreceivables02` failed on such data (PostgreSQL rolled back; SQLite, whose DDL is not
transactional, was left half-upgraded with the append-only UPDATE trigger dropped). A purely
additive fix is not possible: a later revision never runs on an affected database, and a
revision spliced in front of `02` could only prevent the failure by rewriting the keys of
historical allocation rows. The revision body of `02` was therefore corrected - declared in its
docstring ("Revision history"), not silently:

* mixed keys get no `allocation_requests` row (the table cannot hold two sources); their
  allocation rows are kept untouched. The application treats a key that has allocation rows but
  no request row as unverifiable and refuses any reuse of it with `409`;
* every object (table, each index, column, trigger) and every backfill statement is checked
  individually and the end state is verified before the revision is stamped, so a database left
  half-upgraded at *any* point is completed by running `alembic upgrade head` again; an
  `allocation_requests` table of a different shape stops the upgrade with an explanation;
* databases that already passed `02` are not touched, and the revised body produces exactly the
  same end state for them (tested).

`python -m app.modules.receivables.upgrade_checks` (read-only) lists mixed keys and detects a
half-upgraded SQLite database.

**Effective dates and historical reports.** Every allocation stores two dates: the
`allocation_date` (settlement date of the documents, never before the invoice) and the
`effective_date` - the date it entered the books. `as of` reports (customer balance, aging,
overdue) count only allocations whose `effective_date` is on or before the reporting date,
on both the invoice side and the unapplied-credit side, so they stay consistent with each
other and with the posted receivables account. Allocations captured while registering a
payment or credit take effect with that document; a later allocation of an older payment
takes effect on the day it is made, so last month's aging cannot change retroactively. A
request may pass an explicit `effective_date` (deliberate backdating) as long as it is not
before the payment/credit or any of the invoices involved. Statuses shown in as-of reports
are derived from the balance at that date rather than the invoice's current status.

**Database rules** (migrations `fmsreceivables01`-`03`, SQLite and PostgreSQL): CHECK constraints
keep amounts non-negative and the running sums within their totals; triggers freeze the lines
and header amounts of an issued invoice, refuse to return an invoice to draft, make
allocations append-only and reject any allocation that exceeds the invoice total, the source
amount, or targets a draft/void invoice. As with the ledger, the SQLite versions compare whole
cents. Tests live in `tests/receivables/`.

Not covered yet: reversing or voiding a payment, refunds of unapplied credit, voiding an invoice
that already has settlements (issue a credit note instead), customer deactivation via the API,
and un-applying an allocation (allocations are append-only; correct with a credit note).
