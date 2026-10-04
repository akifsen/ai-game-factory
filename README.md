# AI Game Factory

AI Game Factory coordinates development work for ordinary Godot projects. It
creates or inspects projects, routes bounded code, design, image, speech and vision
tasks, validates candidate changes, and records approvals, artifacts, attempts and
costs. Meshy and Blender support the existing asset-production pipelines. Generated
games remain usable without Factory installed.

The development branch extends the completed Tide Bastion asset pilot to the
[master Factory scope](docs/work-plan-master-completion.md). A configured adapter
does not imply a verified live service: capability and provider reports distinguish
missing tools or credentials from configured but unverified integrations. See the
[integration status](docs/integrations/status.md) and
[master usage guide](docs/guides/master-factory.md).

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
static-prop spec from natural language, without initializing a project:

```sh
gamefactory plan "A wooden treasure chest for Tide Bastion" --output plans/chest.spec.json
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

General production uses a versioned, operator-readable workflow manifest:

```sh
gamefactory factory manifest preflight workflow.json --json
gamefactory factory run --manifest workflow.json --json
gamefactory approvals
```

The [master guide](docs/guides/master-factory.md) covers provider configuration,
exact output paths, real gameplay and performance evidence, visual review,
accepted-asset installation, build and release. Preflight reports missing
capabilities without launching an external provider.

For a production workflow, supply a strict spec, a concept PNG and its provenance:

```sh
gamefactory asset create --spec spec.yml --concept concept.png --provenance provenance.json --provider meshy
gamefactory approvals
gamefactory asset inspect ASSET_ID
```

Meshy access, Blender and Godot are needed for real production. Read the
[production guide](docs/pipelines/generalized-asset-production.md) before spending
credits; example IDs and files must be replaced with your own inputs.

## Development capabilities

- Natural-language static-prop planning and [existing GLB reuse](docs/pipelines/existing-static-prop-reuse.md)
  with source hashes, provenance and final human acceptance.
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
