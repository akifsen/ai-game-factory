# V0.5 — Generalized asset factory

Start: 2026-09-27. Package target: **0.5.0**.

V0.4 produced `prop_energy_crate_01` / `r001`. That raw GLB, processed GLB, concept, receipt, final approval, and the historical 15-credit Meshy call stay immutable. V0.5 reads them as the `static_prop@1` golden reference. It does not regenerate them.

## Order of work

1. Classify crate-specific behavior.
2. Add the profile model and bind `static_prop@1` without changing the 0.4.0 specification fingerprint.
3. Re-validate the golden processed GLB. Do not overwrite it.
4. Add `pickup@1` and `modular_piece@1` with labeled test fixtures.
5. Drive Blender, validation, Godot, runtime checks, cameras, and evidence from the profile.
6. Prove the offline path on Windows locally and on Linux in CI, using the fake provider.
7. Create `pickup_energy_cell_01` only through the concept gate. Do not approve it in this sprint and do not call Meshy.

## Classification

| Kind | Examples | Action |
|---|---|---|
| Generic | GLB parse, material and texture budget checks, Meshy polycount clamp of 15000 | Leave shared |
| Profile-specific | LOD requirement, origin, collider, snap, Godot body, review views, framing fractions | Move onto the profile |
| Asset-specific | Crate dimensions 1.2 × 1.0 × 1.0, triangle budgets, import path | Stay on the specification |
| Fixture or test only | Acceptance crate, `energy_pickup_test`, `wall_panel_test`, golden regression | Keep labeled |
| Accidental hardcode | Schema that only allowed `static_prop`, three fixed capture angles, crate id as the fake-provider default | Remove from the production path |

## Out of scope

Character, rigged character, weapon, vehicle, building, terrain, animation, VFX, and foliage. Gameplay collection logic. A plugin marketplace. Batch production of many assets. A second paid Meshy generation.
