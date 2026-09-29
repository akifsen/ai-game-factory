# Evidence-driven recovery (`recovery-policy-0.6.0`)

Decision record: [ADR 0011](../adr/0011-evidence-driven-recovery.md). Implementation: `src/gamefactory/workflows/recovery.py`.

## Inspect (read-only)

```bash
gamefactory recovery inspect WF-ASSET-xxxxxxxx
```

For each failed, blocked or uncertain task, the command shows the latest execution (attempt, status, retryable, pid, exit code, presence of stdout and stderr, error), the attempts used against the limit, the provider intent for paid tasks, the task's immutable artifacts, a classification, and the exact safe next commands. It writes nothing, including audit events.

| Category | Meaning / safe action |
|---|---|
| `PRE_EXECUTION_TOOL_CONFIGURATION` | tool unavailable before launch; `retry`, or `reclassify` for historical rows |
| `TOOL_EXECUTION_FAILURE`, `ENGINE_IMPORT_FAILURE` | the tool ran and failed; inspect the logs |
| `RUNTIME_VALIDATION_FAILURE`, `ASSET_VALIDATION_FAILURE` | deterministic failure; fix inputs, never auto-retry |
| `PRODUCTION_READINESS_FAILURE` | fix the configuration, then `retry` the READINESS task |
| `PRE_SUBMISSION_FAILURE` | paid task failed before any provider submission; the reservation was released |
| `PAID_SUBMISSION_UNCERTAIN` | acceptance unknown; provider-side reconciliation required; the hold stays |
| `PROVIDER_RESULT_REUSABLE` | a provider task ID exists; `resume` queries only and never regenerates |
| `PROVIDER_TERMINAL_FAILURE` | allocate a new asset revision |
| `APPROVAL_DECISION` | a human decision blocks or ends the task |

## Reclassify (audited)

```bash
gamefactory recovery reclassify --execution EXEC-xxxxxxxx               # dry run
gamefactory recovery reclassify --execution EXEC-xxxxxxxx --apply --actor NAME --reason TEXT
```

The only mutation is `executions.retryable 0 → 1`. It is applied as a compare-and-set (`WHERE retryable = 0 AND status = 'FAILED'`) together with a `RECOVERY_RECLASSIFIED` audit event that records the evidence, policy, actor and reason. It requires every one of the following:

- task type `asset_godot` or `asset_process`, not paid, metered or expensive;
- latest attempt of a FAILED task, execution FAILED;
- no pid, no exit code, no stdout or stderr;
- the error matches a pre-launch signature (for example "Godot executable is required…");
- the error code is absent or one of `TOOL_UNAVAILABLE`, `ENGINE_IMPORT_FAILED` (the historical misclassification), `FACTORY_ERROR`;
- for Godot: no stage directory and no import or render log for that execution.

Launched processes, validation failures, provider failures, paid tasks and ambiguous evidence are refused. There is no `--force`.

## Paid exactly-once invariants

Recovery never deletes or resets a provider intent and never marks a paid task retryable. One approved request fingerprint gives at most one provider submission. Timeouts and malformed responses are UNCERTAIN. Once a task ID is persisted, every later operation only queries or retrieves. A downstream failure never regenerates.
