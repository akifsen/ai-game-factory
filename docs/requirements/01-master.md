# AI GAME FACTORY — MASTER BOOTSTRAP PROMPT

You are the Principal Software Architect, Engineering Lead, and initial implementation engineer for a new project named:

# AI Game Factory

Your task is to design and implement the foundation of a reusable, production-quality, agentic game-development orchestration platform.

This is NOT a game.

This is a development-time platform that coordinates AI agents, game engines, asset-generation systems, DCC tools, testing, visual review, performance validation, and human approval gates to create and improve games.

The first-class game engine is Godot.

Initial external integrations will include:

- Godot
- Blender
- Meshy through MCP or an adapter
- AI coding agents such as Codex
- image-generation systems
- vision-capable models
- optional audio-generation systems

However:

DO NOT tightly couple the core architecture to any specific AI provider, model provider, asset-generation provider, or coding agent.

The architecture must allow providers to be replaced.

---

# 1. PRIMARY OBJECTIVE

Build the foundation for a system where a user can eventually execute workflows conceptually similar to:

```bash
gamefactory new iron-bastion
gamefactory init
gamefactory plan
gamefactory status
gamefactory run
gamefactory test
gamefactory review
gamefactory build
```

and interact through natural-language instructions such as:

> Add a heavy enemy to the game.
> Follow the existing art direction.
> Generate a concept first.
> Do not spend Meshy credits without approval.
> Convert the approved concept into a 3D asset.
> Process it through Blender.
> Validate it.
> Integrate it into Godot.
> Test gameplay.
> Capture screenshots.
> Review visual quality and performance.
> Report the evidence.

The Factory must orchestrate this process rather than blindly executing arbitrary AI actions.

---

# 2. CORE PRINCIPLE

The platform must separate:

## Factory knowledge

How games should be developed.

From:

## Game knowledge

How a specific game should behave and look.

The Factory repository contains reusable orchestration, agents, pipelines, validators, adapters, templates, schemas, policies, and tooling.

Each game contains only its own Factory contract under something conceptually similar to:

```text
.gamefactory/
    factory.yml
    GAME_SPEC.md
    GAME_VISION.md
    CORE_LOOP.md
    ART_BIBLE.md
    UX_SPEC.md
    AUDIO_BIBLE.md
    PROGRESSION.md
    ASSET_MANIFEST.yml
    PERFORMANCE_BUDGET.yml
    FEATURE_MATRIX.yml
    ACCEPTANCE_CRITERIA.yml
```

Do not unnecessarily force all these files to exist immediately.

Determine which documents are mandatory, optional, generated, or introduced later.

---

# 3. NON-NEGOTIABLE ARCHITECTURAL RULE

AI Game Factory is DEVELOPMENT-TIME tooling.

A generated or managed game MUST NOT require AI Game Factory at runtime.

For example:

```text
game/
    project.godot
```

must continue functioning even if AI Game Factory is completely removed.

Never introduce Factory runtime dependencies into the shipped game unless a future feature explicitly requires one.

---

# 4. PRODUCT PHILOSOPHY

This project must not become:

- a pile of prompts;
- a giant shell script;
- a collection of hard-coded Codex instructions;
- a Godot-specific monolith;
- an MCP wrapper;
- an autonomous agent that can spend money without control;
- an unbounded recursive agent system;
- a workflow where "agent says done" means "task completed."

Instead, build a deterministic orchestration layer around probabilistic AI workers.

The system owns:

- workflow state;
- contracts;
- policies;
- dependencies;
- artifacts;
- evidence;
- approvals;
- budgets;
- execution history;
- quality gates.

Agents perform bounded work.

Agents never own the authoritative workflow state.

---

# 5. DESIGN DOCTRINE

Use these principles.

## Deterministic core, probabilistic edges

Workflow state transitions must be deterministic.

AI output may suggest actions but must not silently mutate authoritative workflow state.

## Evidence over claims

An agent saying:

> Tests passed.

is insufficient.

The system should eventually require executable or inspectable evidence.

Examples:

- test result;
- process exit code;
- build artifact;
- screenshot;
- profiler output;
- asset validation report;
- generated file hash;
- human approval record.

## Explicit state machines

Important workflows must have explicit lifecycle states.

Example:

