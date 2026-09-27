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

## Supported Blender

Minimum supported Blender is 4.0.2. ADR 0004 still allows the 4.x and 5.x
range. CI acceptance uses the Ubuntu `blender` apt package on `ubuntu-latest`
together with `python3-numpy`. On the current GitHub image that package is
4.0.2. CI does not download an unpinned newer build.

A clean Ubuntu 24.04 run of `blender 4.0.2+dfsg-1ubuntu8` exported a nonempty
processed GLB for the offline acceptance fixture. That is why 4.0.2 stays
inside the contract. A newer local Blender is allowed when this same processing
script finishes the export. Blender 4.3.2 on Linux and 5.2.1 on Windows already
met that contract. They are not a second processing policy.

`BlenderAssetProcessor` always passes `--python-exit-code` with
`BLENDER_PYTHON_FAILURE_EXIT_CODE`. A Python exception in the processing script
is a non-zero process exit. A zero exit that leaves no nonempty processed GLB
is still a failure. Blender exit status alone is not a successful export.

## Python dependency resolution and preflight

Blender embeds a Python interpreter (or links to system libpython) to run scripts
and internal add-ons such as `io_scene_gltf2`. Debian/Ubuntu Blender packages embed
the system Python 3.12 and resolve their Python executable by locating the first
`python3.12` binary found on the `PATH`. In CI environments like GitHub Actions,
`actions/setup-python` prepends a toolcache Python binary to `PATH`. Although
`ProcessRunner` maintains a minimal environment, it preserves `PATH` for essential
tool discovery, which redirects Blender's `sys.prefix` to the toolcache Python. As
a result, `/usr/lib/python3/dist-packages` (where system packages like `python3-numpy`
reside) is excluded from Blender's `sys.path`, causing glTF import
(`io_scene_gltf2/blender/imp/gltf2_blender_mesh.py`) to fail with:
`ModuleNotFoundError: No module named 'numpy'`.

To resolve this deterministically without relaxing generic environment isolation:
- `GAMEFACTORY_BLENDER_PYTHONPATH`: An explicit environment variable containing
  `os.pathsep`-delimited absolute paths to Python module directories. When configured,
  `BlenderAssetProcessor` injects these paths into the Blender process execution via
  request-scoped `PYTHONPATH` overrides. Host `PYTHONPATH` and `PYTHONHOME` are never
  read or forwarded globally.
- **Blender dependency preflight**: Before writing the processing contract or launching
  the processing command, `BlenderAssetProcessor` runs an automated preflight probe
  (`run_blender_dependency_preflight`). The probe executes non-destructively in
  background mode (`--python-expr`), verifies the Blender version (minimum 4.0.2),
  queries runtime metadata (`sys.executable`, `sys.prefix`, `sys.path`), and checks
  that all required modules (`numpy`) are importable. Preflight results are cached per
  processor instance. If preflight fails, a `DccFailedError` is raised with detailed,
  actionable diagnostics and the processing command is never run.
