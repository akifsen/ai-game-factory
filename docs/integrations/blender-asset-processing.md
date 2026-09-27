# Blender static prop processing

AI Game Factory processes the V0.4 `static_prop` profile with Blender in
background mode. It does not use GUI automation. The managed raw GLB stays
immutable; processing writes a separate GLB and a JSON report containing input
and output SHA-256 hashes, Blender version, processing script hash (recorded by
the adapter), duration, exit code, dimensions, triangle counts, and effective
LOD ratio.

The supported coordinate contract is Godot/glTF: meters, +Y up, +X right,
-Z front. Dimensions are measured as X width, Y height, Z depth. The configured
origin can be `bottom_center` or `center`. Mesh node transforms are baked before
export. Output nodes are `SM_<asset_id>_LOD0`, `SM_<asset_id>_LOD1`, and
`COL_<asset_id>`. The collider is a box mesh with real geometry, sized to the
normalized visual bounds. LOD1 uses the specification's editable
`geometry_budget.lod_ratio` (default 0.5); no remesh, UV rewrite, repaint, or
LOD0 decimation is performed.

```python
from pathlib import Path

from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.assets.glb_validator import validate_glb

processed = Path("processed.glb")
result = BlenderAssetProcessor().process_asset(raw, processed, spec)
if result.status != "SUCCESS":
    raise RuntimeError("Blender processing failed")
validation = validate_glb(processed, spec)
if not validation.passed:
    raise RuntimeError(validation.summary)
```

Before Blender starts, a bounded GLB reader rejects malformed chunks,
out-of-bounds accessors, unsupported extensions, animation/skin data,
external buffers and textures, missing/corrupt embedded images, and unsupported
static mesh structures. The validator independently reads the resulting GLB and
decodes index and POSITION data to measure world-space bounds. It checks actual
LOD and collider geometry, triangle/material/texture budgets, scale, origin,
and residual transforms. Accessor-provided min/max values and node names alone
cannot satisfy geometry checks. The validator handles only the safe static
triangle subset needed by this profile; unsupported glTF features produce a
FAIL finding rather than a guessed PASS.

Raw GLBs are limited to 50 MiB, one million accessor elements, 100,000 total
triangles, 100,000 nodes with maximum hierarchy depth 128, and embedded images
up to 16,384 pixels on an edge / 100 million pixels total. The source is also
rejected before Blender when its LOD0 triangle, material, or texture budgets
already exceed the specification; this processor does not decimate LOD0 or
rewrite textures to hide an over-budget source.

Each validation finding has a rule ID, PASS/WARNING/FAIL severity, expected and
actual values, artifact path, and message. `AssetValidationResult.to_dict()`
is the persistence format; `render_asset_report()` creates a readable text
summary bound to raw and processed hashes. Blender exit status alone is not
validation evidence.

Real Blender processing is an integration check. Tests skip it when Blender is
unavailable; a fake GLB fixture validates the parser and mutations but does not
count as real Blender acceptance. No paid provider call is made by this stage.
