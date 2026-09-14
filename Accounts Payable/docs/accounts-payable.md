# Accounts Payable — handoff

Scope: `app/modules/payables`, its migrations (`fmspayables01`..`06`), its tests, the
development utilities in `scripts/` and this
documentation. Shared modules (core ledger / posting, GL periods, Cash, Tax, Audit,
authentication) were not modified; every cross-module need is declared as a port in
`app/modules/payables/integration.py`.

The README section *Accounts payable* is the endpoint-level reference. This document
records what was built, how it was verified, and what is known not to be verified. Part 1
covers the latest turn (Turn 5: evidence-based completion, the verifier, monetary
validation, recovery races, period-end integrity, scratch-database safety). Part 2 is the
Turn-4 record (durable operations, recovery, reconstruction, statement imports) and Part 3
the milestone-3 record, both kept for traceability — where they disagree with Part 1
(e.g. manual resolution), Part 1 is current.

---

# Part 1 — Turn 5: financial correctness, evidence integrity, recovery, concurrency

## 1. The manual-completion defect and its fix

**Reproduced defect (Turn 4).** A gateway confirms a transfer, the GL refuses the payment
posting repeatedly, recovery exhausts its attempts and flags the operation for manual
investigation. `POST /payment-operations/{uid}/resolve` with `mark_completed: true` and a
free-text note then moved the operation to `completed` — with no vendor payment, no posted
journal entry, `recovery_status = resolved`. The regression test
`test_payables_execution_api.py::test_attempts_are_bounded_and_manual_completion_requires_evidence`
reproduces exactly this sequence and proves the same request is now refused with **422
"A resolution note is not evidence …"**; so are a non-existent `payment_id` and another
supplier's payment (`payment_vendor_mismatch`).

**Fix, service level (`services/recovery.py`).**

* Investigation and financial completion are separate workflows.
  `POST …/investigation` (note, opens) and `POST …/investigation/close` change
  `investigation_status` and append events; the accounting status stays what the records
  say (`unrecorded` for a confirmed, unrecorded transfer) and the state stays
  `reconciliation_required`. `resolve` without `mark_completed` is a note.
* `POST …/complete` (`payment_id`, `reason`, optional `idempotency_key`) completes only
  after `services.verification` accepts the candidate payment against the gateway's
  transfer record, the operation's event chain, the vendor / amount / cash account /
  currency / confirmation reference, the posted entry's cash movement, the allocations and
  the run. `resolve` with `mark_completed` requires `payment_id` and goes through the same
  path.
* `POST …/corrections` is the authorised correction workflow: vendor, amount, cash account,
  method and reference come from the operation (what the gateway moved); the operator
  supplies the accounting date and allocations; the payment is recorded, verified and the
  operation completed in **one transaction** — nothing is sent to the gateway. Idempotent
  by key; same key with different content is 409.
* The `accounting_status` column is explicit and independent of the execution state:
  `pending` → `unrecorded` (money moved, nothing posted) → `recorded` (automatic) /
  `corrected` (manual correction, verified); `unverified` marks historical completions
  without evidence (migration preflight); `inconsistent` is what the verifier derives when
  the records contradict the row.

**Fix, database level (migration `fmspayables06`, both backends, identical messages).**
The `payment_operations` guard now refuses: `→ completed` unless `payment_id` points at a
`recorded` vendor payment of the same vendor, amount and cash account whose journal entry
is `posted` and credits the cash account by the amount (allocations ≤ amount), plus a
gateway confirmation reference and `accounting_status ∈ {recorded, corrected}`;
`→ confirmed` without `gateway_status = 'accepted'` and a reference; `→ failed` once the
gateway accepted (or a confirmation reference exists); setting `payment_id` to a payment
of another vendor or amount; any change to a completed operation's payment link, gateway
reference, status flags, evidence, `completed_at` or `attempts`. `gateway_confirmation_ref`
and `payment_id` are UNIQUE (one transfer, one payment, one operation).

## 2. State machine review and evidence per transition

The full table (evidence per transition, what the trigger re-checks, what is frozen) is in
the docstring of `services/execution_state.py`. Key decisions:

* `failed` never implies "money did not move" unless the gateway said so (`rejected`, or
  `unknown` after the attempt budget). The verifier reports `failed_but_transfer_accepted`
  (critical) when the gateway record contradicts a `failed` state.
* `confirmed` requires gateway acceptance evidence; `completed` requires durable AP and GL
  records (service + trigger).
* Recovery and manual review preserve the original gateway outcome and the history: events
  are append-only and hash-chained; the gateway reference is set once; completion stores an
  evidence snapshot (`completion_evidence_json`) and the completing actor's decision.
* Delivery, accounting, settlement and investigation are reported as four separate statuses
  (`GET /payment-operations/{uid}`).

## 3. Monetary validation

`execution_state.canonical_amount` now accepts `Decimal`, `int` and decimal strings only
and raises `MoneyError` (a `ValidationFailed`, HTTP 422) for: non-finite values (`NaN`,
`Infinity`, `-Infinity`, `sNaN`), magnitudes beyond `NUMERIC(18,2)` (`1e40`, `1e100`,
`1E+17`, `10000000000000000.00`), more than two decimals, negative amounts, negative zero
(`-0`, `-0.00`), floats, booleans, `None`, other types, and unparsable text. Nothing is
rounded; `decimal.InvalidOperation` can no longer escape (`quantize()` is only reached
after the scale and magnitude checks). `canonical_plan` validates every part with its name
(`pay for bill 7: 1E+40 exceeds …`). `cents()` gives exact integer cents. The float
comparison and the `0.005` tolerance in diagnostics are gone; all comparisons in
verification, diagnostics, reconciliation and reconstruction are exact `Decimal`.
`tests/test_payables_money.py` (63 cases) includes one-cent discrepancies at the API
level (bill lines vs total, allocation vs payment, outstanding `0.01`, replay `0.01`) and
the largest representable magnitude minus one cent.

## 4. The verifier (`services/verification.py`)

`verify_operation(db, op, gateway=…)` re-derives an operation's financial state from
durable rows only — never from `recording_status` / `ledger_status` / `allocation_status`
/ `state`:

| Source | Checks (finding codes) |
| --- | --- |
| gateway transfer record (`PaymentGatewayPort.evidence(token)`) | `gateway_evidence_missing`, `gateway_not_accepted`, `gateway_reference_mismatch`, `gateway_amount_mismatch`, `gateway_currency_mismatch`, `gateway_destination_mismatch`, `gateway_vendor_mismatch`, `failed_but_transfer_accepted`, `confirmed_without_reference` |
| event chain | `events_missing`, `events_sequence_gap`, `events_hash_chain_broken`, `events_state_mismatch` (state changed without a transition event), `events_no_gateway_acceptance`, `events_no_completion`, `events_unhashed` |
| vendor payment | `payment_missing`, `payment_not_found`, `payment_vendor_mismatch`, `payment_amount_mismatch`, `payment_cash_account_mismatch`, `payment_reference_mismatch`, `payment_reversed`, `payment_not_linked_to_operation`, `payment_date_differs`, `payment_corrected` (reverse-and-replace chain followed), `transfer_unaccounted_after_reversal`, `currency_unsupported` |
| posted journal | `journal_missing`, `journal_not_posted`, `journal_cash_mismatch` (credit on the cash account ≠ amount), `journal_unbalanced`, `journal_date_mismatch` |
| allocations / discounts / advances | `allocations_exceed_payment`, `allocation_foreign_vendor`, `allocation_outside_plan`, `unapplied_without_payable_account`, `discount_not_posted`, `allocation_status_forged`, `allocation_status_stale` |
| status claims | `status_flag_forged`, `accounting_status_mismatch`, `accounting_unverified`, `completed_without_evidence` |
| duplicates | `duplicate_payment_for_transfer`, `duplicate_payment_for_operation`, `duplicate_operation_for_payment`, `duplicate_confirmation_reference`, `duplicate_live_operation` |
| payment run | `run_items_inconsistent` (warning; recovery re-syncs), `run_items_paid_without_completion`, `run_completed_with_incomplete_operation` |
| supplier | `vendor_not_active` (warning) |

Each finding carries severity (`critical` / `error` / `warning` / `info`), operation uid,
vendor, amount, expected, actual and a remediation. The result also gives the four derived
statuses and the `required_evidence` checklist; `completion_policy_satisfied` is true only
when every required item holds and no critical / error finding exists. `verify_run`
categorises every operation (`recorded`, `recorded_no_cash`, `confirmed_unrecorded`,
`rejected`, `unknown_outcome`, `partial_allocation`, `manual_review`, `not_started`) and
decides `can_complete`. Batch verification prefetches with a handful of `IN` queries
(`Store.prefetch`: 787 → 75 statements for 61 operations in the benchmark).

