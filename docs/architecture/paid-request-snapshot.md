# Paid request snapshot (`paid-request-0.6.0`)

Decision record: [ADR 0009](../adr/0009-approved-paid-request-snapshot.md).

## Why

A human approves a paid request, not an adapter's current defaults. The snapshot is the complete, provider-executable description of what the approval pays for. Dispatch executes that snapshot or refuses.

## Shape

```json
{
  "schema": "paid-request-0.6.0",
  "provider": "meshy",
  "operation": "image-to-3d",
  "adapter": {"id": "meshy-cli-image-to-3d", "contract_version": 1,
              "cli_package": "meshy-cli@0.4.0", "output_schema": "v1"},
  "binding": {"asset_id": "...", "revision_number": 1, "concept_version": 1,
              "concept_sha256": "<64 hex>", "specification_sha256": "<64 hex>",
              "profile_id": "static_prop", "profile_version": 1},
  "request": {"model_type": "smart-topology", "target_polycount": 10000,
              "should_texture": true, "enable_pbr": true, "texture_resolution": "2k",
              "target_formats": ["glb"], "image_enhancement": "omit",
              "remove_lighting": "omit", "pose_mode": "omit", "texture_prompt": "omit",
              "remesh": "omit", "rig": false, "animate": false, "variants": 1},
  "cost": {"estimate": null, "reservation": 20.0, "unit": "credits"}
}
```

`"omit"` means the adapter does not send the option, so the provider's own default applies. The factory records the omission; it cannot record a server-side default value.

## Canonical form and hash

`src/gamefactory/core/domain/paid_request.py`:

- `canonical_json`: sorted keys, compact separators, UTF-8, no NaN or Infinity. It accepts only str, int, finite float, bool, None, lists and string-keyed objects.
- It rejects absolute paths (POSIX, drive letter, UNC), credential-like keys (KEY/SECRET/TOKEN/PASSWORD/AUTH/CREDENTIAL), and any value the secret redactor would change.
- `paid_request_sha256 = SHA-256(canonical bytes)`. The identity has no timestamps or report paths.
- `PaidRequestSnapshot` verifies that its hash matches its content. `load_verified(text, sha)` requires canonical text with the expected digest.

## Lifecycle

| Step | Where | What is checked |
|---|---|---|
| Resolve | `asset_paid_request_snapshot` task | Concept approval is APPROVED; the binding uses the **active** concept version; `provider.resolve_paid_request` then `check_paid_request` |
| Persist | the same task | Artifact bytes are the canonical JSON, so artifact hash = request hash; a `paid_request_snapshots` row is ACTIVE and earlier rows are SUPERSEDED |
| Bind | `refresh_parameters` of the paid task | Parameters gain the snapshot, its hash and the PASS readiness hash, so the approval fingerprint covers them; `approvals.paid_request_snapshot_hash` is set |
| Dispatch | `paid_generate` | Parameters = active row = approval = artifact hash; `load_verified`; the binding names the active concept, asset, revision and spec; a critical readiness recheck runs before a new submission |
| Execute | adapter | `check_paid_request` runs, then `build_create_args(snapshot)`. The pure function never reads defaults. It refuses before any intent claim or process launch |

For V0.6 intents `request_fingerprint = paid_request_snapshot_hash`. The unique fingerprint index therefore allows at most one provider submission per approved request.

## Legacy workflows

Pre-V0.6 workflows have no `graph_version` and keep their graph. Their existing intents resume query-only. A new submission without a snapshot raises `PAID_REQUEST_REQUIRED`.
