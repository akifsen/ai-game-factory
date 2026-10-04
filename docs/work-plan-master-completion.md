# Master Factory completion plan

This plan interprets the current user request as completing the Factory capabilities in
`docs/requirements/01-master.md` and `docs/requirements/02-extensions.md`, including capabilities
previously described as future work. It supersedes the priority freeze in
`product-direction.md` for this work; historical milestone and pilot reports remain
evidence of what was done at those times. A capability is complete only when its
executable path, durable evidence, failure behavior, and user-facing status agree.

The user has deferred **all test, build, lint, typecheck, runtime validation, and
action execution until the full code scope is written**. Writing validation code is
allowed during implementation. No pass claim can be made during the code phase.
After code completion, run the relevant checks and repair failures before calling
this plan complete. No paid provider request is part of validation without the
required operation-specific approval.

## Architectural decision

Keep Python, the local SQLite repositories, the existing deterministic
`WorkflowEngine`, policy/approval/cost ledger, and process runner. Add small typed
contracts and adapters at the boundaries they serve. CLI composes them; the domain
does not import CLI, SQLite, Godot, Blender, or model vendors. One local workflow
runner remains authoritative; an agent or provider returns a proposal and evidence
references, never an authoritative state transition. Do not add a distributed job
system, plugin marketplace, event sourcing, or runtime dependency in generated games.

## Capability acceptance matrix

| Capability and source | Current repository evidence | Completion condition |
| --- | --- | --- |
| Durable workflow, approvals, cost, artifact/evidence, recovery (§4–7, §11, §21, §24–28, §37–39, §52–53) | Existing engine, SQLite repositories, approval service, ledger, artifact manager, recovery flow | Every new task family enters through central policy, durable attempt, validated evidence and configured gate; charged calls have input-bound approval, durable intent, external ID reconciliation and no blind repeat. |
| Agent/Director/context/routing (§8–10, §46–51) | Narrow Codex CLI static-prop planner; no general Director | Versioned bounded task and result contracts; explicit capability/permission/timeout/cost routing; allowlisted path/hash context; validated proposals; versioned prompt metadata only when a prompt is used; fail-closed fallback. Director cannot approve, run, complete, or spend. |
| New project and existing-game onboarding (§2, §22, §40–45) | Existing `init` and Godot discovery; no `new` command | `new` creates a minimal independent Godot project and small versioned Factory contract without overwriting; `init` safely inventories existing project and preserves existing files, reporting unknowns honestly. Optional game documents are created only when needed. |
| Feature/level/UI/audio/game creation (§12, §19, §21, §23) | Asset workflows and demo/Godot workflows; no general production orchestration | Generic operator-readable, versioned workflow manifests compose registered real capabilities, dependencies, constraints and family-appropriate evidence gates. A requested family with missing executor/provider is blocked with an actionable reason, not marked complete. Family labels alone are not implementation. |
| Godot engine and development harness (§13–14, §19) | Discovery, staged headless verification, bounded asset/capture harnesses | A development-only, local, schema-validated scenario harness handles only declared scene/entity/input/state/capture/metric operations. It records engine/scenario/seed/state/log artifact identity, refuses arbitrary code and unsafe paths, and is absent from release output. |
| Asset generation and DCC (§12, §15–17) | Profile-driven asset DAG, deterministic Blender processing, GLB validation, real `MeshyCliRunner` transport with durable intent/query/download | Existing working paths retained; generic charged-provider invocation has equivalent approval/intent/recovery semantics. Provider readiness reflects credentials/tool availability without implying a successful remote call. No paid request in automatic validation. |
| Image, coding, vision, audio providers (§15, §18, §30–31, §50, §55) | Image concept ingestion contract and fake provider; narrow Codex planner; no general live vision/audio | Replaceable ports with at least one genuine configured execution adapter where an available external interface exists, validated structured response and bounded input disclosure. Explicitly report unavailable/misconfigured when no usable provider exists. Fakes are test/demo only and cannot satisfy production gates. |
| Visual and gameplay QA (§18–19) | Captures and strict asset/Godot observations; no general advisory visual or gameplay model | Deterministic scenario observations and independent assertions bind hashes and versions. Vision findings are stored as advisory evidence, cannot turn failed/missing deterministic evidence into PASS, and are tied to exact images/references. |
| Performance QA (§20) | No general per-project/platform budget comparator | Versioned platform budget, measured value and unit/provenance, strict comparable-metric evaluation, explicit missing/unsupported result. One image is not performance evidence. |
| Build and release (§12–13, §21–23) | No general Godot export/release workflow | Export from validated preset through controlled process; persist exit/result, target, artifact path/hash and logs. Release requires explicit human approval bound to the exact build artifact and required gates. Signing is optional/configured, and credentials never enter state or logs. |
| CLI/capability/docs (§23, §30–32, §57–59) | Useful CLI and doctor, but current `plan` is pilot-specific; `docs/integrations/status.md` describes old V0.3 state | Commands expose only working operations; `--json` truthfully distinguishes ready, unverified, blocked and unavailable. README/overview/integration status match final behavior and preserve historical reports. |

