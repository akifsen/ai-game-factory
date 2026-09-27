# AI Game Factory

AI Game Factory is local development tooling for coordinating bounded game-development workflows. It stores workflow state, task attempts, artifacts, evidence, approvals, and policy decisions locally. A managed game remains usable without the Factory installed.

V0.1 is an orchestration foundation. It provides SQLite persistence, sequential DAG workflows, approval gates, local fake demo providers, Godot and Blender detection, and an argparse CLI. It does not implement Meshy generation, Godot project execution, automatic Blender processing, or AI workers.

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

Godot and Blender are optional. `doctor` reports availability and versions; it does not claim that detection implies execution support. Meshy is classified as paid but unavailable because generation is not implemented. Never run paid external operations with this milestone.

See [architecture overview](docs/architecture/overview.md), [integration status](docs/integrations/status.md), [development workflow](docs/development/README.md), [acceptance requirements](docs/requirements/03-acceptance.md), and [deferred boundaries](docs/requirements/future-boundaries.md).

The [documentation index](docs/README.md) links the detailed implementation plan,
all 123 requirement sections, and the [V0.1 verification report](docs/reports/v0.1-completion-report.md).