```text
REQUESTED
SPECIFIED
WAITING_APPROVAL
APPROVED
GENERATING
GENERATED
PROCESSING
VALIDATING
INTEGRATING
VERIFYING
COMPLETED
FAILED
REJECTED
```

Do not blindly use this exact list everywhere.

Model appropriate state machines per workflow.

## Idempotency

Where reasonably possible, retrying a workflow step must not corrupt state or duplicate expensive work.

## Resumability

Long-running workflows must be resumable after interruption.

## Auditability

We must be able to answer:

- who requested this;
- which agent performed it;
- which provider/model/tool was used;
- which input was supplied;
- which artifacts were created;
- which command was executed;
- how long it took;
- whether it cost money;
- what evidence exists;
- who approved it;
- why it failed.

---

# 6. HUMAN-IN-THE-LOOP

The system is agentic but not recklessly autonomous.

Human approval must be a first-class domain concept.

Examples:

```text
game design approval
art-direction approval
concept approval
paid-generation approval
vertical-slice approval
release approval
```

Some workflows may combine approvals.

Design the approval system generically.

An approval should be capable of storing:

```text
approval type
requested timestamp
request context
artifact references
decision
decision timestamp
actor
comment
```

Support:

```text
APPROVED
REJECTED
CHANGES_REQUESTED
```

Do not fake approval.

---

# 7. COST SAFETY

External operations can consume money.

Examples:

- Meshy generation;
- image generation;
- audio generation;
- expensive model calls;
- future video generation.

Create a cost-policy concept.

Operations should be classifiable, for example:

```text
LOCAL
FREE_EXTERNAL
METERED
PAID
EXPENSIVE
```

Do not hard-code this exact taxonomy if a better model exists.

The system should eventually support:

```text
project budget
workflow budget
operation estimate
actual cost
provider
currency/credits
approval requirement
```

Critical rule:

PAID GENERATION MUST NOT OCCUR WITHOUT THE REQUIRED POLICY/APPROVAL CHECK.

For the initial implementation, real billing integration is not necessary.

But architecture and state must support it.

---

# 8. AGENT MODEL

Agents are specialized workers.

Initial conceptual roles include:

```text
Game Director
Game Designer
Gameplay Engineer
Game Architect
Art Director
Concept Artist
3D Asset Agent
Technical Artist
Level Designer
UI/UX Agent
Audio Agent
QA Agent
Visual QA Agent
Performance Agent
Build/Release Agent
```

Do NOT implement fifteen autonomous agents merely because they are listed here.

First design a generic agent capability model.

An agent definition should eventually be able to express:

```text
identity
role
capabilities
allowed tools
forbidden tools
inputs
outputs
quality expectations
cost class
approval requirements
timeout
retry policy
```

Agents should receive bounded task contracts.

Avoid giant global prompts.

---

# 9. GAME DIRECTOR

The Game Director is the primary orchestration-facing intelligence.

But:

The Game Director must NOT become a god object.

It may:

- interpret goals;
- propose plans;
- decompose work;
- select appropriate capabilities;
- inspect results;
- request revisions;
- recommend acceptance.

It must NOT bypass:

- policy engine;
- approval gates;
- workflow state;
- cost controls;
- validators;
- evidence requirements.

The orchestration engine remains authoritative.

---

# 10. TASK CONTRACTS

Every substantive delegated operation should eventually have a structured contract.

Conceptually:

```yaml
task:
  id: TASK-001
  type: asset.create
  objective: Create heavy enemy asset
  project: iron-bastion

inputs:
  art_bible: ...
  asset_spec: ...

constraints:
  max_triangles: 18000
  max_texture_size: 2048

allowed_tools:
  - image_generation

forbidden_tools:
  - meshy

requires:
  - concept_generation

acceptance:
  - concept_created
  - visual_style_check_passed

evidence:
  required: true
```

This is illustrative.

Create proper schemas instead of copying this blindly.

---

# 11. ARTIFACT MODEL

Artifacts must be first-class objects.

Examples:

```text
design document
concept image
3D model
texture
audio
Godot scene
script
test report
screenshot
performance report
build
validation report
```

Store metadata independently from physical storage.

An artifact should eventually support:

```text
ID
type
path/URI
producer
workflow
task
created timestamp
content hash
metadata
validation state
version
```

Do not build a complicated object-storage service in V1.

Local filesystem storage is acceptable initially.

Design the abstraction so remote storage could be introduced later.

---

