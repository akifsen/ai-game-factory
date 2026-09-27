# Godot headless verification (V0.2)

V0.2 adds controlled local scene verification to the V0.1 orchestration core.
The real engine supplies observations; Python independently evaluates assertions.
See [the design decision](../adr/0005-godot-staging-and-independent-oracle.md),
[work plan](../work-plan-v0.2.md) and [completion evidence](../reports/v0.2-completion-report.md).

## CLI journey

```powershell
gamefactory --project "C:\games\my game" --godot-path "C:\tools\godot_console.exe" init
gamefactory --project "C:\games\my game" --godot-path "C:\tools\godot_console.exe" --json run godot-verify --scenario "C:\scenarios\combat.json"
gamefactory --project "C:\games\my game" inspect WORKFLOW_ID
gamefactory --project "C:\games\my game" artifacts --workflow WORKFLOW_ID
```

Relative scenario paths resolve from the selected project root; absolute paths also work.
Use the IDs actually returned by the commands. If policy requires approval:

```powershell
gamefactory --project "C:\games\my game" approvals --workflow WORKFLOW_ID
gamefactory --project "C:\games\my game" approve APPROVAL_ID --comment "Reviewed this local scene and its exact inputs"
gamefactory --project "C:\games\my game" resume WORKFLOW_ID
```

Set `policies.require_approval_for_process_execution: true` in
`.gamefactory/factory.yml` to require process approval. Managed-write policy also
applies. Approval happens before import/runtime; changed inputs require a new
approval. Package version is 0.2.0 while project config remains schema 0.1.0.

Exit codes retain the CLI contract: 0 success, 1 invalid input/configuration,
2 failed workflow, 3 blocked approval/recovery, 4 unavailable/misconfigured tool,
5 internal error. The engine's own exit code is separate process metadata.

## Scenario and observation contract

The packaged JSON schemas describe version 0.2.0. The Python models additionally
validate relationships between ticks, actions and assertions. Unknown fields,
versions, actions, bool-as-integer, non-finite numbers, duplicate identifiers,
unsafe scene paths and exceeded bounds are rejected before launch.

A scenario names a `res://` scene, a maximum tick, runtime/import timeouts, ordered
actions, snapshot ticks and independent assertions. Actions are limited to
`apply_damage` with a positive integer amount and `defeat_enemy`. Assertion fields
are `player_hp`, `enemies_remaining`, `score`; operators are `equals`, `minimum`,
`maximum`. Input is limited to 256 KiB, 10,000 ticks, 256 actions/snapshots/assertions
and 120 seconds per process. Snapshots include tick zero and the final tick.

The fixture in `examples/godot-verification` starts at HP 100, enemies 3 and score
0. Tick 30 applies 20 damage, tick 60 defeats an enemy worth 100 points, and tick
90 applies 40 damage. The required final observation is HP 40, enemies 2, score
100. All four states and the final values were observed in three actual acceptance runs; see the completion report.

The real scene implements `apply_damage(int)`, `defeat_enemy()` and
`verification_snapshot()`. The harness waits until ready, observes tick zero,
then uses an end-of-frame timer checkpoint before actions and snapshots. It writes
an atomic completed observation with current execution/scenario identifiers and
fingerprint. The runtime request contains no assertions or expected values.
Python rejects a report with an unrelated identity, missing ticks, wrong state
types, incomplete completion marker or additional fields such as an engine PASS.

## Isolation, evidence and recovery

The original project is not the engine working directory. Each attempt gets a
bounded staged copy and separate user-data directory. Source manifests, full
scenario, request, executable/harness identity, process logs/metadata, observed
state and validation findings are retained as attempt-correlated evidence.
Generated caches are scratch data; permanent evidence must survive retries.
Root `override.cfg` is excluded from staging, and the staged custom project-settings
override pointer is cleared. Feature-qualified variants of controlled user-data
settings are removed. `disable_project_settings_override` stays false because real
Godot 4.7.2 testing showed that true also suppresses the `--script` entrypoint.
These changes affect only the staged project; source override/config files stay intact.

This runs trusted local game code with user permissions. Staging prevents routine
writes to the original project; it is not a security sandbox for hostile scripts.
There is no network control channel or arbitrary user-supplied method execution.

Zero process exit alone does not pass verification. Import failure prevents runtime;
script errors, timeouts, truncated output and malformed/stale observations fail.
Assertion findings show their ID, tick, field, expected, actual and explanation.

A durable launch intent without a known terminal receipt means process ownership
is uncertain. Repeated resume and automatic retry must not launch it again or kill
an arbitrary reused PID. A known terminal failed attempt can use the existing
`retry WORKFLOW_ID TASK_ID`; its old logs remain. Resuming a completed workflow is
idempotent and launches no new engine process.

## Verification and packaging

Normal tests use fake process boundaries and pure contract tests. The separate
`real_godot` pytest marker runs only when `GAMEFACTORY_TEST_GODOT` names a real
executable. `scripts/verify_godot_acceptance.py` exercises fresh CLI processes.
The wheel includes GDScript and JSON schemas; acceptance includes a fresh install
and commands from outside the source checkout with spaced/Unicode paths.

CI defines a separate Linux real-engine job pinned to Godot 4.7.2, downloaded from
[official releases](https://github.com/godotengine/godot-builds/releases/tag/4.7.2-stable)
and checked against the SHA-512 pinned from
[official release metadata](https://raw.githubusercontent.com/godotengine/godot-builds/main/releases/godot-4.7.2-stable.json).
A job definition is not proof that remote CI or Linux runtime has passed; consult
the completion report for commands actually run on this host.

No visual, screenshot, audio, physics-wide determinism, AI-player, mobile export,
Meshy, Blender or paid provider verification is claimed by this milestone.

`scripts/verify_godot_recovery.py` adds test-only fault injection after a real runtime return and after a durable terminal journal. Run it with a non-editable installed Python/CLI and an outside-checkout `--workspace`. It verifies conservative blocking and explicit new-attempt retry without production debug commands.
