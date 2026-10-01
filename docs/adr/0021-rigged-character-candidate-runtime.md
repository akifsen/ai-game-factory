# ADR 0021: rigged_character CLOSED candidate runtime design

## Status

Accepted for V0.8-3A foundation (static validation). **V0.8-3B** implements the CLOSED candidate capsule + nine-view Godot runtime slice (request digest, observation verification, candidate-only harness). **V0.8-3C1** implements the local WorkflowEngine DAG through TEST_ONLY review and an injectable C2 evidence export hook (full envelope and trusted cold verifier remain a separate C2 contract). Workflow DAG, evidence envelope, and production promotion remain **not implemented** beyond C1 scope.

## Context

V0.8-2 delivered internal rig evidence, a frozen `humanoid_12bone_v1` contract, and cold verification for a deterministic `SM_HumanoidSkin` fixture. V0.8-3A adds an explicit CLOSED candidate profile/spec contract and candidate-only static validation. Production `rigged_character` remains **UNSUPPORTED** in the public registry; candidate receipts are never production-eligible.

## Decision

1. **Visual identity.** The processed GLB carries exactly one skinned visual mesh named `SM_HumanoidSkin`. No `SM_<asset_id>_LOD0` fallback and no legacy `bottom_center` origin policy. The immutable reference frame is `origin_contract=humanoid_reference_root` (`HumanoidRoot` as the sole scene root).

2. **Capsule center (static binding, runtime observation).** After static validation, Python computes the capsule center as the midpoint of the validated visual AABB in the reference-root frame:
   - `c_i = (min_i + max_i) / 2` for `i ∈ {x, y, z}`.
   - A future runtime request digest binds center, bounds, capsule shape, processed GLB SHA-256, and contract version. Godot observes the declared center without reselecting or repositioning the GLB. Cold verification independently recomputes the same center from bundle bytes.

   **Capsule envelope fit (static, V0.7 structural semantics).** The declared capsule is a structural fit against the asset height and the maximum horizontal extent of the envelope, not full axis-aligned containment on every horizontal axis. Static validation requires `height_m <= measured_height + tolerance` and `2 * radius_m <= max(measured_width, measured_depth) + tolerance` (and the same against declared spec dimensions). It does **not** require `2 * radius_m <= min(width, depth)`; shallow depth (for example canonical Blender export depth ≈ 0.12 m) remains valid when the larger horizontal extent carries the diameter bound.

3. **GLB byte identity.** Raw-to-processed pipeline must not alter skeleton, rest pose, weights, inverse bind matrices, or root hierarchy. Runtime adds `StaticBody3D` and `CapsuleShape3D` as siblings outside the GLB.

4. **Oracle thresholds (frozen).** Rig deformation oracle keeps V0.8-2 thresholds: min affected displacement `0.04`, max affected `0.45`, max unaffected `0.008`, rest tolerance `1e-3`, region membership epsilon `1e-6`.

5. **Candidate-only validation composition.** Static groups run in fixed order: `core_v08_candidate` → `rig_skin` → `collider_capsule`. `runtime_rig_oracle` is a separate later gate.

6. **Promotion boundary.** Test-only / `CANDIDATE_ONLY` receipts cannot satisfy production promotion (`production_eligible=false`). Candidate readiness and unsigned execution provenance remain distinct.

## Planned follow-ons (out of V0.8-3A scope)

- Nine-view capture harness for the candidate profile
- Candidate `WorkflowEngine` DAG and evidence envelope
- Public CLI surfacing and registry promotion

## Consequences

Engineers implement workflow, Godot staging, and evidence against this ADR after the CLOSED contract and static validator are reviewed. The V0.8-3A Python module `v08_candidate_geometry.capsule_center_from_aabb` is the authoritative static formula until request digest binding lands.
