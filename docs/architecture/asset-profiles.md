# Asset profiles

An asset profile is the processing contract for a class of asset. An asset specification is the value set for one asset. The workflow engine remains the lifecycle authority.

## Registry

The built-in registry is explicit Python registration of three documents:

| Profile | Version | Category | Godot body | Review views |
|---|---|---|---|---|
| `static_prop` | 1 | `prop` | `StaticBody3D` | front, three_quarter, side |
| `pickup` | 1 | `pickup` | `Area3D` | front, three_quarter, top |
| `modular_piece` | 1 | `modular` | `StaticBody3D` | front, side, three_quarter |

`gamefactory asset profiles` also lists `character`, `rigged_character`, `weapon`, `vehicle`, `building`, `terrain`, `animation`, `vfx`, and `foliage` as **UNSUPPORTED**. That status is not a promise of later support.

Schema `asset-profile-0.5.0` is separate from `asset-spec-0.4.0` and `asset-spec-0.5.0`. Profile documents reject unknown fields. There is no entry-point loading and no remote profile download.

## What a profile owns

- dimension range, snap grid, and tolerance
- LOD and collider policies that are actually implemented (box collider only in V0.5)
- origin policy
- Godot node contract and runtime requirements
- review views and screen-fraction framing

Camera placement uses the asset bounding box, the view, the field of view, and the profile framing target. It does not store coordinates for a particular asset.

## What a specification owns

Identity, measured dimensions, material and texture budgets inside the profile limits, style constraints, and the import path. Node names are templates (`SM_{asset_id}_LOD0`, optional LOD1, `COL_{asset_id}`), not special cases.

## Binding

A stored revision keeps `profile_id` and `profile_version`. Approval fingerprints include both. A V0.4 specification has no `profile_version` field in its canonical dump and binds to `static_prop@1` when read.
