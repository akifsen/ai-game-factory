# 30. PLUGIN / EXTENSION ARCHITECTURE

AI Game Factory must be extensible without turning the core into a provider-specific system.

Design explicit extension points for capabilities such as:

```text
game engines
AI/model providers
coding agents
asset generators
image generators
DCC tools
audio generators
validators
reviewers
artifact storage
execution backends
```

Prefer capability-oriented interfaces over vendor-oriented abstractions.

For example, prefer concepts such as:

```text
EngineAdapter
DccAdapter
AssetGenerationProvider
ImageGenerationProvider
AgentProvider
VisionReviewProvider
AudioGenerationProvider
ArtifactStore
Validator
Executor
```

rather than spreading:

```text
if provider == "meshy"
if model == "..."
if engine == "godot"
```

throughout the core.

Provider-specific behavior belongs inside adapters/plugins.

However:

DO NOT build a complicated dynamic plugin marketplace or remote plugin-loading system in V1.

Internal extension interfaces and explicit registration are sufficient initially.

The architecture should make a future plugin ecosystem possible without requiring one now.

---

# 31. CAPABILITY REGISTRY

Create a capability registry so the orchestration layer can discover what the current environment can actually do.

Examples:

```text
engine.godot.detect
engine.godot.run
engine.godot.test

dcc.blender.detect
dcc.blender.process

asset.3d.generate
image.generate
vision.review
agent.code
```

The registry should distinguish:

```text
AVAILABLE
UNAVAILABLE
MISCONFIGURED
APPROVAL_REQUIRED
```

where appropriate.

A provider being configured does not automatically mean every capability is usable.

For example:

```text
Meshy adapter installed
API credential absent
→ asset.3d.generate unavailable
```

or:

```text
Meshy available
operation is paid
→ capability available but approval required
```

The Director and workflow planner must query capabilities rather than assume tools exist.

---

# 32. ENVIRONMENT DOCTOR

Implement a useful environment diagnostic command early.

Target command:

```bash
gamefactory doctor
```

It should inspect the local development environment and report useful information such as:

```text
AI Game Factory

Core
✓ configuration valid
✓ state storage available

Game Engines
✓ Godot 4.x detected

DCC
✓ Blender detected

External Providers
○ Meshy adapter configured
! credentials not verified

Project
✓ Godot project detected
✓ project.godot readable

Warnings
- no test harness detected
```

Never expose secrets.

Support structured output where useful:

```bash
gamefactory doctor --json
```

The doctor command must perform real checks rather than printing hard-coded statuses.

---

# 33. REPOSITORY STRUCTURE

Design a clean repository structure.

A possible direction is:

```text
ai-game-factory/

├── src/
│   ├── core/
│   │   ├── domain/
│   │   ├── workflows/
│   │   ├── policies/
│   │   ├── approvals/
│   │   ├── artifacts/
│   │   ├── evidence/
│   │   └── costs/
│   │
│   ├── application/
│   │
│   ├── adapters/
│   │   ├── engines/
│   │   │   └── godot/
│   │   ├── dcc/
│   │   │   └── blender/
│   │   ├── asset_generation/
│   │   │   └── meshy/
│   │   ├── ai/
│   │   └── storage/
│   │
│   ├── agents/
│   ├── pipelines/
│   ├── validators/
│   ├── execution/
│   ├── cli/
│   └── config/
│
├── schemas/
├── templates/
├── docs/
│   ├── architecture/
│   ├── adr/
│   ├── agents/
│   ├── pipelines/
│   └── integrations/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── examples/
└── README.md
```

This is illustrative, not mandatory.

Inspect the selected language/framework ecosystem and choose idiomatic conventions.

Do not create empty directories solely to make the repository appear sophisticated.

Every created architectural layer must have a concrete purpose.

---

# 34. TECHNOLOGY SELECTION

Before substantive implementation, determine the most appropriate implementation language and minimal dependency stack.

Important characteristics:

- excellent CLI support;
- subprocess/process management;
- filesystem operations;
- JSON/YAML handling;
- SQLite support if selected;
- strong schema validation;
- asynchronous execution where useful;
- cross-platform support;
- Windows support is mandatory;
- Linux support is mandatory;
- straightforward testing;
- easy integration with Godot and Blender CLI;
- easy interaction with MCP/provider processes;
- maintainable packaging/distribution.

