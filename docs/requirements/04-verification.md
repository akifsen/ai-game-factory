# 95. DO NOT OVER-ASK

Do not repeatedly ask the user to make routine engineering decisions.

You are responsible for making well-supported implementation decisions within the architecture and constraints defined above.

Examples that normally do NOT require user confirmation:

```text
file naming
internal class/module naming
test organization
minor dependency selection
SQLite table details
CLI library details
logging implementation details
fixture organization
internal refactoring
```

Use engineering judgment.

Ask the user only when a decision:

```text
changes product scope materially
creates significant irreversible architectural lock-in
requires paid external operations
requires credentials
may destroy user data/work
contradicts this specification
cannot reasonably be resolved from repository evidence
```

Do not convert implementation into a sequence of unnecessary clarification questions.

---

# 96. REPOSITORY-FIRST EXECUTION

Before modifying the repository:

1. inspect the current repository;
2. determine what already exists;
3. inspect configuration;
4. inspect existing source code;
5. inspect tests;
6. inspect documentation;
7. inspect Git status where available.

Never assume this is still an empty repository.

The user may execute this prompt incrementally, and previous portions may already have created substantial implementation.

Treat existing correct implementation as authoritative repository evidence.

Do NOT recreate or replace working components merely because this prompt describes them differently.

Prefer:

```text
inspect
understand
verify
extend
refactor only when justified
```

over:

```text
delete
rewrite
rebuild
```

Preserve working behavior.

---

# 97. CONTINUOUS VERIFICATION

Do not postpone all verification until the end.

After each meaningful implementation slice:

```text
implement
↓
run targeted tests
↓
inspect failure
↓
fix
↓
continue
```

Examples:

After state-machine work:

```text
run state transition tests
```

After persistence work:

```text
run persistence integration tests
```

After CLI work:

```text
execute real CLI commands
```

After Godot detection:

```text
execute detection against the actual environment when available
```

Do not accumulate a large unverified change set.

---

# 98. TEST FAILURES

Never solve a failing test by weakening the test merely to obtain green status.

When a test fails, determine whether:

```text
implementation is wrong
test expectation is wrong
requirement changed
environment is unavailable
fixture is invalid
```

Fix the correct layer.

Do not:

```text
skip meaningful tests
remove assertions
catch and ignore errors
return hard-coded success
```

unless there is a documented and justified reason.

Skipped tests must state why they were skipped.

---

# 99. INDEPENDENT REVIEW PASS

After implementation appears complete, perform a separate review pass.

Treat the implementation as if another engineering team produced it.

Do not rely on previous assumptions.

Review at minimum:

```text
architecture
domain boundaries
workflow correctness
state-machine invariants
persistence integrity
recovery behavior
approval enforcement
cost safety
duplicate execution protection
command execution safety
filesystem safety
secret handling
CLI semantics
error handling
test quality
documentation accuracy
cross-platform behavior
```

Search specifically for places where the implementation contradicts the architecture defined by this specification.

---

# 100. ARCHITECTURE DRIFT REVIEW

Explicitly inspect for architecture drift.

Look for:

```text
provider-specific logic leaking into core
Godot-specific logic leaking into domain
CLI framework types leaking into domain
database implementation leaking into domain
workflow state changed outside state-machine authority
policy checks duplicated across adapters
approval checks bypassed
agent output directly mutating authoritative state
filesystem paths handled unsafely
shell execution shortcuts
```

For every meaningful drift:

```text
identify
classify
fix
verify
```

Do not merely list architectural violations in the final report if they can reasonably be fixed now.

---

# 101. SECURITY REVIEW

Perform a targeted security review before declaring V0.1 complete.

Inspect at least:

```text
command injection
shell interpolation
path traversal
unsafe file overwrite
secret leakage
environment-variable leakage
untrusted AI output handling
external-provider boundaries
destructive operations
unsafe subprocess invocation
malformed configuration
```

Where applicable, add regression tests for discovered vulnerabilities.

Do not claim a formal security audit.

This is an engineering security review.

---

# 102. FAILURE-MODE REVIEW

Think deliberately about what happens when:

```text
Godot is missing
Blender is missing
database is locked
workflow process crashes
provider times out
provider returns malformed output
agent returns malformed structured data
artifact file disappears
approval is rejected
approval remains pending
task retry fails
configuration is invalid
repository is read-only
command exceeds timeout
```

The system must fail predictably and provide useful diagnostics.

Do not attempt to hide failures behind generic:

```text
Something went wrong.
```

errors.

---

# 103. WINDOWS VERIFICATION

Windows is a first-class target.

Review code for assumptions involving:

```text
path separators
drive letters
spaces in executable paths
quoting
temporary directories
executable discovery
process termination
environment variables
line endings
```

Avoid manually concatenating paths.

Where the current environment permits, execute relevant tests on Windows.

If the current execution environment is not Windows, ensure Windows-specific behavior has appropriate tests and document what could not be physically verified.

---

# 104. CLI UX REVIEW

Execute the CLI as a user would.

At minimum inspect:

