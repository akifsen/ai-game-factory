# Cost Ledger & Accounting Architecture

## 1. Executive Summary

AI Game Factory V0.6 replaces the legacy `SUM(executions.cost)` budget calculation with an authoritative, append-only **Cost Ledger**.

Prior to V0.6, project spend was computed dynamically by summing mutable `cost` columns on task attempt executions. This caused three fundamental accounting hazards:
1. **Pre-submission failure stranded holds**: A failed task attempt that was blocked or failed before contacting a provider retained its full reservation in `executions.cost`.
2. **Query-only recovery over-reservation**: Multi-attempt crash recoveries settled only the delta on subsequent attempts while leaving earlier over-reservations intact.
3. **Absence of financial audit trails**: Spend adjustments required raw SQL mutations to historical execution rows, destroying provenance.

Under V0.6:
- The **Cost Ledger** (`cost_ledger`) is the **single source of truth** for project spend and budget authorization.
- `executions.cost` is strictly a **legacy display mirror** preserved for backwards compatibility and human-facing logs; it is never read for policy or budget gating decisions.
- Existing historical execution rows are never mutated by migrations or reconciliations.

---

## 2. Ledger Schema & Immutability

The `cost_ledger` table records atomic financial events:

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | `TEXT` | `NOT NULL` (PK) | Prefixed unique identifier (e.g. `LEDGER-xxx`, `LEDGER-LEGACY-xxx`) |
| `project_id` | `TEXT` | `NOT NULL` | Project holding the budget liability |
| `workflow_id` | `TEXT` | `NOT NULL` | Associated workflow |
| `task_id` | `TEXT` | `NOT NULL` | Paid operation identifier |
| `execution_id` | `TEXT` | `NULL` | Task attempt execution id (if bound to a specific attempt) |
| `intent_id` | `TEXT` | `NULL` | Durable provider operation intent id |
| `request_fingerprint` | `TEXT` | `NULL` | Approved request fingerprint |
| `entry_type` | `TEXT` | `NOT NULL` | `RESERVE`, `SETTLE`, `RELEASE`, `ADJUSTMENT` |
| `amount` | `REAL` | `NOT NULL` | Non-negative for RESERVE/SETTLE/RELEASE; signed for ADJUSTMENT |
| `cost_unit` | `TEXT` | `NOT NULL` | Unit of cost (default: `credits`) |
| `reason` | `TEXT` | `NOT NULL` | Human-readable explanation |
| `source` | `TEXT` | `NOT NULL` | System provenance (e.g. `execution_claim`, `provider_terminal`) |
| `actor` | `TEXT` | `NOT NULL` | Triggering actor (e.g. `WorkflowEngine`, `migration-0007`) |
| `created_at` | `TEXT` | `NOT NULL` | UTC ISO-8601 timestamp |

### Immutability Guarantees
The cost ledger enforces append-only semantics at the database engine level:
- SQLite triggers `BEFORE UPDATE` and `BEFORE DELETE` unconditionally raise `ABORT`.
- The table defines no foreign keys with `ON DELETE CASCADE`. If a parent project or workflow is archived or removed, ledger history persists.

---

## 3. Entry Semantics & Signed Mathematics

Project spend is computed as the algebraic sum of signed entry amounts:

$$\text{Project Committed Spend} = \sum_{e \in \text{Ledger}} \text{signed\_amount}(e)$$

Where `signed_amount(e)` is defined as:
- **`RESERVE`**: $+ \text{amount}$ (provisional liability hold against project budget during execution).
- **`SETTLE`**: $+ \text{amount}$ (authoritative final charge upon terminal outcome).
- **`RELEASE`**: $- \text{amount}$ (unwinding of provisional liability hold).
- **`ADJUSTMENT`**: $\text{amount}$ as stored (signed adjustment for operator reconciliation).

### Lifecycle Example
1. **Dispatch Hold**: An asset generation task estimating 20 credits is claimed.
   - Entry: `RESERVE 20`
   - Committed Spend: $+20$
2. **Terminal Settlement**: The provider confirms success with an actual invoice of 15 credits.
   - Entries: `SETTLE 15`, `RELEASE 20`
   - Net Operation Spend: $+20 + 15 - 20 = 15$ credits.
   - 5 credits of unused budget are immediately freed for concurrent workflows.

---

## 4. Operation Account Balance

Ledger queries aggregate entries per paid operation (keyed by `task_id`) into an `OperationAccount`:
- **`reserved_total`**: $\sum \text{amount}$ for all `RESERVE` entries.
- **`released_total`**: $\sum \text{amount}$ for all `RELEASE` entries.
- **`settled_total`**: $\sum \text{amount}$ for all `SETTLE` entries.
- **`adjustments`**: $\sum \text{amount}$ for all `ADJUSTMENT` entries.
- **`held`**: $\text{reserved\_total} - \text{released\_total}$ (active reservation held while unsettled).
- **`settled`**: Boolean flag indicating whether any `SETTLE` entry exists.
- **`net`**: $\text{held} + \text{settled\_total} + \text{adjustments}$.

Once settled, `held` is 0 (or neutralized), and `net` reflects the authoritative provider billing.

---

## 5. Settlement Rules

Settlement is evaluated per paid **operation** (`task_id`), not per individual execution attempt:

1. **Terminal Provider Success with Known Actual ($A$)**:
   - If not yet settled: append `plan_settlement(account, A)`.
   - Produces `SETTLE A` and `RELEASE held` (omitting zero-amount releases).
   - Even if $A = 0$, a `SETTLE 0.0` entry is written to definitively mark the operation settled.
   - Emits audit event `COST_SETTLED`.
