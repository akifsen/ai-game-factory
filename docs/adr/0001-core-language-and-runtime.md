# ADR 0001: Core Language and Runtime

## Status
Accepted

## Context
AI Game Factory is a development-time orchestration platform that coordinates AI workers, game engines (primarily Godot), DCC tools (primarily Blender), asset-generation providers, and validation tools.
The platform must run cross-platform on Windows and Linux (with macOS compatibility where natural), manage child processes safely without shell injection, operate with durable local persistence, handle JSON and YAML configurations, and provide a robust developer CLI.

Key requirements:
1. Python 3.11+ modern typed runtime with conservative dependency requirements.
2. Standard library-first approach for CLI, SQLite persistence, process execution, and filesystem operations.
3. Windows as a first-class target (paths with spaces, backslashes, `.exe` extensions, non-blocking process management).
4. Deterministic workflow execution with clear inward dependency boundaries.
5. Strict typing and schema validation.

## Decision
We select **Python 3.11+** (specifically tested on Python 3.12 in the host environment) as the core implementation language and runtime.
- **CLI**: Standard library `argparse` with structured formatters, exit codes, and `--json` support.
- **Typing & Validation**: `pydantic` v2 and standard library `dataclasses` / `typing` for domain models and strict schema validation; `pyyaml` for YAML parsing.
- **Process Management**: Standard library `subprocess` with `shell=False`, argument arrays, explicit working directories, sanitized/minimal environment variables, and timeouts.
- **File System**: Standard library `pathlib.Path` for cross-platform path handling, with custom path normalization and jail checks to prevent traversal.

## Consequences
### Positive
- Universal support across Windows and Linux without native binary compilation.
- Rich integration ecosystem for tooling (Blender uses Python scripting natively, Godot interacts cleanly via subprocess and GDScript/C#).
- Fast test execution via `pytest` and comprehensive static analysis via `ruff` and `mypy`.
- Clean package distribution via `pyproject.toml` and entry points.

### Negative / Trade-offs
- Interpreted language performance: workflow orchestration does not require sub-millisecond execution, but careful handling of disk I/O and SQLite transactions is required.
- Global Interpreter Lock (GIL): parallel execution in future milestones will use subprocesses rather than shared-memory threads, aligning with our process-isolated worker model.

## Alternatives Considered
1. **Rust**:
   - *Pros*: Excellent static safety, zero-cost abstractions, single binary distribution.
   - *Cons*: Slower iteration during early design, complex interop with Python-based DCC tools (Blender scripting), higher barrier to entry for game developers authoring custom workflows.
2. **Go**:
   - *Pros*: Fast compilation, single binary, simple concurrency.
   - *Cons*: Less natural fit for the Python-dominated AI / DCC tooling ecosystem; reflection-based typing is less expressive for dynamic task contracts than Python with Pydantic.
3. **TypeScript / Node.js**:
   - *Pros*: Rapid CLI development, strong JSON ecosystem.
   - *Cons*: Process management on Windows has historical quirks with command quotation; weaker alignment with local scientific/AI/Blender libraries.
