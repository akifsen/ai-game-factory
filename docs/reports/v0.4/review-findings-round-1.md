# Independent review — preliminary findings, changes required

These probes ran while the implementation was in progress. Recheck the final
diff before treating any as unresolved; no parallel edits to implementation.

## Reproduced schema findings

`AssetSpecification.model_validate` on 2026-09-27 accepted:

- `width_m: true` as 1.0 (strict numeric input is required).
- `width_m: 0.000001` normalized to **0.0** (invalid dimension after validation).
- `target_import_path: assets/x:stream` (Windows ADS).
- `target_import_path: assets/CON/model.glb` (reserved device component).
- `orientation: {up: +Y, front: -Y}` (collinear axes).

Use existing cross-platform PathGuard lexical protections in the spec contract,
reject unsupported orientation/policies rather than pretending processing supports
them, and validate post-normalization or do not round dimensions. YAML duplicate
keys also require explicit rejection, not safe_load last-key-wins.

## Preliminary provider inspection

The first `meshy_cli.py` draft defaults unknown cost to **5.0**, uses `4k` textures
despite 2048 spec, accepts optional durable repository, invents missing approval /
workflow IDs, and does not yet query known SUBMITTED task IDs. Existing known IDs
can fall through to another create. These violate paid safety and must not survive
final review. CLI nonzero/invalid JSON after attempted submission is ambiguous and
must not authorize another create. No signed URL/credential/raw output leaks.

## Architecture checkpoint

`AssetRevisionStatus` mirrors the entire workflow state; the revision table stores
this mutable status separately with no workflow relationship in the initial draft.
Avoid two authoritative lifecycles. Derive phase from authoritative workflow tasks;
revision metadata must identify the owning workflow. Monotonic allocation needs an
atomic unique claim and never ON CONFLICT overwrite a prior revision/spec.

## Completion criterion

Each correction requires focused executable tests. This document records findings,
not approval, and must be updated against the delivered final implementation.
