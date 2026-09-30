# Assembly production (V0.7)

An assembly is an asset with named, movable parts, such as a vehicle with a yaw turret and a pitch barrel, a weapon with a sliding bolt, or an aircraft with control surfaces. In V0.7 an operator authors the assembly (in Blender or by script), and the factory normalizes, validates, runs, renders and records it. A provider never generates an assembly, and no step costs money.

Profiles: `vehicle@1`, `weapon@1`, `aircraft@1`. Design and decisions: [advanced asset profiles](../architecture/advanced-asset-profiles.md), ADR 0013–0018.

## 1. Author the source GLB

The source must follow the factory node contract:

```
ROOT                        identity; the only scene root
├── PART_<part_id>          local transform == declared pivot; extras {gf_motion, gf_axis}
│   ├── SM_<asset>_<part>_LOD0     identity; the part's geometry in its own frame
│   ├── SM_<asset>_<part>_LOD1     only when the lod policy requires it
│   ├── SOCKET_<socket_id>  empty; forward is local -Z
│   └── PART_<child> ...
└── COL_<asset>             box collider enclosing the rest pose (box policy)
```

- `gf_motion` is `fixed`, `revolute` or `prismatic`; `gf_axis` is the unit axis in the part's local frame and is omitted for `fixed`. In Blender these are custom properties on the part Empty, exported with **Include > Custom Properties**.
- Scale parts uniformly and positively. Do not bake the pivot into the mesh: a part whose Empty sits at its parent's origin while the declared pivot does not fails `pivot.collapsed`.
- Model facing Blender's front view (−Y). Blender's glTF exporter turns that into a glTF **+Z** front, which you declare in the registration. Keep `ROOT` at the origin without rotation; the factory never guesses a correction.

`scripts/blender_author_assembly.py` is a worked example that builds a vehicle this way in real Blender.

## 2. Write the specification

An `asset-spec-0.7.0` file declares the profile, `source_kind: local_operator_assembly`, dimensions, the collider block, the `parts` (id, role, parent, pivot position/basis/motion) and the `sockets`. Roles and motions must satisfy the profile, for example `vehicle@1` requires a `turret` revolute about +Y and a `barrel` revolute about +X with a `muzzle` socket at its forward end. JSON Schema: `src/gamefactory/schemas/asset-spec-0.7.0.schema.json`.

## 3. Register the source

```bash
gamefactory asset register-source --spec tank.spec.json --source tank.glb \
  --source-front=+Z --authoring-tool blender --authoring-tool-version 4.0.2 \
  --actor "your name" --reason "why this source" --output tank.registration.json
```

Write the front as `--source-front=+Z` or `--source-front=-Z` (with `=`, because a bare `-Z` looks like an option). The registration records the SHA-256 and size of the source, `paid: false`, the authoring tool, the source front, and part and socket maps copied from the specification. There is no default front: a missing or unknown value is a rejection.

## 4. Assemble

```bash
gamefactory asset assemble --spec tank.spec.json --source tank.glb \
  --registration tank.registration.json --dry-run
gamefactory --godot-path /path/to/godot asset assemble --spec tank.spec.json \
  --source tank.glb --registration tank.registration.json
```

The workflow runs:

1. **PREPARE** retains the specification, the source and the registration with immutable digests. A source or registration that changed after creation stops here.
2. **PROCESS** normalizes the declared front. A `-Z` source is kept byte-for-byte. A `+Z` source gets exactly one 180° rotation about +Y, baked into the root-level children; nothing else changes. Assemblies are not re-exported through Blender (ADR 0016).
3. **VALIDATE** runs the rule groups derived from the profile: `core`, `parts`, `orientation`, `pivot`, `sockets` (when declared) and `collider_box` or `collider_capsule`. Each kind of defect has its own rule id.
4. **GODOT** imports the result in a staged copy, checks the part tree, pivots and sockets (sockets become `Marker3D`), moves every movable part about its pivot and asserts that everything else stays put, runs the physics check and renders every review view of the profile.
5. **FINAL-REVIEW** blocks for your visual decision. Review the captures, then `gamefactory approve APPROVAL_ID` and `gamefactory resume WORKFLOW_ID`.
6. **EVIDENCE** records the result.

## 5. Export and verify the evidence

```bash
gamefactory report --workflow WORKFLOW_ID
python -I .gamefactory/reports/WORKFLOW_ID/snapshot-001/verify_asset_bundle.py \
  .gamefactory/reports/WORKFLOW_ID/snapshot-001
```

The `asset-evidence-0.7.0` bundle contains the retained source, the registration, the normalization record, the processed GLB, the reports, the captures, the final approval receipt and a `production-receipt-0.7.0`. The verifier needs only the Python standard library. It rejects `paid: true`, any concept or provider role, and any mismatch between the processed GLB and "retained source × the declared rotation".

## Provider path

`character@1` is a single mesh and still uses `asset create` with a concept, the paid request snapshot and the paid approval; it gets a capsule collider instead of a box, and Blender exports no collider mesh for it. A specification that declares parts or sockets, uses `local_operator_assembly` or binds an assembly profile is rejected by `asset create` and by both provider adapters before any snapshot, approval or intent.
