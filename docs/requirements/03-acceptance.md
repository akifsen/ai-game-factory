# 63. REQUIRED V0.1 USER JOURNEY

At the end of this milestone, demonstrate a real end-to-end local workflow.

The implementation must support a journey conceptually equivalent to:

```bash
gamefactory doctor
```

Expected:

- Factory configuration is checked.
- Local persistence is checked.
- Godot availability is detected.
- Blender availability is detected.
- Missing optional integrations are reported without crashing.
- No secrets are displayed.

Then, from a small existing Godot fixture/project:

```bash
cd example-game

gamefactory init
```

Expected:

- repository/project root is discovered;
- Godot project is detected;
- `.gamefactory/` is created safely;
- project configuration is created;
- existing files are not destructively modified;
- initialization can be executed again safely.

Then:

```bash
gamefactory status
```

Expected:

- project identity;
- engine;
- configured targets where available;
- workflow summary;
- pending approvals;
- recent executions;
- detected capabilities.

Then create or execute a demonstration workflow using the actual CLI design selected during implementation.

For example:

```bash
gamefactory run demo
```

The demo workflow must prove the orchestration core rather than merely print simulated output.

A suitable workflow could resemble:

```text
Task A
Project inspection
        ↓
Task B
Generate deterministic fake artifact
        ↓
Task C
Validate artifact
        ↓
Task D
Approval required
        ↓
Task E
Controlled local execution
        ↓
Task F
Record evidence
        ↓
COMPLETE
```

The exact workflow may differ if a better demonstration is implemented.

However, it MUST exercise real:

- workflow persistence;
- task dependencies;
- state transitions;
- artifact creation;
- evidence recording;
- approval blocking;
- approval continuation;
- execution records;
- completion gates.

At the approval stage, the workflow must stop correctly.

For example:

```text
Workflow: DEMO-001

Status: BLOCKED

Pending approval:

APP-001
Type: external_operation
Task: TASK-004

Continue with:

gamefactory approvals
```

The user must be able to inspect the approval.

An explicit CLI operation must record the decision.

For example:

```bash
gamefactory approve APP-001
```

or another clean CLI design.

After approval:

```bash
gamefactory resume DEMO-001
```

The workflow should continue from durable state rather than restart from the beginning.

Finally:

```bash
gamefactory status
```

should show the completed workflow and associated evidence.

---

# 64. RESTART / RECOVERY DEMONSTRATION

V0.1 must demonstrate that workflow state is not dependent on a single in-memory process.

Test the equivalent of:

```text
start workflow
↓
complete one or more tasks
↓
persist state
↓
terminate CLI/process
↓
start CLI again
↓
load workflow
↓
resume
```

Already completed tasks must not execute again unless explicitly designed to do so.

Pending approvals must remain pending.

Artifacts and evidence must remain discoverable.

Execution history must remain available.

Create automated integration coverage for the important recovery behavior.

---

# 65. FAILURE DEMONSTRATION

A successful workflow alone is insufficient.

Create at least one deterministic failure scenario using a fake/test adapter.

For example:

```text
Task A → PASS
Task B → provider failure
Task C → must not execute
```

Verify:

```text
Task A = COMPLETED
Task B = FAILED
Task C = BLOCKED/PENDING as appropriate
Workflow = FAILED or BLOCKED according to defined semantics
```

The CLI must show an actionable error.

The execution history must contain the failure evidence.

The system must not corrupt workflow state.

---

# 66. RETRY SEMANTICS

Define explicit retry behavior.

Not every failure should be retryable.

Distinguish conceptually between:

```text
transient failure
permanent failure
policy failure
validation failure
approval requirement
timeout
```

A retry must create a new execution attempt rather than rewriting historical execution evidence.

For example:

```text
TASK-003

Attempt 1
FAILED

Attempt 2
COMPLETED
```

Historical attempts must remain inspectable.

Paid/external operations require special duplicate protection before retry.

---

# 67. APPROVAL CLI

V0.1 must provide practical approval management.

Support the equivalent of:

```bash
gamefactory approvals
```

and an explicit decision command.

The exact syntax is your design decision.

Users must be able to inspect:

```text
approval ID
workflow
task
approval type
reason
cost classification if relevant
requested timestamp
associated artifacts
```

A decision should support an optional comment where practical.

