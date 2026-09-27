# ADR 0004: Adapter and Provider Architecture

## Status
Accepted

## Context
AI Game Factory must integrate with heterogeneous external tools and services:
- Game engines: Godot 4.x (with planned future support for Unity, Unreal)
- DCC tools: Blender 4.x/5.x
- External asset generation providers: Meshy (paid 3D generation)
- Local command execution and filesystem interactions

To prevent vendor lock-in and preserve clean inward dependency boundaries, core domain logic must never import vendor SDKs, Godot-specific scripts, or external APIs directly. Adapters must be strictly segregated from the domain core, and fake implementations must never masquerade as real production integrations.

## Decision
We implement a **Capability-Oriented Adapter Architecture** adhering to the Inversion of Control principle.

Key design points:
1. **Inward Interfaces**:
   - `EngineAdapter`: V0.1 defines `detect_engine()` and `inspect_project()`.
   - `DccAdapter`: V0.1 defines `detect_tool()`.
   - `AssetGenerationProvider`: the inward `workflows/ports.py` contract defines `name`, `cost_class`, `is_configured()`, and `generate()`; concrete provider adapters implement it.
2. **Capability Registry**:
   - Capabilities are cataloged centrally (e.g. `engine.godot.detect`, `dcc.blender.detect`, `asset.3d.generate`).
   - Capability states include `AVAILABLE`, `UNAVAILABLE`, `MISCONFIGURED`, and `APPROVAL_REQUIRED`; the last indicates policy status, not proof that a production integration exists.
   - Discovery does not assume tool availability: missing executables result in `UNAVAILABLE`; an unimplemented provider remains `UNAVAILABLE` regardless of credential presence.
3. **Real Tool Detection for Godot & Blender**:
   - `GodotAdapter` performs real detection via `PATH`, explicit config overrides or `GAMEFACTORY_GODOT_PATH`, conventional install locations, and version inspection via `--version`. It inspects `project.godot` without modifying it. V0.1 does not run a game.
   - `BlenderAdapter` performs real detection via `PATH`, explicit config, and version inspection via `blender --version`.
4. **Segregation of Fakes**:
   - `FakeAssetGenerationProvider` is isolated in `gamefactory.adapters.fakes`. V0.1 does not add unused fake agent or command-runner classes; tests inject bounded collaborators when needed.
   - Fake adapters are explicitly named and only used in automated tests and designated demo workflows. Real production paths never return dummy success.
5. **Meshy V0.1 Boundary**:
   - In V0.1, `MeshyProvider` is an unavailable boundary with known `PAID` classification and approval requirement. No Meshy transport is implemented, and credentials do not change availability.

## Consequences
### Positive
- The core domain and workflow lifecycle do not depend on a named provider. Built-in task actions live separately from scheduling, policy, and durable state handling.
- Unit and integration tests run fast, reliably, deterministically, and with zero external network or financial dependencies.
- Adding future engines or DCC tools requires only implementing their respective adapter contracts without touching core workflow state machines.

### Negative / Trade-offs
- Writing adapters requires defining normalized data transfer objects (DTOs) for engine/tool results instead of passing raw tool output directly into core domain structures.

## Alternatives Considered
1. **Monolithic Tool Helpers**:
   - *Pros*: Faster to write initial helper functions (`run_godot()`, `run_blender()`).
   - *Cons*: Tightly couples core code to specific tool binaries and CLI arguments; difficult to mock or test across platforms; violates inward dependency principles.
2. **Dynamic Plugin Marketplace / Remote Class Loading**:
   - *Pros*: Decoupled third-party packages installed on demand.
   - *Cons*: Massive premature complexity in V0.1; security risks around remote code loading; explicitly deferred in specification section 30.
