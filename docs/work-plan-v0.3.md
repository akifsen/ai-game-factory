# V0.3 work plan

V0.2 is already closed at `fef168c` / CI `36290153250`, with the closeout record at `920af9e`. This plan does not reopen that closeout. V0.3 starts from the `920af9e` tree and adds rendered capture.

## Dependencies

- Existing staging, `ProcessRunner`, SQLite workflow engine, artifact hashes, and approval fingerprints.
- Installed Godot 4.7.2 with `--display-driver`, `--rendering-driver`, `--rendering-method`, and `--windowed`.
- Pillow for full PNG decode. It is a package dependency. Core workflows that do not capture images still run without a Godot renderer.
- Headless verification stays on scenario schema 0.2.0. Capture uses schema 0.3.0.
- No database migration. Package version 0.3.0, project config schema 0.1.0, and scenario schemas stay separate.

## Slice

1. One real windowed Compatibility capture, fully decoded in Python, through the approved process runner.
2. Four checkpoints bound to the same attempt's state, plus the 720×1280 profile as a separate workflow.
3. Independent image checks, static HTML, and a visual-review approval that is not a process approval.
4. Windows acceptance and a clean wheel install. Linux Xvfb/llvmpipe is a separate CI job and is not claimed until that job runs.

## Out of scope

Vision or image-generation APIs, Meshy, Blender processing, screenshot-diff platforms, a web app, mobile certification, and a new workflow engine.