```text
gamefactory --help
gamefactory --version
gamefactory doctor
gamefactory init
gamefactory status
gamefactory approvals
```

and whichever workflow execution/inspection commands were implemented.

Evaluate:

```text
command discoverability
error clarity
output consistency
JSON output validity
exit codes
non-interactive usability
```

Fix obvious usability problems.

The CLI is part of the product, not merely a debugging interface.

---

# 105. REAL COMMAND VERIFICATION

Do not infer that commands work because unit tests pass.

Actually execute the important CLI commands against:

1. a controlled fixture;
2. the repository/environment where appropriate.

Capture the commands and outcomes for the final report.

If a command cannot be executed because an external executable is unavailable, verify graceful degradation.

Example:

```text
Godot not installed
→ doctor reports Godot unavailable
→ Factory does not crash
```

This is a valid verification result.

---

# 106. END-TO-END ACCEPTANCE RUN

Perform the complete V0.1 user journey defined earlier.

Use a clean or controlled test project.

The run must demonstrate:

```text
doctor
↓
init
↓
project discovery
↓
workflow creation
↓
task execution
↓
artifact
↓
evidence
↓
approval request
↓
workflow blocked
↓
process ends
↓
new process starts
↓
approval granted
↓
workflow resumed
↓
remaining tasks execute
↓
quality gate
↓
workflow completed
↓
status/inspection
```

Do not substitute isolated unit tests for this acceptance run.

Automated integration tests are desirable, but execute the user-facing flow as well.

---

# 107. NEGATIVE ACCEPTANCE RUN

Perform at least one end-to-end negative path.

For example:

```text
workflow
↓
task
↓
fake provider failure
↓
task FAILED
↓
dependent task does NOT execute
↓
workflow reports failure
↓
failure evidence inspectable
```

Then exercise retry if the workflow supports retry:

```text
retry
↓
new execution attempt
↓
successful completion
```

Verify that the failed attempt remains in history.

---

# 108. PAID SAFETY ACCEPTANCE

Using ONLY a fake provider, prove:

```text
paid task reached
↓
approval absent
↓
provider invocation count = 0
↓
approval requested
```

Then:

```text
approval granted
↓
resume
↓
provider invocation count = 1
```

Then simulate restart/recovery and ensure the same logical paid operation is not accidentally invoked again.

No real paid API may be called for this verification.

---

# 109. TEST SUITE

Before completion, run the complete applicable test suite.

Also run:

```text
formatter check
linter
type checker
```

where configured.

Do not report:

```text
all tests passed
```

unless they actually ran successfully.

Report exact counts when readily available.

Example:

```text
214 passed
3 skipped
0 failed
```

For skipped tests, explain the categories/reasons.

---

# 110. CLEAN REPOSITORY REVIEW

Inspect repository contents before completion.

Remove accidental artifacts such as:

```text
temporary files
debug dumps
local databases not intended for source control
generated logs
test output
coverage artifacts
secret files
IDE-specific junk
```

Update `.gitignore` appropriately.

Do NOT delete user-owned files.

Do not automatically revert unrelated user modifications.

---

# 111. DOCUMENTATION REALITY CHECK

Compare README and documentation against actual code.

For every documented command or feature ask:

```text
Does this actually work?
```

If not:

```text
implement it
```

or:

```text
mark it clearly as planned/experimental
```

Never leave aspirational architecture presented as existing functionality.

---

# 112. INSTALLATION FROM CLEAN STATE

Where practical, verify the documented development installation from a clean environment or isolated virtual environment.

The expected flow should be straightforward.

After installation, verify:

```text
gamefactory --version
gamefactory --help
```

The CLI must not rely on hidden state from the developer's current shell or IDE.

---

# 113. NO V0.2 WORK YET

Do NOT proceed into V0.2 features merely because V0.1 finishes early.

Specifically, do not begin implementing production versions of:

```text
Meshy generation
image generation
vision review
automatic Blender processing
AI gameplay evaluation
web dashboard
distributed execution
Unity
Unreal
cloud workers
complex multi-agent autonomy
```

unless some minimal interface is strictly required for V0.1 architecture/tests.

Finish V0.1 cleanly.

We will authorize the next milestone separately.

---

# 114. V0.1 RELEASE READINESS

Once implementation and verification are complete, evaluate release readiness.

Use exactly one final engineering decision:

```text
APPROVED
APPROVED WITH COMMENTS
CHANGES REQUESTED
REJECTED
```

Do not choose APPROVED simply because implementation work was performed.

Decision criteria include:

```text
correctness
architecture
tests
safety
recoverability
documentation
CLI usability
maintainability
```

Any unresolved issue that violates a non-negotiable requirement must prevent APPROVED.

---

# 115. SEVERITY CLASSIFICATION

Classify unresolved findings as:

```text
CRITICAL
MAJOR
MINOR
```

Use these meanings:

## CRITICAL

Examples:

```text
paid operation can bypass approval
secret exposure
destructive data-loss path
workflow corruption
serious command injection
```

V0.1 cannot be approved.

## MAJOR

Examples:

