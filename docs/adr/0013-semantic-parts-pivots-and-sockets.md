# ADR 0013: Semantic parts, pivots and sockets

## Status

Accepted and implemented in V0.7.0. Schema detail: `docs/architecture/advanced-asset-profiles.md`.

## Context

V0.5 profiles describe one rigid mesh: `SM_{asset_id}_LOD0`, optional `SM_{asset_id}_LOD1` and `COL_{asset_id}`, all with identity transforms (`orientation.identity`). Assets with moving parts (a turret on a hull, a barrel on a turret) and attachment points (a muzzle) cannot be expressed.

A V0.7 spike exported a Blender assembly hull → turret → barrel → muzzle and imported it into Godot 4.7.2. The hierarchy and the muzzle socket survived. **Every part pivot collapsed to the origin.** A hierarchy that imports cleanly can still be wrong. Pivots and sockets therefore need to be declared, placed and validated explicitly, and each kind of transform error needs its own finding.

## Decision

1. **Parts are a capability, not a profile.** Semantic parts belong to the `assembly` geometry mode (ADR 0018). Domain profiles (`vehicle@1`, `weapon@1`, `aircraft@1`) opt into it through typed profile fields. The part, pivot and socket machinery is shared, and it never branches on a profile id. Profiles differ only in data: role vocabulary, required roles, role motion constraints, required sockets and review views.
2. **Part.** An `asset-spec-0.7.0` specification may declare `parts`: an ordered list of `{part_id, role, parent, pivot}`.
   - `part_id` is a lowercase identifier, unique within the asset.
   - `parent` is another `part_id` or `root`. Cycles and orphans are rejected.
   - `role` comes from the bound profile's closed role vocabulary. The profile lists which roles are required.
   - The GLB node for a part is `PART_{part_id}`. Its meshes are `SM_{asset_id}_{part_id}_LOD0` (plus `_LOD1` when required). They are direct children with identity local transforms.
3. **Pivot contract.** A movable part's pivot captures all of the following. They are declared in the canonical frame (+Y up, −Z front, ADR 0014):
   - `position_m`: the pivot origin, in the parent's frame
   - `basis`: the part's local orientation in the parent's frame, as a unit quaternion or `identity` (orthonormal, right-handed, determinant +1)
   - the parent-relative transform, which is `position_m` plus `basis`. The `PART_` node's glTF local transform must equal it.
   - `motion.kind`: `fixed`, `revolute` or `prismatic`
   - `motion.axis`: a unit vector in the part's local basis. It is required unless the kind is `fixed`. Optional limits are carried as data only.

   A profile may constrain a role's motion. For example, `vehicle@1` requires `turret` to be `revolute` about local `+Y` (yaw) and `barrel` to be `revolute` about local `+X` (pitch):

   ```
   hull            fixed
   └── turret      revolute, axis +Y  (yaw)
       └── barrel  revolute, axis +X  (pitch)
           └── SOCKET_muzzle
   ```

   The processed GLB carries each part's motion declaration in node `extras` (`gf_motion`, `gf_axis`), exported from Blender custom properties. That lets the axis be checked against the artifact rather than only against the specification.
4. **Independent pivot findings.** Each defect has its own rule id. None of them is folded into a generic `part.invalid`.

   | Rule | Fails when |
   |---|---|
   | `part.missing` / `part.unexpected` | the `PART_` node set does not equal the declared set |
   | `part.parent` | a `PART_` node's parent differs from the declared parent |
   | `part.scale` | the node scale is non-uniform (tolerance 1e-5), zero or negative |
   | `pivot.collapsed` | the declared `position_m` is non-zero but the node sits at the parent origin, or the part's geometry is baked in the parent frame (the mesh bounds are displaced by roughly the declared pivot) |
   | `pivot.position` | the translation differs from `position_m` by more than `pivot_tolerance_m` and the case is not a collapse |
   | `pivot.orientation` | the rotation differs from `basis` by more than `basis_tolerance_deg` |
   | `pivot.axis` | the `extras` motion kind or axis differs from the specification, or the specification violates the profile's role constraint |

   A correctly positioned pivot with a wrong basis fails `pivot.orientation`. A correctly positioned and oriented pivot with a wrong articulation axis fails `pivot.axis`. The spike's collapsed assembly becomes a required negative fixture.