Integrated into: diagnostics (`GET /payment-operations/{uid}` checklist and
`verification` block, `…/verification`, `/verification/operations`), manual completion
and corrections, recovery (escalates instead of re-recording when a payment for the
transfer exists or gateway evidence is missing), payment-run finalisation
(`/payment-runs/{id}/verification`), period-end validation (`unverified_completed_operations`
and `unrecorded_confirmed_transfers` are blockers), close-package verification.

## 5. Payment-run finalisation

`payment_runs.finalize_run` decides `completed` only when every non-excluded item is
settled without cash or covered by a completed operation whose evidence verifies. Otherwise
the run stays `failed` with `finalization_status` ∈ {`incomplete_accounting`,
`awaiting_gateway`, `partially_rejected`, `manual_review`, `not_started`} and a
`settlement_summary_json` (categories, approved vs actual cash totals, blocking findings).
Items keep the approved plan in the original columns and record what actually happened in
`actual_pay_amount` / `actual_discount_amount` / `actual_credit_amount` /
`actual_advance_amount`, `settlement_status` and a `settlement_note` naming the reason
(e.g. "bill 12: settled meanwhile; 40.00 kept as advance"). A changed allocation never
triggers a second transfer: the cash already moved is recorded with the remainder as an
advance. Recovery and manual completion re-finalise the run (`_sync_run_items`,
`resync_stale_runs` for executors that died after the recording commit).

## 6. Concurrency (independent PostgreSQL connections, deterministic gates)

`tests/test_payables_concurrency_v5.py` parks a worker at a named durability point with a
`Gate` fault port (threading events), lets a second connection act, then releases it:

| Scenario | Outcome proven |
| --- | --- |
| two workers, same confirmed transfer | the second is refused by the lease / CAS; one payment, one posted cash movement |
| expired lease taken over mid-recording | the slower worker's CAS fails and its books roll back; one payment |
| manual correction vs recording retry (both orders) | the row lock serialises them; the loser gets 409 / "lost a race"; one payment |
| gateway callback during recovery | the callback waits for the row lock, is logged afterwards; contiguous hash-chained history |
| run finalisation while recording in flight | re-execution sees the lease; the run is never `completed` while the transfer is unrecorded; then `completed` |
| stale snapshot during a recording commit | the stale transition is refused by the CAS |
| period close vs confirmed-payment recording | the close waits for the dated writer (shared advisory lock) and sees the payment; closing first blocks (`unrecorded_confirmed_transfers`), a forced close keeps the obligation, recording into the closed period is refused, the March correction resolves it |
| supplier suspended after authorisation | before the claim: approval invalidated, nothing sent; between the claim and the gateway call: money moves, the transfer is recorded anyway, `vendor_not_active` reported |

Lock ordering, CAS semantics, lease expiry and recovery after lost connections are
documented in the `execution_state` module docstring.

## 7. Period-end integrity

* Critical dated records are protected by triggers independent of the advisory lock
  (fmspayables03/05/06): payments, allocations, applications, discounts dated in a closed
  period are refused on direct SQL; period locks, attestations, operation events and close
  exceptions are append-only; operations can only be completed with the records above.
* **Close packages are independently verifiable** (`GET /period-end/attestations/{id}/verification`,
  `closing.verify_close`): the attestation's `content_hash` (sha256 over the attested
  content, back-filled by the migration), the source fingerprint of the subledger events up
  to the cutoff (backdated changes after the close), the attested liability against the
  plain-SQL replay, every obligation the close carried (its state and verification today)
  and the accepted exceptions against their resolutions.
* A confirmed but unrecorded payment is a closing exception (`unrecorded_confirmed_transfers`,
  blocker) and appears in the reconciliation report as `pending_obligations` (effective-dated:
  a transfer recorded by a March correction is still an obligation in the February view).
* A forced close writes one immutable `ap_close_exceptions` row per accepted blocker item
  (actor, reason) and the obligations list into the attestation; exceptions are resolved once
  (`POST /period-end/exceptions/{id}/resolve`, note + reference, DB-enforced write-once);
  unresolved exceptions of earlier periods are a warning in later validations.

## 8. Scratch-database cleanup (`tests/payables_support/dbscratch.py`)

The owner comment now records `token;host;pid;boot;created`. Cleanup by the owning process
is token-verified (`drop_scratch_database`, `drop_owned_databases`). Cross-run cleanup is a
separate, explicit `administrative_cleanup(url, names, confirm=True, operator=…)`: a listed
database is dropped only when its token is handed over (`tokens=`) or it is proven
abandoned — parsable ownership, created on this host and boot, creator pid dead, older
than the grace period, no sessions — and never with `FORCE`. Name prefix and comment prefix
are no longer grounds for deletion; unparsable or foreign ownership, live processes, other
hosts, connections and young databases are refused with a reason. `scripts/ap_scratch_cleanup.py`
wraps it. Tests (`test_payables_testdb.py`, 14, PostgreSQL): token mismatch, concurrent runs,
failed creation, crashed child process (recovered only after the grace period),
cross-run refusal and token hand-over, connected database, other host/boot, pattern-based
and unlisted deletion impossible.

## 9. Migration `fmspayables06` and the preflight

`migrations/versions/20261201_fmspayables06_completion_evidence.py` (revises `fmspayables05`,
earlier revisions untouched): columns listed in §1 and §5, `payment_operation_events.prev_hash /
event_hash` (chain back-filled), `ap_close_attestations.content_hash / obligations_json`,
`ap_close_exceptions`, UNIQUE constraints, the replaced operation guard. Preconditions
(duplicate confirmation references or payment ids) refuse the upgrade without changing
anything. **Preflight**: every already-completed operation is checked against the evidence
rules; rows with evidence get `accounting_status = recorded` (`corrected` for Turn-4
manual resolutions with a payment, `legacy_backfill` kind for Turn-3 back-fills), rows
without get `unverified` plus an appended event naming what is missing — state, payment
link, notes and actor decisions are untouched; remediation is explicit.
`scripts/ap_preflight.py --url … [--json]` prints the same report without migrating (exit
code 1 when remediation is needed). Downgrade is restricted (`FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1`).
`tests/test_payables_migration_v6.py` upgrades a populated Turn-4 database (completed,
reconciliation-required and note-only-completed operations, transfers, a run, events, a
failed attestation, a statement import) on both backends and checks labels, hashes, the
blocked close and the explicit remediation; `test_migrations.py` checks schema drift
(`compare_metadata == []`) and down/up on both backends.

## 10. Historical replay extension

`reconstruction.replay` positions every payment operation at the cutoff
(`operation_positions`: delivered by the cutoff? recorded by the cutoff? obligation?) and
`compare` adds `pending_obligations` (production report vs replay), `operation_completion`
(completed without a backing payment), `statement_match_target` and
`closed_period_obligations` (attested vs replayed). Later corrections do not change earlier
as-recorded positions: the E2E scenario asserts February's obligations stay `750.00` after
the March correction, while March shows `0.00`.

## 11. Verification

### Commands

```bash
. .venv/bin/activate
python -m pytest -q -m "not perf" -p no:cacheprovider tests                      # SQLite
FMS_TEST_POSTGRES_URL=postgresql+psycopg://fms:fms@localhost:5432/fms \
  python -m pytest -q -m "not perf" -p no:cacheprovider tests -k postgres         # PostgreSQL
python -m pytest -q -m perf -p no:cacheprovider tests/test_payables_performance_v5.py -k sqlite
FMS_TEST_POSTGRES_URL=…/fms_bg python -m pytest -q -m perf -p no:cacheprovider tests/test_payables_performance_v5.py -k postgres
python scripts/ap_preflight.py --url <db url> [--json]
python scripts/ap_recovery_worker.py --url <db url> --worker-id w1 [--json]
python scripts/ap_scratch_cleanup.py --url <admin url> --list
```

### Tests added / changed this turn (per backend unless noted)

