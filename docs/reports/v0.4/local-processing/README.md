# Local Blender processing evidence

These files record actual Blender 5.2.1 LTS background runs of the committed
processing script on the deterministic synthetic GLB fixture. They prove local
processing and glTF axis conversion only; they are not Meshy-generated asset
acceptance evidence.

`standard-*` targets 1.2 × 1.0 × 1.0 m. `axis-probe-*` targets 1.2 × 0.8 ×
1.6 m so height and depth cannot be confused. The validator measures the
exported glTF world bounds as X=1.2, Y=1.6, Z=0.8 m for the probe. Blender's
internal coordinate convention is Z-up; the Blender glTF exporter converts the
final output to Godot/glTF +Y-up.

The current path guards and report-hash contract were rerun under
[`verified-run-02`](verified-run-02/host-evidence.json). Earlier root-level
files are preserved as historical records and may contain the earlier script
hash.

Both output reports include the immutable raw hash, processed hash, Blender
version, processing script hash, duration, exit code, and effective LOD ratio.
The paired validation JSON and human-readable report were produced from the
processed GLB bytes with the Python validator.

The verified command was equivalent to:

```powershell
python -m pytest tests/unit/test_glb_validator.py tests/integration/test_blender_processing.py `
  --basetemp=.verification/v04-local-tests -p no:cacheprovider -q
```

The integration suite includes actual processed-output mutations that remove
the collider and scale LOD0 by 10×; the collider and bounds rules fail as
expected. It also validates malformed GLB/accessor data, material and texture
budgets, missing LOD, missing UVs, external image references, and geometry-backed
LOD/collider checks.