```text
resume unreliable
core architectural boundary violated
important CLI command broken
significant required test absent
Windows support materially broken
```

Normally requires changes before release.

## MINOR

Examples:

```text
small UX issue
documentation polish
non-blocking refactor opportunity
```

May be accepted with comments.

Do not inflate severity.

---

# 116. MAINTAINABILITY REVIEW

Assess maintainability from three horizons:

```text
6 months
1 year
3 years
```

Consider:

```text
provider growth
additional engines
additional pipelines
schema evolution
workflow complexity
test maintainability
CLI evolution
storage growth
agent-definition growth
```

Do not invent future abstractions merely because they might someday be useful.

Identify only concrete architectural pressure points.

---

# 117. SIMPLICITY REVIEW

Ask:

```text
Could this implementation be materially simpler
without losing required capability or extensibility?
```

Look for:

```text
unnecessary layers
interfaces with only hypothetical value
premature factories
duplicate abstractions
over-generalized plugin systems
excessive configuration
```

Simplify where justified.

The project should be sophisticated because the problem requires it, not because architecture diagrams look impressive.

---

# 118. PATTERN CONSISTENCY

Inspect whether similar concepts are implemented consistently.

Examples:

```text
provider adapters
validation results
CLI errors
repository interfaces
workflow transitions
IDs
timestamps
configuration
structured results
```

Do not allow multiple competing patterns to emerge during bootstrap.

Choose one clear pattern and normalize implementation where reasonable.

---

# 119. FINAL REPORT

At completion, produce a concise but evidence-based report.

Use this structure:

```text
AI GAME FACTORY — V0.1 COMPLETION REPORT

Decision:
APPROVED / APPROVED WITH COMMENTS / CHANGES REQUESTED / REJECTED

Version:
...

Architecture:
...

Implemented:
...

CLI:
...

Persistence:
...

Workflow Engine:
...

Policies & Approvals:
...

Artifacts & Evidence:
...

Godot:
...

Blender:
...

External Providers:
...

Security:
...

Recovery:
...

Tests:
...

End-to-End Verification:
...

Paid-Operation Safety:
...

Known Limitations:
...

Critical Findings:
...

Major Findings:
...

Minor Findings:
...

Architecture Drift:
...

6-Month Maintainability:
...

1-Year Maintainability:
...

3-Year Maintainability:
...

Simplicity:
...

Pattern Consistency:
...

Long-Term Verdict:
...
```

Do not fill sections with generic statements.

Provide concrete repository evidence.

Examples:

```text
command executed
test count
file/module
workflow ID
observed state
```

Do not paste enormous logs.

Summarize evidence and provide relevant paths.

---

# 120. FINAL VERIFICATION EVIDENCE

The final report must include actual commands executed.

For example:

```text
gamefactory --version
gamefactory doctor
gamefactory init
gamefactory status
<workflow command>
<approval command>
<resume command>
<inspection command>
<test command>
<lint command>
<type-check command>
```

Report actual outcomes.

If a tool was unavailable, state:

```text
NOT AVAILABLE IN TEST ENVIRONMENT
```

and show that graceful-degradation behavior was verified.

Never fabricate command execution.

---

# 121. IMPLEMENTATION SUMMARY FOR NEXT MILESTONE

At the very end, provide a small section:

```text
V0.2 RECOMMENDED NEXT MILESTONE
```

Do NOT implement it.

Based on actual V0.1 repository state, identify the smallest coherent next milestone.

The expected direction will likely involve the first real game-development pipeline, potentially:

```text
Asset Specification
→ Concept
→ Approval
→ 3D Generation
→ Blender Processing
→ Validation
→ Godot Integration
```

But do not assume this is automatically correct.

Use V0.1 evidence to recommend the next boundary.

Keep the recommendation focused.

---

# 122. IMPORTANT FINAL RULE

Do not optimize for:

```text
number of files
number of abstractions
number of agents
number of features
amount of generated code
```

Optimize for:

```text
correctness
determinism
evidence
recoverability
safety
extensibility
maintainability
developer experience
```

The fundamental principle of AI Game Factory is:

> AI workers may be probabilistic. The system controlling them must not be.

And:

> Agent completion is not task completion. Evidence and quality gates determine completion.

And:

> No paid operation is trusted to execute merely because an AI agent requested it.

Build Factory Core V0.1 accordingly.

---

# 123. BEGIN

Now continue from the CURRENT repository state.

Do not restart the project.

Do not recreate already-correct implementation.

First inspect what the previous implementation work produced.

Compare the repository against this specification.

Create a concrete gap list internally.

Then:

```text
implement missing V0.1 requirements
fix violations
run targeted verification continuously
run full verification
perform independent review
execute end-to-end acceptance
produce the final V0.1 completion report
```

Continue until Factory Core V0.1 satisfies the Definition of Done or until a genuine external blocker requires user action.

Do not stop merely because code compiles.

Do not stop merely because tests pass.

Do not stop merely because the architecture exists.

Stop when the V0.1 product behavior has been implemented, independently reviewed, and demonstrated with evidence.