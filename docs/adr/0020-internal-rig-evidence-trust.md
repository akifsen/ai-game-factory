# ADR 0020: Internal rig evidence trust model

## Status

Accepted (V0.8-2 slice).

## Context

V0.8-1 established bounded internal skin validation and a real Godot deformation oracle, but did not export portable evidence or provide a cold verifier. Operators need hash-bound bundles that recompute static validation and runtime displacement metrics without importing `gamefactory`.

## Decision

1. Export `rig-evidence-0.8.0` bundles with manifest roles for the skinned GLB, pinned verification contract, validation report, runtime request/observation, reviewed Blender export script, reviewed Godot harness, and source declaration.
2. Ship `verify_rig_bundle.py` as a **stdlib-only** script (byte-identical under `scripts/` and package resources). It embeds the same GLB decode/validation ordering as `internal_skin` and pins reviewed contract/tool digests.
3. Cold outcomes (separate fields in the JSON summary):
   - **`integrity_outcome` = VERIFIED** — manifest integrity, recomputed static findings match the bundled report, runtime bindings, GLB-backed vertex samples, and reviewed pins match.
   - **`execution_provenance` = CONSISTENT_BUT_UNAUTHENTICATED** — always for this unsigned slice (offline synthetic fixtures, real Blender, and real Godot). Tool/machine provenance is never authenticated without an external trust anchor.
   - **`outcome` = CONSISTENT_BUT_UNAUTHENTICATED** — conservative overall label for any valid complete unsigned bundle that passes integrity checks.
   - **FAILED** — any hash/path tamper, forged validation PASS, binding mismatch, non-pinned contract/tools, or static/runtime FAIL.
4. `source_declaration.positive_fixture_glb_sha256` is a **claim** bound to schema and reviewed contract digests; it does not authenticate execution. `reviewed_pins_match` reports pin equality only.
5. Cold verification must be invoked from a **trusted** installed copy of `verify_rig_bundle.py` outside the evidence bundle; bundled copies are not executed.
6. Trust is **technical integrity**, not human identity. External approval remains out of scope for this slice.

## Runtime request bindings (V0.8-2 revision)

- `runtime_request_digest` hashes a canonical copy of the bound request: Godot-parsed JSON may represent `vertex_count`, `affected_vertex_count`, and `unaffected_vertex_count` as floats; harness and Python both coerce these fields to strict integers before `JSON.stringify` / `json.dumps`. Counter fields reject booleans and non-integral finite values at digest time.
- `region_boundary_tolerance` is pinned to `1e-6` (`REGION_BOUNDARY_TOLERANCE`) in the pre-execution request and included in the digest. Digest serialization rewrites only that field’s numeric literal to Godot’s `0.000001` form; other strings and numeric fields are untouched.
- Region **membership** (population counts and displacement sampling) uses inclusive box membership with epsilon **only** on min/max face comparisons. For each axis \(i\), a rest position \(p\) is inside \([\mathit{min}, \mathit{max}]\) when \(p_i \ge \mathit{min}_i - \varepsilon\) and \(p_i \le \mathit{max}_i + \varepsilon\) with \(\varepsilon =\) `REGION_BOUNDARY_TOLERANCE` (`1e-6`). Displacement thresholds (`min_affected_displacement` `0.04`, `max_affected_displacement` `0.45`, `max_unaffected_displacement` `0.008`) and rest-to-GLB matching (`1e-3`) are unchanged.
- Numerical membership and counter-domain rules for this slice were reviewed in [ChatGPT design thread](https://chatgpt.com/c/6abc52b8-e850-83ed-9e42-004410a5fdbf) (numerical design approval only; not V0.8-2 closure).

## Consequences

- Evidence export uses `validate_internal_skinned_glb` and optional `run_skin_deformation_oracle`.
- Production profiles and `rigged_character` availability remain unchanged until a later promotion ADR.