| File | Tests | Proves |
| --- | --- | --- |
| `test_payables_execution_api.py` (regression rewritten) | 22 | the Turn-4 invalid completion is refused; correction completes once; retries / conflicts |
| `test_payables_money.py` | 63 | monetary edge cases (service + API), one-cent detection |
| `test_payables_adversarial_v5.py` | 25 | direct-SQL attacks refused by the DB or detected by the verifier and refused by close / finalisation / recovery |
| `test_payables_concurrency_v5.py` | 10 (PostgreSQL; SQLite skips) | the eight race scenarios of §6 |
| `test_payables_failure_injection_v5.py` | 8 | the expected sequence; faults after flush, before / after commit, during lookup and recovery; recovery from a separate app instance and a restarted worker process |
| `test_payables_migration_v6.py` | 1 | populated Turn-4 upgrade, preflight, labels, remediation, restricted downgrade |
| `test_payables_e2e_turn5.py` | 1 | the multi-period scenario with exact balances (§12) |
| `test_payables_testdb.py` | 14 (PostgreSQL) | scratch-database ownership and administrative cleanup |
| `test_payables_performance_v5.py` | 1 (`perf`) | benchmarks with statement counts and plans |
| updated | | `db_rules_v4` (evidence for direct-SQL paths), `period_end_api` (new checks), `migration_v5`, `e2e_turn4`, `reconstruction`, `bulkdata` |

### Results

Final runs on the development container (13 Oct 2026), every database disposable (SQLite
files in pytest tmp dirs; PostgreSQL scratch databases `fmsscratch_session_*` created and
dropped by the suite; base databases `fms` / `fms_bg` untouched and empty afterwards).

| Command | Result |
| --- | --- |
| `python -m pytest -q -m "not perf" -p no:cacheprovider tests` (SQLite; PostgreSQL variants skip) | **584 passed, 0 failed, 545 skipped, 6 deselected** (perf), 549 s |
| `FMS_TEST_POSTGRES_URL=…/fms_bg python -m pytest -q -m "not perf" -p no:cacheprovider tests -k postgres` | **517 passed, 0 failed, 4 skipped** (SQLite-only simulations), 614 deselected, 761 s |
| `python -m pytest -q -m perf … test_payables_performance_v5.py -k sqlite` (2,000 suppliers) | 1 passed, 104 s; numbers in §11 |
| `FMS_TEST_POSTGRES_URL=…/fms_bg … test_payables_performance_v5.py -k postgres` | 1 passed, 175 s; numbers in §11 |
| `FMS_LIFECYCLE_SEEDS=3,17,101,5,8,13,21 FMS_LIFECYCLE_ROUNDS=8 … test_payables_lifecycle_properties.py -k sqlite` | 7 passed (random gateway behaviour + crashes + recovery; replay == reports, no double payment) |
| `ruff check --select F app/modules/payables scripts tests migrations` | clean |

Migration upgrade and schema drift: `test_migrations.py` (6: head == models via
`compare_metadata`, downgrade to base and up again, both backends) pass;
`test_payables_migration_history.py` (fmspayables02 → head with history),
`test_payables_migration_v5.py` (fmspayables04 → head, now through 06) and
`test_payables_migration_v6.py` (populated Turn-4 database: labels, hash back-fill,
preflight exit code 1, blocked close, explicit remediation, restricted downgrade, up again)
pass on both backends.

Monetary edge cases (`test_payables_money.py`, 63 + 1 per backend): `1e40`, `1e100`,
`1E+17`, `Infinity`, `-Infinity`, `NaN`, `sNaN`, `-0`, `-0.00`, `-1.00`, `0`, `1.005`,
`0.001`, `1E-3`, `abc`, `""`, `1,000.00`, floats, `True`, `None`, lists → `MoneyError`
(422), never `InvalidOperation`; `1E+2` → `100.00`; API refuses the same inputs for
payments, bill totals and line prices with 422 and writes nothing; one-cent discrepancies
are detected in bill totals, allocations, outstanding, balances, reconciliation and replay;
the SQLite evidence trigger compares integer cents (`0.10 + 0.20 + 0.70 + 1.15 + 2.85`
completes).

Verifier findings in the adversarial suite (`test_payables_adversarial_v5.py`, 25 per
backend): refused by the database — completing an unrecorded operation (with or without
forged flags or a real payment of another operation), `failed` after acceptance, replacing /
nulling / cross-linking payment ids, reusing confirmation references (update and insert),
every column of a completed operation, closed-period inserts / unallocations / discounts,
editing or deleting events, attestations and period locks; detected by the verifier and
refused by close / finalisation / recovery / completion — forged status flags
(`status_flag_forged`, `accounting_status_mismatch`), a look-alike payment linked by SQL
(`payment_reference_mismatch`, `payment_not_linked_to_operation`, `events_state_mismatch`,
`completed_without_evidence`), a duplicate live operation (`duplicate_live_operation`,
`gateway_evidence_missing`; recovery escalates it and records only the genuine transfer),
a state changed without an event, bypassed append-only guards (`events_sequence_gap`,
`events_hash_chain_broken`, `attestation_content_modified`), after which a forced close still
carries the obligation and the accepted exceptions.

Concurrency and fault injection: `test_payables_concurrency_v5.py` 10/10 on PostgreSQL
(three consecutive runs), `test_payables_concurrency_v4.py` 20/20 (an unclosed session in a
Turn-4 test was found to stall the suite under PostgreSQL and fixed),
`test_payables_failure_injection.py` 11 and `_v5` 8 per backend: after every injected
fault the row counts of payments, allocations, posted payment entries, draft entries and
events are asserted, recovery completes exactly once, and the restarted worker *process*
(`scripts/ap_recovery_worker.py`) completes the operation from durable state with its own
actor in the events.

Recovery evidence for the original defect (`test_attempts_are_bounded_and_manual_completion_requires_evidence`
and `test_payables_failure_injection_v5.py::test_the_expected_sequence`): gateway accepted →
`reconciliation_required` after 6 recovery rounds (attempts ≥ max, `recovery_action =
manual`, `accounting_status = unrecorded`, `delivery_status = delivered`, 1 transfer,
0 payments, 0 draft entries) → note: 200, state unchanged → `mark_completed` without
`payment_id`: **422** → bogus / other-supplier payment: 422 → investigation closed: state
and accounting status unchanged, reconciliation shows the 300.00 obligation → correction:
200, `completed` / `corrected`, 1 transfer, exactly one new payment and posted entry →
identical retries 200 with the same payment id; changed date / reason / allocations under
the same key 409; `complete` with the same payment 409; recovery has nothing to do; the run
is `completed`; obligations empty.

### Performance (Turn-5 benchmark, `tests/test_payables_performance_v5.py`)

Dataset: 2,000 suppliers, 10,000 bills, 6,000 payments, 667 credits, 2,020 payment
operations (2,005 accepted gateway transfers, 10,055 hash-chained events), 1,000 statement
lines, 3 close attestations, 16,667 posted entries; plus, during the benchmark, a 150-supplier
payment run executed through the simulated gateway and 200 confirmed transfers recorded by
4 concurrent recovery workers (PostgreSQL) / one worker (SQLite). Times are wall-clock from
`time.perf_counter_ns()` reported as exact Decimal seconds; *stmts* is the number of SQL
statements issued (SQLAlchemy cursor events). Single runs on a 4-CPU container with the
PostgreSQL server on the same host; the Turn-4 column is the 2,000-supplier run recorded in
Part 2 §5 where the measurement existed.

| Measurement | SQLite s (stmts) | PostgreSQL s (stmts) | Turn 4 SQLite / PG | Note |
| --- | --- | --- | --- | --- |
| recovery scan (20 incomplete) | 0.086 (1) | 0.110 (1) | 0.005 / 0.007 | ORM load of 2,370 operations' `NOT IN` scan; trivial |
| verify all money-moved operations (2,005) | 1.07 (63) | 1.63 (63) | – | batch prefetch: was 2,063 stmts / 36 s on PG before `Store.prefetch` + `evidence_many` |
| payment status lookup with verification | 0.022 (15) | 0.029 (15) | 0.007 / 0.014 (no verification) | one operation, full verifier |
| unverified-completions scan (period close check) | 0.62 (29) | 0.74 (29) | – | |
| payment-run proposal (150 suppliers, cash only) | 0.36 (453) | 3.49 (20) | 5.1 / 8.6 (all 2,000 suppliers) | not comparable: subset |
| payment-run execution (150 transfers, recorded + finalised) | 11.2 (10,458) | 14.9 (10,324) | – | ≈ 75 ms / 100 ms per supplier incl. gateway, posting, verification |
| run verification (150 operations) | 0.13 (19) | 0.15 (19) | – | |
| prepare + submit 200 operations | 3.6 (3,600) | 4.3 (3,600) | – | 18 stmts per intent (commit per durability point) |
| recording 200 confirmed transfers | 12.1 (10,850) one worker | **15.2 (15,860) 4 workers** | – | 200 of 200 recorded exactly once; PostgreSQL workers contend on the lease CAS |
| supplier statement matching (one supplier) | 0.010 (5) | 0.008 (5) | 0.010 / 0.015 | |
| AP ↔ GL reconciliation | 5.4 (176) | 5.3 (176) | 4.8 / 4.7 | + obligations (3 stmts); dataset has 350 more payments than Turn 4; profile: pre-existing ORM loading of bills dominates |
| period-end validation | 7.4 (836) | 6.6 (836) | 4.6 / 6.3 | + unrecorded-transfer and unverified-completion checks (verifier over 2,005 operations) |
| historical replay (plain SQL) | 0.24 (14) | 0.23 (14) | 0.14 / 0.25 | + operation positions, statement counts |
| replay + compare with every report | 13.6 (696) | 16.3 (696) | 14.0 / 21.9 | 3 attestations re-replayed; statement targets in 4 stmts (was 1,000) |
| close-package verification | 1.3 (46) | 1.7 (46) | – | fingerprint + replay + obligations |