# 12. PIPELINE MODEL

Do not hard-code entire workflows in procedural spaghetti.

Create reusable pipeline/workflow concepts.

Future pipelines include:

```text
create-game
onboard-existing-game
create-feature
create-character
create-enemy
create-prop
create-environment
create-level
create-ui
create-audio
integrate-asset
test-game
visual-review
performance-review
build-game
release-game
```

For example, the future 3D asset pipeline may resemble:

```text
Asset Request
    ↓
Asset Specification
    ↓
Art Direction Validation
    ↓
Concept Generation
    ↓
Visual Review
    ↓
Human Approval
    ↓
Paid Generation Approval
    ↓
Meshy
    ↓
Blender Processing
    ↓
Asset Validation
    ↓
Godot Integration
    ↓
In-Game Validation
```

Do NOT fully implement this entire pipeline in the bootstrap phase.

Implement the abstractions necessary to support it.

---

# 13. GODOT INTEGRATION

Godot is the first supported engine.

Eventually we need capabilities such as:

```text
detect Godot project
detect Godot version
inspect project structure
launch editor
run project
headless execution
execute tests
build/export
capture logs
capture screenshots
collect debug state
validate imported assets
inspect scenes
```

Create an EngineAdapter abstraction.

Conceptually:

```text
EngineAdapter
    GodotAdapter

future:
    UnityAdapter
    UnrealAdapter
```

Do not implement fake Unity or Unreal integrations.

Only design extension points.

Godot support must be real.

---

# 14. GODOT AI TEST HARNESS

Plan for a future optional development-only Godot addon or harness.

It should eventually allow controlled operations such as:

```text
start_game
load_scene
start_level
spawn_entity
simulate_input
wait
capture_screenshot
dump_state
collect_metrics
quit
```

Security and determinism matter.

Do NOT expose an arbitrary remote-code execution interface.

Do not make this a runtime dependency.

Initially document/design this subsystem and implement only what is appropriate for the bootstrap phase.

---

# 15. MESHY INTEGRATION

Meshy is an external asset-generation provider.

Treat it as a provider adapter.

Conceptually:

```text
AssetGenerationProvider
    MeshyProvider
```

The core must not depend on Meshy-specific concepts.

We already may use Meshy through MCP in the development environment.

Do not assume credentials exist.

Never print or persist API secrets.

Never trigger paid Meshy generation during bootstrap work.

No paid generation should be performed unless explicitly authorized.

---

# 16. BLENDER INTEGRATION

Blender is a local technical-art processing tool.

Eventually support deterministic scripted operations such as:

```text
inspect mesh
normalize scale
normalize orientation
set origin
clean geometry
inspect materials
optimize textures
generate LOD
generate collision proxy
validate UVs
export GLB
```

Prefer deterministic Blender Python automation over AI manipulating Blender UI.

Create a DccAdapter abstraction if justified.

Example:

```text
DccAdapter
    BlenderAdapter
```

Do not over-engineer hypothetical DCC integrations.

---

# 17. ASSET VALIDATION

Asset validation must eventually be executable.

Example validation rules:

```text
triangle budget
texture dimensions
material count
scale
orientation
naming convention
collider presence
LOD presence
file type
file size
broken references
```

Validation results must be structured and machine-readable.

Example conceptual result:

```text
PASS
WARNING
FAIL
```

with evidence and reasons.

---

# 18. VISUAL QA

Plan a visual-review capability.

Inputs:

```text
screenshot
reference images
ART_BIBLE
UI/UX constraints
scene context
```

Review dimensions may include:

```text
visual hierarchy
silhouette readability
contrast
composition
style consistency
HUD readability
safe areas
VFX obstruction
lighting
environment readability
```

Visual AI findings are advisory evidence, not absolute truth.

Store findings separately from deterministic validator results.

---

# 19. GAMEPLAY QA

Plan support for automated gameplay verification.

Eventually combine:

```text
deterministic test scenarios
game-state snapshots
logs
metrics
AI analysis
simulation
```

The Factory should eventually detect issues such as:

```text
impossible progression
difficulty spikes
broken spawn rules
economy anomalies
unreachable states
unexpected win/loss conditions
```

Do not pretend this can all be solved in V1.

Design extension points.

---

# 20. PERFORMANCE QA

Performance budgets must be data-driven per project/platform.

Example:

