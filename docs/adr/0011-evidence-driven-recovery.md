# ADR 0011: Evidence-driven recovery reclassification

## Status

Accepted for V0.6. Details: `docs/architecture/recovery.md`.

## Context

In V0.5.1 a historical Godot failure (no process launched, "Godot executable is required…", recorded as `ENGINE_IMPORT_FAILED`) was made retryable by a hand-written SQL update. A generic "set retryable" command would make that easy, and it would also make it easy to re-run a paid operation or a deterministic failure.

## Decision

1. `gamefactory recovery inspect <workflow>` is read-only. It classifies each failed or uncertain task from stored evidence and lists safe next actions as exact commands.
2. `gamefactory recovery reclassify --execution E` changes only `retryable 0→1`, and only when the current policy (`recovery-policy-0.6.0`) proves pre-launch tool unavailability. The proof is: no pid, no exit code, no output, a pre-launch error signature, an allowed error code, and for Godot no stage directory and no engine logs. The command is dry-run by default and applies with `--apply --actor --reason`, as one atomic compare-and-set plus audit event.
3. It never applies to paid, metered or expensive tasks, launched processes, runtime or asset validation failures, or ambiguous evidence. There is no force option.
4. Paid recovery stays on the existing exactly-once path: a SUCCEEDED or SUBMITTED intent with a task ID resumes query-only, an uncertain submission requires provider-side reconciliation, and a terminal provider failure requires a new revision.

## Consequences

The production repair class becomes a supported, audited operation. Classification of the real historical row reproduces `PRE_EXECUTION_TOOL_CONFIGURATION`. Recovery tooling cannot create a second paid submission.
