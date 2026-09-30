# ADR 0014: Normative orientation and review views

## Status

Accepted for V0.7 (Step 2 architecture decision). Normative review-view placement is implemented for the existing runtime path; source-front normalization and its audit trail remain pending.

## Context

The factory has used +Y up and −Z front since V0.4. `OrientationConfig` only accepts `up: +Y, front: -Z`, and the Blender processor rejects anything else. The convention has never been written down as a decision.

Verified in the repository (V0.6.0, `b8b3334`):

- `camera_framing.view_direction` returns the unit vector **from the asset center toward the camera**. `front` is `(0, 0, −1)`, so the front camera images the −Z face.
- `side` is `(+1, 0, 0)`. For an asset facing −Z with +Y up, that is the asset's own right side.
- The V0.7 spike's muzzle socket pointed along −Z. −Z is also Godot's node forward (`-basis.z`, and the direction `look_at` turns toward).
- `PLACED_VIEWS` is `front, three_quarter, three_quarter_front, side, top`. `rear`, `left`, `right` and `three_quarter_rear` are named in `IMPLEMENTED_VIEWS` but have no placement.
- The Godot harness helper `_view_direction_vector` (`asset_runtime_harness.gd`) returns the three-quarter vector for any view it does not recognize, and its axis label defaults to `+X-Z`. This is a silent fallback.

Divergence that must be recorded: glTF 2.0 defines the asset front as **+Z**, and Godot 4.x `Vector3.MODEL_FRONT` is +Z. Blender's glTF exporter maps the Blender front view (−Y) to glTF +Z. A model authored facing Blender's front view therefore arrives facing +Z.

## Decision

1. **The canonical convention is unchanged: +Y up, −Z front for the asset root, right-handed, metres.** Socket forward is local −Z (ADR 0013). The factory keeps −Z despite glTF's +Z because V0.4–V0.6 evidence, receipts and approval fingerprints are bound to it, and because it matches Godot node forward.
2. **`source_front` normalization for authored assemblies.**
   - The source registration of every V0.7 operator-authored assembly declares `source_front: "-Z" | "+Z"` (ADR 0016).
   - If `source_front` is missing or has any other value, the source is **rejected** at registration. There is no default and no guess.
   - `-Z` means no normalization.
   - `+Z` allows exactly one deterministic rotation: 180° about +Y, quaternion `[0, 1, 0, 0]` (x, y, z, w), applied once at the assembly root. It is never applied separately to child parts. Because the rotation sits at the root, only root-level part transforms change. Every deeper parent-relative transform is untouched.
   - Normalization happens **before** pivot and socket validation. Declared pivots and sockets are always in the canonical frame.
   - No other correction is allowed: no arbitrary Euler correction, no orientation inferred from geometry, no best guess. A processed result that differs from "source × at most the one declared rotation" fails `orientation.source_front`.
3. **Auditability.** The processing report and `asset-evidence-0.7.0` record:
   - the declared `source_front`
   - `normalization_applied` (true or false)
   - the normalization transform (quaternion and 4×4 matrix, or identity)
   - the resulting canonical front (`-Z`)

   The retained source package is hash-bound (ADR 0016). The cold verifier recomputes the root-level transforms of the processed GLB from the source and the recorded transform, and rejects any mismatch.
4. **Meshy single-mesh sources** keep the V0.6 behavior. Their front is confirmed only by human review of the `front` capture, and the review states that limitation. Where a profile asserts a socket's rest forward, the validator provides a machine check of facing (ADR 0013).
5. **Review view placements.** Every view is placed explicitly, and each vector points from the asset center toward the camera:

   | View | Direction | Axis label |
   |---|---|---|
   | `front` | (0, 0, −1) | `-Z` |
   | `rear` | (0, 0, +1) | `+Z` |
   | `left` | (−1, 0, 0) | `-X` |
   | `right` | (+1, 0, 0) | `+X` |
   | `side` | (+1, 0, 0) | `+X` (legacy alias of `right`) |
   | `three_quarter`, `three_quarter_front` | (+1, 0.65, −1) normalized | `+X-Z` |
   | `three_quarter_rear` | (+1, 0.65, +1) normalized | `+X+Z` |
   | `top` | (0, 1, 0), up (0, 0, −1) | `+Y` |

6. **No fallback.** A view without a placement is an **explicit failure** at profile load, at capture-request build time, in the Godot harness and in cold bundle verification. The three-quarter fallback in the harness is removed. The Python and GDScript placement tables must agree, and a parity test compares every view's direction, up vector and axis label. Every placed view has a capture test.
7. **`side` compatibility.** `side` is a supported **legacy compatibility alias** of `right` for placement. It has its own explicit table entry and never reaches an unknown-view path. It is not deprecated, not an error and not removed. Historical 0.4, 0.5 and 0.6 artifacts keep exposing and verifying `side` exactly as before. `side`-specific behavior outside placement (the scale-reference check, correction-capture selection) is unchanged and is not extended to `right` by this decision. Historical manifests and view identifiers are never rewritten. New V0.7 profiles use `left` or `right` wherever direction matters.

## Consequences

The convention becomes a recorded decision with evidence. Authors of Blender assemblies either face the model toward Blender +Y or declare `+Z`, and forgetting the declaration is a rejection, not a guess. The review view vocabulary becomes fully placed. A typo in a view id can no longer silently produce a three-quarter image.

## Implementation status

All nine review views now have explicit Python and Godot direction, up-vector, and axis-label entries. Python profile loading and capture-request validation use the complete placed-view set; the Godot harness rejects an unknown or duplicate angle before opening the asset, and cold bundle verification already validates the requested view set against its closed vocabulary. The `side` placement entry remains an explicit alias of `right`; its scale-reference checks and correction-capture behavior are unchanged. The accepted `source_front` registration, normalization, evidence, and cold transform-recomputation requirements remain unimplemented.
