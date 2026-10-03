# Active product direction — Tide Bastion pilot

Decision: 2026-10-04. The user selected Tide Bastion as the first customer game.
The existing ChatGPT planning discussion supports this pivot. Engineering approval
and independently verified implementation remain the responsibility of the lead.

## Next milestone

One natural-language request produces a valid `static_prop@1` specification without
manual JSON edits; a Factory-accepted asset is then used in Tide Bastion's actual
playable battle scene. Planning alone, a generated GLB alone and passing unit tests
alone do not complete this milestone.

Candidate: a treasure chest. Tide Bastion already has a manually generated source
at `assets/treasure_chest.glb`, with documented provenance. Its existing generation
is not Factory production, and it is outside the game's `game/assets` tree. Reuse
it where compatible instead of consuming new credits for exploration. The real
presentation integration point is `game/ui/island_stage.gd`, which creates the
battle's props. Placement must preserve lane, tower-slot and combat behavior.

## Bounded implementation

1. Add one real Codex CLI planner that emits a strict static-prop spec. It cannot
   approve, spend, install assets or execute instructions from its output.
2. Feed that spec into the existing asset workflow. Establish an honest source
   reuse route if needed; do not relabel a manually copied source as generated or
   validated by Factory.
3. Validate with Blender and Godot, obtain final acceptance, install the accepted
   artifact into Tide Bastion and verify the installed SHA matches.
4. Verify the actual battle scene consumes the asset, including smoke boot and
   visual inspection. A headless import alone is not rendering evidence.

Track first-pass spec validity, manual spec edits, new paid submissions and cost,
accepted/installed hash equality, actual game consumption, elapsed time and human
interventions. Current accepted customer assets: **0**. Update only with evidence.

## First implementation checkpoint

The real installed Codex CLI produced `prop_tide_treasure_chest_01` from a Turkish
request on its first attempt, without manual JSON edits. The existing domain parser
accepted the resulting `static_prop@1` specification. Independent planner, CLI and
process-runner checks passed 62 tests with one existing platform-specific skip;
scoped lint and type checks passed.

The retained manual chest source passed Factory preflight (1,930 triangles, one
material, 2,048-pixel textures). Real Blender processing and independent GLB
validation passed; the existing standalone Godot harness passed runtime checks and
rendered three views. New paid submissions: **0**. Raw results remain under ignored
local verification storage. This is a candidate check, not a persisted workflow's
final human acceptance or a Tide Bastion installation. Real battle consumption and
accepted/installed hash comparison remain outstanding.

## Work frozen

New animation review UI slices, V0.8-15 performance optimization, new evidence
schemas, general agent frameworks and another RC series are not active priorities.
Existing code, measurements and historical records are retained. Existing merge
and release gates remain in force; they are not prerequisites for local pilot work.
Do not manually dispatch repeated CI runs to substitute for product progress.

## Evidence policy

New generated logs, JUnit, screenshots, bundles and measurement data belong in
ignored local verification directories, CI artifacts or release attachments.
Git stores short authored results, architecture and essential regression fixtures.
New raw evidence under `docs/reports` is ignored. Existing tracked reports are
historical and frozen, preserving release references and verifier dependencies;
no history rewrite or bulk physical deletion is part of the pilot. Any later
archive migration must preserve referenced evidence before untracking it.

## Release policy

Commit completed, reviewed increments. Merge and publish when the existing relevant
checks and release qualification are satisfied. Do not invent a new version merely
for a planning document or claim an unqualified development package is released.
