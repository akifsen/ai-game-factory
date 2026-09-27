# AI Game Factory

AI Game Factory is local development tooling for coordinating bounded game-development workflows. It stores workflow state, task attempts, artifacts, evidence, approvals, and policy decisions locally. A managed game remains usable without the Factory installed.

V0.2 adds real Godot headless scene verification with staged projects, bounded import/runtime processes, durable evidence and independent Python assertions. It preserves the V0.1 SQLite workflow, approvals, fake demo providers and CLI. Meshy generation, Blender processing and AI workers remain outside this release.

## Install and run

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
gamefactory --version
gamefactory --help
```

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
gamefactory --version
gamefactory --help
```

## V0.1 journey

From this repository or another directory, check local readiness before initialization:

```bash
gamefactory doctor
```

For an existing game project, run `gamefactory init` from its root or a subdirectory. Initialization creates `.gamefactory/factory.yml`, `discovery.json`, SQLite state, and lock storage without editing game source files. It is idempotent. Discovery records scene/script/test counts, main scene, engine version when available, export configuration, and Git-root presence.

```bash
cd examples/minimal-godot
gamefactory init
gamefactory status
gamefactory run demo
gamefactory approvals
gamefactory inspect WF-DEMO-12345678
gamefactory approve APP-12345678 --comment "Reviewed the fake generation step"
gamefactory resume WF-DEMO-12345678
gamefactory artifacts
```

Replace the example IDs with the workflow and approval IDs returned by the preceding commands.

`run demo` and `run paid-safety` use an explicitly named local fake provider and pause before its paid-classified operation. No real provider transport is available. `run failure` demonstrates a deterministic failure; use `retry <workflow> <task>` to create a new attempt. Workflow IDs are returned by `run` and `status`.

The CLI accepts `--project DIR` / `-p DIR` and explicit `--godot-path` / `--blender-path` overrides. Invalid explicit paths are reported as misconfigured and never fall back to another executable. Add `--json` before or after a command for compact JSON output. Exit codes are: `0` success, `1` configuration or usage error, `2` workflow failure, `3` approval blocked, `4` tool unavailable/misconfigured, and `5` internal error.

## Configuration and safety

The versioned project contract is `.gamefactory/factory.yml`; the JSON Schema is in `schemas/`. Unknown fields are rejected. User-wide defaults can be set in `%APPDATA%/gamefactory/config.yml` on Windows or `$XDG_CONFIG_HOME/gamefactory/config.yml` (falling back to `~/.config/gamefactory/config.yml`) on Linux/macOS. Precedence is defaults, user config, project config, then explicit command-line executable paths. Do not put credentials in either YAML file; provider secrets belong in environment variables, and no V0.1 command sends them anywhere. Local state is in `.gamefactory/state/factory.db`. State and config paths are checked against project-root escapes before use.

Godot is required only for `godot-verify`; other core workflows remain usable without it. Blender is optional and detection-only. `doctor` reports actual capabilities and versions. Meshy remains unavailable and no real paid-provider operation is implemented.

See [architecture overview](docs/architecture/overview.md), [integration status](docs/integrations/status.md), [development workflow](docs/development/README.md), [acceptance requirements](docs/requirements/03-acceptance.md), and [deferred boundaries](docs/requirements/future-boundaries.md).

The [documentation index](docs/README.md) links the detailed implementation plan,
all 123 requirement sections, and the [V0.1 verification report](docs/reports/v0.1-completion-report.md).

## V0.2 real Godot journey

Use a trusted local Godot project. The included fixture implements actual damage,
enemy and score behavior; its scenario expects HP 40, two enemies and score 100
at tick 90.

```bash
gamefactory --project examples/godot-verification init
gamefactory --project examples/godot-verification --json run godot-verify --scenario scenario.json
gamefactory --project examples/godot-verification inspect WORKFLOW_ID
gamefactory --project examples/godot-verification artifacts --workflow WORKFLOW_ID
```

Supply `--godot-path PATH` if detection cannot locate your executable. Configure
`policies.require_approval_for_process_execution: true` to require approval before
launch; use `approvals`, `approve` and `resume` as in the existing workflow. The
approved source/scenario/executable/harness fingerprint is checked before dispatch.
The package is 0.2.0; project config schema stays 0.1.0.

Godot runs a separate staged copy and writes observations without receiving the
assertion expectations. Python validates current-attempt evidence and checks the
assertions. Zero exit code alone cannot pass a gate. Unknown child-process state
after interruption stays blocked; terminal failures can create a fresh attempt
with `retry WORKFLOW_ID TASK_ID`. Historical logs remain available.

Staging is for source preservation and reproducibility, not an OS security sandbox.
The original game remains runnable without Factory or its harness. Verification
covers the bounded integer-state fixture contract, not visual quality, arbitrary
physics determinism, AI gameplay or mobile exports.

See the [V0.2 plan](docs/work-plan-v0.2.md),
[Godot contract and recovery guide](docs/integrations/godot-headless-verification.md)
and [V0.2 completion report](docs/reports/v0.2-completion-report.md).
