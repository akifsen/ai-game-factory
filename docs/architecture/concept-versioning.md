# In-revision concept versioning

Decision record: [ADR 0012](../adr/0012-in-revision-concept-versioning.md).

## Semantics (V0.6 workflows)

| Concept review decision | Result |
|---|---|
| `APPROVED` | proceed to the paid request snapshot |
| `CHANGES_REQUESTED` | BLOCKED with `CONCEPT_REVISION_REQUIRED`; the revision continues |
| `REJECTED` | terminal; the revision ends |

Workflows created before V0.6 keep the V0.5 mapping (`CHANGES_REQUESTED` fails the revision).

```bash
gamefactory request-changes APP-xxxxxxxx --actor NAME --comment "tone down the glow"
gamefactory asset concept replace --workflow WF-ASSET-xxxxxxxx \
    --concept concept-v2.png --provenance concept-v2.json \
    --actor NAME --reason "revised lighting" [--dry-run]
gamefactory approve APP-new --actor NAME
gamefactory resume WF-ASSET-xxxxxxxx
```

## Model

`concept_versions` holds one immutable row per version: artifact, content hash, provenance artifact and hash, provenance type, source type, actor, reason and timestamps. Exactly one row is ACTIVE per revision, enforced by a partial unique index. Triggers forbid content updates and deletes. A revision without rows (V0.5 and earlier) is read as version 1.

The active concept is the single source of truth for the concept-review context, the paid request snapshot binding, the paid dispatch check, the Meshy adapter check, the final review, evidence export and the approval checkpoint.

## Replacement boundary

Replacement is refused, with the message "allocate a new asset revision", when:

- any provider operation intent or provider invocation exists;
- the paid task has any execution;
- a paid approval is APPROVED;
- the revision has a raw GLB;
- the workflow is completed, the concept was rejected, or the workflow uses the legacy graph.

When allowed, replacement:

1. ingests `concept-v{n}.png` and `concept-provenance-v{n}.json` (never overwriting) and registers them;
2. supersedes the active version and adds the new ACTIVE version;
3. updates the revision `concept_hash` through a guarded method (only while there is no raw GLB and the old hash matches);
4. marks the active paid request snapshot and readiness report SUPERSEDED;
5. reopens CONCEPT-REVIEW, PAID-REQUEST, READINESS and PAID-GENERATION (audited `TASK_REOPENED_FOR_CONCEPT_REPLACEMENT`);
6. opens a fresh PENDING concept approval and records `CONCEPT_REPLACED`.

Old files, versions and approvals are preserved. An old approval cannot authorize the new concept, because approval fingerprints bind the active concept. Paid dispatch additionally refuses a snapshot whose binding does not name the active concept version.

## Provenance

Recognized concept provenance types: `diffusers_sdxl`, `local_blender_render`, `local_operator_drawing`, `external_image`. Anything else is `UNKNOWN`. Provenance records `paid` (from the sidecar's `generation.paid`, which must be a boolean) and an optional `source_script_sha256`. Concept provenance is independent of the 3D production provider and never implies paid-provider semantics.