Query plans recorded in `build/perf/<backend>-v5.json` (`query_plans`): unrecorded-transfer
scans use `ix_payment_operations_state`; events by operation use the `(operation_id, seq)`
unique index; gateway transfers by token use the unique token index; payments by vendor +
reference use `ix_vendor_payments_vendor_id` with a filter; `journal_lines` by entry is a
sequential scan (core table without an `entry_id` index — a core-ledger concern, noted since
milestone 3); operation positions are an index scan on the primary key with a date filter.

Regressions found **and fixed** by the benchmark: (1) the SQLite completion-evidence trigger
summed `NUMERIC` (stored as `REAL`) in floating point and refused 9 of 150 legitimate
completions (`0.10 + 0.20 > 0.30`); it now compares integer cents like the fmspayables02
triggers (`test_payables_money.py::test_completion_evidence_is_checked_in_exact_cents_on_both_backends`);
(2) per-operation gateway connections and per-operation queries in batch verification
(36 s → 1.6 s on PostgreSQL); (3) per-item queries in run finalisation (4 per item → 4 per
run) and per-line statement-target checks in the replay (1,000 → 4); (4) per-operation lazy
loads in the reconciliation's obligations (2,379 → 176 statements). Remaining differences to
Turn 4 are the added checks themselves (period validation +2.8 s on SQLite) and a larger
dataset.

## 12. End-to-end scenario (exact figures)

`tests/test_payables_e2e_turn5.py`, accounts AP 2000 / Expense 6000 / Cash 1000 / Discount
4900, suppliers OAK (2 % / 10 days), IRON, GLASS:

| Cutoff | Open bills | Advances | AP control (GL = subledger) | Cash credits (cumulative) | Obligations |
| --- | --- | --- | --- | --- | --- |
| 2026-01-31 | OAK-1 1000.00, IRON-1 300.00, IRON-2 450.00, GLASS-1 800.00 = **2550.00** | GLASS 300.00 | **2250.00** | 988.00 (200 + 488 + 300) | 0.00 |
| 2026-02-28 | IRON-1 300.00, IRON-2 450.00, OAK-3 310.00 = **1060.00** | 0.00 | **1060.00** | 2488.00 (+ 1000 OAK, + 500 GLASS) | **750.00** (IRON, confirmed, unrecorded) |
| 2026-03-31 | OAK-3 **310.00** | 0.00 | **310.00** | 3238.00 (+ 750 IRON correction dated 03-02) | 0.00 |

Journal effects: 14 posted entries, 0 drafts; AP debits 3350.00 / credits 3660.00 at
March 31; the IRON correction posts `Dr AP 750.00 / Cr Cash 750.00` on 2026-03-02; the
gateway holds exactly three accepted transfers (1000 + 750 + 500 = 2250.00) before and
after the correction. February's forced close carries the 750.00 obligation and two
accepted exceptions; both are resolved in March; `verify_close` reports the February
package verified with the obligation `completed / corrected / verified`.

## 13. Implemented guarantees vs assumptions

**Guaranteed by code and database rules (tested):** completion requires evidence (service
+ trigger); completed operations are frozen; one transfer ↔ one payment ↔ one operation
(unique constraints + verifier); events and attestations are append-only and tamper-evident
(hash chains, content hash); money is exact; obligations are effective-dated; a forced
close preserves obligations and exceptions; recovery never writes the books twice (CAS,
row locks, verifier gating); scratch databases are dropped only with proven ownership.

**Assumptions about the simulated gateway:** `DurableSimulatedGateway` honours the token
(a repeated token returns the original outcome), records accepted transfers durably, and
its `evidence(token)` is authoritative. A real provider must offer an equivalent durable
confirmation store for `PaymentGatewayPort.evidence`; until then "gateway evidence" means
the simulator's table. No real banking connection exists or was made.

**Future GL integration:** the ledger is written only through `ledger_adapter` /
`app.services.posting`; `PeriodPort` still consults AP's own locks only. The verifier
reads posted journal rows directly (read-only). Wiring the GL period module or the audit
module changes no AP rule.

**Not verified:** real process kills under load (crashes are in-process `BaseException`s;
the restarted worker is a real subprocess), network partitions, PostgreSQL serialization
under heavy parallel load beyond four workers, non-`XXX` currencies (refused).

---

# Part 2 — Turn 4: reliable payment operations and recovery (historical)

## 1. Features implemented

### Durable payment operations (`services/execution_state.py`, `services/orchestration.py`)

* One `payment_operations` row per (run, vendor) instruction with a stable `operation_uid`
  (the gateway idempotency token), the canonical amount (`Decimal`, exactly two places —
  `canonical_amount` refuses anything finer), an `instruction_fingerprint` of the plan
  (bills, amounts, destination, cash account, pay date), `state`, `version`, `attempts`,
  worker lease, last error + category, and the ids of what was recorded (payment, entry).
* Every state change is a compare-and-swap `UPDATE … WHERE id AND state IN (…) AND
  version = ?` plus an append-only `payment_operation_events` row (actor, category,
  detail, timestamp). Rowcount 0 ⇒ `OperationConflict` (409); nothing is overwritten.
* State machine (documented in `execution_state.py`):
  `prepared → submitted → confirmed → recording → completed`, with
  `submitted → confirmation_pending → confirmed` for asynchronous gateways,
  `prepared/submitted/confirmation_pending → failed` only while money has *not* moved, and
  `confirmed/recording → reconciliation_required → recording → completed` once it has.
  **Money-moved states are never resubmitted**: the only way forward is to record again
  or to resolve manually with authorisation.
* Explicit durability points: the intent is committed as `submitted` *before* the gateway
  is called; the gateway answer is committed as its own transition; AP payment +
  allocations + journal entry + `completed` are one transaction. Fault-injection hooks
  (`submit.before_commit`, `submit.after_gateway`, `confirm.before_commit`,
  `recording.before_books`, `recording.after_flush`, `recording.before_commit`,
  `recording.after_commit`, `close.before_commit`) sit at exactly these seams.
* Re-preparing the same (run, vendor) with the same plan returns the existing operation
  (idempotent); with a different plan it is an `InstructionConflict` (409) — a changed
  proposal can never ride an old token.
* `_write_books` always records the full cash movement; if part of the plan can no longer
  be applied at recording time (a bill settled in between), the remainder is recorded as
  an advance rather than silently dropped, and the event says so.

### Gateway contract (`services/gateway.py`, `integration.py`)

* `PaymentGatewayPort`: `submit(GatewayRequest) → GatewayResponse` and
  `lookup(operation_uid) → GatewayResponse`; outcomes `accepted / rejected / pending /
  unknown`; `GatewayTimeout` for lost responses. Replaces the milestone-3
  `PaymentExecutionPort` / `SimulatedPaymentExecutor`.
* `DurableSimulatedGateway` persists its transfers in `simulated_gateway_transfers` **on
  its own connection** (outside the AP transaction, like a real provider) and honours the
  token: a repeated uid returns the original outcome. `GatewayScript` drives deterministic
  behaviour keyed by vendor code: `reject`, `lose_response` (transfer happens, answer
  lost once), `drop` (nothing happens, answer lost once), `pending` + `pending_lookups`.

### Recovery (`services/recovery.py`, `/recovery/scan`, `/recovery/run`)

* `decide(op)` maps each incomplete operation to one action with a reason: `resume`
  (prepared, run still executing), `reconcile_gateway` (submitted / pending → look up by
  uid), `retry_recording` (confirmed or retryable recording failure), `ignore` (leased by
  another worker / terminal), `manual` (attempt budget exhausted, non-retryable error
  category, orphaned intent, unrecognised state).
* `process` takes a lease per operation (`worker_id`, `lease_expires_at`), performs the
  action through the same orchestration code, releases the lease, and re-syncs the run
  items / run status (a `failed` run is reopened to `executing` before its items change).