Conceptually:

```bash
gamefactory approve APP-001 --comment "Concept reviewed"
```

and:

```bash
gamefactory reject APP-001 --comment "Wrong visual direction"
```

If CHANGES_REQUESTED is implemented in V0.1, ensure it has meaningful workflow semantics.

Do not add it merely as a label.

---

# 68. ARTIFACT CLI

Provide basic artifact visibility.

Conceptually:

```bash
gamefactory artifacts
```

and potentially:

```bash
gamefactory artifacts --workflow WF-001
```

The user should be able to identify:

```text
artifact ID
type
producer
path
workflow/task
validation state
created timestamp
```

Do not build a full digital asset manager.

This is inspection and traceability.

---

# 69. EXECUTION / EVIDENCE INSPECTION

Provide a practical way to inspect why the Factory believes work is complete.

For example:

```bash
gamefactory status --workflow WF-001
```

or:

```bash
gamefactory inspect WF-001
```

The exact CLI is your choice.

It should make relationships visible:

```text
Workflow
 ├─ Task
 │   ├─ Execution
 │   ├─ Artifact
 │   └─ Evidence
 └─ Gate
```

This is a critical product property.

Users must not have to inspect the database manually to understand what happened.

---

# 70. GODOT DETECTION — REAL IMPLEMENTATION

Godot detection in V0.1 must be real, not mocked in production code.

Detect Godot using safe platform-aware mechanisms.

Where available, collect:

```text
executable path
version
availability
```

Also detect whether the current target repository appears to contain a Godot project.

At minimum:

```text
project.godot
```

must be handled correctly.

Do not assume Godot exists.

Missing Godot should produce:

```text
capability unavailable
```

rather than crashing Factory Core.

Tests may use fake executables or controlled fixtures.

---

# 71. BLENDER DETECTION — REAL IMPLEMENTATION

Apply the same principle to Blender.

Where available detect:

```text
executable
version
availability
```

Do not require Blender for Factory Core to work.

Missing Blender must degrade capability availability cleanly.

Do not perform asset modification in V0.1 merely to prove Blender integration.

Detection and adapter boundaries are sufficient for this milestone.

---

# 72. MESHY IN V0.1

Do NOT perform real paid Meshy generation during this milestone.

Implement only the appropriate provider boundary/configuration/capability representation.

If Meshy support is represented:

```text
provider known
configuration state known
paid capability classified
approval requirement known
```

is sufficient.

Automated tests must use a fake asset-generation provider.

Never consume credits while testing.

Never require a real Meshy API key for the test suite.

---

# 73. PAID-OPERATION SAFETY TEST

Add an automated test proving that a paid operation cannot execute without required approval.

Conceptually:

```text
Given:
  operation cost class = PAID
  policy requires approval
  approval absent

When:
  workflow reaches operation

Then:
  external provider is NOT called
  task becomes blocked
  approval request exists
```

Then:

```text
Given:
  approval granted

When:
  workflow resumes

Then:
  provider may execute exactly once
```

Use a fake provider with invocation counting.

This is a mandatory safety invariant.

---

# 74. DUPLICATE PAID-EXECUTION TEST

Add a test covering interruption/retry around a paid provider.

The system must make a serious attempt to prevent:

```text
local request sent
provider accepts request
local process dies
workflow resumes
same paid request sent again
```

The exact V0.1 mechanism may use:

```text
idempotency key
external operation ID
durable execution state
```

or a justified combination.

Document residual limitations honestly.

Do not claim exactly-once execution if the external provider cannot guarantee it.

Use precise semantics such as:

```text
duplicate-resistant
```

where appropriate.

---

# 75. POLICY TEST MATRIX

Create tests around important policy decisions.

At minimum cover:

```text
local read-only operation
repository write operation
process execution
external free operation
paid operation
destructive operation
```

The exact policy model may differ.

The tests should prove that policy evaluation is centralized rather than scattered through adapters.

---

# 76. STATE-MACHINE TEST MATRIX

Explicitly test valid and invalid transitions.

Examples:

```text
PENDING → RUNNING        valid
RUNNING → COMPLETED      valid with required conditions
RUNNING → FAILED         valid

PENDING → COMPLETED      invalid when execution is required
COMPLETED → RUNNING      invalid by default
REJECTED → COMPLETED     invalid
```