5. **Socket contract.** A socket is not merely a named Empty or `Node3D`. The specification declares `sockets`: `{socket_id, parent_part, translation_m, rotation, placement}`.
   - The GLB node is `SOCKET_{socket_id}`. It has no mesh, no children and identity scale, and in Godot it becomes a `Marker3D`.
   - **Socket forward is local −Z** and socket up is local +Y.
   - `placement` is a profile-defined rule. V0.7 defines `forward_end`: the socket lies in the forward (−Z) end region of its parent part's rest bounds, meaning within `forward_end_fraction` of the part's Z extent from its −Z face, and it is laterally within the part's bounds plus tolerance.
   - A profile may assert a socket's rest forward in root space. For example, a muzzle's forward is root −Z within `socket_angle_tolerance_deg`, and the profile states that tolerance explicitly.

   Socket findings are also independent: `socket.missing`, `socket.structure` (mesh, children or scale), `socket.parent`, `socket.position` (translation or placement rule), `socket.orientation` (basis or asserted forward). A socket with the correct name but the wrong orientation fails.
6. **Transform preservation.** Part and socket local transforms are compared at three stages: the authored source (after the single normalization in ADR 0014), the processed GLB, and the Godot imported scene. A difference beyond tolerance at any stage is a FAIL that names the stage.
7. **Godot articulation verification.** For each `revolute` or `prismatic` part, the runtime check applies one fixed test displacement about or along the declared axis at the declared pivot. It asserts three things:
   - the part rotates or translates about the expected pivot: the pivot's world position is unchanged for revolute motion, and the displacement matches the expected amount
   - all descendant parts, pivots and sockets move rigidly with the part
   - the parent chain, sibling parts and the root are unchanged

   The check then restores the rest pose and verifies the restoration.

## Consequences

The scene contract grows from a single `Visual` mesh to a `Visual` subtree that mirrors the part tree. `orientation.identity` does not apply to assemblies, so ADR 0018 selects it only for the single-mesh geometry mode. Single-mesh profiles (`static_prop@1`, `pickup@1`, `modular_piece@1`, `character@1`) declare no parts and keep today's contract. Only operator-authored assemblies can carry parts (ADR 0016).

## Implementation (V0.7.0)

- The `parts`, `pivot` and `sockets` rule groups live in `src/gamefactory/adapters/assets/validation_rules.py`. Every rule id listed in the architecture document is emitted by exactly one rule.
- Three rule ids were added so that a tree defect never hides behind another finding: `part.root` (exactly one identity `ROOT` as the only scene root), `part.mesh` (the `SM_<asset>_<part>_LOD0/1` meshes sit directly under their part with identity transforms) and `socket.placement` (the `forward_end` rule measured along the socket forward in the parent part's frame, using the profile's `forward_end_fraction`). Assemblies also report `nodes.unique` over every node, not only mesh nodes.
- `pivot.collapsed` fails when a part's declared pivot is off its parent origin but the `PART_` node sits at the parent origin, which is exactly the spike result. `pivot.position`, `pivot.orientation` and `pivot.axis` are reported independently; the negative fixtures prove that a wrong basis fails only `pivot.orientation` and a wrong axis fails only `pivot.axis`.
- The Godot harness re-checks the imported tree, pivots and sockets, replaces each socket with a `Marker3D` carrying the same transform, and articulates every movable part (revolute: 15 degrees about the declared axis; prismatic: 0.05 m along it). It asserts that the pivot stays fixed or moves along the axis, that descendants and sockets follow rigidly, that every other node is unchanged, and that the rest pose is restored. Python re-checks the observation and never trusts its status alone.
