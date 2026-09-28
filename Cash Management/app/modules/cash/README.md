# Cash management module

Bank statement import, bank reconciliation, cash reports and a short-term
liquidity projection on top of the General Ledger.

## Accounting boundary

* The **General Ledger is the only source of truth**. Every balance in this
  module is derived on the fly from lines of *posted* journal entries of the
  ledger account linked to a cash account. Nothing is stored as a balance.
* The module never creates, edits or posts journal entries. Posting happens only
  through `app.services.posting.post_entry` (the journal API); reconciliation,
  imports and projections leave posted history untouched (tests prove it).
* **Never counted as posted financial transactions:** draft journal entries,
  bank statement transactions (matched or not), planned cash flows,
  auto-match suggestions.
* All monetary values are `Decimal` (2 decimals) end to end; no `float`.

Sign convention for bank transactions and planned flows: **positive = money
received** into the cash account (a *debit* on the asset ledger account),
**negative = money paid out** (a *credit*).

## Endpoints (prefix `/api/v1`)

| Area | Endpoint | Notes |
| --- | --- | --- |
| Cash accounts | `POST/GET /cash-accounts`, `GET/PATCH /cash-accounts/{id}`, `GET /cash-accounts/{id}/balance?as_of=` | `minimum_balance` = liquidity alert threshold |
| Statements | `POST /cash/bank-statements` | 201 created, 200 identical re-import, 409 same reference with different content, 422 statement does not foot / dates outside period / zero amounts |
| | `GET /cash/bank-statements[?cash_account_id]`, `GET/DELETE /cash/bank-statements/{id}` | delete refused while any line is matched |
| | `GET /cash/bank-transactions?cash_account_id&status&date_from&date_to` | status: `unmatched`, `partially_matched`, `matched` |
| Reconciliation | `POST /cash/reconciliations` | from a statement (`statement_id`) or manual period + balances; one session per account and period |
| | `GET /cash/reconciliations/{id}/summary` | review: cleared balance, difference, ledger balance, outstanding items, adjusted balances, discrepancies, `is_balanced` |
| | `GET /cash/reconciliations/{id}/unmatched` | bank items not fully matched + posted ledger lines not fully matched, with remaining amounts |
| | `POST /cash/reconciliations/{id}/matches` `{bank_transaction_id, journal_line_id, amount?}` | partial matches allowed; `amount` defaults to the smaller remaining side |
| | `DELETE /cash/reconciliations/{id}/matches/{match_id}` | only while the session is open |
| | `POST /cash/reconciliations/{id}/auto-match` `{date_tolerance_days, dry_run}` | unambiguous one-to-one suggestions with reasons; ambiguous ones are listed, not applied |
| | `POST /cash/reconciliations/{id}/close` `{force, notes}` | refuses an unbalanced session unless `force` + notes |
| | `POST /cash/reconciliations/{id}/reopen` | matches become editable again; `reopen_count` incremented |
| Reports | `GET /cash/reports/balances?as_of=` | dated balance per cash account + consolidated |
| | `GET /cash/reports/daily-balances?start&end[&cash_account_id]` | daily inflows / outflows / transfers / running balance |
| | `GET /cash/reports/cash-flow?start&end` | operating / investing / financing by counterpart account, internal transfers listed separately and excluded |
| | `PUT/GET /cash/classifications`, `DELETE /cash/classifications/{account_id}` | explicit activity per account (default by type otherwise) |
| Projection | `GET /cash/projection?as_of&horizon_days=30&cash_account_id&threshold&history_days` | see below |
| Planned flows | `POST/GET /cash/planned-flows`, `GET/DELETE /cash/planned-flows/{id}`, `PATCH /cash/planned-flows/{id}/status` | forecast items only; idempotent on `(cash_account, source, external_reference)` |

## Import idempotency

* A statement is keyed by `(cash_account_id, statement_reference)` and a
  content hash. Re-sending the same payload returns the existing statement
  (`created: false`, HTTP 200). Different content under the same reference is a
  409 (delete the statement first if the bank reissued it).
* Every transaction gets a fingerprint of `(reference, date, amount,
  description, occurrence-within-payload)` scoped to the cash account, enforced
  by a unique constraint. Lines already present (overlapping statements) are
  skipped and listed in `skipped_duplicates`; identical lines inside one
  statement are kept (occurrence 1, 2, ...).
* A statement must foot: `closing - opening == sum(transactions)`.

## Reconciliation figures (all derived, never stored)

