# ADR 0009: Approved paid request snapshot

## Status

Accepted for V0.6. Verification is recorded in `docs/reports/v0.6-completion-report.md`.

## Context

In V0.5 the paid approval fingerprint covered task parameters, artifact hashes and the review context. The Meshy `image-to-3d create` arguments (model type, polycount clamp, texture flags, 2k resolution, GLB format) were literals inside the adapter's dispatch code. A code change between human approval and dispatch could silently change what was bought. Provider options the adapter never sends (image enhancement, remove lighting, pose mode, texture prompt, remesh, rig, animation, variants) were recorded nowhere.

## Decision

1. Before the paid approval exists, a dedicated task resolves one immutable **paid request snapshot** (`paid-request-0.6.0`). The snapshot lists provider, operation, adapter identity and contract version, the binding (asset, revision, concept version and SHA-256, specification SHA-256, profile), every material request option, and the cost estimate, reservation and unit.
2. The canonical form is `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` over a strict value model. Timestamps, absolute paths and credential-like keys or values are rejected. Options the adapter does not send are the explicit value `"omit"`. The request fingerprint is `SHA-256(canonical bytes)`.
3. The snapshot is stored as an artifact whose bytes are exactly the canonical form, so the artifact hash equals the request hash, and it is also recorded in `paid_request_snapshots`.
4. The paid task's parameters (and therefore its approval fingerprint) carry the snapshot content, its hash and the PASS readiness report hash. `approvals.paid_request_snapshot_hash` stores the bound hash explicitly.
5. At dispatch the handler loads the approved artifact and verifies it (`load_verified`) against the parameters, the approval and the active record. The adapter builds its create arguments **only** from the snapshot through a pure function. It refuses (`PAID_REQUEST_INCOMPATIBLE`) before any intent claim or process launch if it cannot execute the snapshot faithfully. It never translates the snapshot or falls back to current defaults.
6. For V0.6 intents `request_fingerprint = paid_request_snapshot_hash`. The existing unique index gives one approved request at most one provider submission.
7. Workflows created before V0.6 keep their graph. Their existing intents keep query-only recovery. A **new** paid submission without a snapshot is refused (`PAID_REQUEST_REQUIRED`), and a new revision is required.

## Consequences

Adapter default drift after approval cannot change a paid request; it can only cause a refusal. Provider-side defaults for `"omit"` options are outside the factory's control; they are recorded as omitted, not as values. Evidence bundles for V0.6 workflows use `asset-evidence-0.6.0` and include the snapshot and the readiness report. Earlier bundle schemas verify unchanged.
