# Generalized asset production

The production sequence is unchanged from V0.4:

Asset specification, concept, human concept approval, paid-generation approval, provider, raw asset, Blender processing, validation, Godot integration, runtime verification, rendered evidence, human review, production receipt.

V0.5 supplies the class-specific parameters from the bound profile.

## Processing

Blender receives a processing contract: target dimensions, origin, LOD requirement, collider policy, and tolerance. The script does not branch on asset id. Shared steps are load, inspect, unit and orientation normalization, allowed transforms, output names, and GLB export.

## Validation

Findings keep the same fields: rule id, severity, expected, actual, artifact, message. Common rules cover a parseable GLB, mesh presence, materials, and textures. Profile rules cover required LOD nodes, collider geometry, origin, and, for `modular_piece`, snap alignment (`dimensions.snap`).

## Godot

The staged scene follows the profile contract:

- `static_prop` and `modular_piece`: `Node3D` / `Visual` / `StaticBody3D` / `CollisionShape3D`, with a physics ray
- `pickup`: `Node3D` / `Visual` / `Area3D` / `CollisionShape3D`, without a physics ray

V0.5 does not generate gameplay scripts. A pickup scene proves visibility, bounds, and a collision shape. It does not collect items.

## Evidence

New bundles use `asset-evidence-0.5.0` and include `production-receipt-0.5.0`. The receipt records asset, revision, profile version, specification and concept hashes, provider request fingerprint, task id, cost, raw and processed hashes, validation and runtime hashes, render hashes, approval ids, and completion time.

`asset-evidence-0.4.0` bundles still verify. The reader adapts them to the same receipt fields in memory and does not rewrite the historical files. HTML may link only paths that are in the manifest.

## Commands

```bash
gamefactory asset profiles
gamefactory asset create --spec spec.yml --concept concept.png --provenance provenance.json --provider meshy
gamefactory asset inspect ASSET_ID
gamefactory doctor
```

`asset create` is the same workflow as `asset-create`. `doctor` separates asset profiles from tool capabilities. Meshy remains "configured / paid approval required" when the CLI is present. Credential values are not printed.

## Cost

Profile selection does not change paid policy. A paid approval fingerprint includes the revision, profile version, specification, concept, and provider request. Tests and CI use the fake provider. A production Meshy call still requires a separate human approval and resume.
