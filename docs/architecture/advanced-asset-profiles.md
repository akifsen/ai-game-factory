# Advanced asset profiles (V0.7 design)

Status: **architecture accepted (Step 2). Not implemented.** Decisions: ADR 0013–0018.

## Accepted decisions

| Topic | Decision |
|---|---|
| Profile vs source vs capability | Domain profiles (`vehicle@1`, `weapon@1`, `aircraft@1`, `character@1`). Source kind `local_operator_assembly`. Geometry mode `assembly`. No `assembly@1` profile (ADR 0016, ADR 0018). |
| Multi-part source | `local_operator_assembly` only. The input is self-contained, and `derived_from` is provenance only (ADR 0016). |
| Meshy V0.7 path | single-mesh only; rejected before snapshot creation for assemblies (ADR 0016) |
| Canonical frame | +Y up, −Z front (ADR 0014) |
| `source_front` | required, `-Z` or `+Z`; `+Z` means exactly one 180° rotation about +Y at the root, recorded and auditable; missing means rejection (ADR 0014) |
| Pivot | position + local basis + parent-relative transform + motion kind + axis; independent rule ids (ADR 0013) |
| Socket | id, parent part, local position, local basis, forward = local −Z, placement rule, explicit angular tolerance (ADR 0013) |
| Part scaling | uniform and positive only (ADR 0013) |
| Review views | `front`, `rear`, `left`, `right`, `side`, `three_quarter`, `three_quarter_front`, `three_quarter_rear`, `top` all placed. Unknown means explicit failure, with no fallback (ADR 0014). |
| `side` | legacy compatibility alias of `right` for placement; not deprecated (ADR 0014) |
| Colliders | `box`, `capsule` only; no `convex` or `compound` claims (ADR 0015) |
| Capsule | `radius_m ≥ 0.10`, `height_m > 2 × radius_m`, plus a separate fit check against bounds (ADR 0015) |
| `rigged_character` | UNSUPPORTED; never in `ProfileRegistry.available` (ADR 0017) |
| Validator | closed rule groups selected from typed capabilities; legacy golden preserved (ADR 0018) |

## Profile layering

```
Asset profile (production contract)   vehicle@1 | weapon@1 | aircraft@1 | character@1 | static_prop@1 | pickup@1 | modular_piece@1
Source kind (provenance)              provider_generated | local_operator_assembly
Geometry mode (capability)            single_mesh | assembly
```

| Profile | Geometry mode | Accepted source kinds | Colliders | Status |
|---|---|---|---|---|
| `static_prop@1`, `pickup@1`, `modular_piece@1` | single_mesh | provider_generated | box | existing, unchanged |
| `character@1` | single_mesh | provider_generated | capsule | V0.7 target |
| `vehicle@1` | assembly | local_operator_assembly | root `box` (advertised only after end-to-end verification) | V0.7 flagship |
| `weapon@1` | assembly | local_operator_assembly | as vehicle | V0.7 target |
| `aircraft@1` | assembly | local_operator_assembly | as vehicle | V0.7 target |

`UNSUPPORTED_PROFILE_IDS` currently holds `character`, `rigged_character`, `weapon`, `vehicle`, `building`, `terrain`, `animation`, `vfx` and `foliage`. `character`, `weapon` and `vehicle` leave the list only in the step that implements them. `aircraft` is not listed today and stays unbindable until implemented. `building`, `terrain`, `animation`, `vfx`, `foliage` and `rigged_character` stay UNSUPPORTED, and the list is not replaced by a generic `assembly`.

Proposed review views, which are profile data and final at implementation:

- `vehicle@1`, `aircraft@1`: front, rear, left, right, three_quarter, three_quarter_rear, top
- `weapon@1`: front, left, right, three_quarter, top
- `character@1`: front, rear, left, right, three_quarter

## Schema versions