Use the actual states chosen by the implementation.

Do not copy these examples if the final domain model differs.

The important requirement is exhaustive testing of transition invariants.

---

# 77. DAG TESTS

Workflow dependency handling must test:

```text
linear dependencies
fan-out
fan-in
independent tasks
cycle detection
dependency failure
blocked downstream tasks
```

For example:

```text
      B
     / \
A ──     ── D
     \ /
      C
```

B and C may become runnable after A.

D must not become runnable until its required dependencies satisfy the workflow rules.

---

# 78. CONCURRENCY BOUNDARY

If V0.1 implements parallel task execution, it must be bounded and tested.

If V0.1 intentionally executes tasks sequentially, document that clearly while preserving DAG semantics for future concurrency.

Do not introduce fragile concurrency solely to claim parallel execution.

Correctness comes first.

---

# 79. DATABASE / PERSISTENCE INTEGRITY

Whichever persistence strategy was selected must protect basic integrity.

If SQLite is selected, use migrations/schema management rather than ad-hoc table creation spread throughout code.

Persistence tests should cover important relationships such as:

```text
project
workflow
task
execution
approval
artifact
evidence
cost record
```

Do not expose database implementation details throughout domain logic.

---

# 80. FILESYSTEM SAFETY

All Factory-managed writes must be deliberate.

Protect against path traversal and accidental writes outside expected boundaries.

Do not allow untrusted agent output such as:

```text
../../important-file
```

to determine arbitrary write locations.

Normalize and validate managed paths.

Never delete arbitrary files based solely on AI output.

---

# 81. SECRET REDACTION

Implement centralized secret redaction for logs and persisted command metadata where relevant.

At minimum consider:

```text
API keys
authorization headers
tokens
credentials from environment variables
```

Tests must verify that representative secrets are not emitted into normal logs.

Do not attempt to persist a complete copy of the user's environment.

Pass only necessary environment variables to child processes where practical.

---

# 82. MACHINE-READABLE OUTPUT

Important inspection commands should support structured output where useful.

For example:

```bash
gamefactory doctor --json
gamefactory status --json
gamefactory approvals --json
```

JSON output must be valid JSON without decorative CLI text mixed into stdout.

Human-readable diagnostics can use stderr where appropriate.

This capability is important because AI coding agents will eventually consume the CLI programmatically.

---

# 83. EXIT CODES

Define meaningful CLI exit-code behavior.

At minimum distinguish:

```text
success
user/configuration error
workflow/task failure
approval required / blocked
tool/provider unavailable
internal error
```

Do not create dozens of unnecessary codes.

Document the selected contract.

CLI automation must not need to parse English prose to determine whether an operation succeeded.

---

# 84. AI-AGENT FRIENDLY CLI

Remember that the CLI will be used by both humans and AI coding agents.

Commands should therefore be:

```text
predictable
non-interactive when flags are supplied
machine-readable
idempotent where possible
explicit about destructive behavior
```

Avoid requiring interactive prompts for automation when the same information can be supplied through arguments.

Interactive UX may exist as a convenience.

---

# 85. VERSION COMMAND

Provide:

```bash
gamefactory --version
```

Use a single authoritative project version source.

Initial milestone version:

```text
0.1.0
```

unless repository/package conventions strongly justify another pre-release representation.

---

# 86. PACKAGING

Make the project installable enough that the CLI does not depend on running an arbitrary source file manually.

The expected developer experience should eventually be similar to:

```bash
gamefactory doctor
```

after normal project installation.

Choose packaging appropriate to the selected ecosystem.

Do not prematurely build platform installers.

Document development installation clearly.

---

# 87. CI

Create a minimal CI workflow if the repository is Git-based and appropriate.

CI should run at least:

```text
format/lint checks
type checks where applicable
unit tests
integration tests that do not require Godot/Blender
```

External-tool tests may be conditional.

Never require paid credentials in CI.

Do not place secrets into workflow files.

---

# 88. STATIC QUALITY

Use the ecosystem's appropriate:

```text
formatter
linter
type checker
test runner
```

Keep configuration understandable.

Do not install overlapping tools that solve the same problem without justification.

The repository should finish this milestone with clean static checks.

---

# 89. PERFORMANCE OF THE FACTORY ITSELF

Factory Core does not need extreme optimization.

