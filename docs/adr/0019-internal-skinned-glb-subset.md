# ADR 0019: Bounded internal skinned GLB subset (V0.8-1)

## Status

Accepted for V0.8-1. **Internal-only**; not a production profile capability. `rigged_character` remains UNSUPPORTED (ADR 0017).

## Context

ADR 0017 recorded a feasible 12-bone humanoid spike and a future skin contract. V0.7 production validation (`core` group) rejects all `skins`, `animations`, `JOINTS_0` and `WEIGHTS_0` (ADR 0018). V0.8-1 needs a **separate**, fail-closed decoder and validator for a bounded skinned subset, plus a Godot deformation oracle, without weakening `parse_glb` / `validate_glb` or promoting any profile.

## Decision

1. **Scope.** One internal entrypoint (`validate_internal_skinned_glb`) and a standalone `skin_internal` rule tuple (`SKIN_INTERNAL_RULES`) used **only** from tests and internal tooling—not registered in production `RULE_GROUPS`. `select_composition` never includes skin rules.
2. **Supported GLB subset.**
   - glTF 2.0 embedded GLB only; no external buffers, images, or URI references; no `extensionsRequired` / `extensionsUsed`; no morph targets; no sparse accessors.
   - Exactly one active scene (strict integer `scene` index; scene entries are objects with integer `nodes` lists); one asset root node that is the sole scene root, has no parent, and spans **all** glTF `nodes` by reachability from that root.
   - Exactly one skinned mesh primitive as a direct child of the asset root; exactly one `skin`.
   - **Skeleton root.** Listed in `skin.joints`. Either explicit `skin.skeleton` (strict integer node index) or **inferred** when omitted (typical Blender glTF export): walk joint parents until a unique root joint is found relative to the asset root. The skeleton root bone (`Hips` on the reference fixture) is a direct child of the asset root **or** of an optional **single Blender armature wrapper** node that is a direct child of the asset root, has **identity** local transform (default TRS only; no `matrix`), and parents only the skeleton root joint among bones (mesh remains a sibling under the asset root).
   - JSON index fields (`joints`, `skeleton`, `inverseBindMatrices`, node `mesh` / `skin`, primitive attribute and `indices` accessors, scene roots, node `children`) must be exact integers (not bool, float, or string); bounds are checked before use.
   - Declared bone map and parent topology must match the asset (12 joints for the reference humanoid fixture), including the armature-wrapper parent when present.
   - Rest pose kind `T` only in V0.8-1 (`A` deferred); joint origins and **rest joint bases** (+Y / −Z arm axes) checked against contract reference in the ADR 0014 frame (+Y up, −Z front), with configured angular tolerance on bases.
   - Per-vertex weights: at most four influences; finite, non-negative; sum normalized within `1e-3`; no vertex with zero total weight.
   - Joint indices valid; joint count within contract **upper bound** (`joint_count_max`); inverse bind matrices consistent with measured rest globals (tolerance `1e-3` on matrix entries).
   - **Forbidden:** animations; malformed accessor bounds/types/counts/strides; non-finite numeric data; unsupported skin weight/joint component types; hierarchy cycles or depth > 128.
3. **Oracle.** A staged Godot harness poses one **named** bone and uses `MeshInstance3D.bake_mesh_from_current_skeleton_pose()` after attaching the import to the scene tree (**Compatibility** renderer; display/visual bake required—headless-only paths that skip the baked mesh are insufficient). Fixed axis-aligned regions distinguish affected vs unaffected geometry; a negative case uses geometry that is **not** weighted to the posed bone. Python verifies measurements and status, not harness self-report alone.
4. **Promotion.** Production availability, evidence, cold verifier, and profile registry changes require a later ADR and milestone; this slice does not claim V0.8 complete.

## Consequences

- Production `character@1` and historical goldens stay unchanged.
- Internal fixtures and Blender export scripts live under package resources for wheel installs.
- Further V0.8 gates (evidence, CI matrix, profile promotion) are tracked in `docs/work-plan-v0.8.md`.

## Implementation (V0.8-1)

- `gamefactory.adapters.assets.internal_skin` and related modules implement decode/validate.
- `SKIN_INTERNAL_RULES` lives in `internal_skin_rules.py` and is not part of closed production `RULE_GROUPS`.
- Reference contract: `resources/internal_skin/humanoid_12bone_contract.json`.
- Godot harness: `resources/godot/skin_deformation_harness.gd`.
