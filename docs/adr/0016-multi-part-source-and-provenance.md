# ADR 0016: Assembly source kind and provenance

## Status

Accepted and implemented in V0.7.0, with the processing deviation recorded below.

## Context

Meshy image-to-3D returns one mesh. Automatically segmenting that mesh into semantic parts with correct pivots would be a research problem, and a silent wrong split would pass an import check (see the pivot collapse in ADR 0013). Paid generation is bound by ADR 0007, ADR 0009 and ADR 0010. Concept provenance already uses `local_operator_*` type names and records `paid` (`concept_ingest.py`).

## Decision

1. **Three separate layers.** They are never merged:

   | Layer | Example | Meaning |
   |---|---|---|
   | Asset profile | `vehicle@1` | production contract for an asset class |
   | Source kind | `local_operator_assembly` | where the geometry came from |
   | Geometry mode (capability) | `assembly` | how the geometry is processed and validated (ADR 0018) |

   There is no `assembly@1` profile. A profile declares which source kinds and which geometry mode it accepts.
2. **Source kinds in V0.7:** `provider_generated` (the existing Meshy path) and `local_operator_assembly` (a GLB supplied by an operator, authored in Blender or by script). Any specification that declares `parts` or `sockets` requires `local_operator_assembly`.
3. **The Meshy V0.7 path is single-mesh only.** A specification that declares `parts` or `sockets`, or a profile whose geometry mode is `assembly`, cannot bind to a provider request. The rejection happens when the paid-request snapshot is created, before any approval, intent or provider call. ADR 0007, ADR 0009 and ADR 0010 are unchanged for single-mesh assets.
4. **Provenance record** (`source_provenance_type: local_operator_assembly`):
   - source artifact SHA-256 and byte size
   - `paid: false` (always, and asserted by the verifier)
   - authoring tool name and version (declared)
   - `source_front`, which is **required** (`-Z` or `+Z`, ADR 0014); missing means rejection
   - the declared part map and socket map, which must match the specification
   - the registering actor and the reason
   - optional `derived_from` entries per part: `{part_id, source_artifact_sha256, source_asset_id, source_revision}`, where available

   The source is immutable. A different source requires a new source version. Approval fingerprints bind the source hash in the same way concept fingerprints do (ADR 0012).
5. **Self-contained input.** A V0.7 assembly submitted for processing contains all the geometry needed to process it. `derived_from` entries are **provenance only**. They are never resolved, fetched or required during processing, validation or cold verification. The assembly stays processable and cold-verifiable from its retained source package and evidence alone, with no other workspace and no external mutable artifact store. External, hash-linked dependency resolution is deferred to a later content-addressed dependency model.
6. **Source provenance is not approval.** A source kind never implies approval. Operator-authored assemblies go through the same review and final approval gates. `local_operator_assembly` creates no provider intent, no paid approval and no cost-ledger entry. The final approval and the production receipt record `generation: local_operator_assembly` and `paid: false` in place of a provider operation.
7. **Processing may normalize, not restructure.** The Blender processor may apply the single front rotation from ADR 0014 and generate LODs per part. It may not re-parent, merge, split or re-pivot parts. Any such difference between the source hierarchy and the processed hierarchy is a FAIL.

## Consequences

A revision gains a source kind. `asset-evidence-0.7.0` carries a `source_provenance` role, plus the retained source GLB, in place of `provider_operation`, `cost_record` and `paid_approval` for assemblies. The cold verifier accepts exactly one of the two role sets, never a mixture, and rejects `paid: true` for `local_operator_assembly`. Reusing earlier Factory meshes is possible by copying them into the assembly, and that reuse is auditable through `derived_from`.

## Implementation (V0.7.0)

- The registration is `asset-source-registration-0.7.0` (`core/domain/assembly_source.py`, JSON Schema in `src/gamefactory/schemas/`). `source_front` has no default; `paid` must be `false`; the part and socket maps must equal the specification; `derived_from` is validated as data and never resolved. `gamefactory asset register-source` drafts it from the specification.
- `gamefactory asset assemble` creates the graph `PREPARE -> PROCESS -> VALIDATE -> GODOT -> FINAL-REVIEW -> EVIDENCE` (graph version `0.7.0`). There is no concept review, paid request, readiness check or paid approval, and a refusing provider occupies the provider slot. The revision records the source hash as its raw hash and has no concept hash.
- Assemblies are rejected before any snapshot, approval or intent in four places: workflow creation on the provider path, the paid-request snapshot handler, and both provider adapters (Meshy and fake).
- **Deviation from item 7: no Blender round trip for assemblies.** Processing is a deterministic, transform-only normalization in Python. The V0.7 spike showed that a Blender import/export collapsed every pivot while the hierarchy imported cleanly, so re-exporting operator assemblies through Blender would reintroduce the very defect ADR 0013 guards against. Blender remains the declared authoring tool: the real acceptance authors a vehicle in Blender, exports it with the default glTF exporter (a +Z source) and runs it through the factory. Per-part LOD generation is therefore not performed; an assembly that needs LOD1 must carry `SM_<asset>_<part>_LOD1` from the operator, and `part.mesh` checks it.
- The final approval context binds the source GLB, the registration, the processing report and the other evidence hashes instead of a concept. `production-receipt-0.7.0` records `source_kind: local_operator_assembly`, `paid: false` and `paid_provider_invocations: 0` (the field is `source_kind` rather than `generation`).
- `asset-evidence-0.7.0` bundles for assemblies carry `source_glb`, `source_provenance` and `normalization` roles. The cold verifier rejects any concept, paid, provider, cost or raw role (mixed role set), rejects `paid: true`, and recomputes the normalization from the retained source.
