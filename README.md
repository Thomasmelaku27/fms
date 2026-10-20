# Financial Management System (backend skeleton)

AI-Enabled Financial Management and Accounting System.
Stack follows the proposal: Python, FastAPI, SQLAlchemy 2, PostgreSQL (SQLite for local dev and tests).

What exists so far:

- Chart of accounts (`Account`): create, list, get, activate / deactivate.
- Journal entries (`JournalEntry` + `JournalLine`): create as draft, list, get, post.
- Posting service (`app/services/posting.py`) reusable outside the API.
- Schema migrations with Alembic.
- Core double-entry rules enforced in the database (see below) as well as in the application.

Not implemented yet: editing/deleting drafts via the API, reversing entries, periods, AP/AR,
budgeting, tax, reporting, auth/RBAC, audit trail, AI components.

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
tests/           pytest suite (migrated SQLite per test; PostgreSQL optional)
```