```yaml
android_mid:
  target_fps: 60
  minimum_fps: 50
  max_memory_mb: 750
  max_draw_calls: 150
  max_texture_size: 2048
```

Future performance evidence may include:

```text
FPS
frame time
CPU time
GPU time
memory
draw calls
object counts
particle counts
asset budgets
```

The platform must compare evidence against project budgets.

---

# 21. QUALITY GATES

Task completion is NOT equivalent to agent completion.

Implement a generic quality-gate concept.

A feature may eventually progress through something similar to:

```text
REQUESTED
DESIGNED
IMPLEMENTED
UNIT_VERIFIED
GAMEPLAY_VERIFIED
VISUALLY_VERIFIED
PERFORMANCE_VERIFIED
APPROVED
```

Do not force every task through irrelevant gates.

Gates should be composable by workflow/task type.

A task cannot become COMPLETED when mandatory gates lack evidence.

---

# 22. PROJECT ONBOARDING

Support two eventual flows.

## New project

```bash
gamefactory new <name>
```

## Existing project

From inside an existing game:

```bash
gamefactory init
```

For existing games, the Factory should eventually inspect the repository and infer:

```text
engine
engine version
2D/3D
platform hints
scene count
script count
asset structure
test infrastructure
build/export configuration
```

Never destructively reorganize an existing project without explicit permission.

---

# 23. CLI

The first user interface is CLI.

Design a clean CLI.

Potential commands:

```text
gamefactory new
gamefactory init
gamefactory doctor
gamefactory status
gamefactory plan
gamefactory run
gamefactory resume
gamefactory tasks
gamefactory approvals
gamefactory artifacts
gamefactory validate
gamefactory test
gamefactory review
gamefactory build
gamefactory config
```

Do not implement empty commands purely to inflate feature count.

Every exposed command must provide real value.

Start with the minimum coherent command set.

The CLI should produce both:

- human-readable output;
- structured output where appropriate.

Plan for something like:

```bash
gamefactory status --json
```

---

# 24. LOCAL-FIRST

V1 should be local-first.

Do NOT require:

- cloud infrastructure;
- Kubernetes;
- Redis;
- Kafka;
- distributed workers;
- external database servers;
- SaaS accounts.

A local embedded database such as SQLite may be used if justified.

File-backed state may be used if more appropriate.

Evaluate the tradeoff.

We need durable:

```text
projects
workflows
tasks
executions
approvals
artifacts
evidence
cost records
```

Choose the simplest reliable persistence model.

Document the decision in an ADR.

---

# 25. CONCURRENCY

Do not prematurely create a distributed job system.

However, workflow execution must be designed so independent tasks could eventually execute concurrently.

For V1:

- local process execution is sufficient;
- concurrency must be bounded;
- paid operations must never accidentally execute twice;
- workflow state updates must be safe;
- interruption/restart must be considered.

---

# 26. SECURITY

Treat this as developer tooling with powerful local capabilities.

Implement strict boundaries.

Never:

- leak environment variables;
- print API keys;
- commit secrets;
- send arbitrary repository contents to external providers without explicit provider boundaries;
- allow agent text to directly execute arbitrary shell commands without policy evaluation.

Command execution should eventually be mediated by a tool/execution policy.

At minimum distinguish:

```text
read-only
repository-write
process-execution
network
external-generation
paid-operation
destructive
```

Dangerous/destructive actions require stronger control.

---

# 27. OBSERVABILITY

Every workflow execution should eventually be traceable.

Use structured logs.

Important concepts:

```text
workflow_id
task_id
execution_id
agent_id
provider
tool
duration
status
cost
artifact_ids
```

Do not build an enterprise telemetry platform.

Good structured local logging is sufficient.

---

# 28. ERROR MODEL

Create a deliberate error taxonomy.

Examples:

```text
ConfigurationError
PolicyViolation
ApprovalRequired
ProviderUnavailable
ToolExecutionError
ValidationError
WorkflowError
ArtifactError
BudgetExceeded
Timeout
```

Errors must contain useful context without leaking secrets.

---

# 29. CONFIGURATION

Use layered configuration.

Conceptually:

```text
Factory defaults
    ↓
User configuration
    ↓
Project .gamefactory configuration
    ↓
Workflow overrides
```

Environment variables may provide secrets.

Never store secrets in project configuration.

---

# 30. PLUGIN /