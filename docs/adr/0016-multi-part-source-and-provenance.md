# ADR 0016: Assembly source kind and provenance

## Status

Accepted for V0.7 (Step 2 architecture decision). Foundation implemented (assembly source provenance, bounded snapshot ingest, and standalone verification). Full workflow/approval integration deferred.

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

## Implementation status

Bounded standalone authored assembly source ingest foundation implemented and revised:
- `gamefactory.core.domain.assembly_source`:
  - Strict immutable typed `local_operator_assembly` provenance schema (`AssemblySourceProvenance`), versioned closed fields (`schema_version: "0.7.0"`, `source_provenance_type: "local_operator_assembly"`, `paid: false` literal boolean False), and spec fingerprint binding.
  - Explicit mandatory sidecar markers: `paid: false`, `schema_version: "0.7.0"`, and `source_provenance_type: "local_operator_assembly"` are required fields with no default inference or fallback from missing metadata.
  - Spec part/socket map alignment, bounded metadata, and validated non-fetched `derived_from` entries.
  - Immutable artifact authority and provenance decoupling: `AssemblyIngestResult` retains canonical serialized JSON bytes (`_provenance_canonical_bytes`) and source artifact bytes. Every access to `.provenance` deserializes a fresh, detached `AssemblySourceProvenance` instance. That model view has mutable Python containers; mutating it cannot alter the retained canonical bytes or later views. Constructor eliminates speculative compatibility kwargs.
  - Publication marker schema (`AssemblyPublicationMarker`): versioned closed schema (`schema_version: "0.7.0"`, `marker_type: "assembly_publication_completion"`) binding source GLB SHA-256 and size, provenance sidecar SHA-256 and size, specification fingerprint, and ISO timestamp.
- `gamefactory.adapters.assets.assembly_ingest`:
  - Bounded (<= 50 MiB) source byte snapshot retention and preflight on the retained snapshot bytes.
  - Reuses bounded `glb_validator._inspect` on decoded GLB documents and BIN chunks, ensuring strict limits on geometry, accessors (<= 1,000,000 elements), embedded PNG/JPEG images, materials, extensions (forbidden), skins/rigs (forbidden), animations (forbidden), and node hierarchy depth (<= 128) without cycles, wrapping failures in controlled `ValidationError`.
  - Requires nonempty processable actual static triangle geometry.
  - GLB hierarchy contract alignment: exactly one active scene root named `ROOT` with identity transform; declared root `PART_` node is its direct child without arbitrary wrappers; each `PART_` node contains only declared child parts, declared socket leaves, and its direct identity LOD0 mesh child (`SM_{asset_id}_{part_id}_LOD0`). Processed generated LOD1 and root box colliders are excluded from source packages and remain downstream processor scope.
  - Normative parent-frame transform validation:
    - In column-vector convention where canonical orientation satisfies $R_{canonical} = R_{y180} \cdot R_{raw}$, the expected raw parent-frame orientation is $R_{raw} = R_{y180} \cdot R_{canonical}$ (parent-frame multiplication on the left, rejecting non-commutative swapped or double rotations).
    - Checks the full root transform including translation: for `+Z` source front, $t_{raw} = R_{y180} \cdot t_{canonical} = [-x, y, -z]^T$.
    - Descendant part pivot positions and 3x3 orientation bases are verified against bound asset profile tolerances (`pivot_tolerance_m` in meters, geodesic trace angle against `basis_tolerance_deg` in degrees), never arbitrary hardcoded tolerances.
    - Transform scale, reflection, and shear rejection: part nodes require positive uniform scale ($s_x = s_y = s_z > 0$), column orthogonality ($|u_i \cdot u_j| \le 10^{-4}$), and positive unit determinant ($\det(U) \approx +1.0$), rejecting negative scale, reflections, and shear. Socket nodes strictly require identity scale.
    - Sockets and motion extras: sockets validate declared local position against `socket_position_tolerance_m` and local rotation against `socket_angle_tolerance_deg`. Nodes with revolute/prismatic motion declare matching `extras["gf_motion"]` and `extras["gf_axis"]` as structural source contract.
    - Semantic pivot correctness is never inferred from bounding box centroids or mesh centroids; human review still establishes semantic pivot correctness.
  - Publication guarantee: exclusive claim + marker-gated completion (not whole-directory filesystem atomicity):
    - Python's portable standard-library directory rename API does not expose a uniform atomic no-replace operation across POSIX and Windows. Publication therefore uses an atomic exclusive directory claim (`safe_package_dir.mkdir(parents=False, exist_ok=False)`) and per-file no-replace hard links.
    - If the target directory already exists (whether populated, empty, or from an interrupted previous publication), publication strictly raises `ValidationError` and refuses to overwrite.
    - The retained GLB and provenance sidecar are linked into the exclusively claimed directory with atomic no-replace hard-link creation. If the filesystem does not support same-volume hard links, publication fails closed. Staging links for those files are cleaned before the marker is linked.
    - `publication_marker.json` is atomically hard-linked into place with no-replace semantics as the final package mutation. Only best-effort cleanup of the staging marker/directory follows; cleanup failure cannot turn an already published package into a reported failure.
    - Reader gates (`verify_retained_assembly` and `inspect_untrusted_assembly_package`) mandate the presence of a valid, uncorrupted, untampered `publication_marker.json` whose bound digests match the package contents. If a crash or concurrent reader accesses the directory before the marker is written, reading fails cleanly.
    - Cleanup on publication failure tracks exact owned files written to the target directory and staging directory. It removes only those files and executes `rmdir()` only if each directory is empty. It NEVER recursively deletes either directory and NEVER deletes foreign or concurrently injected files.
  - Reader security and verification:
    - Authenticated verification (`verify_retained_assembly`): requires a mandatory trusted sidecar digest pin (sha256). The publication marker signals completion, not replacement authentication.
    - Unauthenticated inspection (`inspect_untrusted_assembly_package`): applies identical pre-resolution lexical path component checks (rejecting symlinks, directory junctions, and reparse points) before resolving paths, effective both with and without `managed_root`. It is explicitly unauthenticated inspection and does not substitute for verification.
- **Platform limitations and future gates**:
  - Exclusive directory creation, atomic per-file no-replace hard links, and completion-marker gating prevent publication from overwriting a path another writer created inside the claimed package. This does not make the entire directory appear atomically: readers arriving before the marker fail closed. Filesystems without hard-link support are unsupported for publication.
  - Full source-to-processed hierarchy preservation, transform preservation, and human review remain future gates; this foundation is restricted to standalone bounded source ingest and verification.