Do not select technology based on novelty.

Document the decision as an ADR:

```text
docs/adr/0001-core-language-and-runtime.md
```

Also document rejected serious alternatives and why they were not selected.

Keep dependencies conservative.

---

# 35. CROSS-PLATFORM REQUIREMENT

Windows is a first-class platform.

Do not write Unix-only assumptions such as:

```text
/bin/bash
/tmp
chmod-based workflows
POSIX-only process control
```

without abstractions/fallbacks.

Paths must be handled using platform-aware APIs.

Commands must properly support paths containing spaces.

Process execution must avoid unsafe string concatenation.

Linux must also remain supported.

macOS compatibility is desirable but not a V1 release blocker unless it comes naturally from the implementation.

---

# 36. COMMAND EXECUTION

External processes such as Godot and Blender must run through a controlled execution abstraction.

Conceptually:

```text
CommandRequest
    executable
    arguments
    working_directory
    environment policy
    timeout
    permission class

CommandResult
    exit_code
    stdout
    stderr
    duration
    timed_out
```

Never default to:

```text
shell=true
```

or equivalent unsafe shell interpolation when argument-array execution is possible.

Capture stdout/stderr separately.

Support timeouts.

Redact sensitive values before persistence/logging.

---

# 37. WORKFLOW ENGINE

Implement a deliberately small workflow engine rather than adopting a heavyweight orchestration framework prematurely.

Required concepts should include the equivalent of:

```text
Workflow
Task
Dependency
Execution
State
Transition
Gate
Evidence
Retry
```

A workflow should be representable as a DAG where appropriate.

Example:

```text
          ┌─ gameplay implementation ─┐
DESIGN ───┤                            ├─ integration ─ test
          └─ asset preparation ───────┘
```

The engine must detect invalid dependencies/cycles.

Task state transitions must be validated centrally.

A task must not mutate itself directly into arbitrary states.

Persist workflow state durably.

---

# 38. EXECUTION LEASES AND DUPLICATE PROTECTION

Design local execution so interrupted processes do not cause dangerous duplicate work.

This is particularly important for paid external operations.

An execution record should be capable of tracking:

```text
execution id
task id
attempt
started at
heartbeat/lease where needed
completed at
status
external operation id
```

If an external provider returns an operation/task ID, persist it before continuing.

Never blindly repeat a paid request simply because the local process restarted.

Recovery logic should first inspect whether the original external operation can be resumed or queried.

---

# 39. IDEMPOTENCY KEYS

Introduce an idempotency concept for operations where duplication matters.

A logical expensive operation should have a stable fingerprint derived from relevant normalized inputs.

Conceptually:

```text
provider
operation type
project
asset/task
input specification version
```

Do not include secrets in fingerprints.

This mechanism should help identify accidental duplicate generation requests.

---

# 40. PROJECT CONTRACT

Every Factory-managed project must eventually have a small authoritative configuration file.

Prefer something conceptually like:

```text
.gamefactory/factory.yml
```

It may describe:

```yaml
project:
  id: iron-bastion
  name: Iron Bastion

engine:
  type: godot

targets:
  - android

policies:
  paid_operations_require_approval: true

documents:
  art_bible: ART_BIBLE.md
  game_spec: GAME_SPEC.md

budgets:
  performance: PERFORMANCE_BUDGET.yml
```

Do not overpopulate the initial file.

Provide a schema.

Validate configuration before execution.

Unknown or invalid configuration should produce actionable errors.

---

# 41. SCHEMA VERSIONING

Factory-controlled structured documents must have explicit schema versions where appropriate.

Examples:

```text
factory.yml
asset manifests
workflow definitions
task contracts
performance budgets
agent definitions
```

Plan for migrations.

Do not silently reinterpret old incompatible configuration.

V1 migration infrastructure can be simple, but versioning must not be ignored.

---

# 42. GAME PROJECT DISCOVERY

When executing:

```bash
gamefactory init
```

inside an existing repository, inspect before modifying.

Discovery should determine as much as safely possible:

```text
repository root
engine
engine version hints
project file
2D/3D hints
existing .gamefactory
source layout
assets
tests
export configuration
Git status
```

Present or persist findings.

Do not overwrite existing files unexpectedly.

If `.gamefactory` already exists, initialization must be safe and idempotent.

---

