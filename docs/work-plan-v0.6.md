# AI Game Factory V0.6 — Work Plan: Production Safety & Recovery

Baseline: `main` at `8c57f8eea53a2da2eabef356bdccd8c96fe53f4b` (v0.5.1, released, CI green).
Local branch: `v0.6-production-safety` (not pushed).
Target package version: `0.6.0`.

Paid-safety rule for all V0.6 work: zero real provider calls. Tests use the fake
provider, recorded/synthetic CLI envelopes and local fixtures. `MESHY_API_KEY` is
never required and is never used by the suite even when present.

Protected production history (read-only; scratch copies only):
`.verification/v05-production-candidate/**` (pickup r001/r002) and
`.verification/v04-human-review/**` (crate r001). SHA-256 manifests of both trees
were recorded before any V0.6 change and are re-checked at the end.

Baseline suite (Windows, Python 3.12, hostile variable removed): 561 passed, 18 skipped.
With `CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH=1` present the suite fails on its first
asset CLI test because a SHA-256 in JSON output is corrupted to
`f76fa45d8ca20[REDACTED]4b69…` (reproduces finding P0-3).

---

## 1. Audit findings

### P0 — financial / paid safety

**P0-1 Paid request is not fully bound by the approval.**
`MeshyAssetGenerationProvider.generate()` (`adapters/external/meshy_cli.py`) builds the
`image-to-3d create` arguments from literals at dispatch time: `smart-topology`,
`min(polycount, 15000)`, `should_texture=true`, `enable_pbr=true`, `2k`, `glb`. None of
these appear in the approval inputs (`operation_scope.build_operation_inputs` hashes
task parameters, artifacts and the handler context only). A code change between
approval and dispatch silently changes the paid request. Provider defaults that are
never sent (image enhancement, remove lighting, pose mode, texture prompt, remesh,
rig, animation, variants) are not recorded anywhere.

**P0-2 No pre-spend readiness gate.** The paid task depends only on concept review.
Blender and Godot availability is first discovered after paid generation. V0.5.1
production reached `asset_godot` with no Godot configured after 15 credits were spent.
`asset-create --dry-run` reports capability status but nothing blocks dispatch.

**P0-3 Secret redactor corrupts machine-readable data.**
`SecretRedactor.redact_text` turns every environment value whose *name* contains
KEY/SECRET/TOKEN/PASSWORD/AUTH/CREDENTIAL into a global exact-match replacement,
regardless of the value. `…AUTH_REFRESH=1` replaces every `1` in hashes, UUIDs and
task IDs. `ProcessRunner` applies this before returning stdout, and with
`structured_json_output=True` it round-trips the JSON *after* redaction, so the
Meshy parsers (`get_task`, `download_glb`, `create`) parse redacted data. The CLI
`_emit` and persistence (`_sanitize_persisted_data`) apply the same redaction.

**P0-4 Accounting conflates reservation, estimate, actual and spend.**
Project spend is `SUM(executions.cost)` (engine `_execute_task` and
`TaskRepository.claim_execution`). `execution.cost` is overwritten in place by
`account_known_cost`. Problems:
- a pre-submission failure (for example approval missing or validation) keeps the full
  reservation in `execution.cost` forever;
- query-only recovery settles only the incremental delta on the *new* execution, so the
  original over-reservation remains (production: reservation 20, actual 15, recorded 20);
- there is no audit trail of settlement and no reconciliation tool (raw SQL only).

### P1 — recovery / audit integrity

**P1-1 Retry classification repair needs raw SQL.** Production `EXEC-41c22667`
(asset_godot, pid/exit/stdout/stderr NULL, "Godot executable is required…", original
code `ENGINE_IMPORT_FAILED`) was repaired by hand (`RETRY_CLASSIFICATION_REPAIRED` audit
event). There is no supported inspect or reclassify capability.

**P1-2 Paid recovery safety is implicit.** Exactly-once is enforced by the unique intent
indexes and adapter logic, but no operator-facing path explains which tasks are safe to
retry, and no test pins "recovery never re-POSTs for the same approved fingerprint".

**P1-3 Timestamps.** `ProviderOperationIntentRepository.save` writes the in-memory
`updated_at`, which is set once at construction (production intent: `SUCCEEDED`,
`updated_at == created_at`). `AssetRevisionRepository.save` writes the existing
`updated_at` back when GLB hashes are added.

**P1-4 Audit gaps.** No audit events for intent claim, external task ID persistence,
provider terminal status, cost settlement, readiness or concept supersession.

### P2 — workflow usability / provenance

