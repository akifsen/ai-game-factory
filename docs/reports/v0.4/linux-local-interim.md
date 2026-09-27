# V0.4 interim Linux Blender check

Status: **INTERIM live-checkout evidence only. This is not frozen-source acceptance.**

Executed in the prepared `gamefactory-v04-linux-verify` container, using its existing `/tmp/v04-venv` and installed Blender 4.3.2. No dependencies were installed and no provider calls were made.

Command:

```sh
cd /workspace
PYTHONPATH=/workspace/src /tmp/v04-venv/bin/python -m pytest \
  tests/integration/test_blender_processing.py \
  tests/unit/test_blender_processor_paths.py \
  tests/unit/test_glb_validator.py \
  --basetemp=/tmp/v04-local-interim-rerun -p no:cacheprovider
```

Result: **10 passed, 2 failed**. All Blender path-safety and GLB validator unit tests passed. Both real Blender processing tests failed because Blender's bundled glTF importer cannot import `numpy` (`ModuleNotFoundError: No module named 'numpy'`). Blender exits with status 0 despite that importer traceback, and consequently produces no processed GLB. This points to a missing dependency in the prepared Linux Blender environment; it does not establish a source regression.

Evidence:

- [pytest output](linux-local-interim.log)
- [direct Blender diagnostic](linux-local-interim-blender.log)

The Linux environment must provide `numpy` to Blender's embedded Python (or a Blender build with a working glTF importer) before these real-processing checks can pass. Rerun against a frozen source snapshot after correcting the environment; these results do not count as final Linux acceptance.
