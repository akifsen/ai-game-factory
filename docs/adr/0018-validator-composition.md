# ADR 0018: Validator composition

## Status

Accepted for V0.7 (Step 2 architecture decision). The V0.6 legacy path has been modularized; V0.7 composition remains pending.

## Context

`validate_glb` is one function. Profile behavior is expressed as branches inside it (origin policy, snap grid), and some rules are hard-coded for all profiles (`orientation.identity`, the rejection of skins and animations). Parts, pivots, sockets and capsules (ADR 0013, ADR 0015) would add more branches, and some of them contradict existing rules: an assembly cannot pass `orientation.identity`.

The current emission order interleaves concerns. For example, `lod1.bounds` is emitted after `dimensions.snap`, and `orientation.identity` comes near the end. Evidence and the compatibility golden depend on that order.

## Decision

1. **A closed set of rule groups.** Validation is an ordered composition of rule groups. Each group is a pure function `(parsed GLB, spec, profile contract) -> findings`. The set is closed and registered in Python:

   | Group | Scope |
   |---|---|
   | `core` | parse, hash, budgets, materials, textures, scale bounds, origin, dimensions, forbidden skin and animation |
   | `single_mesh` | V0.5 node naming, LOD presence and bounds, `orientation.identity`, snap grid when the profile enables it |
   | `parts` | `PART_` tree, part naming, `part.*` rules |
   | `orientation` | `orientation.source_front` (ADR 0014) |
   | `pivot` | `pivot.*` rules (ADR 0013) |
   | `sockets` | `socket.*` rules (ADR 0013) |
   | `collider_box` | the existing box collider rules |
   | `collider_capsule` | `collider.capsule.*` (ADR 0015) |
   | `skin_internal` | internal spike only (ADR 0017), never selected by a production profile |

   The implementation may split or rename groups. The set stays closed, and any change is recorded in this ADR.
2. **Selection derives from typed capabilities.** It never depends on profile ids. The composition is computed from typed profile and specification fields: `geometry_mode` (`single_mesh` | `assembly`), the collider policy, whether sockets are declared, and whether the source kind requires normalization.
   - Code such as `if profile_id == "vehicle"` is forbidden in the generic pipeline.
   - Profile documents do not name code or groups. An unknown `geometry_mode` or collider policy rejects the profile at load.
   - The evidence records the derived group list, and the verifier rejects unknown group ids.
3. **Aggregation.** Findings keep their rule ids. The overall severity is the worst finding. No group can suppress, downgrade or rewrite another group's finding. A group that cannot run because its input is missing produces a FAIL, never a skip.
4. **Compatibility: refactor first, behavior change second.**
   - `static_prop@1`, `pickup@1` and `modular_piece@1` map to a fixed legacy composition. It must emit the **identical ordered findings** (rule id, severity, expected, actual, message) that V0.6 emits.
   - A golden test over the existing fixtures lands **before** the modularization, and the modularization lands with zero intended behavior change.
   - Group boundaries are chosen to preserve the legacy emission order. The golden is authoritative over the table in item 1.
   - V0.4, V0.5 and V0.6 evidence and approval fingerprints must still verify.
5. **Runtime and cold verification follow the same composition.** Runtime checks are keyed by the same group ids. Review views follow ADR 0014: an unknown view is an explicit failure.

## Consequences

Adding a V0.7 rule means adding one group and one selection condition. Rule ordering becomes part of the evidence format. `vehicle@1`, `weapon@1` and `aircraft@1` share the `parts`, `pivot` and `sockets` groups without profile-specific code.

## Implementation status

The legacy validator now uses a frozen decoded context and an executable ordered rule plan: `mesh_structure`, `collider_box`, `budgets`, `dimensions_origin_snap`, `lod_bounds`, and `orientation`. Parse and hash findings remain in the facade so their historical timing and order are preserved. The plan is selected through typed capabilities and rejects unknown groups or an empty selection. This does not implement V0.7 parts, source orientation, pivots, sockets, capsules, skin-internal checks, evidence composition, or runtime/cold-verifier integration; those obligations remain open.
