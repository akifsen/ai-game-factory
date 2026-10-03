# AI Game Factory

AI Game Factory is local development tooling for coordinating bounded game-development workflows. It stores workflow state, task attempts, artifacts, evidence, approvals, and policy decisions locally. A managed game remains usable without the Factory installed.

The development package version is 0.8.0rc4. The published [authored clips and multi-clip review technical prerelease](https://github.com/akifsen/ai-game-factory/releases/tag/v0.8.0-rc.3) remains immutable at main commit `6e49d17d` with all 24 required CI jobs green on that exact source plus installed-wheel acceptance proof. This checkout is not a public production release: production and promotion eligibility remain false. V0.8-6 added authored local animation clip preview ([animation clip preview guide](docs/guides/v0.8-6-authored-animation-clip-preview.md)); V0.8-7 adds interactive animation review with a readiness-gated 13-file exporter and live currentness API ([animation review guide](docs/guides/v0.8-7-animation-review.md), [V0.8-7 checkpoint](docs/reports/v0.8-7-animation-review-checkpoint.md)). V0.7 added advanced asset profiles ([V0.7 completion report](docs/reports/v0.7-completion-report.md)). V0.6 added production safety and recovery ([V0.6 completion report](docs/reports/v0.6-completion-report.md)). V0.4 added a gated production pipeline: concept ingestion and human review, separately approved Meshy CLI generation, deterministic Blender processing, decoded GLB validation, and staged Godot runtime/render evidence. V0.5 runs that same pipeline from an asset profile (`static_prop@1`, `pickup@1`, `modular_piece@1`) instead of crate-specific rules. Real paid generation and final visual approval remain explicit human checkpoints. See the [V0.5 completion report](docs/reports/v0.5-completion-report.md). V0.2 headless verification and V0.3 rendered capture remain available.

V0.8-9 development adds durable per-clip review sessions with status, notes, bookmarks, raw-SHA conflict detection, and a disposable Godot viewer. Saved history remains readable when live authority is unavailable; edits require a current review set. See the [review session guide](docs/guides/v0.8-9-animation-review-sessions.md). Final production-viewer UI acceptance passed against both the source package and an independently installed RC4 wheel. Release qualification remains pending.

V0.8-8 adds multi-clip animation review: two to eight current authored clip
packages for one character share an ordered selector and the existing playback
controls. The exporter preserves source bytes and detects collection drift.
See the [multi-clip guide](docs/guides/v0.8-8-multi-clip-animation-review.md) and
[verified checkpoint](docs/reports/v0.8-8-multi-clip-animation-review-checkpoint.md).
Release proof for this development slice is still pending.

## Install and run

The development checkout also includes a readiness-gated Godot character preview
API. It exports verified candidate GLB bytes, a capsule scene and a deterministic
manifest, with live currentness checks. See the [preview guide](docs/guides/v0.8-4-candidate-preview.md)
and [development checkpoint](docs/reports/v0.8-4-preview-checkpoint.md). V0.8-5 adds a
readiness-gated animation preview export that consumes a current V0.8-4 preview package and
publishes a separate `rig_smoke_01` animation scene; see the
[animation preview guide](docs/guides/v0.8-5-candidate-animation-preview.md). V0.8-6 adds an
authored local animation clip preview that consumes a current V0.8-4 preview and caller clip
JSON; see the [animation clip preview guide](docs/guides/v0.8-6-authored-animation-clip-preview.md).

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
gamefactory --version
gamefactory --help
```

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
gamefactory --version
gamefactory --help
```

## V0.1 journey

From this repository or another directory, check local readiness before initialization:

```bash
gamefactory doctor
```

For an existing game project, run `gamefactory init` from its root or a subdirectory. Initialization creates `.gamefactory/factory.yml`, `discovery.json`, SQLite state, and lock storage without editing game source files. It is idempotent. Discovery records scene/script/test counts, main scene, engine version when available, export configuration, and Git-root presence.

```bash
cd examples/minimal-godot
gamefactory init
gamefactory status
gamefactory run demo
gamefactory approvals
gamefactory inspect WF-DEMO-12345678
gamefactory approve APP-12345678 --comment "Reviewed the fake generation step"
gamefactory resume WF-DEMO-12345678
gamefactory artifacts
```

Replace the example IDs with the workflow and approval IDs returned by the preceding commands.

The legacy V0.1 `run demo` and `run paid-safety` examples use an explicitly named local fake provider and pause before its paid-classified operation. V0.4 asset production has a separate Meshy CLI adapter guarded by concept, paid-operation, and final-review approvals; see the V0.4 section below. `run failure` demonstrates a deterministic failure; use `retry <workflow> <task>` to create a new attempt. Workflow IDs are returned by `run` and `status`.

The CLI accepts `--project DIR` / `-p DIR` and explicit `--godot-path` / `--blender-path` overrides. Invalid explicit paths are reported as misconfigured and never fall back to another executable. Add `--json` before or after a command for compact JSON output. Exit codes are: `0` success, `1` configuration or usage error, `2` workflow failure, `3` approval blocked, `4` tool unavailable/misconfigured, and `5` internal error.

## Configuration and safety

The versioned project contract is `.gamefactory/factory.yml`; the JSON Schema is in `schemas/`. Unknown fields are rejected. User-wide defaults can be set in `%APPDATA%/gamefactory/config.yml` on Windows or `$XDG_CONFIG_HOME/gamefactory/config.yml` (falling back to `~/.config/gamefactory/config.yml`) on Linux/macOS. Precedence is defaults, user config, project config, then explicit command-line executable paths. Do not put credentials in either YAML file; provider secrets belong in environment variables, and no V0.1 command sends them anywhere. Local state is in `.gamefactory/state/factory.db`. State and config paths are checked against project-root escapes before use.

Godot is required for `godot-verify`, `godot-capture`, and V0.4 asset runtime verification. Other core workflows remain usable without a renderer. `doctor` does not open a window. Blender is optional for legacy workflows; V0.4 asset production requires its locally detected executable. Meshy readiness reports local CLI configuration separately from paid authorization and generation results.

See [architecture overview](docs/architecture/overview.md), [integration status](docs/integrations/status.md), [development workflow](docs/development/README.md), [acceptance requirements](docs/requirements/03-acceptance.md), and [deferred boundaries](docs/requirements/future-boundaries.md).

The [documentation index](docs/README.md) links the detailed implementation plan,
all 123 requirement sections, and the [V0.1 verification report](docs/reports/v0.1-completion-report.md).

## V0.2 real Godot journey

Use a trusted local Godot project. The included fixture implements actual damage,
enemy and score behavior; its scenario expects HP 40, two enemies and score 100
at tick 90.

```bash
gamefactory --project examples/godot-verification init
gamefactory --project examples/godot-verification --json run godot-verify --scenario scenario.json
gamefactory --project examples/godot-verification inspect WORKFLOW_ID
gamefactory --project examples/godot-verification artifacts --workflow WORKFLOW_ID
```

Supply `--godot-path PATH` if detection cannot locate your executable. Configure
`policies.require_approval_for_process_execution: true` to require approval before
launch; use `approvals`, `approve` and `resume` as in the existing workflow. The
approved source/scenario/executable/harness fingerprint is checked before dispatch.
Project config schema stays 0.1.0. Headless scenarios stay on schema 0.2.0 (these schemas were introduced in V0.2 and are unchanged by later releases).

Godot runs a separate staged copy and writes observations without receiving the
assertion expectations. Python validates current-attempt evidence and checks the
assertions. Zero exit code alone cannot pass a gate. Unknown child-process state
after interruption stays blocked; terminal failures can create a fresh attempt
with `retry WORKFLOW_ID TASK_ID`. Historical logs remain available.

Staging is for source preservation and reproducibility, not an OS security sandbox.
The original game remains runnable without Factory or its harness. Verification
covers the bounded integer-state fixture contract, not visual quality, arbitrary
physics determinism, AI gameplay or mobile exports.

See the [V0.2 plan](docs/work-plan-v0.2.md),
[Godot contract and recovery guide](docs/integrations/godot-headless-verification.md)
and [V0.2 completion report](docs/reports/v0.2-completion-report.md).

## V0.3 rendered capture

`godot-capture` stages the project, imports it headless, then runs a windowed Compatibility renderer. It does not use `--headless` for the image. Supported viewports are 1280×720 and 720×1280, as separate workflows. The portrait profile is not a mobile-device test. Capture schema is 0.3.0; existing headless scenarios are not required to take screenshots.

```bash
gamefactory --project examples/godot-verification init
gamefactory --project examples/godot-verification run godot-capture --scenario visual-scenario.json
gamefactory --project examples/godot-verification report --workflow WORKFLOW_ID
gamefactory --project examples/godot-verification approvals --workflow WORKFLOW_ID
gamefactory --project examples/godot-verification approve APPROVAL_ID --comment "Reviewed"
gamefactory --project examples/godot-verification resume WORKFLOW_ID
```

A technical PASS leaves the workflow blocked for visual review. That approval is not the process-execution approval. Rejecting it does not start another capture. `inspect`, `artifacts`, `retry` and `godot-verify` keep their existing roles.

The published Windows review is `docs/reports/v0.3-rendered/landscape/index.html`. Its human decision is still pending. See [rendered capture](docs/integrations/godot-rendered-capture.md) and the [V0.3 completion report](docs/reports/v0.3-completion-report.md).

## V0.4 asset production

V0.4 adds a gated static-prop production path from a reviewed concept through Meshy image-to-3D, retained raw GLB, deterministic Blender processing, structured validation, staged Godot import/runtime, and Godot-rendered evidence. Concept approval, paid generation approval, and final visual review are separate human decisions. Rejection never triggers automatic regeneration. The real sample concept remains pending human review until an operator explicitly decides.

Start with the [asset production guide](docs/pipelines/asset-production.md), the [Meshy safety guide](docs/integrations/meshy.md), and the [V0.4 work plan](docs/work-plan-v0.4.md). Offline evidence packages can be cold-checked without installed package imports using `python -I scripts/verify_asset_bundle.py <bundle-directory>`. CI and tests must use fake generation only; no Meshy secrets or real paid requests belong in CI.

## V0.5 asset profiles

`static_prop@1`, `pickup@1`, and `modular_piece@1` are built in. Other named classes are reported as unsupported. A revision keeps the profile version it was created with. Historical V0.4 receipts stay readable and are not rewritten.

```bash
gamefactory asset profiles
gamefactory doctor
gamefactory asset create --spec spec.yml --concept concept.png --provenance provenance.json --provider fake
gamefactory asset inspect ASSET_ID
```

`asset create` is the profile-aware form of `asset-create`. Status lines include the asset, revision, profile version, workflow, and current gate. See [asset profiles](docs/architecture/asset-profiles.md), [generalized production](docs/pipelines/generalized-asset-production.md), and the [V0.5 work plan](docs/work-plan-v0.5.md).

The golden asset `prop_energy_crate_01` / `r001` is the `static_prop@1` regression reference. Its processed GLB and the historical one-call, 15-credit Meshy record are not regenerated by this release. The V0.5 production candidate `pickup_energy_cell_01` / `r002` completed in V0.5.1 (workflow `WF-ASSET-ddfcdc8e`: one paid Meshy submission at 15 actual credits, Godot validation and review evidence passed, final human review approved); see the [V0.5.1 release notes](docs/releases/v0.5.1.md).

## V0.6 production safety and recovery

New asset workflows add two automatic steps before the paid approval:

`concept review → paid request snapshot → production readiness → paid approval → generation → …`

- **Paid request snapshot.** The exact provider request (model type, polycount, texturing, format, omitted options, cost) is resolved, hashed, stored and bound into the paid approval. Dispatch executes that snapshot or refuses. It never uses newer adapter defaults. See [paid request snapshot](docs/architecture/paid-request-snapshot.md).
- **Production readiness.** Provider, Blender (including Python dependencies), Godot, workspace and profile are checked before any spend and cheaply rechecked just before the paid submission. See [production readiness](docs/architecture/production-readiness.md).
- **Cost ledger.** Spend is an append-only ledger of reservations, settlements and releases. Recovery settles the original reservation. See [accounting](docs/architecture/accounting.md).
- **Concept iteration.** `CHANGES_REQUESTED` blocks for a revised concept instead of failing the revision. A new concept version can be appended until paid production starts. See [concept versioning](docs/architecture/concept-versioning.md).
- **Recovery.** Failures are classified from stored evidence, and pre-launch tool failures can be reclassified with an audit trail. See [recovery](docs/architecture/recovery.md).

```bash
gamefactory request-changes APPROVAL_ID --actor NAME --comment TEXT
gamefactory asset concept replace --workflow WF --concept new.png --provenance new.json --actor NAME --reason TEXT
gamefactory accounting reconcile --workflow WF                       # dry run (still migrates the DB)
gamefactory accounting reconcile --workflow WF --apply --actor NAME --reason TEXT --plan-hash HASH
gamefactory accounting ledger --workflow WF
gamefactory recovery inspect WF
gamefactory recovery reclassify --execution EXEC                     # dry run
gamefactory recovery reclassify --execution EXEC --apply --actor NAME --reason TEXT
```

Mutating recovery and accounting commands are dry runs unless `--apply` is given with `--actor` and `--reason`. There is no force mode. A dry run writes no ledger entries, but like every command that opens the project database it first applies pending schema migrations; inspect a live V0.5.1 database read-only and dry-run on a Backup API copy as described in [accounting](docs/architecture/accounting.md#operator-procedure-for-a-live-database). Workflows created before V0.6 keep their task graph and evidence schema.

## V0.7 advanced asset profiles

V0.7 adds assets with named, movable parts and a character profile with a capsule collider. `gamefactory asset profiles` now lists:

| Profile | Geometry | Source | Collider |
|---|---|---|---|
| `static_prop@1`, `pickup@1`, `modular_piece@1` | single mesh | provider | box (unchanged) |
| `character@1` | single mesh | provider | capsule |
| `vehicle@1`, `weapon@1`, `aircraft@1` | assembly | operator-authored | box |

`rigged_character`, `building`, `terrain`, `animation`, `vfx` and `foliage` stay UNSUPPORTED.

- **Assemblies are authored, not generated.** An operator supplies a GLB (Blender or script) with a `ROOT` → `PART_<id>` tree, declared pivots and sockets. `asset register-source` records its hash, `paid: false` and the required `source_front`; `asset assemble` runs prepare → normalize → validate → Godot → final review → evidence with no concept, provider or paid step. See [assembly production](docs/pipelines/assembly-production.md).
- **Normalization, not guessing.** A `+Z` source (what Blender's glTF exporter produces) gets exactly one 180° turn about +Y at the root; a `-Z` source is kept byte-for-byte; nothing else is corrected.
- **Validator composition.** Validation is composed from closed rule groups selected by capabilities, never by profile id (ADR 0018). The three historical profiles keep their exact V0.6 findings, pinned by golden tests.
- **Godot articulation.** The runtime harness checks the imported part tree, pivots and sockets, moves every movable part about its declared pivot and asserts that nothing else moves.
- **Every review view is placed** (`front`, `rear`, `left`, `right`, `side`, `three_quarter`, `three_quarter_front`, `three_quarter_rear`, `top`); an unknown view is an error, not a three-quarter fallback.
- **Evidence 0.7.** `asset-evidence-0.7.0` bundles for assemblies carry the retained source, the registration and the normalization record; the stdlib verifier rejects paid or provider roles and recomputes the normalization from the source.
- Assembly specifications never reach a provider request (rejected before any snapshot, approval or intent). Real Meshy calls during V0.7 development: 0.

```bash
gamefactory asset register-source --spec tank.spec.json --source tank.glb --source-front=+Z \
  --authoring-tool blender --authoring-tool-version 4.0.2 --actor NAME --reason TEXT \
  --output tank.registration.json
gamefactory asset assemble --spec tank.spec.json --source tank.glb --registration tank.registration.json
```

Design: [advanced asset profiles](docs/architecture/advanced-asset-profiles.md), ADR 0013–0018. Plan and evidence: [V0.7 work plan](docs/work-plan-v0.7.md), [V0.7 completion report](docs/reports/v0.7-completion-report.md), [release notes](docs/releases/v0.7.0.md).