| Schema | Status | Change |
|---|---|---|
| `asset-profile-0.5.0` | frozen | existing three profiles, unchanged bytes and fingerprints |
| `asset-profile-0.7.0` | new | `geometry_mode`, `accepted_source_kinds`, role vocabulary and required roles, role motion constraints, required sockets with placement rules and angular tolerance, `capsule` collider, extended views |
| `asset-spec-0.4.0`, `asset-spec-0.5.0` | frozen | unchanged fingerprints |
| `asset-spec-0.7.0` | new | `source_kind`, `parts`, `sockets`, `collider` block |
| `asset-processing-contract-0.7.0` | new | hierarchy, pivots, sockets, capsule, `source_front` and normalization |
| `asset-evidence-0.7.0` | new | `source_provenance` role set with retained source, derived validator groups, normalization record, extended view set |
| `paid-request-0.6.0` | frozen | assemblies never reach it |
| `asset-evidence-0.4.0`, `0.5.0`, `0.6.0` | frozen | verified exactly as before, including `side` |

A runtime-observation or production-receipt schema is versioned independently, and only if its content genuinely changes. A schema version is never bumped just because the package version changed.

## `asset-spec-0.7.0` (sketch)

```yaml
schema_version: "0.7.0"
profile: vehicle
profile_version: 1
source_kind: local_operator_assembly   # provider_generated | local_operator_assembly
parts:
  - part_id: hull
    role: hull
    parent: root
    pivot:
      position_m: [0.0, 0.0, 0.0]
      basis: identity                  # or unit quaternion [x, y, z, w]
      motion: {kind: fixed}
  - part_id: turret
    role: turret
    parent: hull
    pivot:
      position_m: [0.0, 0.62, 0.1]
      basis: identity
      motion: {kind: revolute, axis: [0, 1, 0]}   # yaw
  - part_id: barrel
    role: barrel
    parent: turret
    pivot:
      position_m: [0.0, 0.18, -0.35]
      basis: identity
      motion: {kind: revolute, axis: [1, 0, 0]}   # pitch
sockets:
  - socket_id: muzzle
    parent_part: barrel
    translation_m: [0.0, 0.0, -1.4]
    rotation: identity                 # forward = local -Z
    placement: forward_end
collider:
  policy: box                          # box | capsule
  capsule: null                        # {radius_m, height_m} when capsule
```

Rules:

- Part ids are unique. Parent references resolve with no cycles.
- Quaternions are normalized within 1e-6.
- Non-fixed motion requires a unit axis.
- `parts` and `sockets` require `source_kind: local_operator_assembly`.
- Roles and motion must satisfy the profile's role constraints.

`source_front` is not part of the specification. It belongs to the source registration (ADR 0016), because it describes the source file and not the desired asset.

## Source registration (`local_operator_assembly`)

```yaml
source_provenance_type: local_operator_assembly
paid: false
artifact_sha256: <hex>
artifact_bytes: <int>
authoring_tool: {name: blender, version: "4.x"}
source_front: "+Z"                     # required: "-Z" | "+Z"
part_map: [...]                        # must equal spec parts
socket_map: [...]                      # must equal spec sockets
derived_from:                          # optional, provenance only, never resolved
  - {part_id: turret, source_artifact_sha256: <hex>, source_asset_id: <id>, source_revision: 3}
actor: <operator>
reason: <text>
```

The normalization record in the processing report and the evidence:

```yaml
source_front: "+Z"
normalization_applied: true
normalization_transform: {quaternion_xyzw: [0, 1, 0, 0], matrix: [[-1,0,0,0],[0,1,0,0],[0,0,-1,0],[0,0,0,1]]}
resulting_front: "-Z"
```

## GLB and Godot node contract (assembly)

```
ROOT (identity)
├── PART_<part_id>              local transform == declared pivot; extras {gf_motion, gf_axis}
│   ├── SM_<asset>_<part>_LOD0  identity
│   ├── SM_<asset>_<part>_LOD1  identity (when required)
│   ├── SOCKET_<socket_id>      no mesh, no children, identity scale
│   └── PART_<child> ...
└── COL_<asset>                 root box enclosing rest pose (capsule: from contract, no mesh)
```