* Manual resolution (`POST /payment-operations/{uid}/resolve`) requires the authorization
  port and a reason; duplicate gateway confirmations (`POST …/confirmations`) are
  recorded as events and never change a terminal state.

### Payment run hardening (`services/payment_runs.py`)

* `version` + `proposal_fingerprint` / `approved_fingerprint`: review freezes what was
  seen; approval compares; any material change after approval (item edit, exclusion,
  revalidation finding) *invalidates* the approval (`approved → draft`,
  `approval_invalidated_reason`, 409 on execute).
* Maker-checker: approver ∉ {creator, reviewer}; executor ≠ approver; authorization
  port consulted for approve / execute / manual resolve / forced close.
* Execution claims the run with a CAS and a lease (`exec:{run}:{actor}`), prepares
  operations for all groups, then drives each one; concurrent executors get 409 and
  exactly one transfer per vendor is made (verified under PostgreSQL threads).
* Credit / advance pools are built in bulk (two queries per run instead of per vendor).

### Diagnostics (`services/diagnostics.py`, `/payment-operations`)

Status view per operation: state, checklist (intent committed, gateway answered, money
moved, AP recorded, ledger posted), masked destination, attempts, last error, events,
and *next steps* text (what an operator should do). Paginated list with state / run /
vendor filters.

### Period close under lock, attestations (`services/closing.py`)

* `close_period` takes an exclusive close lock (PostgreSQL advisory lock; SQLite write
  lock), every dated AP write takes the shared side in `common.assert_period_open`, so a
  close and a backdated write serialise; whichever wins, the other sees the result.
* Every attempt leaves an `ap_close_attestations` row (`closed`, `forced`, `failed`)
  with the validation result snapshot, the liability balance, actor, forced reason and an
  optional GL period reference. A forced close needs a reason and authorization.
  `GET /period-end/attestations`.

### Historical reconstruction (`services/reconstruction.py`, `GET /reconstruction`)

Independent replay in plain SQL of bill outstanding, vendor credits / advances, AP control
balances and the ledger at any `as_of` (`view=as_of`: what was true then; `view=restated`:
today's knowledge of then), then compared with every production report (outstanding,
aging, balances, statements, GL reconciliation) and with each closed-period attestation.
Disagreements come back as itemised `Finding`s. Used as the oracle in the lifecycle
property tests and the E2E scenario.

### Supplier statement imports (`services/supplier_statements.py`)

CSV import per vendor (`kind,reference,date,amount`; UTF-8, size, row-count, precision
and in-file duplicate checks), deterministic matching against the documents in force on
the statement date (invoice / credit / payment by reference, then by amount + date),
preview without persisting, persisted import with per-line status, manual resolution of
unmatched lines with a reason. Results are append-only.

### Test infrastructure

`tests/payables_support/dbscratch.py`: every PostgreSQL test session creates a uniquely
named scratch database (`fmsscratch_<label>_<time>_<pid>_<rand>`) marked with an owner
token in the database comment, and drops **only** databases whose token it owns. Base
databases are never migrated or dropped; `drop_listed_scratch_databases(confirm=True)`
cleans leaks after a killed run. `tests/payables_support/bulkdata.py` generates consistent
large datasets by direct insert for the benchmarks.

## 2. Migration `fmspayables05`

`migrations/versions/20261120_fmspayables05_payment_operations.py` (revises
`fmspayables04`, branch label `payables`): `payment_operations`, `payment_operation_events`,
`simulated_gateway_transfers`, `ap_close_attestations`, `supplier_statement_imports`,
`supplier_statement_lines`; `payment_runs.version / proposal_fingerprint /
approved_fingerprint / approval_invalidated_reason`; `payment_run_items.destination_reference`.
Completed operations are back-filled from paid run items so history stays explainable.
The run transition trigger now allows `approved → draft` (approval invalidation). Triggers:
operations are append-only in `operation_uid`, `run_id`, `vendor_id`, amount and
fingerprint; events, attestations and statement lines cannot be updated or deleted;
transitions must follow the state machine. Identical messages on SQLite and PostgreSQL.

**Downgrade is restricted**: it deletes operations, events, attestations and imports, so it
refuses to run unless `FMS_ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set. Verified up/down/up parity
on both backends (`test_payables_migration_v5.py`).

## 3. Changes to existing contracts

* `Integrations(ledger, audit, gateway, periods, faults, authorization)` — `executor`
  is gone; tests that injected `SimulatedPaymentExecutor` now inject a
  `DurableSimulatedGateway(GatewayScript(...))`.
* `POST /payment-runs/{id}/execute` returns `RunExecutionOut` with the operations; a
  vendor whose transfer is confirmed but whose recording failed shows `reconciliation_required`
  (previously `failed` + nothing recorded — which was wrong when money had moved).
* Approval invalidation: editing an approved run's items no longer 409s; it succeeds and
  sends the run back to `draft` with `approval_invalidated_reason`. Executing it is 409.
* Executor must differ from the approver (422).
* One-active-application-per-(credit, bill) and per-(advance, bill) rule removed: several
  dated applications are allowed (totals still capped by the DB). **Repeat-request
  protection remains**: an identical active application (same bill, amount, date) is 422
  "already applied to bill … with amount … on …".
* `POST /period-end/close` accepts `force`, `force_reason`, `gl_period_reference`; a
  failed close is 422 *and* leaves a `failed` attestation.
* Run state machine: `approved → draft` is now a legal DB transition (`db_rules_v3` updated).
* Crash simulation in tests raises `SimulatedCrash(BaseException)` so it is not caught by
  `except Exception` paths — i.e. it behaves like a process kill, not an application error.

## 4. Verification

### Commands

```bash
. .venv/bin/activate
# SQLite (PostgreSQL variants skip)
python -m pytest -q -m "not perf" -p no:cacheprovider tests
# PostgreSQL (a uniquely named scratch database is created and dropped; base DB untouched)
FMS_TEST_POSTGRES_URL=postgresql+psycopg://fms:fms@localhost:5432/fms \
  python -m pytest -q -m "not perf" -p no:cacheprovider tests -k postgres
# benchmarks (results in build/perf/<backend>-v4.json; FMS_PERF_VENDORS / FMS_PERF_BILLS)
python -m pytest -q -m perf -p no:cacheprovider tests/test_payables_performance_v4.py -k sqlite
FMS_TEST_POSTGRES_URL=…/fms_bg python -m pytest -q -m perf -p no:cacheprovider tests/test_payables_performance_v4.py -k postgres
# wider property search
FMS_LIFECYCLE_SEEDS=1,2,3,5,8,13,21,34,55,89,144,233 FMS_LIFECYCLE_ROUNDS=10 \
  python -m pytest -q -p no:cacheprovider tests/test_payables_lifecycle_properties.py -k sqlite
