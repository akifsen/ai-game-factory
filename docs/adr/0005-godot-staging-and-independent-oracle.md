# ADR 0005: Staged Godot execution and an independent assertion oracle

## Status
Accepted for V0.2. Implementation and runtime acceptance are tracked in
[the work plan](../work-plan-v0.2.md); this decision alone is not test evidence.

## Context
V0.1 can discover Godot but cannot establish whether a scene behaves correctly.
A successful process exit or a harness-generated PASS is insufficient evidence.
Running an editor/import against the original game also creates caches and may
execute project scripts. Approval must cover the actual inputs that will run.

## Decision

1. Keep the existing sequential workflow engine, SQLite repositories, task
   attempts, approvals, artifacts and evidence gates. Add three bounded tasks:
   `godot_execute`, `godot_validate`, and existing `record_evidence`. Engine-specific
   process construction and staging belong to the Godot adapter/integration.
2. Parse a strict, bounded, versioned scenario before any workflow process launch.
   Snapshot the full scenario durably for audit. Send only scene, ordered actions,
   snapshot ticks and correlation identifiers to Godot. Expected values and
   assertion operators remain in Python. The selected scenario file is excluded
   from the runtime project copy even when located inside the source project.
3. Use a per-execution staged project, separate persistent artifacts and transient
   scratch. Validate source paths without following symlinks or Windows reparse
   points. Bound file count and bytes. Exclude Factory data, VCS, environments,
   caches and credentials. Recheck source content against the approved manifest.
   Use a distinct writable Godot user-data location for each execution.
4. Package a small development-only GDScript SceneTree harness. It instantiates
   the real scene and exposes a fixed action/snapshot contract. Game code owns
   damage, enemy removal and scoring. The harness neither computes expected
   results nor creates a success gate. Normal game startup never loads it.
5. Tick zero observes the initialized scene. Each later tick waits for the
   end-of-frame checkpoint, executes actions in scenario order, and then observes
   state. This is a deterministic integer fixture contract, not a guarantee of
   universal cross-platform physics determinism.
6. Launch import and runtime as separate bounded argument-array processes using
   ProcessRunner. Persist their individual stdout, stderr and process metadata.
   Truncated output, failed import, timeout, malformed/incomplete observation or
   known script errors cannot open a gate. Python verifies current execution and
   scenario identity, complete snapshots and independent integer assertions.
7. Extend registered handler metadata so central policy sees process execution
   and managed writes. Default existing handlers retain their prior behavior.
   Process approval is opt-in and additive to the V0.1 config schema. Bind approval
   to scenario, source manifest, executable, harness and launch parameters;
   recheck before dispatch and request fresh approval when the inputs change.
8. Persist launch intent before a process call and a terminal receipt after a
   known result. A lost orchestrator does not prove its child exited. An unmatched
   intent remains UNCERTAIN/BLOCKED on repeated resume and cannot be silently
   retried. PID alone is not ownership proof. A safely terminal failed attempt can
   be retried as a new attempt; historical artifacts remain immutable.

## Consequences

The game remains usable without Factory. No production runtime dependency,
network control channel, arbitrary method dispatch or engine-specific domain type
is added. SQLite/config versions need not change for additive task parameters,
artifact records and operational journal files.

Staging has a deliberate I/O cost and excludes unsupported source paths. It is
not an OS sandbox: authorized local project code runs with the user's permissions.
The design assumes a trusted local project and cannot defend against a malicious
project that deliberately reads arbitrary host files or forges its own state.
Conservative recovery may require investigation instead of automatic retry.

## Alternatives rejected

- Executing against the original game: cache/user-data writes compromise source
  preservation and approval reproducibility.
- Sending expected values to the harness or accepting its PASS: makes the test
  oracle dependent on the component being tested.
- Replaying after an orchestrator crash based on a missing lock/PID: can duplicate
  an operation whose child is still running.
- New scheduler, RPC service or distributed worker: unnecessary for this bounded
  local milestone.

## Official references

- [Godot command-line interface](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html)
- [OS.get_cmdline_user_args](https://docs.godotengine.org/en/stable/classes/class_os.html#class-os-method-get-cmdline-user-args)
- [SceneTree timers and frame ordering](https://docs.godotengine.org/en/stable/classes/class_scenetree.html#class-scenetree-method-create-timer)