The master lists possible agent roles and pipeline names as **conceptual examples**,
not a requirement for fifteen autonomous agents or fifteen procedural implementations.
Its V0.1 bootstrap exclusions limited the original milestone; the present user request
authorizes completing deferred Factory capabilities. Unity, Unreal, cloud workers,
marketplace, web dashboard, and mass level generation remain explicit extension-only
examples or rejected early designs. Existing rigged-character candidate contracts
must not silently be advertised as production-ready. The V0.8 review UI backlog does
not substitute for master Factory capabilities.

## Ownership and ordering

1. **Shared contracts (agent worker owns `core/domain/agent_contracts.py` and
   `agents/context.py`, `agents/registry.py`, `agents/director.py`).** Define strict
   versioned contracts/results, allowed capability selection, minimal path/hash
   context and proposals. `core/domain/game_quality.py` and
   `validators/game_quality.py` are independently owned by the quality worker;
   they define and evaluate performance, gameplay and advisory visual evidence.
   Neither worker edits CLI composition or workflow state transitions.
2. **Onboarding and Godot boundary.** Own new-project template/service, safe existing
   discovery, bounded development-only harness, engine export adapter, and release
   artifact contract. Use the existing process runner and path guard. A release
   must work when Factory is removed.
3. **Provider boundary.** Reuse the real Meshy adapter and narrow Codex planner.
   Add explicit provider registration/readiness and image/coding/vision/audio
   adapters only where real invocation is possible. Missing credentials or installed
   tools are truthful unavailable/misconfigured states. Charged-provider durability
   must be audited beyond the current Meshy-specific path before general use.
4. **Workflow integration.** Own manifests, task handlers and gates joining the
   above contracts to the existing engine and repositories. Bind every proposal,
   observation and approval to immutable inputs and artifact hashes. Persist
   per-attempt records before external side effects. Ensure interrupted charged
   submissions reconcile by external ID or remain blocked.
5. **CLI/docs integration.** Wire `new`, generic planning/creation, review, test,
   build, release and capability status only once their services exist. Update
   active docs from implementation, retain historical reports. Compose providers
   explicitly and keep old pilot commands compatible unless a documented migration
   is required.
6. **Verification phase after all code is written.** Run focused and integration
   checks, then real local Godot/Blender checks when installed. Verify paid adapters
   with fakes and static contract inspection; remote paid calls require their normal
   explicit approval. Inspect build output for harness and secret leakage. Repair
   failed checks before any completion claim.

Parallel owners must not edit one another's modules. Cross-owner interfaces are
versioned contracts and registered handlers, with the CLI/workflow integrator
responsible for final composition. SQLite migration changes require a single owner
and should be additive, transactional and backward compatible.

## Completion and verification boundary

The implementation phase remains open until provider adapters, generic workflow
composition, candidate quality runners, accepted-asset installation, CLI commands,
packaged resources, examples, and active documentation are all written and reviewed.
Individual worker reports do not close this boundary. Source inspection and test
authoring do not count as executed verification.

The code-writing boundary is **CLOSED** as of 2026-10-04. Provider adapters,
generic workflow composition, gameplay/performance/visual gates, project creation,
accepted-asset installation, export/release, CLI, packaged resources, examples,
and active documentation are written. Independent source review found no remaining
concrete blocker after revisions. Authored regressions include exact approved
operation recovery, immutable backup/concurrent-edit preservation, shared workflow
locking, and bounded normal-engine continuation.

No test, build, lint, typecheck, runtime validation, or action was run before this
boundary closed. Verification is now authorized by the user's original ordering:
run focused and broader repository checks, local Godot checks, and packaged-resource
checks, then repair observed failures. Source clearance does not claim production
verification or passing tests. Antigravity and Cursor were not used, following the
user's current routing instruction. Remote paid provider calls are outside this
validation scope; configured adapters remain operationally unverified until a real
authorized invocation supplies evidence.

Verification targets strict JSON round trips, provider authorization and uncertain
submission recovery, cost reservations/reconciliation, candidate provenance and
failed gates, original-path link rejection, source drift, concurrent file
preservation, build/release output binding, command compatibility, and installed
prompt/harness resources. Preserve the original untracked `.verify-pytest/` and
`data/`; write new local check logs under a separate `.verification/` directory.

The local verification boundary is **CLOSED — APPROVED** for the implemented
required master scope. Independent source and scope reviews found no remaining
concrete blocker; the executed checks below passed after corrections. This does
not certify a live paid provider, experimental rigged-character production, or
an external publication.