2. **Terminal Provider Success with Unknown Actual**:
   - Reservation stays held; no settlement entry is appended until actual cost evidence is retrieved.
3. **In-Flight Partial Actual ($A > \text{held}$)**:
   - Append `RESERVE (A - held)` with `source='provider_partial_actual'`.
   - Never release held reservations while the task is in-flight.
4. **Pre-Submission / No-Submission Failure**:
   - If a failure occurs before any provider intent is persisted (e.g. validation error, approval missing):
   - Append `RELEASE held` with `source='no_provider_submission'`.
   - Emits audit event `COST_RELEASED`.
5. **Post-Submission Failure with Persisted Intent**:
   - `UNCERTAIN` or `SUBMITTING` without external remote ID: hold reservation.
   - `FAILED` with verified actual cost: settle to actual cost (`SETTLE actual`, `RELEASE held`).
   - `FAILED` with unknown actual cost: hold reservation.
6. **Crash & Query-Only Recovery**:
   - Attempt 1 claims with reservation (e.g. 20) and crashes after external task submission.
   - Attempt 2 runs as query-only recovery with `cost = 0.0` (no additional reservation).
   - When Attempt 2 retrieves remote success (e.g. actual 15), it settles the **original** reservation (`SETTLE 15`, `RELEASE 20`).
7. **Builtin Demo Tasks**:
   - Builtin `paid_generation` settles to `execution.cost` on success, ensuring demo workflows align with ledger totals.

---

## 6. Reconciliation CLI

The operator CLI provides audited, non-destructive reconciliation:

```bash
# Dry run preview (default); prints a plan hash
gamefactory accounting reconcile --workflow WF-123 --task TASK-ID

# Apply exactly the reviewed plan
gamefactory accounting reconcile --workflow WF-123 --task TASK-ID --apply   --actor operator-name --reason "Reconcile verified invoice" --plan-hash <hash from dry run>
```

> **The dry run is not a read-only command.** Like every CLI command that opens the
> project database, `accounting reconcile` goes through `_db()`, which applies pending
> schema migrations first. On a schema-6 (V0.5.1) database even the default dry run
> migrates it to the current schema. Do not use it as a "read-only inspection" of a
> live database; follow the operator procedure below.
>
> `accounting ledger` and `recovery inspect` are read-only: they open the database with
> SQLite `mode=ro`, never migrate it, and refuse a database whose schema is older than
> the CLI instead of migrating it.

### Eligibility Invariants
An operation is eligible for reconciliation if and only if:
1. **Provider Evidence Available**: The operation is unsettled, the durable intent status is `SUCCEEDED` or `FAILED`, and `actual_cost` is non-null. Proposal: `SETTLE actual` + `RELEASE held`.
2. **Definitive No-Submission (asset paid generation only)**: The operation is unsettled, the task type is `asset_paid_generation`, no provider intent exists, a reservation is held, and every record supports that no provider request was sent: no `RUNNING` attempt, no attempt with an external operation ID, no `COMPLETED` attempt or task, and no recorded provider invocation for the workflow. Proposal: `RELEASE held` only (`source='reconciliation_no_submission'`). This relies on the invariant that asset paid generation journals a durable intent (`claim_intent`) before any provider contact, which has held since the task type was introduced in V0.4. An `UNCERTAIN` attempt without an intent is therefore a crash before contact.

Everything else is **not eligible, manual evidence required** (`manual_evidence_required: true`), including:
- any other paid, metered or expensive task type without an intent (for example the legacy builtin `paid_generation`), because those never journal an intent and a missing intent is not evidence that the provider was not called or charged;
- an intent in `SUBMITTING`, `SUBMITTED` or `UNCERTAIN`, or a terminal intent with unknown actual cost.

### Apply Semantics
- The preview is read in one read transaction. `--apply` then opens a `BEGIN IMMEDIATE` transaction and, under the write lock, re-evaluates the plan and re-reads the project balance on the same connection.
- If the plan in the transaction differs from the previewed plan (or from `--plan-hash`), for example a changed intent, actual cost, reservation or eligibility, nothing is written and the command fails with `RECONCILIATION_STATE_CHANGED` (exit code `3`). Re-run the dry run and review the new plan.
- The `ACCOUNTING_RECONCILED` audit event and the returned `spend_before`, `spend_after` and `net_change` are computed inside the apply transaction, so they match the ledger change that was actually committed, even when another workflow spent in between. The audit event also records the plan hash and the ledger entry IDs.
- A second apply is a clean no-op returning exit code `0` (`EXIT_SUCCESS`) and reporting `Nothing to reconcile`.
- Reconcile does not take the workflow lock. It never contacts a provider, and its only write is the ledger append inside `BEGIN IMMEDIATE`; the same-transaction plan check covers the races that matter. Running it with workflows stopped is still the operating rule for live databases (below).

### Operator Procedure for a Live Database
Reconciling a live production database is an operator decision, not a release prerequisite.

1. **Inspect read-only.** Open the live database with SQLite `mode=ro` (for example `sqlite3 "file:factory.db?mode=ro"` or Python `sqlite3.connect("file:...?mode=ro", uri=True)`). Do not run any `gamefactory` command against it yet.
2. **Make a consistent scratch copy** with the SQLite Backup API (`sqlite3` `.backup`, or Python `src.backup(dst)` from the read-only connection). Do not copy the file while a writer may be active.
3. **On the scratch copy only**, run the migration and `accounting reconcile --workflow WF --task TASK` dry run, and check the plan against provider billing evidence.
4. **Apply on the live database** only in a separate maintenance window: stop all workflows (quiescence), take a backup, confirm the provider cost evidence, re-run a dry run narrowed to the target task on the live database, then apply with `--plan-hash` from that dry run.