# 43. SOURCE CONTROL AWARENESS

The Factory should be Git-aware without making Git mandatory for every internal operation.

Before potentially broad repository modifications, be capable of detecting:

```text
Git repository
current branch
dirty working tree
untracked files
```

Never automatically:

```text
git reset --hard
git clean -fd
force push
delete branches
rewrite history
```

The Factory must not destroy user work to recover from its own mistakes.

Future workflows may optionally create branches/commits, but that is not required for bootstrap.

---

# 44. FACTORY KNOWLEDGE

Create a clear place for reusable Factory knowledge.

Conceptually:

```text
knowledge/

├── godot/
├── blender/
├── art/
├── gameplay/
├── performance/
├── patterns/
└── lessons/
```

However, do not create a large fake knowledge base.

Bootstrap only with useful documents actually required by the implementation.

Design the retrieval interface so future agents can request targeted knowledge rather than injecting the entire knowledge base into every prompt.

Context efficiency matters.

---

# 45. PROJECT KNOWLEDGE

Game-specific knowledge stays inside the game repository.

For example:

```text
.gamefactory/
    GAME_SPEC.md
    ART_BIBLE.md
    PERFORMANCE_BUDGET.yml
```

Never pollute reusable Factory knowledge with game-specific rules.

Conversely, do not copy the entire Factory manual into every game.

---

# 46. CONTEXT MANAGEMENT

AI agents must receive minimal sufficient context.

Do not automatically send an entire repository, all documentation, and every previous conversation to every agent.

Design a ContextBuilder concept.

It should eventually select context based on:

```text
task type
agent capability
project contract
relevant documents
relevant files
previous task evidence
constraints
```

Record what context was supplied where feasible.

This is important for:

- cost;
- latency;
- correctness;
- privacy;
- prompt stability.

---

# 47. AGENT OUTPUT CONTRACT

Agent output must be structured enough for orchestration.

Do not rely solely on prose such as:

> Everything looks good.

Define an agent-result envelope conceptually similar to:

```text
status
summary
changes
artifacts
evidence
findings
recommended_next_actions
blocking_issues
```

The exact representation may differ.

Validate structured output.

Malformed AI output must not corrupt workflow state.

---

# 48. AGENT TRUST MODEL

AI output is untrusted input.

Treat it similarly to input from an external system.

An AI agent must not be allowed to declare:

```text
APPROVED
TESTED
VALIDATED
COMPLETED
```

without the orchestration layer independently verifying required evidence.

The Director can recommend acceptance.

Only the Factory state machine can record authoritative completion after gates pass.

---

# 49. PROMPT MANAGEMENT

Prompts are versioned project assets of AI Game Factory.

Do not scatter long prompt strings throughout implementation code.

Use maintainable prompt templates or agent definitions.

Prompt versions should be identifiable in execution records when practical.

Separate:

```text
role definition
task contract
context
output schema
```

Avoid monolithic prompts.

---

# 50. MODEL / PROVIDER ROUTING

The architecture must permit different tasks to use different providers/models.

Example conceptual policy:

```text
architecture reasoning → high-reasoning model
implementation → coding-capable model
simple metadata work → inexpensive model
visual review → vision-capable model
```

Do not hard-code current commercial model names into core domain logic.

Use capabilities.

Examples:

```text
reasoning.high
code.edit
vision.inspect
text.fast
```

Provider adapters map capabilities to actual models.

Routing policy must be replaceable.

---

# 51. FALLBACKS

Provider failure must not automatically result in uncontrolled fallback to expensive alternatives.

Fallback behavior must be policy-driven.

Example:

```text
preferred provider unavailable

→ retry according to policy
→ use approved fallback if configured
→ otherwise block with actionable error
```

Record fallback usage.

---

# 52. FACTORY EVENT MODEL

Use internal domain events where they simplify decoupling.

Examples:

```text
WorkflowCreated
TaskReady
TaskStarted
TaskCompleted
TaskFailed
ArtifactCreated
ValidationCompleted
ApprovalRequested
ApprovalGranted
ApprovalRejected
BudgetExceeded
```

Do not introduce Kafka or an external event broker.

An in-process/local durable mechanism is sufficient for V1.

Avoid event sourcing unless there is a compelling demonstrated need.

---

# 53. AUDIT TRAIL

Maintain append-oriented execution/audit history.