## Executed verification

Verification ran only after the code-writing boundary closed. Logs are local,
ignored artifacts under `.verification/master-factory-20261004/`.

- Full source typecheck: 179 files passed. Ruff lint passed; all 362 checked
  Python files were formatted. `git diff --check` passed.
- New Factory tests: 160 passed, one Windows symlink-privilege test skipped,
  and two real-Godot tests deselected from that run. Both real-Godot tests
  passed separately against Godot 4.7.2, including physical gameplay evidence,
  invalid-script rejection, and installed asset LOD/collider behavior.
- Focused engine/recovery/approval tests: 30 passed. Provider uncertain-outcome
  regressions: 15 passed. Final legacy engine, approval, paid-request, asset,
  reuse and assembly regressions: 92 passed, two skipped. These focused runs
  overlap the broader suites and are not added to the distinct-test total.
- The final wheel built and installed offline into a disposable local target.
  All 191 packaged source/prompt/Godot files matched source bytes. The installed
  CLI created an independent Godot project and listed providers without remote
  calls or a Factory runtime dependency in the generated game.
- Original repository regression run: 1,904 passed, 40 skipped, 428 slow-gate
  cases deselected, and 15 initially failed. All 15 subsequently passed:
  two passed with short writable paths and isolated Godot user directories;
  ten passed outside the sandbox so Godot could read the Windows root
  certificate store; three controller tests passed against a newly generated
  local fixture instead of the old inaccessible external fixture pointer.
  The ten-test real-tool rerun includes Blender processing, Godot captures,
  animation session/compare UI writes, conflicts, stale evidence, offline
  database recovery, physical counters, and the TEST_ONLY acceptance receipt.
  No diagnostic rule was weakened to obtain those results.
- Across the original and new suites, **2,081 distinct tests passed** and 41
  were skipped. The 428 `candidate_slow` installed-wheel CI-shard cases were
  not executed in this local run. Paid/live provider checks were not executed.

Key final logs: `focused-final.log`, `real-godot-final.log`,
`existing-regression-01.log`, `regression-environment-retry-01.log`,
`regression-environment-retry-02.log`, `controller-fixture-final.log`,
`legacy-engine-final.log`, `mypy-final-02.log`, `ruff-final-02.log`,
`format-final-02.log`, `wheel-build-final.log`, and `wheel-smoke-last.log`.
The last wheel smoke repeated byte matching and installed CLI checks against
the final frozen product source.

During local implementation verification, no paid remote provider invocation,
remote GitHub Action, publication, commit, or push was performed. The original
`.verify-pytest/` and `data/` are preserved. The user subsequently authorized
push, main integration and continuation of the release process; remote checks
and release qualification are a separate step from the local approval above.

## Remote integration follow-up

Implementation commit `00f901d2bad952120da4f7a05e86aaf9a74bc05b` was pushed to
PR 21. Its first Ubuntu quick run, `37205180916`, failed with eight mypy errors:
the Windows-only `msvcrt` branches in the Factory journal and asset installation
locks used `os.name`, which did not narrow the platform-specific stubs on Linux.
The guards now use `sys.platform == "win32"`, consistent with existing core
locks; lock modes, offsets and acquisition/release behavior are unchanged.

Independent full-source mypy checks passed for Linux and Windows/Python 3.11
(179 files each), Ruff and formatting passed, and the Team Lead's 33 recovery,
approval and installation regressions passed. The implementation engineer also
ran 54 focused tests successfully; these overlap existing coverage. Exact-main
remote qualification is still pending. The earlier local wheel predates this
typing revision and must not be published as the revised release artifact.

## Assumptions, trade-offs and risks

- Local-first single-process execution is sufficient. Sequential DAG dispatch
  simplifies durable claims and paid duplicate protection; parallel dispatch can
  wait for a demonstrated throughput need and concurrency proof.
- A generic manifest executes only registered, constrained handlers. This covers
  named production families without pretending that an absent coding, audio or
  image provider delivered an artifact. A provider adapter alone is not a complete
  workflow.
- A real external provider integration can be code-complete without credentials or
  network access. Its operational state remains unverified or unavailable until a
  real authorized invocation supplies evidence. Capability reports must preserve
  that distinction.
- Remote providers may accept a request while the local response is lost. Durable
  intent, stable fingerprint, persisted provider task ID and query-first recovery
  reduce duplicate risk; where the provider lacks idempotent query, block for
  reconciliation rather than submit again.
- Generated code and assets can modify a game project. Use explicit scopes,
  path containment, source-control awareness and validation before acceptance;
  agent text is never a shell command.
- Reject a universal Director service, arbitrary command harness, fake provider
  production results, hard-coded vendor branches in core, remote plugin loading,
  and automatic expensive fallback. Each weakens authority, security or truthful
  completion without satisfying an actual requirement.
