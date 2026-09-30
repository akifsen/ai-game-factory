# V0.7 work plan: advanced asset profiles

Design: [advanced asset profiles](architecture/advanced-asset-profiles.md), ADR 0013–0018. Result: [V0.7 completion report](reports/v0.7-completion-report.md).

Invariants for every step: real Meshy calls = 0, paid submissions = 0, credits = 0, production database mutations = 0, production artifact mutations = 0. V0.4–V0.6 findings, fingerprints and bundles stay identical.

| # | Step | Status | Where |
|---|---|---|---|
| 0 | Linux CI repair: `tests/golden` compared a Pillow/zlib-dependent PNG hash, so both Ubuntu jobs of `main` (31d795c) failed | Done | `2041967` |
| 1 | Dual-version profile and spec foundations | Done (PR #5) | `f79eb08`, `894c5b3` |
| 2 | Backward-compatibility golden tests | Done (PR #4) | `09073e8` |
| 3 | Validator modularization, zero behavior change | Done | `e63cf49` |
| 4 | Review-view placements, fallback removed | Done | `52b2485` |
| 5 | Semantic part and tree contracts | Done | `7641b45` |
| 6 | Pivot contracts | Done | `7641b45` |
| 7 | Socket contracts | Done | `7641b45` |
| 8 | Generated positive and negative fixtures | Done | `7641b45` |
| 9 | Authored-assembly processing (transform-only; see ADR 0016 deviation) | Done | `5b0c847` |
| 10 | Godot hierarchy, socket and articulation verification | Done | `5b0c847` |
| 11 | `vehicle@1` | Done | `7641b45`, acceptance |
| 12 | `weapon@1` | Done | `7641b45`, acceptance |
| 13 | `aircraft@1` | Done | `7641b45`, acceptance |
| 14 | `character@1` (capsule) | Done | `7641b45`, `5b0c847`, acceptance |
| 15 | Evidence and receipt 0.7 | Done | `5b0c847` |
| 16 | Cold verification and tamper coverage | Done | `5b0c847` |
| 17 | Real Blender and Godot acceptance | Done | `24fabc4`, `scripts/verify_v07_acceptance.py` |
| 18 | V0.4–V0.6 compatibility audit | Done | completion report |
| 19 | Release-candidate validation and 0.7.0 | Done | this change set |

## Not in V0.7

- Human decisions that belong to the operator are unchanged: the pending V0.3 visual reviews, the V0.4 concept approval `APP-9878c627` and the V0.5 `pickup_energy_cell_01/r001` concept approval `APP-73209635` were neither approved nor closed.
- Rigged characters, skin weights and animation (ADR 0017), `convex`/`compound` colliders (ADR 0015), dependency resolution through `derived_from` (ADR 0016) and per-part LOD generation.
- V0.6 deferred items (resumable concept replacement, workflow lock in `accounting reconcile`, P3 release provenance) are unchanged.
