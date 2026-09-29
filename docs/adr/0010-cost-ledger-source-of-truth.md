# ADR 0010: Append-only cost ledger as the spend source of truth

## Status

Accepted for V0.6. Details: `docs/architecture/accounting.md`.

## Context

Project spend was `SUM(executions.cost)`, and `execution.cost` was rewritten in place. That conflated reservation, estimate, actual cost and settlement. Pre-submission failures kept their reservation. Query-only recovery settled only a delta on the new attempt, so the original over-reservation remained (production: reserved 20, actual 15, recorded 20). Corrections required raw SQL.

## Decision

1. A new `cost_ledger` table is append-only (triggers abort UPDATE and DELETE) with entry types RESERVE, SETTLE, RELEASE and ADJUSTMENT. Each entry is bound to project, workflow, task, execution, intent and request fingerprint, with amount, unit, reason, source and actor.
2. Project committed spend is the signed sum of the ledger. It is the **only** budget authority: the atomic claim, the policy check, `status` and `inspect` all read it. `executions.cost` stays as a legacy display mirror and is never read for decisions.
3. Settlement is per paid operation (the task), not per attempt:
   - dispatch → RESERVE;
   - known partial actual above the hold → RESERVE top-up;
   - terminal outcome with known actual → SETTLE actual plus RELEASE of everything held;
   - unknown actual or uncertain outcome → hold;
   - failure with no provider submission → RELEASE.
   A query-only recovery attempt reserves nothing and settles the original reservation.
4. Migration 0007 backfills one conservative RESERVE per historical `executions.cost > 0`, so spend is identical immediately after migration. History is never rewritten.
5. `gamefactory accounting reconcile` is dry-run by default and requires `--actor` and `--reason` to apply. It settles only operations whose intent is terminal with a recorded actual, and it refuses uncertain submissions.

## Consequences

One reconciliation settles historical overstatements with an audited trail and no raw SQL. Budget checks and displays can no longer diverge. Uncertain submissions stay conservatively held until provider evidence exists.