```

### Tests added this turn (counts per backend)

| File | Tests | What it proves |
| --- | --- | --- |
| `test_payables_testdb.py` | 7 (PG only) | scratch DB naming, ownership token, no drop of foreign / base DBs, collision handling, leak listing |
| `test_payables_execution_api.py` | 22 | execution through operations; repeat execute / retry / recovery endpoints; idempotent re-preparation; fingerprint conflict; diagnostics; manual resolution authorization; duplicate confirmations |
| `test_payables_failure_injection.py` | 11 | each fault point: what is committed, what is rolled back, what recovery does next; lost response vs dropped instruction; recording failure after money moved ⇒ `reconciliation_required`, retry records exactly once |
| `test_payables_concurrency_v4.py` | 10 | threads on PostgreSQL / SQLite: competing executors (one transfer), competing recoveries (leases), approve vs cancel vs send-back vs edit (serialisable history, version = 1 + successful transitions), close vs backdated write, duplicate confirmations |
| `test_payables_reconstruction_and_statements.py` | 7 | replay equals production reports; restated vs as-of; attestation mismatch finding; statement import validation, matching, resolution, repeat import |
| `test_payables_db_rules_v4.py` | 34 | direct-SQL rules of migration 05 on both backends (append-only, state machine, amounts, run version, `approved → draft`) |
| `test_payables_lifecycle_properties.py` | 3 seeds (default) | randomised lifecycles incl. faults and recovery; invariants: no double payment per (run, vendor), cash recorded = gateway transfers, replay = reports, ledger = subledger, terminal operations immutable |
| `test_payables_migration_v5.py` | 1 | up / down / up parity with data; restricted downgrade refuses without the flag |
| `test_payables_e2e_turn4.py` | 1 | full scenario: vendors, bills, review, approval, invalidation, execution with one recording failure, recovery, diagnostics, statement import, reconstruction, close, attestations |
| `test_payables_performance_v4.py` | 1 (marker `perf`) | benchmarks at 2,000 vendors / 10,000 bills |

Updated: `test_payables_runs_api`, `test_payables_e2e_milestone3`, `test_payables_db_rules_v3`,
`test_payables_history_api`, `test_payables_operations_api`, `test_payables_controls`
(new contract: identical application is the duplicate, not the pair).

### Results (recorded at completion, 13 Oct 2026, development container)

All against newly created disposable databases (SQLite files in pytest tmp dirs;
PostgreSQL scratch databases `fmsscratch_session_*` created and dropped by the suite;
the base databases `fms` / `fms_bg` stay empty).

| Command | Result |
| --- | --- |
| `python -m pytest -q -m "not perf" -p no:cacheprovider tests` (SQLite; PostgreSQL variants skip) | **485 passed, 465 skipped**, 4 deselected (perf), 438 s |
| `FMS_TEST_POSTGRES_URL=…/fms_bg python -m pytest -q -m "not perf" -p no:cacheprovider tests -k postgres` | **454 passed, 4 skipped** (SQLite-only simulations), 496 deselected, 456 s |
| `python -m pytest -q -m perf … test_payables_performance_v4.py -k sqlite` (2,000 vendors) | 1 passed, 80 s, numbers in §5 |
| `FMS_TEST_POSTGRES_URL=…/fms_bg … test_payables_performance_v4.py -k postgres` (2,000 vendors) | 1 passed, 151 s, numbers in §5 |
| `FMS_LIFECYCLE_SEEDS=<12 seeds> FMS_LIFECYCLE_ROUNDS=10 … test_payables_lifecycle_properties.py -k sqlite` | 12 passed (earlier in the turn, before the final formatting pass) |
| `ruff check --select F app/modules/payables tests migrations` | clean (one pre-existing unused import in `app/modules/tax`, not touched) |

The `test_competing_approve_and_modify_and_cancel` assertion was loosened during the final
PostgreSQL run: cancel *and* send-back can both succeed when the cancel reads the run after
the send-back committed (`reviewed → draft → cancelled` is a legal history). The test now
asserts a serialisable history and `version == 1 + successful transitions` instead of
"exactly one winner"; verified three further times on PostgreSQL.

### Idempotency, rollback and recovery evidence

* **Repeat requests**: `execute` on a completed run → 409, no new operation; re-prepare
  with the same plan → same uid; `POST /payments` with the same idempotency key → the
  original payment; statement import of identical content for the same vendor → 409, nothing written; duplicate
  confirmation → event only.
* **Failure rollback**: every fault point test asserts the exact row counts of
  `vendor_payments`, `payment_allocations`, `journal_entries`, `payment_operations` and
  `payment_operation_events` after the failure and after recovery; a failure before the
  gateway leaves `failed` + nothing recorded; a failure after money moved leaves
  `reconciliation_required`, and recovery records **once**.
* **Concurrency**: PostgreSQL threads on a scratch database; SQLite variants exercise the
  same code under the file lock.

## 5. Performance (scale: 2,000 vendors, 10,000 bills, 6,000 payments, 667 credits, 2,020 operations, 16,667 posted entries)

| Measurement (seconds) | SQLite | PostgreSQL |
| --- | --- | --- |
| data generation (direct inserts, triggers on) | 41.1 | 94.5 |
| payment-run proposal (7,289 items) | 5.1 (was 7.9 before bulk pools) | 8.6 |
| run revalidation | 2.1 (was 4.8) | 2.5 |
| historical aging | 2.3 | 3.5 |
| outstanding report | 2.7 | 3.2 |
| vendor balances (all) | 2.5 | 3.2 |
| supplier statement (one vendor) | 0.03 | 0.04 |
| statement reconciliation (import preview) | 0.01 | 0.02 |
| recovery scan (20 incomplete) | 0.005 | 0.007 |
| payment status lookup / listing page | 0.007 / 0.005 | 0.014 / 0.009 |
| AP ↔ GL reconciliation | 4.8 | 4.7 |
| period validation | 4.6 | 6.3 |
| reconstruction replay (plain SQL) | 0.14 | 0.25 |
| reconstruction compare (= replay + every production report) | 14.0 | 21.9 |

Single runs; the compare step is by construction the sum of the reports it checks. The
remaining cost centres are the report queries themselves (one pass per report over bills
/ allocations / applications); no index was added without evidence. `journal_lines` has no
`account_id` index in the core schema (core concern).

## 6. Known limitations and unverified behaviour

* No real gateway. The contract assumes token idempotency on the provider side; a provider
  that pays twice for the same token cannot be corrected by this module (it would show up
  in supplier statement reconciliation).
* Single currency; refunds / returned transfers are not modelled (a returned payment is a
  manual reversal).
* Recovery is invoked by endpoint (or the property tests), not by a scheduler; leases
  expire by time, so a worker killed mid-recovery is retried after expiry, not immediately.
* Fault injection and crashes are simulated in-process (`SimulatedCrash`); no real process
  kill or network partition was tested.
* A test session killed with SIGKILL can leak its scratch database; list and drop with
  `tests/payables_support/dbscratch.py` helpers (never dropped automatically by others).
* Statement matching is deterministic and conservative (reference, then amount + date);
  fuzzy matching is a review task, not automated.
* The period close lock is advisory on PostgreSQL and relies on every dated write going
  through `assert_period_open` (all AP services do; direct SQL is guarded only by the
  period-lock trigger, which does not take the lock).
* Performance numbers are single runs on a shared container.

---

# Part 3 — Milestone 3 record (historical)

## M3.1 Features implemented

### Accounting history
* **Durable credit application lifecycle.** `credit_applications` rows are never
  deleted. Unapplying stamps `unapplication_date`, `unapplied_by`, `unapplied_at`,
  `unapplication_reason`. Every report reconstructs the state as of a date from the
  application / unapplication dates (`CreditApplication.in_force_on`). Backdating is
  allowed back to the later of the credit date and the bill date (applications) and
  back to the application date (unapplications); never into a closed AP period.
* **Payment immutability.** `vendor_payments` is frozen in every column once written
  (method, reference, recorded_by, idempotency_key, payable account, amounts, dates,
  parties). Corrections are explicit: `reverse` (mirror entry, discounts reversed with
  it) or `correct` (reverse + replacement, one transaction, two posted entries).
* **Closed periods.** `ap_period_locks` (append-only). Approvals, payments,
  allocations, applications, discounts, reversals, unallocations and unapplications
  dated on or before the lock are refused by the services (`PeriodClosed`, HTTP 422)
  and by database triggers. Re-dating a draft into an open period is the documented
  way forward for unapproved documents.

### Advances and vendor balances
* A payment's allocations may total less than its amount; the remainder is an
  **advance** booked `Dr payables / Cr cash` on the payment's `payable_account_id`
  (vendor default unless given). `POST /payments/{id}/applications` applies part of
  the remainder to a bill later (an `advance` allocation, no ledger effect);
  `.../unallocate` closes such an allocation with date, actor and reason. Original
  allocations can never be unallocated (use reversal / correction).
* `GET /vendors/{id}/balance` and `GET /vendor-balances` distinguish **bill
  balances** (`bills_outstanding`, `open_bill_count`) from the **net amount owed**
  (`net_owed = bills_outstanding - unapplied_credits - unapplied_advances`), as of any date.
  Unapplied amounts never count as settled invoices.

### Payment operations
* **Payment runs** (`payment_runs`, `payment_run_items`): proposal from approved,
  unheld bills of *active* vendors whose installments fall due by `due_through`,
  settled on `pay_date` with discount (if paid in full inside the window), available
  credits, available advances, then cash. Each item carries an explanation. States:
  `draft -> reviewed -> approved -> executing -> completed | failed`, `failed ->
  executing` (retry), `reviewed -> draft` (send back), cancel from draft / reviewed /
  approved / failed. Items editable (partial settlement, exclusion) only in `draft`.
  Approver must differ from reviewer. **Approval never posts**; only execution does.
* **Execution adapter** `PaymentExecutionPort` (`ExecutionRequest` → `ExecutionResult`)
  with a deterministic `SimulatedPaymentExecutor` (fails for listed vendor codes or a
  predicate; remembers successful tokens). Execution claims the run (`executing`,
  committed), then commits per vendor group: rejection, stale item, or ledger
  refusal → items `failed`, no payment / allocation / entry; retry re-executes failed
  items only with the same idempotency token; paid items are skipped.
* **Holds and disputes** (`bill_holds`): placed with reason and actor, released with a
  resolution, never deleted. Held bills are excluded from proposals and refuse
  payment allocations (API and DB trigger); credits may still settle them.
* **Early-payment discounts**: configurable `discount_policies` (percent, days,
  discount account), assigned per vendor, snapshotted onto the bill at approval
  (`discount_date`, `discount_amount`, `discount_account_id`). Taking the discount
  (`take_discount` on an allocation, or automatically in a run) requires settling the
  bill in full inside the window; it books `Dr payables / Cr discount account` and is
  reversed together with the payment that earned it.
* **Installments / partial settlement**: proposals select by installment due date and
  propose the due installments' remaining amounts; reviewers may lower an item's
  `settle_amount`.

### Vendor master data and duplicates
* Vendor `status` (`active`, `on_hold`, `suspended`, `inactive`) with reason, actor and
  time; `is_active` kept in sync for compatibility. `preferred_method`,
  `bank_reference` (opaque), `discount_policy_id`, payment terms.
* **Near-duplicate review** (`duplicate_flags`): scored rules with explanations.
  Same compact number at the same vendor, or same compact number + amount at another
  vendor → *blocking* (approval refused until cleared; DB trigger too). Near number +
  same amount within 7 days → *advisory*. Same amount and date alone is not flagged.
  Exact canonical duplicates are still rejected outright (unchanged normalization).

### Reconciliation and period end
* `GET /reconciliation` lists **exceptions**: `missing_entry`, `entry_not_posted`,
  `entry_mismatch`, `foreign_entry`, `over_settled`, each with vendor, account,
  document, journal entry, expected vs actual and difference.
* `GET /period-end/validate` returns structured checks with severities
  (unapproved documents, rejected documents, blocking / advisory duplicate flags,
  held bills, unapplied credits, unapplied advances, reconciliation defects, foreign
  ledger entries, runs in flight, post-cutoff transactions). `POST /period-end/close`
  writes the lock when no blocker remains and notifies the `PeriodPort`.

### Maintainability
`services.py` became the `services/` package: `common`, `vendors`, `duplicates`,
`documents`, `credits`, `settlement`, `payments`, `payment_runs`, `reconciliation`,
`validation`; `services/__init__.py` re-exports the previous public names and
documents transaction ownership.

## M3.2 Migrations

| Revision | Content | Downgrade |
| --- | --- | --- |
| `fmspayables03` | application / allocation lifecycle columns (`kind`, dates, actors, unapplication / unallocation stamps), `vendor_payments.payable_account_id`, vendor status and payment preferences, bill discount snapshot, tables `discount_policies`, `bill_holds`, `duplicate_flags`, `bill_discounts`, `ap_period_locks`, indexes, rebuilt trigger sets (frozen payments, lifecycle rules, period locks, hold and flag rules). Back-fills `allocation_date` / `allocated_by` from the payments (PostgreSQL: the v2 append-only guard is disabled around the back-fill) and `vendors.status` from `is_active`. Drops the `(credit_id, bill_id)` and `(payment_id, bill_id)` UNIQUE constraints (re-application over time is legitimate; the services keep one *active* row per pair). | Restores the `fmspayables02` trigger bodies from that revision file. **Irreversible:** closed (unapplied) credit applications and unallocated advance allocations are deleted; discounts, holds, duplicate flags, period locks, discount policies and vendor status details are dropped; payments with an unapplied remainder keep their rows (the old code assumed allocations equal the amount). |
| `fmspayables04` | `payment_runs`, `payment_run_items`, their state-machine / freeze triggers and indexes. | Drops both tables; vendor payments produced by runs survive as ordinary payments. |

SQLite specifics: batch rebuilds drop a table's triggers, so each migration recreates
complete trigger sets; `PRAGMA legacy_alter_table=ON` is used around rebuilds because
modern SQLite validates other tables' trigger bodies during `RENAME`.

Verified: upgrade from empty on both backends; model/migration parity
(`compare_metadata == []`); downgrade to base and back; intermediate downgrades
`04 -> 03 -> 02` restore the exact trigger sets; upgrade of a database seeded with
`fmspayables02`-era history (approved bills with posted entries, payments, a
reversal, an applied credit) followed by new-code operations, reports, and a
downgrade with v3-only rows present (`tests/test_payables_migration_history.py`).

## M3.3 Tests added (this milestone)

| File | Covers |
| --- | --- |
| `test_payables_history_api.py` | dated application / unapplication and advance allocation lifecycle, month-by-month reconstruction, backdating rules, closed-period refusals (API and raw SQL) |
| `test_payables_operations_api.py` | advances and vendor balances, holds / disputes, discount policies and discount accounting incl. reversal, vendor status controls, blocking / advisory / cross-vendor duplicate flags and review |
| `test_payables_runs_api.py` | proposal selection and explanations, discounts / credits / advances in proposals, state machine and freezes (API + SQL), execution, failed vendor + retry without duplication, ledger refusal after bank success, stale items, credit-only settlement, closed period, concurrent execution |
| `test_payables_period_end_api.py` | reconciliation exceptions (foreign entry, missing entry, entry mismatch), every period-end check with items, close / already closed, GL period port veto and notification |
| `test_payables_db_rules_v3.py` | direct-SQL immutability of payments, allocation and application lifecycle rules, advance caps, discount rules, holds, flags, period locks, vendor status check, run transitions and item freezes, downgrade pruning |
| `test_payables_migration_history.py` | upgrade with pre-existing AP history, reports and services over legacy rows, downgrade with v3 rows |
| `test_payables_properties.py` | seeded randomized operations with invariants: conservation, non-negative balances, no double counting, statements reconcile, ledger == subledger, historical replay invariance (`FMS_PROPERTY_SEEDS`, `FMS_PROPERTY_STEPS`) |
| `test_payables_concurrency.py` | truly parallel requests: credit applications, payments on one bill, advance applications, duplicate bill submissions, idempotent payment submissions |
| `test_payables_performance.py` | generated data set (marker `perf`), timings written to `build/perf/<backend>.json` |
| `test_payables_e2e_milestone3.py` | the end-to-end scenario (approval, partial payment, credit apply / unapply, advance, hold, run review / approval / execution, failed execution + retry, correction, reconciliation, period close) |

Existing tests were kept; the following assertions were changed because the
behaviour they pinned was deliberately changed (see §7): under-allocated payments
are now valid (advance), a second unapply is 409 (row persists) instead of 404,
`vendor_payments.reference` is immutable, credit applications cannot be deleted,
the `(credit, bill)` UNIQUE pair is gone, statement totals gained discount keys,
and monkeypatch targets moved into the service submodules.

## M3.4 Commands and results

```bash
# SQLite only (PostgreSQL variants skip):
python -m pytest -q -m "not perf" tests
# Both backends (scratch database <db>_test is created and dropped by the suite):
FMS_TEST_POSTGRES_URL=postgresql+psycopg://fms:fms@localhost:5432/fms python -m pytest -q -m "not perf" tests
# Performance (one backend at a time; uses its own base database to avoid colliding scratch names):
FMS_PERF_SCALE=1 python -m pytest -q -m perf tests -k sqlite -s
FMS_TEST_POSTGRES_URL=postgresql+psycopg://fms:fms@localhost:5432/fms_bg python -m pytest -q -m perf tests -k postgres -s
# Wider randomized search:
FMS_PROPERTY_SEEDS=2,3,5,11,13,17,19,23,29,31 FMS_PROPERTY_STEPS=120 python -m pytest -q tests/test_payables_properties.py -k sqlite
```

Results are recorded in §9 (filled in from the final runs).

## M3.5 Concurrency and rollback verification

Verified by tests on both backends unless noted:

* Parallel HTTP requests (threads, barrier-synchronised): two applications of a 50.00
  credit (30 + 30) → exactly one 200 and one 422, 30.00 applied; two 70.00 payments on a
  100.00 bill → one 201, one 422, one allocation, one journal entry; two advance
  applications beyond the remainder → one succeeds; three parallel submissions of the
  same invoice number in different spellings → one 201, two 409, one bill; three
  parallel idempotent payment submissions → one 201, two 200, same payment id.
* Two executors on one run: the second gets 409 (the `executing` claim is committed
  first; on PostgreSQL the `FOR UPDATE` on the run row serialises them).
* Run execution: a vendor rejected by the bank leaves no payment, allocation or
  entry; a ledger refusal *after* the bank accepted leaves nothing and the retry books
  once (the simulated bank returns the remembered reference for the same token);
  stale items (bill paid or held between approval and execution) fail safely.
* Multi-entry rollback: a correction whose second posting is refused leaves neither
  the reversal nor the replacement (SAVEPOINT-joined posting session); unexpected
  exceptions during correction leave no trace; ledger refusals during credit approval
  and payment reversal roll back completely.
* Database backstops exercised by bypassing the service checks (monkeypatched
  validation) for over-allocation and over-application.
* SQLite-only simulations of lost races (the writer lock makes true interleaving
  impossible there): reversal of an already-reversed payment, over-settlement by a
  concurrent credit.

## M3.6 Performance measurements

Generated through the real services (every approval / payment posts an entry);
timings are single runs on the development container, so treat them as indicative.
Scale 1 = 20 vendors × 50 bills (1,000 bills, 600 payments, 100 credits, 100
applications, 60 unapplications, 40 reversals, 60 holds, 1,740 journal entries, 633
open bills, 573 proposal items).

| Measurement (seconds) | SQLite, scale 1 | PostgreSQL, scale 1 (clean run) | SQLite, scale 2 (4,000 bills) |
| --- | --- | --- | --- |
| data generation | 131.0 | 157.7 | 389.2 |
| aging report | 0.37 | 0.54 | 1.32 |
| outstanding report | 0.20 | 0.41 | 1.48 |
| reconciliation report | 0.58 | 0.66 | 2.43 |
| vendor balances (all) | 0.54 | 0.48 | 2.02 (before bulk rewrite) |
| vendor statement (busiest vendor) | 0.08 | 0.13 | 0.09 |
| payment-run proposal | 0.67 | 1.23 | 2.36 |
| period validation | 0.90 | 1.18 | 2.94 |

Bottlenecks found and fixed from these measurements:

1. **Payment-run proposal**: the credit / advance pools were rebuilt for every bill
   because of `dict.setdefault(key, expensive())`; rebuilt once per vendor instead.
   SQLite scale 1: 3.18 s → 0.67 s; profile at half scale 0.82 s → 0.33 s.
2. **Vendor balances**: per-vendor queries (~10 per vendor) replaced by one pass over
   bills, credits and payments. PostgreSQL scale 1: 2.54 s → 0.48 s (round trips dominated).

Indexes added in `fmspayables03/04` (vendor ids, dates, payment / credit / bill
foreign keys, run ids) are foundational; at these volumes they did not produce a
measurable change on their own and no further index was added on evidence. The
reconciliation's ledger side scans `journal_lines` by `account_id`, which has no
index in the core schema; that is a core-ledger concern, noted in §8.

## M3.7 Changes to existing contracts

* `POST /payments`: `allocations` is optional and may total less than `amount` (the
  remainder becomes an advance); over-allocation is the only 422 ("exceed the payment
  amount"). New optional `payable_account_id`; `AllocationIn.take_discount`.
* `POST /credits/{id}/applications/{app}/unapply`: the row is closed, not deleted;
  repeating it is 409 (was 404). Body accepts optional `reason`, `unapplication_date`.
* Direct SQL: `vendor_payments.reference` (indeed every column) is immutable;
  `credit_applications` cannot be deleted; UNIQUE `(credit_id, bill_id)` and
  `(payment_id, bill_id)` removed (the services enforce one *active* row per pair).
* Response models gained fields only (vendor status block, bill discount / holds /
  flags / `on_hold`, payment `status` and allocation lifecycle fields, statement
  `discounts` totals and `unapplied_advances`, reconciliation `exceptions`, outstanding
  `on_hold`). Existing fields keep their meaning; `ScheduleOut.amount_paid` includes
  credits and discounts (settled amount), as before for credits.
* Bill approval is additionally refused for pending *blocking* duplicate flags and for
  invoice dates inside a closed AP period; vendor checks use the status model
  (messages read "Vendor X is inactive; cannot …").
* `services` is a package; test monkeypatch targets moved to the submodules
  (`services.documents.approval_problems`, `services.payments._allocation_problems`
  — now also takes `pay_date` —, `services.payments._existing_by_key`,
  `services.payments.lock`, `services.documents._assert_no_duplicate_invoice`,
  `services.credits._assert_no_duplicate_credit`). Public function names are
  re-exported unchanged.
* `pyproject.toml`: pytest marker `perf` registered.

## M3.8 Known limitations and unverified behaviour

* No real banking integration: `SimulatedPaymentExecutor` only. The contract assumes
  the provider honours the idempotency token; a provider that does not could pay twice
  after a booking failure (mitigation: manual reconciliation of run attempts).
* Execution commits per vendor group by design; a crash between a successful bank
  call and the commit leaves the item `failed` and relies on the token-idempotent
  provider on retry. Not tested with a real process kill.
* Currency is a placeholder (`"XXX"` in execution requests); the module is
  single-currency.
* Discount policy resolution is per vendor (no per-bill override); discounts require
  full settlement inside the window.
* Advances must be applied to bills on the payment's payable account; cross-account
  application is refused rather than re-posted.
* The reconciliation's ledger scan has no index on `journal_lines.account_id` (core
  table); fine at the measured scale, unmeasured beyond ~7k entries.
* Period locks are AP-only; the GL period module is consulted through `PeriodPort`
  but does not exist yet. The lock is a single "closed through" date (no reopening).
* SQLite `UPPER()` is ASCII-only: canonical numbers with non-ASCII letters differ
  between SQLite and PostgreSQL.
* The PostgreSQL test scratch database name is derived from the URL (`<db>_test`);
  two test sessions on the same URL collide (the conftest refuses to drop a database
  in use — observed once while running a perf job and a suite concurrently; use
  different base databases).
* Performance numbers are single runs on a shared container; generation throughput
  (≈7–9 operations / s) is dominated by one transaction + one posted entry per
  operation and was not optimised.
* Not tested: behaviour under real network partitions, very large runs (>10k items),
  PostgreSQL serialization failures under heavy parallel load (row locks were
  exercised with two to three concurrent requests).

## M3.9 Final test run (recorded at completion)

Final runs on the development container (12 Oct 2026), all against newly created
disposable databases (SQLite files in pytest's tmp dirs; PostgreSQL scratch database
`fms_test` created and dropped by the suite, `fms_bg_test` for the performance run):

| Command | Result |
| --- | --- |
| `python -m pytest -q -m "not perf" tests` (SQLite; PostgreSQL variants skip) | **396 passed, 369 skipped**, 2 deselected, 549 s |
| `FMS_TEST_POSTGRES_URL=…/fms python -m pytest -q -m "not perf" -k postgres tests` | **365 passed, 4 skipped** (3 SQLite-only race simulations, 1 SQLite-only scale check), 398 deselected, 686 s |
| `python -m pytest -q -m perf tests -k sqlite -s` (scale 1) | 1 passed, measurements in §6 |
| `FMS_TEST_POSTGRES_URL=…/fms_bg python -m pytest -q -m perf tests -k postgres -s` (scale 1) | 1 passed, measurements in §6 |
| `FMS_PERF_SCALE=2 … -k sqlite` | 1 passed, measurements in §6 |
| `FMS_PROPERTY_SEEDS=2,3,5,11,13,17,19,23,29,31 FMS_PROPERTY_STEPS=120 … test_payables_properties.py -k sqlite` | 10 passed, 296 s |

Test inventory (per backend; each database-backed test runs once per backend):
`test_payables_api` 62, `test_payables_controls` 35, `test_payables_credits_api` 44,
`test_payables_db_rules` 88, `test_payables_db_rules_v2` 82, `test_payables_db_rules_v3` 69,
`test_payables_history_api` 8, `test_payables_operations_api` 28, `test_payables_runs_api` 10,
`test_payables_period_end_api` 5, `test_payables_statements_api` 10, `test_payables_services` 37,
`test_payables_concurrency` 5, `test_payables_properties` 3 seeds (default), `test_payables_migration_history` 1,
`test_payables_e2e_milestone3` 1, `test_payables_performance` 1 (marker `perf`), plus the
unchanged core suites (`accounts`, `journal`, `ledger_rules`, `migrations`, `module_starters`,
`posting`, `health`).

## M3.10 Recommended next milestones

1. **Real payment execution**: bank-file / API adapter behind `PaymentExecutionPort`,
   remittance advice, status callbacks, and an execution-attempt log table.
2. **Multi-currency AP**: currency on vendors / bills / payments, FX at payment,
   realised gain / loss postings.
3. **GL period integration**: wire the GL period module into `PeriodPort`; reopen /
   adjust periods with a controlled workflow.
4. **Tax**: tax codes on bill lines and tax postings at approval.
5. **Audit wiring**: plug the audit module into `AuditPort` (one-line change) and add
   an event-log endpoint for AP documents.
6. **Operational reporting**: vendor aging trends, payment-run history, and discount
   capture statistics; index `journal_lines(account_id, entry_id)` in the core ledger
   when reconciliation volumes grow.
7. **Recurring / template bills and purchase-order matching** (three-way match) for
   stronger duplicate and price controls.
