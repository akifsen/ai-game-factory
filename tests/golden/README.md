# Golden Test Suites

This directory contains deterministic backward-compatibility golden datasets and tests freezing V0.6 behavior.

## Frozen Suites

1. **`tests/golden/test_backward_compat_golden.py`** (backed by `golden_data.json`):
   - Freezes GLB validation ordered findings for built-in profiles (`static_prop@1`, `pickup@1`, `modular_piece@1`).
   - Freezes canonical specification model dumps and SHA-256 fingerprints for representative 0.4.0 and 0.5.0 specs.
   - Freezes built-in profile contracts (processing policy, runtime bounds, Godot body kind, camera framing, capture request profiles).
   - Freezes profile registry availability and parsed profile document representations.

2. **`tests/unit/test_v06_behavior_goldens.py`** (backed by `v06_asset_profile_behavior.json`):
   - Freezes literal specification fingerprints and canonical JSON dumps for historical specs.
   - Freezes paid request snapshot binding digests for external and fake providers.
   - Freezes built-in profile registry availability and document dumps.
   - Freezes GLB validator finding ordering, status, and summary across built-in profile cases.
   - Freezes historical schema compatibility (strict rejection of advanced identifiers, categories, policies, and V0.7-only fields).

## Provenance

Both suites and their corresponding JSON golden data files (`golden_data.json` and `v06_asset_profile_behavior.json`) were generated from commit `af0f379` (V0.6 behavior).

## Regeneration Instructions

Regeneration is **never automatic** and must never occur on import or during pytest test collection. `generate_goldens.py` writes output only when run explicitly as a standalone script:

```bash
python tests/golden/generate_goldens.py
```

## Governance

Regenerating either golden dataset requires an explicit architecture decision (ADR) and formal approval. Golden test data must never be edited or regenerated merely to make a failing backward-compatibility test pass; any failure in these suites indicates that historical behavior has changed.