**P2-1 Concept iteration.** Engine maps `CHANGES_REQUESTED` exactly like `REJECTED`
(task FAILED, workflow FAILED, `VISUAL_REVIEW_REJECTED`). No CLI command records
`CHANGES_REQUESTED`. Any concept change requires a new revision. `asset_revisions.concept_hash`
is immutable, and several code paths take "the first `asset-concept` artifact".

**P2-2 Concept provenance.** `KNOWN_CONCEPT_PROVIDERS = {"diffusers_sdxl"}`; the real
pickup r002 sidecar declares `generation.provider = local_blender_render`,
`paid = false`, which is normalized to `UNKNOWN`. Sidecar `paid` is ignored.

### P3 — CI / release polish

**P3-1 Duplicate CI.** `on: [push, pull_request]` without filters runs the full matrix
twice for a feature branch with an open PR.

**P3-2 Node 20 action runtime.** `checkout@v4`, `setup-python@v5` and
`upload-artifact@v4` run on `node20`. Verified from official release metadata:
`checkout@v5`, `setup-python@v6` and `upload-artifact@v6` are the lowest majors that run
on `node24`. Their only breaking change is the runtime (runner ≥ 2.327.1, satisfied by
GitHub-hosted runners).

---

## 2. Architecture decisions

### D1 Paid request snapshot (`paid-request-0.6.0`) — ADR 0009

- A pure domain module (`core/domain/paid_request.py`) defines the schema, the strict
  validator, canonical serialization and the hash. The canonical form is
  `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`
  over a strict value model: str, int, finite float, bool, None, list and dict. There are
  no timestamps, no absolute paths and no credential-like keys or values. Enums are
  lower-case strings, and omitted provider options are the explicit string `"omit"`.
  `request_sha256 = SHA-256(canonical bytes)`.
- The provider adapter owns resolution (`resolve_paid_request(...)`) and execution
  (`build_submission(snapshot)`). Adapter defaults live in one named constant block.
  `build_submission` reads only the snapshot and refuses unknown schema, adapter
  identity or version, or unsupported values (`PaidRequestIncompatibleError`) before any
  intent claim or process launch.
- A new task `asset_paid_request_snapshot` resolves the snapshot after concept approval,
  writes `paid-request-snapshot-v{concept_version}-a{attempt}.json` as an immutable
  artifact (`asset-paid-request-snapshot`) and records a DB row.
- The paid approval context carries `paid_request_snapshot_sha256`, the snapshot content
  and `production_readiness_report_sha256`, and `approvals.paid_request_snapshot_hash`
  stores it explicitly. The approval operation hash therefore binds the snapshot too.
- At dispatch the handler loads the approved snapshot artifact, verifies its hash
  against the approval, and passes it to the provider. For V0.6 intents
  `request_fingerprint = paid_request_snapshot_hash` (the unique index then enforces
  one approved request, at most one submission), and the intent stores
  `paid_request_snapshot_hash`.
- Legacy (pre-0.6 graph) workflows: existing intents keep query-only recovery. A
  **new** paid submission without a snapshot is refused (new revision required).

### D2 Production readiness (`production-readiness-0.6.0`)

- A new task `asset_production_readiness` runs after the snapshot and before the paid
  task. Probes are injectable (`ReadinessProbes`): provider (adapter available,
  CLI/doctor status, credential presence, capability, snapshot executable; no paid
  call), Blender (configured, exists, regular file, launches, version, Python dependency
  preflight), Godot (configured, exists, regular file, executable, version), filesystem
  (asset directory writable, scratch creatable, basic free space) and profile (supported,
  runtime validations and review views implemented).
- The persisted report is written as an artifact and a DB row. FAIL raises
  `ProductionReadinessFailedError` (a retryable `ToolExecutionError`), so the paid
  approval is never created.
- A critical recheck runs immediately before a *new* paid submission: snapshot hash
  unchanged, adapter compatible, credential configured, Blender/Godot present, workspace
  writable. On failure it stops before the POST. There is no silent re-approval.
- `doctor` shows a "Production Readiness Capabilities" section built from the same
  probes.

### D3 Cost ledger — ADR 0010

- A new append-only `cost_ledger` has entry types RESERVE, SETTLE, RELEASE and
  ADJUSTMENT, bound to project, workflow, task, execution, intent and request fingerprint.
- The **only** budget authority is project committed spend = Σ signed ledger amounts
  (RESERVE, SETTLE and ADJUSTMENT as signed; RELEASE negative). `claim_execution` and the
  engine budget check read the ledger. `executions.cost` remains a legacy, display-only
  mirror.
