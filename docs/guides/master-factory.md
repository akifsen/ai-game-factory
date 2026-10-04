# Master Factory workflows

The master Factory layer coordinates development for independent Godot projects.
Its manifest describes the work, provider identity, bounded inputs and outputs,
cost ceiling, tools, and acceptance gates before execution. An agent produces a
proposal; the workflow records and validates it before a human accepts game writes.

The code-phase completion and verification status are tracked in
[the master work plan](../work-plan-master-completion.md). This guide describes
the implementation interface; it does not certify a live provider or a release.

## Project setup

Create a project in a new directory whose parent already exists:

```sh
gamefactory new "My Game" --path ../my-game --dimension 3d
```

For an existing Godot project, run these commands from its root:

```sh
gamefactory discover --path . --include-git --json
gamefactory init
gamefactory factory providers list --json
```

Discovery reads project structure without starting Godot. Initialization keeps
game files and creates local Factory state. Use `-p PROJECT_DIRECTORY` to select
a project from another directory. Game scenes and scripts remain ordinary Godot
resources and do not require the Factory Python package at runtime.

## Explicit providers

Provider configuration lives in `.gamefactory/providers.json`. Configuration
stores credential environment-variable names, never credential values. The
provider listing reports local readiness without making a remote call.

Configure Codex with an explicit model and operator-selected cost ceiling:

```sh
gamefactory factory providers add-codex --model YOUR_MODEL --cost-class METERED --max-cost YOUR_CEILING --currency USD --cost-unit request
gamefactory factory providers list --json
```

`YOUR_MODEL` and `YOUR_CEILING` are placeholders. Select a cost classification and
unit that match the configured account. A subscription or an unknown charge must
not be represented as a verified zero-cost request. The Codex adapter runs a
bounded read-only proposal operation; promotion is handled separately.

The built-in OpenAI adapters have explicit model, environment-variable and cost
configuration. Load an operator-authored strict config with:

```sh
gamefactory factory providers add-openai-image --config image-provider.json
gamefactory factory providers add-openai-speech --config speech-provider.json --voice YOUR_VOICE
gamefactory factory providers add-openai-vision --config vision-provider.json
```

Image generation produces validated PNG files. Speech produces validated WAV
files; this adapter does not synthesize arbitrary music or sound effects. Vision
reviews declared image inputs and returns advisory findings. It cannot declare a
deterministic quality gate passed or authorize game changes.

Other providers can use an operator-owned fixed executable and argument template:

```sh
gamefactory factory providers add-process --config process-provider.json --agent-definition agent-definition.json
```

The process contract specifies supported kinds, capabilities, tools, environment
names, limits and cost semantics. Its explicit network permission is an operator
trust decision; a minimal environment is not an operating-system network sandbox.
Unsupported capabilities fail preflight instead of silently choosing a fallback.

## Declare and run work

The strict manifest version is `factory-workflow-1.0.0`. Supported task families
are `design`, `feature`, `level`, `ui`, `image`, `audio`, and `vision`. Each task
selects a registered agent and executor and declares its dependencies. IDs must
be unique across persisted tasks; the manifest authoring command generates a
unique task ID unless the operator supplies one.

`factory manifest create` writes a one-task manifest. Required options include
`--output`, `--workflow-name`, `--executor`, `--agent`, `--objective`,
`--cost-class`, and `--max-cost`. Repeat `--input PATH=PURPOSE` for approved context,
`--output-scope` for exact file paths, and `--artifact` for required output files.
Declare prompt identity, capabilities, allowed tools and process/network limits
to match the chosen provider. Add `--game-write` and a deterministic `--gate` for
changes intended for the game. `--parameters` loads project-relative JSON containing
the selected gate's scenario or other deterministic inputs.

For several tasks, edit the resulting JSON into a dependency graph. Inputs can
select an existing project file or an exact predecessor output. For replacements,
declare the existing file as approved context so the proposal can bind its original
hash. Exact output scopes and expected artifacts must agree for game writes.
Reserved state directories, secrets, traversal paths and ambiguous output paths
are rejected.

The [code-change example](../../examples/factory/code-change-manifest.json)
shows a complete declaration. Copy it into the initialized game project and
replace its project ID, workflow/task IDs, player-controller paths, objective and
cost ceiling with your own values. The example assumes an existing
`scripts/player.gd`; a newly scaffolded project uses `main.gd`. Placeholder IDs
and missing inputs should fail preflight rather than create an accidental run.

```sh
gamefactory factory manifest preflight workflow.json --json
gamefactory factory run --manifest workflow.json --json
gamefactory approvals
gamefactory inspect WORKFLOW_ID
```

Preflight checks declarations and configured provider/gate availability. Running
the workflow stops at required approvals. Inspect the approval's exact request,
inputs, output scopes, provider configuration and cost ceiling before deciding:

```sh
gamefactory approve APPROVAL_ID --comment "Reviewed exact request and scope"
gamefactory resume WORKFLOW_ID
gamefactory report --workflow WORKFLOW_ID --json
```

Approval IDs and workflow IDs come from command output. Reject an unsuitable
request with `reject`, or request a revision with `request-changes`. Changed inputs,
configuration, proposals or candidate hashes require fresh matching approval.

Provider outputs are staged separately from the game. Final deterministic gates
evaluate the combined candidate from every writing task, so individually valid
fragments cannot bypass an integration failure. Final game acceptance is distinct
from permission to call a provider. Promotion captures and checks original files
and preserves unexpected concurrent edits for recovery.

If promotion is interrupted, inspect its retained journal before recovering:

