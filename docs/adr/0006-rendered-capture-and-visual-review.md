# ADR 0006: Rendered capture, checkpoint sync, and human visual review

## Status

Accepted for V0.3.

## Context

V0.2 can show that a headless scene reached an expected integer state. That does not show what the viewport drew. A PNG from another attempt, a headless dummy renderer, or a technical PASS must not be treated as a person's approval of the picture.

## Decision

1. Keep one Godot executor and the existing workflow engine. Capture is a separate task chain: `godot_capture_execute`, `godot_capture_validate`, `godot_visual_review`, then `record_evidence`. Import may stay headless. The image run uses a windowed `gl_compatibility` / `opengl3` context. Dummy or headless rendering fails the capture.
2. `doctor` records `engine.godot.rendered_capture` as not verified and does not open a window. Display and authority variables are passed only on Linux, and only `DISPLAY`, `XAUTHORITY`, and `LIBGL_ALWAYS_SOFTWARE` when present.
3. The harness deep-copies gameplay state, presents that state, waits for a bounded number of `RenderingServer.frame_post_draw` signals, reads the root viewport, and only then continues the simulation. Warm-up frames do not advance ticks. The whole `SceneTree` is not paused.
4. Python decodes the PNG with Pillow and hashes the bytes. Engine-reported paths and hashes are not authoritative. Fixture region checks apply only to `visual-fixture` and `visual-fixture-portrait`.
5. Visual review is its own approval type and always waits for a decision, even when process execution does not. The fingerprint covers capture and report hashes, not the approval row. A later receipt records the decision. Rejecting the review fails the task and does not launch another capture. The model already has changes-requested; this workflow uses reject plus a new workflow rather than a silent recapture.

## Consequences

Same-GPU pixel hashes are not promised. Software rendering on Linux is recorded as software rendering. A pending human review is a successful stop, not a missing feature.