Important changes should be reconstructable.

At minimum record:

```text
timestamp
actor
action
entity type
entity id
previous relevant state
new relevant state
execution reference
```

Do not log sensitive payloads indiscriminately.

---

# 54. TESTING STRATEGY

Testing is mandatory.

Use an appropriate pyramid.

## Unit tests

Cover:

```text
state transitions
policy decisions
approval logic
budget logic
configuration parsing
schema validation
DAG validation
idempotency
artifact metadata
```

## Integration tests

Cover:

```text
persistence
CLI
process execution
fake adapters
workflow execution
recovery
```

## External-tool tests

Godot/Blender tests should be isolated and skipped with a clear reason when the tool is not installed.

Tests must not require paid APIs.

Meshy and other paid integrations must use fakes/mocks in automated tests.

---

# 55. FAKE PROVIDERS

Create fake/test providers for external capabilities.

Examples:

```text
FakeAssetGenerationProvider
FakeImageGenerationProvider
FakeAgentProvider
FakeVisionProvider
```

This allows complete workflows to be tested without:

- network;
- API credentials;
- cost;
- nondeterministic external behavior.

Fake providers should be deterministic.

This is important.

---

# 56. FIXTURES

Create minimal real fixtures where useful.

For example, a tiny valid Godot project fixture may be included for adapter tests.

Do not include large binary assets.

Fixtures must remain lightweight.

---

# 57. DOCUMENTATION

Documentation is part of the product.

At minimum maintain:

```text
README.md

docs/
    architecture/
    adr/
    development/
    integrations/
```

README should explain:

- what AI Game Factory is;
- what it is not;
- current maturity;
- installation;
- quick start;
- commands;
- architecture summary;
- safety/cost behavior;
- current integrations;
- roadmap.

Do not advertise unimplemented capabilities as working.

Clearly distinguish:

```text
Implemented
Experimental
Planned
```

---

# 58. ARCHITECTURE DOCUMENT

Create:

```text
docs/architecture/overview.md
```

Include:

```text
system boundaries
core components
dependency direction
workflow lifecycle
task lifecycle
artifact/evidence model
approval flow
provider/adapters
persistence
security boundaries
```

Use Mermaid diagrams where they improve clarity.

Keep diagrams synchronized with actual implementation.

---

# 59. ADRs

Use Architecture Decision Records for consequential choices.

Initial ADRs should cover at least:

```text
0001 core language/runtime
0002 persistence strategy
0003 workflow execution model
0004 adapter/provider architecture
```

Only create ADRs for real decisions.

Each should contain:

```text
Context
Decision
Consequences
Alternatives
```

---

# 60. CODE QUALITY

Production-quality code is required.

Expect:

```text
clear boundaries
small cohesive modules
strong typing where supported
meaningful names
explicit error handling
testability
low coupling
high cohesion
```

Avoid:

```text
god classes
giant service classes
global mutable state
stringly typed domain state
deep inheritance trees
premature abstractions
copy-pasted provider logic
```

Comments should explain WHY, not narrate obvious code.

---

# 61. DEPENDENCY RULE

Core domain code must not depend directly on:

```text
Godot
Blender
Meshy
specific AI vendors
CLI framework
database implementation
```

Dependencies point inward.

Provider/tool integrations implement interfaces owned by the appropriate inner layer.

Do not pursue textbook architecture purity at the expense of readability, but preserve these boundaries.

---

# 62. BOOTSTRAP SCOPE

This first implementation is NOT the entire vision.

Implement a coherent **Factory Core V0.1**.

The first milestone should prove:

```text
project initialization
project discovery
durable local state
workflow creation
task dependencies
task state transitions
approval gates
policy checks
artifact/evidence records
fake agent/provider execution
real controlled local command execution
basic Godot detection
basic Blender detection
CLI visibility
resume/recovery fundamentals
tests
documentation
```

Do NOT attempt full:

```text
Meshy production generation
automatic concept generation
complex Blender asset repair
vision AI integration
gameplay AI
200-level generation
web dashboard
distributed workers
Unity
Unreal
cloud orchestration
```

during this milestone.

Build the foundation correctly first.

---

# 63. REQUIRED V0.1 USER JOURNEY

At the end of this task, demonstrate a real local workflow.

Something conceptually equivalent to:

```bash
gamefactory init