```sh
gamefactory factory recover-apply --journal .gamefactory/operations/factory-apply/OWNED_JOURNAL.json --json
```

Use the exact journal path and SHA reported by inspection. Explicit recovery
requires `--apply`, `--expected-journal-sha256`, `--actor`, and `--comment`. It
verifies the original workflow, execution, approval and file bindings under a
recovery lock. Conflicting edits remain preserved. A committed application is
verified and re-finalized through the normal engine path; recovery does not
resubmit providers or bypass task completion checks. Follow the returned
workflow/task instructions. A verified committed application leaves the exact
original attempt eligible for one explicit `gamefactory retry WORKFLOW_ID TASK_ID`;
the normal engine verifies its evidence and completes the task. An execution that
still appears active is blocked rather than taken over. Resume its workflow first
to reconcile an interrupted execution before inspecting the journal again.

## Gameplay, performance and vision evidence

The gameplay harness accepts a versioned allowlisted request with fixed seed and
tick rate, scene/entity/input allowlists, explicit property bindings and scenario
assertions. Actions include loading a scene, spawning an entity, simulating input,
waiting, capturing a screenshot, dumping state and collecting metrics. Arbitrary
commands and arbitrary script evaluation are outside this contract.

```sh
gamefactory test-game --scenario gameplay-request.json --godot-path PATH_TO_GODOT
gamefactory performance-review --scenario performance-request.json --godot-path PATH_TO_GODOT
```

These commands execute Godot and require the workflow's process approval. The
harness runs in a staged copy, records the actual execution identity and engine
provenance, and retains physical evidence with hashes. Gameplay assertions are
evaluated independently. Performance uses actual supported engine monitors and an
explicit sample window/platform budget. Missing metrics, unsupported monitors,
nonfinite samples and failing budgets cannot pass by substituting invented data.
Rendered visual evidence requires an actual render; headless output alone does
not prove appearance.

Allow a warm-up before collecting FPS: Godot's FPS monitor updates once per
second, and some monitors are unavailable in release builds. Choose the collection
window and metrics accordingly; see the [official Performance monitor reference](https://docs.godotengine.org/en/stable/classes/class_performance.html).

A vision task can consume named physical evidence from a predecessor gate. Set
the input's `source_task_id`, `source_gate`, `source_gate_scope`, `evidence_name`,
`path`, and `purpose`. `source_gate_scope: "combined_candidate"` captures the
combined writing candidate before vision review; it is restricted to vision tasks
with an explicit writing dependency. The evidence name must match a retained gate
file, and `path` names the approved image context. This avoids asking a visual
agent to infer appearance from source code alone.

For the capture runner's `candidate.png` evidence, a vision input can be:

```json
{
  "path": "review/candidate.png",
  "source_task_id": "task.feature",
  "source_gate": "visual",
  "source_gate_scope": "combined_candidate",
  "evidence_name": "candidate.png",
  "purpose": "screenshot of the combined candidate"
}
```

Here `path` is a context alias for retained evidence, not an instruction to write
a screenshot into the game. Declare `task.feature` as a dependency and provide
the capture runner's bounded scenario parameters, plus direct reference images
and an art-bible input. The final visual gate validates the immutable captured
bytes against the current candidate hash; it does not make another provider call
or replace the reviewed image with a new render.

The review records the reviewed image hashes and candidate provenance. Findings
remain advisory and require a separate human decision. A later change invalidates
the earlier review's applicability.

## Accepted assets, exports and release

Install an exact accepted asset revision from its production workflow:

```sh
gamefactory asset install --source-workflow SOURCE_WORKFLOW_ID --revision REVISION_ID
```

Installation verifies the accepted revision, artifact hashes and production
evidence, creates an ordinary Godot scene wrapper, and requires game-write
approval. Existing targets require explicit replacement baselines; unexpected
changes stop promotion. Dedicated asset profile support is listed in
[the profile architecture](../architecture/advanced-asset-profiles.md).

Native operations also use bounded staged projects and process approvals:

```sh
gamefactory editor --executable PATH_TO_GODOT --timeout 300
gamefactory run-scene --executable PATH_TO_GODOT --scene res://Main.tscn --timeout 120
gamefactory build --executable PATH_TO_GODOT --preset YOUR_EXPORT_PRESET --output-name YOUR_OUTPUT_FILE
gamefactory release --build-attempt COMPLETED_EXPORT_EXECUTION_ID
```

Configure ordinary Godot export presets and required export templates beforehand.
An export retains its receipt and all output sidecars. Release selects an exact
completed export execution, checks its physical artifacts and matching evidence,
and asks for hash-bound human release approval. It does not publish to a store.

## Uncertain operations and accounting

Provider intent is persisted before an external effect. A timeout or lost response
does not prove the provider did nothing, and resuming must not blindly submit the
same charged request again. Inspect durable intents first:

```sh
gamefactory operation inspect --workflow WORKFLOW_ID --json
```

`operation reconcile` requires the task, request fingerprint, expected current
status, actor, comment and supporting evidence references. It previews by default;
`--apply` records the explicit audited decision. Supply an external operation ID
only when the provider actually returned one. A response/request receipt is not
automatically a remotely queryable operation ID.

Unknown actual charges keep their reservation until supported reconciliation.
Observed charges above the approved ceiling are accounting facts: record them,
block further acceptance/spending, and investigate. Different currency or cost
units cannot be silently converted. Consult the retained intent, ledger and audit
records rather than inferring cost from a provider success status.

For implementation and integration qualification, see
[architecture](../architecture/overview.md),
[integration status](../integrations/status.md), and
[the master acceptance plan](../work-plan-master-completion.md).