- Semantics:
  - claim → RESERVE R;
  - known partial actual A > held → RESERVE top-up;
  - terminal SUCCESS or FAILED with known A → SETTLE A plus RELEASE of everything held
    for the operation (task);
  - actual unknown or UNCERTAIN → hold;
  - no provider submission happened → RELEASE the reservation.
  Settlement is per paid *operation* (task + intent), so a query-only attempt settles
  the original reservation.
- The migration backfills one RESERVE per historical `executions.cost > 0`
  (`source = legacy_execution_cost`). Project spend is identical before and after the
  migration.
- `gamefactory accounting reconcile` is dry-run by default. `--apply` requires `--actor`
  and `--reason`, and writes SETTLE/RELEASE plus an audit event. It refuses
  UNCERTAIN or unknown-actual operations.

### D4 Evidence-driven recovery — ADR 0011

- `gamefactory recovery inspect <workflow>` is read-only. It classifies each failed
  or uncertain task (PRE_EXECUTION_TOOL_CONFIGURATION, TOOL_EXECUTION_FAILURE,
  ENGINE_IMPORT_FAILURE, RUNTIME_VALIDATION_FAILURE, ASSET_VALIDATION_FAILURE,
  PAID_SUBMISSION_UNCERTAIN, PROVIDER_TERMINAL_FAILURE, PROVIDER_RESULT_REUSABLE,
  DOWNSTREAM_FAILURE, UNKNOWN) and lists the safe actions.
- `gamefactory recovery reclassify --execution E` is dry-run by default. `--apply`
  requires `--actor` and `--reason`. It changes only `retryable 0→1`, only when the
  current policy evaluator proves pre-launch tool unavailability from stored evidence.
  It always refuses paid or metered tasks, launched processes, validation and runtime
  failures, provider failures and ambiguous evidence. There is no force mode.

### D5 In-revision concept versioning — ADR 0012

- A new `concept_versions` table holds (asset, revision, version, artifact, hashes,
  provenance, status ACTIVE/SUPERSEDED). Legacy revisions read as version 1 in memory.
- For V0.6 graphs, `CHANGES_REQUESTED` on concept review BLOCKS with `CONCEPT_REVISION_REQUIRED`.
  `REJECTED` remains terminal. This is controlled by handler metadata, not task-type
  special cases.
- `gamefactory request-changes <approval>` records `CHANGES_REQUESTED`.
- `gamefactory asset concept replace` appends a version. It is allowed only while no
  paid intent, provider invocation or paid execution exists and no paid approval is
  APPROVED. It never overwrites files, marks derived snapshot and readiness records
  SUPERSEDED, reopens only the pre-paid task segment (audited), and creates a fresh
  PENDING concept approval. Old approvals stay historical and cannot authorize the new
  concept (hash binding).

### D6 Task graph (V0.6 workflows only; `graph_version = "0.6.0"` in task parameters)

`PREPARE → CONCEPT-REVIEW → PAID-REQUEST → READINESS → PAID-GENERATION → PROCESS → VALIDATE → GODOT → FINAL-REVIEW → EVIDENCE`.
Existing persisted workflows keep their graph. No task is inserted into historical
workflows.

### D7 Structured output boundary

`CommandResult` gains an in-memory-only `protocol_stdout` (raw text, `repr=False`, never
in `to_dict`, never persisted). It is populated only for `structured_json_output=True`.
Provider parsers read `protocol_stdout`, while logs and evidence keep the sanitized
`stdout`. Global exact-value redaction requires an eligible secret: length ≥ 8 and not
a common literal. Key-aware patterns (`API_KEY=…`, `Bearer …`) still redact short
values.

---

## 3. Delivery slices (each independently reviewed before the next)

| Slice | Scope | Migration |
|---|---|---|
| A | Redactor eligibility + protocol/parsing boundary, timestamps, concept provenance registry, CI triggers/actions | — |
| B | Cost ledger, settlement, reconciliation CLI, historical/crash fixtures | 0007 |
| C | Paid request snapshot, readiness gate, recheck, V0.6 task graph, drift tests | 0008 |
| D | Recovery inspect/reclassify, paid recovery invariants | — |
| E | Concept versions, request-changes, concept replace | 0009 |
| F | Offline E2E acceptance, migration compatibility, ADRs, docs, version 0.6.0, completion report | — |

## 4. Non-goals

No new asset categories, no event sourcing, no workflow DSL, no remote control plane,
no batch farm. P3 release provenance (signed tags, attestations, SBOM) is evaluated
only after the core work.