```
cleared_balance         = opening_balance + matched bank movements in the period
difference              = closing_balance - cleared_balance     (0 when every bank item is explained)
ledger_balance          = posted balance of the ledger account as of period_end
outstanding ledger      = posted lines up to period_end not (fully) matched
adjusted_bank_balance   = closing_balance + outstanding ledger items (signed)
adjusted_ledger_balance = ledger_balance + unmatched bank items up to period_end (signed)
unexplained_difference  = adjusted_bank_balance - adjusted_ledger_balance
is_balanced             = difference == 0 and no unmatched / partially matched bank items in the period
```

Discrepancy kinds: `unmatched_bank_transaction`, `partially_matched_bank_transaction`,
`unmatched_prior_bank_transaction`, `outstanding_ledger_line`,
`partially_matched_ledger_line`, `opening_balance_mismatch` (vs. the previous
closed session), `difference`, `unexplained_difference`.

### Certified evidence (closing, reopening, re-certifying)

Closing a session (`POST .../close`) writes a **certification** in the same
transaction: the canonical JSON of everything that was approved (figures,
every match with bank fingerprint and journal line, the bank transactions and
posted ledger lines in scope, discrepancies, notes, `forced`) and its SHA-256.
The status only flips to `closed` once that row exists - the database refuses
a bare status change.

Afterwards the summary of a closed session returns three things that are kept
apart: the live figures (what the data says *now*), `certification` (what was
approved, with `verified` = the hash and chain re-check), and `drift` - every
difference between the two, itemised (`late_bank_transaction`,
`late_ledger_line`, `certified_figure_changed`, `matches_changed`, ...). Late
statement imports are also flagged in the import response
(`late_transactions`, `warnings`). The certified result never changes.

* `GET .../certification` - latest certification, its canonical `content`,
  verification result and drift.
* `POST .../reopen` `{reason}` - mandatory reason; records a reopen event and
  completes the reopening in the same transaction (the certification stays in
  the history).
* `POST .../recover` - completes a close that was committed half-way; 409 when
  there is nothing to recover.
* `PATCH /cash/reconciliations/{id}` - correct balances/notes of an *open*
  session (e.g. the bank reissued the statement); frozen while closed.
* closing again certifies version *n+1* with `previous_hash` = hash of version
  *n* (a tamper-evident chain); `GET .../history` lists
  certified / reopened events with reasons.

#### State machine (tolerant to partial processes)

The session row is the **only authoritative state**: `status`, `reopen_count`
(completed reopenings) and `last_reopen_event_id`. Certification version *n*
certifies cycle *n* (= `reopen_count + 1` at the time of closing); a reopen
event carries `reopen_number` (= `reopen_count + 1` when asked for).
Certifications and events are evidence the database demands before a
transition, but their mere existence never changes state:

* a reopen event committed without the status flip (aborted process, raw SQL)
  is **orphaned**: the certification stays active and verifiable, the next
  legitimate `reopen` records its own event and completes the cycle; the
  orphan is then shown as `superseded`. Duplicated events are handled the same
  way. Nothing is ever deleted.
* a certification committed without the status flip is an **orphaned
  certification** (reported under `integrity.problems`): a retry of `close`
  adopts it when it still matches the current state; otherwise
  `POST .../recover` completes the close with that evidence (its `verified`
  flag says whether it is sound) so the normal reopen-with-reason → certify
  path is available again.
* `GET .../history` returns every event with its `state`
  (`active` / `superseded` / `orphaned` for certifications,
  `effective` / `superseded` / `orphaned` for reopen events) plus an
  `integrity` report of the invariants.

#### Migrating existing databases (`cashmgmt03`)

The rules are structural (links between rows), not arithmetic on
`reopen_count`, so a database whose history is irregular keeps working
without any row being rewritten. The upgrade:

* runs as **one transaction on both backends** (SQLite DDL is normally not
  transactional; the revision opens an explicit transaction first) - a failure
  anywhere leaves the database exactly at `cashmgmt02`, and the upgrade can
  be retried;
* inspects the data **before any DDL**: events referencing a missing
  certification or another session's certification abort the upgrade with a
  report (no honest value exists for the new columns); irregular but
  representable history - reopen events that never completed, several
  certifications without a completed reopening in between, `reopen_count`
  not matching the events - is **kept as is and logged** as warnings;
* fills only the new columns, only where unambiguous: `reopen_number` = version
  of the referenced certification, `last_reopen_event_id` = the single event of
  number `reopen_count`, `reopen_event_id` = the single event of the previous
  version; otherwise `NULL`;
