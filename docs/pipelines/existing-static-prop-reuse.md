# Existing static prop reuse (product pilot)

Bounded route for adopting a **manual or third-party GLB** into Factory as a durable
`static_prop@1` revision **without** a provider submission, concept image, or ledger spend.

## Command

```powershell
gamefactory --project . asset reuse `
  --spec plans/prop_tide_treasure_chest_01.json `
  --source assets/treasure_chest.glb `
  --provenance provenance/treasure_chest.json
```

Optional `--dry-run` validates the spec, GLB preflight, and provenance SHA binding without
creating workflow state.

## Provenance sidecar

`existing-external-source-provenance-1.0` JSON (strict, no extra fields):

| Field | Required | Meaning |
| --- | --- | --- |
| `source_mode` | yes | Must be `existing_external` |
| `source_sha256` | yes | Exact SHA-256 of the source `.glb` bytes |
| `original_provider` | yes | Human label (e.g. `meshy`, `manual-export`) — not a Factory provider receipt |
| `generated_by_this_workflow` | yes | Must be `false` |
| `paid_by_this_workflow` | yes | Must be `false` |
| `historical_task_id` | no | Optional single third-party task id (metadata only) |
| `historical_task_ids` | no | Optional list of third-party task ids (e.g. geometry and texture tasks) |
| `historical_credits` | no | Optional historical cost (metadata only; never ledger spend) |

Factory **never** overwrites the external `--source` file. At prepare it copies bytes into
`.gamefactory/assets/<asset_id>/r###/raw.glb` and binds `raw_glb_hash` on the revision.

## Workflow graph

```
REUSE-PREPARE → PROCESS (Blender) → VALIDATE → GODOT → FINAL-REVIEW → EVIDENCE
```

- No concept review, paid request, readiness, or provider invocation.
- `gamefactory resume`, `inspect`, `status`, and `approvals` behave like other asset workflows.
- `gamefactory report` lists verified managed artifacts, runtime captures, and `final_visual_review`
  status as JSON (no cold evidence bundle export and no approval mutation).
- Final human `final_visual_review` is mandatory; the engine does not self-approve.

## Drift policy

Creation and resume fail closed when:

- the external `--source` or `--provenance` file changes after workflow allocation,
- retained managed `raw.glb` or provenance artifacts no longer match `source_*_hash` parameters, or
- the revision `raw_glb_hash` disagrees with the retained artifact.

## Evidence export

Portable cold evidence bundle export (`export_asset_evidence_bundle` / the production
`gamefactory report` snapshot path) **refuses** reuse workflows: the standard cold bundle
verifier has no supported schema for `existing_external` reuse without inventing provider
receipts or a new evidence format. Use `gamefactory report` for the in-repo reuse review
summary instead. Managed artifacts, `inspect`, and the final review gate still apply.

## Scope limits

- **Profile:** `static_prop@1` only (`asset-spec-0.4.0`).
- **Not supported:** pickups, modular pieces, V0.7 assemblies, or relabeling external sources as
  Factory-generated.
