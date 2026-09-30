# ADR 0015: Collider policies and runtime body kinds

## Status

Accepted for V0.7 (Step 2 architecture decision). Not implemented.

## Context

V0.5 implements one collider policy (`box`) and two body kinds (`static_body`, `area`). ADR 0008 forbids advertising collider policies that are not implemented. Godot represents the existing box collider points with a `ConvexPolygonShape3D`, but that is an engine detail and not a `convex` Factory contract. A character fits a box poorly, and a review scene does not need a gameplay controller.

## Decision

1. **V0.7 collider scope is deliberately reduced to `box` and `capsule`.** `convex`, `simple_mesh` and `compound` are neither implemented nor advertised. The engine's internal use of `ConvexPolygonShape3D` for box points does not count as `convex` support, and a set of boxes is never called `compound`.
2. **First implementation pass:**
   - `static_prop@1`, `pickup@1`, `modular_piece@1`: existing `box` behavior, unchanged
   - `character@1`: `capsule`
3. **Capsule contract.** The axis is root +Y. `asset-spec-0.7.0` declares `collider.capsule.radius_m` and `collider.capsule.height_m`, where the height includes both hemispheres. The capsule's base is at the asset origin for `bottom_center`, and the capsule is centered for `center`.

   Structural schema rules, which apply to every profile:
   - `radius_m ≥ 0.10`. This is a conservative structural floor, not a human-size rule.
   - `radius_m > 0` and `height_m > 2 × radius_m` (strict)
   - both finite

   Geometric sanity is validated separately against the specification bounds:
   - `height_m ≤ asset height_m + dimension_tolerance_m`
   - `2 × radius_m ≤ max(width_m, depth_m) + dimension_tolerance_m`

   Typical adult human proportions are **not** encoded in the schema. The V0.7 `humanoid_character_test` fixture uses a radius in 0.25–0.35 m, and that value is fixture data. If implementation finds a better invariant relative to dimensions, it is documented here and tested; arbitrary constants are not added.

   The GLB carries no capsule mesh. The Godot scene contract builds a `CapsuleShape3D` from the declared values, and the runtime check compares the shape to them. The rule ids are `collider.capsule.shape` (structure) and `collider.capsule.fit` (sanity).
4. **Body kinds in V0.7:** still `static_body` and `area` only. `CharacterBody3D`, `RigidBody3D` and `AnimatableBody3D` are not V0.7 body kinds. A body kind describes the review and validation scene, not gameplay.
5. **`character@1`** is a single-mesh, unrigged profile with `body_kind: static_body` and `allowed_collider_policies: [capsule]`. It requires a ray hit and forbids rigs and animation (ADR 0017).
6. **Assembly colliders: designed, advertised only after verification.**
   - **V0.7 candidate, `box` at the root:** one `COL_{asset_id}` box that encloses the whole assembly in its rest pose, parented to the root body. It does not follow part motion. This is the existing `box` semantics applied to the assembly bounds. `vehicle@1`, `weapon@1` and `aircraft@1` may advertise `box` only after assembly Blender generation, GLB verification and Godot runtime verification all pass for it.
   - **Recorded design, not V0.7: `part_boxes`.** One box per part, `COL_{asset_id}_{part_id}`, parented under its `PART_` node so that it follows articulation. This is a separately named policy with its own Blender, GLB and Godot oracle. It is never presented as `compound`.
   - If implementation shows that a multi-part profile is not useful with the root `box`, that is raised as an explicit decision. It is not solved by relabelling.

## Consequences

`ProcessingPolicy` accepts `capsule`. `collider_policy: capsule` and the `collider` block exist only in `asset-spec-0.7.0`. Existing profiles and specifications are unchanged. Gameplay bodies and per-part colliders remain later work.

## Geometry validator implementation note

The test-only V0.7 geometry validator enforces the capsule's declared dimensions against the spec bounds and rejects any `COL_` mesh when the capsule policy is selected. It does not create the Godot `CapsuleShape3D`, verify its root-axis/base placement at runtime, or make a character profile available. Those remain vertical-integration gates.