Godot: a `Node3D` root, then `Visual` (which mirrors the PART tree, with sockets as `Marker3D`), then `StaticBody3D` or `Area3D`, then `CollisionShape3D` (`BoxShape3D` or `CapsuleShape3D`). Single-mesh profiles keep the V0.6 contract unchanged.

Articulation check: for each non-fixed part, apply a test displacement about the declared pivot and axis. Assert that the pivot stays fixed (revolute), that descendants and sockets follow rigidly, and that the parent chain, siblings and root are unchanged. Then restore the rest pose.

## Validator composition

| Geometry mode / contract | Groups (derived, never by profile id) |
|---|---|
| single_mesh, box (legacy three) | fixed legacy composition, identical ordered findings to V0.6 (golden) |
| single_mesh, capsule (`character@1`) | core, single_mesh, collider_capsule |
| assembly | core, parts, orientation, pivot, sockets (when declared), collider_box or collider_capsule |

## Rule ids introduced

`part.missing`, `part.unexpected`, `part.parent`, `part.scale`, `pivot.collapsed`, `pivot.position`, `pivot.orientation`, `pivot.axis`, `socket.missing`, `socket.structure`, `socket.parent`, `socket.position`, `socket.orientation`, `orientation.source_front`, `collider.capsule.shape`, `collider.capsule.fit`, `view.unplaced`.

## Required fixtures

All fixtures are generated deterministically.

Positive fixtures:

- a vehicle assembly with `source_front: -Z`
- the same assembly authored `+Z` and normalized
- a weapon assembly with a muzzle socket
- an aircraft assembly
- `humanoid_character_test` with a capsule of radius 0.25–0.35 m
- every placed review view

Negative fixtures:

- a collapsed-pivot assembly (the spike result), which must fail `pivot.collapsed`
- a pivot with the correct position but the wrong basis (`pivot.orientation`)
- a pivot with the correct position and basis but the wrong axis (`pivot.axis`)
- a part under the wrong parent (`part.parent`)
- non-uniform, zero or negative part scale
- a socket with the right name but the wrong orientation, and a socket under the wrong parent
- a socket not at the forward end, and a socket with scale, a mesh or children
- a missing `source_front`, `source_front` with any other value, and a result with an extra or arbitrary rotation
- a parts-declaring spec bound to a Meshy request, rejected before snapshot creation with zero intents
- a rigged or skinned GLB under `character@1`, and `rigged_character` selected by a production spec
- a capsule with radius < 0.10, with height ≤ 2 × radius, and one that does not fit the bounds
- an unplaced or unknown review view in a profile, a capture request, the Godot harness or an evidence manifest
- `local_operator_assembly` evidence with `paid: true`, or with a mixed role set
- tampering with the retained source or the normalization record, detected by the cold verifier

The golden test requires the V0.4–V0.6 fixtures to produce identical ordered findings and to verify their historical bundles unchanged.

## Implementation order

1. Dual-version profile and spec foundations
2. Backward-compatibility golden tests
3. Validator modularization, with zero intended behavior change
4. Review-view placements and removal of the fallback
5. Semantic part and tree contracts
6. Pivot contracts
7. Socket contracts
8. Generated fixtures and negative fixtures
9. Blender authored-assembly processing
10. Godot hierarchy, socket and articulation verification
11. `vehicle@1`
12. `weapon@1`
13. `aircraft@1`
14. `character@1`
15. Evidence and receipt 0.7
16. Cold verification and tamper coverage
17. Real Blender and Godot acceptance
18. V0.4–V0.6 compatibility audit
19. Release-candidate validation

These invariants hold throughout: real Meshy calls = 0, paid submissions = 0, credits = 0, production DB mutations = 0, production artifact mutations = 0.
