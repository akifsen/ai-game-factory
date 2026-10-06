# Product direction — real customer use

## Current instruction (2026-10-04)

Implementation is frozen; real customer use begins. This instruction supersedes
the master-scope expansion and the instruction to finish all implementation before
testing. Add no capability, command, adapter, schema, ADR, RC or version number.
Fix only faults exposed by real use, with relevant tests, lint and type checks
after each change. A capability works only after a real Tide Bastion job; unit
tests, fake providers and source inspection are insufficient evidence.

Complete these milestones in order, without starting the next before the previous
one succeeds:

1. **M0:** main's natural CI must be green. Fix failures first; do not repeatedly
   dispatch CI manually.
2. **M1:** use `gamefactory factory run --manifest ...` to obtain a bounded Codex
   code proposal for Tide Bastion, pass the code gate, obtain human approval and
   apply it. Completion requires a Tide Bastion commit.
3. **M2:** run at least one `gamefactory test-game` scenario in the real battle
   scene and independently verify the expected states. Obtain gameplay acceptance.
4. **M3:** use `gamefactory build` to export one platform; inspect the output for
   Factory harness files and secrets and verify it launches without Factory.
5. **M4:** produce a second asset from natural-language request through `plan`,
   concept, paid Meshy approval, Blender, Godot validation, installation and real
   battle-scene use. At most one paid submission, with prior human approval.

For every milestone, record workflow ID, customer commit, human interventions,
manual corrections, credits/fees and elapsed time in a short Markdown summary
under `docs/reports`. Keep raw logs and screenshots out of Git. Use explicit
unknown or not-run values instead of invented evidence or costs.

The integration status table must include **Tide Bastion'da gerçek kullanım**:
workflow ID plus customer commit, or **YOK**. Every capability without customer
evidence is **experimental** in README and CLI help. OpenAI image, speech and
vision adapters remain experimental until an approved real invocation. As of
2026-10-18, prepare a removal proposal for capabilities still marked YOK; remove
nothing without approval.

During use, address the outside-project path errors for `plan --output` and
`factory manifest preflight` by actionable diagnostics or a documented example
copy step; do not weaken path containment to bypass them.

Only after M1–M4, in a separate PR, split `factory_workflow.py` into modules
without behavior changes, using the same tests before and after. Also write an
archive migration plan for historical reports, identifying verifier dependencies
and release-attachment preservation. Do not execute the migration without approval.

Stop for prior approval before every paid call, for visual/gameplay acceptance,
before deleting any file or history, and when a milestone fails twice. Do not
write new capabilities to work around a failed milestone. ChatGPT may answer
planning questions; it cannot substitute for the user's human acceptance or
paid-call approval.

## Historical master capability completion instruction (2026-10-04; superseded)

The user expanded the active scope to all Factory capabilities in the master
requirements, including capabilities previously deferred beyond bootstrap.
The completed Tide Bastion pilot below remains historical customer evidence.
Its scope freeze no longer limits the newly authorized implementation.

Implementation uses local subagents; Antigravity and Cursor are temporarily
excluded by the user's instruction. All implementation must finish before running
tests, builds, lint, type checks, runtime verification or GitHub Actions. Test
sources can be written during implementation, but must remain unexecuted until
that boundary is satisfied. Read-only repository discovery and source review
remain permitted.

Existing approval, cost, evidence and standalone-game boundaries remain in force.
Writing an integration does not authorize paid calls, publication or human
acceptance. Implementation and independently verified readiness must be tracked
separately; historical passing results do not qualify newly written code.

## Historical Tide Bastion pilot

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
interventions. Current accepted customer assets: **1**. Update only with evidence.

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
local verification storage. Those standalone checks did not record acceptance
or installation; the durable workflow and customer adoption are described below.

The reviewed `asset reuse` route for `static_prop@1` retains an
existing external GLB with exact provenance and hash, then runs the existing
Blender, independent validation, Godot and final human review stages. It has no
concept, paid-generation, budget-reservation or provider-invocation stage. Historic
Meshy cost remains provenance, not a new ledger charge. Source acquisition is a
workflow-level distinction; it does not change the existing profile contract or
introduce a new evidence schema. Independent checks passed 65 tests with two
existing assembly skips; full source lint, formatting and type checks passed.
The real durable reuse workflow completed processing, validation and Godot checks.
After the user accepted the presented candidate and instructed continuation,
`APP-5b2a9f71` was approved and `WF-REUSE-230ee7ad` completed. Its ledger has zero
entries. The accepted GLB is installed in Tide Bastion and consumed by the real
intro battle's `IslandStage._props`; accepted and installed SHA-256 are identical:
`57c71ed83686e9d0baf6bf8667f5ec8ab84cc6ad206bd1d1f03ba2c69f695b08`.
Independent post-installation import, chest, slot, eleven-layout coast and battle
flow tests passed, as did real-renderer capture and normal boot. A clean import
without an existing Godot cache passed. Customer commit `0a0d834` is recorded in
[Tide Bastion PR 1](https://github.com/akifsen/tide-bastion/pull/1).

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
