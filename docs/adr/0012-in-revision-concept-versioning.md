# ADR 0012: In-revision concept versioning before paid production

## Status

Accepted for V0.6. Details: `docs/architecture/concept-versioning.md`.

## Context

`CHANGES_REQUESTED` on a concept failed the whole revision exactly like `REJECTED`, so every concept iteration consumed an asset revision. The revision's `concept_hash` was immutable, and several code paths assumed a single concept artifact.

## Decision

1. A `concept_versions` table records immutable versions (artifact, hashes, provenance type, actor, reason). One version is ACTIVE per revision. Revisions created before V0.6 are read as version 1 in memory, with no backfill.
2. For V0.6 workflows `CHANGES_REQUESTED` blocks the concept review with `CONCEPT_REVISION_REQUIRED`. `REJECTED` still terminates the revision. Legacy workflows keep the V0.5 mapping.
3. `gamefactory asset concept replace` appends a version, but only while no provider intent, provider invocation or paid execution exists and no paid approval is APPROVED. It never overwrites files, supersedes the active paid-request snapshot and readiness records, reopens only the pre-paid tasks (audited), updates the revision concept hash through a guarded method (only while no raw GLB exists), and opens a fresh PENDING concept approval.
4. Old approvals are never rewritten. Because approval fingerprints bind the active concept, an old approval cannot authorize the new concept.
5. Once paid production has begun, a new concept always requires a new asset revision.

## Consequences

Concept iteration no longer burns revisions. The legacy concept-review fingerprint shape is frozen so pre-V0.6 approvals still re-verify. The replacement is a sequence of guarded steps rather than a single transaction. An interruption part-way cannot authorize a paid request for the wrong concept, because dispatch verifies the snapshot binding against the active concept, but it may require operator cleanup (see Known limitations in the completion report).