* afterwards `GET .../history` reports such history under `integrity.anomalies`
  (preserved, blocking nothing) as opposed to `integrity.problems` (current
  state needs attention). The downgrade refuses - changing nothing - when a
  certification has several reopen events, which `cashmgmt02` cannot represent.

| Layer | Protection |
| --- | --- |
| Database (`cashmgmt02` + `cashmgmt03` triggers, both backends) | certifications and reopen events are append-only (no UPDATE/DELETE); a certification needs an open session, the next version number, must reference `last_reopen_event_id`, and the latest certification must have been superseded by that completed reopening (one per cycle); a reopen event needs a closed session, the latest certification and `reopen_number = reopen_count + 1`; `open -> closed` needs a latest certification that is not superseded; `closed -> open` must increment `reopen_count` by one and point `last_reopen_event_id` at a matching event; `reopen_count` / `last_reopen_event_id` never change otherwise; a closed session's scope cannot change; a certified session cannot be deleted |
| Application | canonical content + SHA-256 + hash chain; verification on every read; drift itemisation; reason required; adoption of a matching orphaned certification / explicit `recover`; integrity report; cash-account lock and conditional updates (`WHERE status = ... AND reopen_count = ...`) for races; rollback on any failure |
| Not covered | a superuser who disables triggers can still alter or forge rows - that is *detected* (`verified: false`, `integrity.problems`), not prevented; no digital signature / external anchoring of hashes; no actor identity (no auth yet); the database cannot tell a legitimate orphaned event from a forged one - it only guarantees that such rows never change state |

### Database-enforced rules (migration `cashmgmt01`, SQLite and PostgreSQL)

* a match references a line of a **posted** entry, on the ledger account of the
  bank transaction's cash account, in the right direction (inflow ↔ debit,
  outflow ↔ credit);
* matches never exceed the bank transaction amount nor the journal line amount;
* matches cannot be inserted / changed / deleted while the session is closed;
* a matched bank transaction cannot be deleted or have its amount changed;
* unique statement reference and transaction fingerprint per cash account,
  unique (bank transaction, journal line) pair, positive whole-cent amounts.

### Concurrency

Writers for one cash account are serialised (`pg_advisory_xact_lock` on
PostgreSQL, `BEGIN IMMEDIATE` on SQLite); sums are always re-read from the
database; close/reopen are conditional updates (`WHERE status = ...`). The
loser of a race gets a 409, never a duplicate.

## Cash flow report

* Debit on a cash ledger account = inflow, credit = outflow.
* An entry that debits one cash account and credits another is an internal
  transfer for `min(cash debits, cash credits)`; that amount is reported under
  `internal_transfers` and excluded from consolidated inflows/outflows and from
  the activity sections. Any remainder (e.g. a fee) is classified normally.
* Activity comes from the counterpart (non-cash) lines: explicit classification
  if present, else by account type (revenue/expense → operating, asset →
  investing, liability/equity → financing). Each line states its
  `classification_source`.

* `ties_out` is computed on every cash-flow report: the activity view
  (counterpart lines) and the account view (cash lines) must agree on the net
  change and `opening + net_change == closing`; gross activity in/outflows may
  exceed the cash movement of a netted entry (e.g. Dr Bank 100 / Cr Sales 150
  / Dr Rent 50 shows 150 in, 50 out) but the net always ties.
* Reports and the projection read one consistent snapshot (REPEATABLE READ on
  PostgreSQL, a read transaction on SQLite) so a posting committed while they
  run is either fully in or fully out.

## Liquidity projection

Read-only. `opening_balance` = posted balance as of `as_of`; each horizon day
lists its items: `posted_journal_entry` (post-dated posted entries) and
`planned_cash_flow` (status `planned`; overdue items are carried to the first
day and flagged). Threshold: `threshold` query parameter, else the cash
account's `minimum_balance`, else the sum of configured minimums. Days below
the threshold produce `alerts`, each with a `reason` (threshold source, movement
since the horizon start, the day's items, the largest outflows so far), the
day's `items` and the cumulative `drivers`, and `first_breach` on the first
one. `counted.backdated_posted_entries_in_history` and a warning name posted
entries in the history window that were recorded after their entry date: they
are counted (the ledger is the truth) but explain why a projection may differ
from an earlier one. `excluded` and `warnings` explain what was left out
(drafts, unmatched bank transactions, realized/cancelled plans, plans linked to
posted entries).
