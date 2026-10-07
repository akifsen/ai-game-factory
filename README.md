# AI Game Factory

AI Game Factory coordinates development work for ordinary Godot projects. It
creates or inspects projects, routes bounded code, design, image, speech and vision
tasks, validates candidate changes, and records approvals, artifacts, attempts and
costs. Meshy and Blender support the existing asset-production pipelines. Generated
games remain usable without Factory installed.

Implementation is **frozen** for real customer use; do not expect active master-scope
expansion in this branch. Treat every capability without a recorded Tide Bastion
workflow and customer commit in
[integration status](docs/integrations/status.md) as **experimental**, including
general Factory manifests, OpenAI media adapters, Meshy paid production, gameplay
and performance harnesses, build/export, and Codex general code proposals. A
configured adapter does not imply a verified live service: capability and provider
reports distinguish missing tools or credentials from configured but unverified
integrations. See the [master usage guide](docs/guides/master-factory.md).

## Quick start

Requires Python 3.11 or 3.12. From this checkout:

```sh
python -m venv .venv
# Activate .venv with your shell, then:
python -m pip install -e '.[dev]'
gamefactory --help
gamefactory doctor
gamefactory asset profiles
```

With an authenticated Codex CLI installed, the development checkout can draft one
static-prop spec from natural language (**experimental**; planner evidence is not
yet linked to a durable workflow ID and customer commit in integration status).
`--output` must resolve inside the selected project directory; use `-p` when the
project root is not the current directory:

```sh
gamefactory -p ../my-game plan "A wooden treasure chest for Tide Bastion" --output plans/chest.spec.json
```

The [planning guide](docs/pipelines/natural-language-planning.md) explains the
bounded adapter and tool requirements. Planning creates a specification, not an
asset, and retains all production approvals.

Create a new independent project, or initialize an existing project in place:

```sh
gamefactory new "My Game" --path ../my-game --dimension 3d
gamefactory -p ../my-game discover --path .
# From an existing Godot project's root:
gamefactory init
gamefactory factory providers list --json
```

`init` records local Factory configuration, discovery and workflow state while
preserving existing game files. `gamefactory run demo` exercises a fake workflow
and stops for approval; fake outputs do not satisfy production capability gates.

General production uses a versioned, operator-readable workflow manifest
(**experimental** until customer evidence):

```sh
gamefactory factory manifest preflight workflow.json --json
gamefactory factory run --manifest workflow.json --json
gamefactory approvals
```

The [master guide](docs/guides/master-factory.md) covers provider configuration,
exact output paths, real gameplay and performance evidence, visual review,
accepted-asset installation, build and release. Preflight reports missing
capabilities without launching an external provider.

For a production workflow, supply a strict spec, a concept PNG and its provenance
(**experimental** Meshy path until customer evidence):

```sh
gamefactory asset create --spec spec.yml --concept concept.png --provenance provenance.json --provider meshy
gamefactory approvals
gamefactory asset inspect ASSET_ID
```

Meshy access, Blender and Godot are needed for real production. Read the
[production guide](docs/pipelines/generalized-asset-production.md) before spending
credits; example IDs and files must be replaced with your own inputs.

Qualified Tide Bastion evidence exists for the bounded static-prop **reuse**
route (Blender processing, Godot validation, install into the customer game) —
see [integration status](docs/integrations/status.md). That does not qualify
general Godot editor, gameplay, build or export commands. The bounded Codex
settings-slider proposal also passed real code gates and approved application
in `WF-M1-TEXT-SCALE-RETRY-20261005`, customer commit `1cbf6ba`; other code
proposal families remain experimental. First-wave battle preparation, archer
build and pause/resume also passed `test-game` and independent Godot checks in
`WF-GAMEPLAY-6fc2f444`, customer commit `ee2fb5a`; other scenarios and performance
measurement remain experimental.
Windows x86_64 export also passed `gamefactory build`, pack inspection and native
startup without Factory in `WF-PROJECT-4c53361f`, customer commit `b1fb5ec`;
other platforms and release workflows remain experimental.

## Development capabilities

Unless [integration status](docs/integrations/status.md) records a Tide Bastion
workflow and customer commit, treat the items below as **experimental**:

- Natural-language static-prop planning and [existing GLB reuse](docs/pipelines/existing-static-prop-reuse.md)
  with source hashes, provenance and final human acceptance (reuse/install path
  has narrow qualified customer evidence; planner evidence is not yet linked to a
  durable workflow ID and customer commit and paid Meshy production remain experimental).
- Provider-generated static props, pickups, modular pieces and single-mesh
  characters; characters do not include a production rig or animation.
- Operator-authored vehicle, weapon and aircraft assemblies with named parts.
- Concept review, exact paid-request snapshots, budget accounting, recovery,
  Blender validation and Godot runtime/render checks.
- Strict agent contracts, bounded hash-bound context, explicit capability routing,
  versioned prompts and non-authoritative proposals.
- General design, feature, level, UI, image, audio and vision workflow families
  backed by explicitly configured executors and independent quality gates.
- Scratch-only Godot gameplay scenarios, documented performance monitors and
  platform budgets; advisory vision findings require a separate human decision.
- Accepted asset revision installation, controlled native editor/run operations,
  export artifacts and hash-bound human release approval.
- Experimental character and animation review tools.

Dedicated `rigged_character`, building, terrain, animation, VFX and foliage asset
production profiles remain unsupported. General code workflows and experimental
review tools do not change those profile qualifications.
See [profile details](docs/architecture/advanced-asset-profiles.md) and
[assembly production](docs/pipelines/assembly-production.md).

## Approval and cost

A generated plan cannot authorize payment or a game write. Paid generation requires
human approval tied to the exact request and budget; final acceptance is a separate
checkpoint. A provider reporting success is insufficient: artifact hashes and
independent validation must agree. See [paid requests](docs/architecture/paid-request-snapshot.md)
and [accounting](docs/architecture/accounting.md).

Development work and published releases have separate qualification. Version
history belongs in [release notes](docs/releases/) and
[GitHub releases](https://github.com/akifsen/ai-game-factory/releases).
Architecture, detailed guides and historical reports are in the [documentation index](docs/README.md).
