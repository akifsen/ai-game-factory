# V0.4 interim Linux Blender check after environment fix

Status: **INTERIM live-checkout verification. This does not constitute frozen-source acceptance.**

Blender 4.3.2 embeds Python 3.13.5 and its bundled glTF importer requires NumPy. The first run failed because NumPy was missing from Blender's Python module search path. The direct diagnostic is recorded in [linux-local-interim-blender.log](linux-local-interim-blender.log); the initial failing pytest run is [linux-local-interim.log](linux-local-interim.log).

Environment-only correction, inside disposable container `gamefactory-v04-linux-verify`:

```sh
apt-get update
apt-get install -y --no-install-recommends python3-numpy
```

Installed Debian package: `python3-numpy 1:2.2.4+ds-1` (plus its required `python3-numpy-dev` package). Verified using Blender's own embedded runtime:

```sh
blender --background --factory-startup --python-expr 'import numpy; print("BLENDER_NUMPY_VERSION=" + numpy.__version__)'
```

Output identified Blender 4.3.2 and `BLENDER_NUMPY_VERSION=2.2.4`.

The interim tests were then rerun without changing source:

```sh
cd /workspace
PYTHONPATH=/workspace/src /tmp/v04-venv/bin/python -m pytest \
  tests/integration/test_blender_processing.py \
  tests/unit/test_blender_processor_paths.py \
  tests/unit/test_glb_validator.py \
  --basetemp=/tmp/v04-local-interim-fixed -p no:cacheprovider
```

Result: **12 passed in 5.31s**, including both real Blender processing tests. Full output is in [linux-local-interim-fixed.log](linux-local-interim-fixed.log). The package installation affected only the disposable verification container; final frozen-source Linux verification remains a separate step.