However:

- avoid repeatedly scanning entire repositories unnecessarily;
- avoid loading large artifacts into memory without reason;
- use streaming/hash APIs where appropriate;
- cache safe discovery information when beneficial;
- invalidate caches deliberately.

Do not optimize without evidence.

---

# 90. NO FAKE IMPLEMENTATIONS IN PRODUCTION PATHS

Do not create methods that return success while doing nothing.

Examples of unacceptable production behavior:

```text
run_godot() → return true
validate_asset() → PASS
review() → "looks good"
```

If functionality is not implemented:

```text
NOT_IMPLEMENTED
UNAVAILABLE
UNSUPPORTED
```

must be represented honestly.

Fake implementations belong only in tests/examples designed for them.

---

# 91. NO ROADMAP MASQUERADING AS IMPLEMENTATION

README and CLI must not imply that planned capabilities currently work.

For example, if Meshy generation is not implemented, say:

```text
Meshy provider integration: Planned / Adapter foundation only
```

not:

```text
Generate production-ready 3D assets automatically.
```

Accuracy matters more than marketing.

---

# 92. DEFINITION OF DONE — FACTORY CORE V0.1

V0.1 is complete only when ALL applicable items below are satisfied.

## Repository

- project structure is coherent;
- installation works;
- CLI entry point works;
- version command works;
- static checks pass;
- test suite passes.

## Architecture

- core domain does not depend directly on Godot/Blender/Meshy/vendor SDKs;
- adapters are separated;
- workflow state transitions are centralized;
- policy evaluation is centralized;
- approvals are first-class;
- artifacts and evidence are first-class.

## Persistence

- projects persist;
- workflows persist;
- tasks persist;
- executions persist;
- approvals persist;
- artifacts/evidence persist;
- process restart does not lose workflow state.

## Workflow

- dependencies work;
- invalid cycles are rejected;
- failure propagation works;
- resume works;
- retries retain execution history.

## Safety

- paid operations require approval according to policy;
- duplicate paid invocation has protection;
- secrets are not logged;
- arbitrary shell interpolation is avoided;
- managed paths are validated.

## Tooling

- real Godot detection exists;
- real Blender detection exists;
- missing tools degrade gracefully;
- fake providers enable deterministic testing.

## CLI

- doctor works;
- init works;
- status works;
- workflow execution is inspectable;
- approvals are inspectable/actionable;
- JSON output exists for important inspection commands.

## Documentation

- README matches reality;
- architecture overview exists;
- initial ADRs exist;
- development setup is documented;
- limitations are documented.

## Verification

- required user journey has been executed;
- restart/resume has been verified;
- deterministic failure has been verified;
- approval gate has been verified;
- tests provide evidence.

If an item is intentionally deferred, it cannot simply be silently ignored.

Explain why it is not applicable to V0.1 and ensure no documentation claims otherwise.

---

# 93. IMPLEMENTATION ORDER

Use approximately this sequence unless repository evidence reveals a better dependency order:

```text
1. Inspect repository
2. Select core technology
3. Record ADR
4. Establish package/project structure
5. Implement core domain types
6. Implement persistence
7. Implement workflow/task state machine
8. Implement DAG/dependencies
9. Implement policy system
10. Implement approval model
11. Implement artifacts/evidence
12. Implement execution abstraction
13. Implement fake providers
14. Implement capability registry
15. Implement Godot detection
16. Implement Blender detection
17. Implement CLI
18. Implement recovery/resume
19. Implement retry semantics
20. Add integration tests
21. Add demo fixture/workflow
22. Execute acceptance journey
23. Complete documentation
24. Perform independent final review
```

Do not rigidly follow numbering when a small reordering improves implementation quality.

Do not begin provider-heavy integrations before the core is stable.

---

# 94. WORKING METHOD

Work autonomously through this milestone.

Do not stop after producing an architecture proposal.

Do not merely create TODO documents.

Implement the V0.1 milestone.

However, stop and ask for user approval before:

```text
spending money
calling a paid generation API
performing destructive repository operations
requiring credentials not already safely configured
making a major irreversible architectural choice with genuinely equivalent alternatives and insufficient evidence
```

Normal implementation decisions do not require user confirmation.

Use repository evidence and tests to resolve routine choices.

---

# 95. DO NOT OVER-ASK