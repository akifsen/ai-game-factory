# AI Game Factory

AI Game Factory helps developers plan and validate assets for Godot games. A real
Codex planner turns natural-language requests into specifications; existing GLBs
can be reused or new assets generated with Meshy. It coordinates Blender processing,
Godot checks and human approval, while recording exactly what was requested and
what it cost. The game remains usable without Factory installed.

The first customer pilot is complete: a natural-language request produced a valid
specification, an existing chest passed Factory validation and human acceptance,
and **Tide Bastion** now uses the accepted asset in its playable intro battle.
New paid submissions were zero. General automatic game development remains outside
the current scope. See the [active product direction](docs/product-direction.md).

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

From a Godot project's root, `gamefactory init` creates local Factory configuration
and workflow state. `gamefactory run demo` exercises a fake workflow and stops for
approval; it does not generate a production asset.

For a production workflow, supply a strict spec, a concept PNG and its provenance:

```sh
gamefactory asset create --spec spec.yml --concept concept.png --provenance provenance.json --provider meshy
gamefactory approvals
gamefactory asset inspect ASSET_ID
```

Meshy access, Blender and Godot are needed for real production. Read the
[production guide](docs/pipelines/generalized-asset-production.md) before spending
credits; example IDs and files must be replaced with your own inputs.

## Current capabilities

- Natural-language static-prop planning and [existing GLB reuse](docs/pipelines/existing-static-prop-reuse.md)
  with source hashes, provenance and final human acceptance.
- Provider-generated static props, pickups, modular pieces and single-mesh
  characters; characters do not include a production rig or animation.
- Operator-authored vehicle, weapon and aircraft assemblies with named parts.
- Concept review, exact paid-request snapshots, budget accounting, recovery,
  Blender validation and Godot runtime/render checks.
- Local character and animation review tooling under development.

`rigged_character`, building, terrain, animation, VFX and foliage production remain
unsupported. Review tooling does not make those production profiles available.
See [profile details](docs/architecture/advanced-asset-profiles.md) and
[assembly production](docs/pipelines/assembly-production.md).

## Approval and cost

A generated plan cannot authorize payment or a game write. Paid generation requires
human approval tied to the exact request and budget; final acceptance is a separate
checkpoint. A provider reporting success is insufficient: artifact hashes and
independent validation must agree. See [paid requests](docs/architecture/paid-request-snapshot.md)
and [accounting](docs/architecture/accounting.md).

The published stable release is [v0.7.0](https://github.com/akifsen/ai-game-factory/releases/tag/v0.7.0).
Development and technical prereleases have separate qualification; version history
belongs in [release notes](docs/releases/) and [GitHub releases](https://github.com/akifsen/ai-game-factory/releases).
Architecture, detailed guides and historical reports are in the [documentation index](docs/README.md).
