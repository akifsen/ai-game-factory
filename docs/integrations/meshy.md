# Meshy image-to-3D integration

V0.4 implements image-to-3D through `AssetGenerationProvider`. Vendor task identifiers stay in adapter/operation records; the workflow engine owns lifecycle state. Text-to-3D, remesh, rigging, animation and texture generation are outside this sprint.

## Local setup and doctor

The supported CLI is pinned to **meshy-cli 0.4.0**, requiring Node.js **22.12 or newer**. The verified Windows installation used Node 24.13.0. Provision the CLI explicitly before running Factory, for example:

```powershell
npm.cmd exec --yes --package=meshy-cli@0.4.0 -- meshy --version
```

Factory uses the local binary or the pinned npm cache with `--offline`; it never silently downloads a different CLI. Windows execution uses Node plus `npm-cli.js` when available, avoiding shell interpolation and `.cmd` execution inside the process runner. A missing offline cache is an unavailable tool, not permission to install automatically.

Use `gamefactory doctor` to distinguish unavailable tooling, credential missing/misconfiguration, and local readiness with approval required. Doctor does not generate a model or establish successful API generation. The child receives the existing `MESHY_API_KEY` only when present; its value is not logged. CLI-reported credential source is authoritative. A balance query, when explicitly performed, is neither an operation estimate nor a spending approval.

`image.generate` remains NOT_VERIFIED by this local doctor: the external image generator is separately authorized and its exact PNG/provenance is ingested. `asset.3d.generate` reports Meshy readiness, while Blender processing, Godot import and capture remain NOT_VERIFIED until an actual workflow supplies execution evidence.

## Operator flow

From an initialized project, with concept and provenance inside that project:

```text
gamefactory asset-create --spec asset-inputs/prop_energy_crate_01.yml --concept asset-inputs/concept.png --provenance asset-inputs/provenance.json --provider meshy --concept-source-type local_generation --dry-run
gamefactory asset-create --spec asset-inputs/prop_energy_crate_01.yml --concept asset-inputs/concept.png --provenance asset-inputs/provenance.json --provider meshy --concept-source-type local_generation
gamefactory approvals --workflow WORKFLOW_ID
gamefactory inspect WORKFLOW_ID
```

The first real checkpoint is human concept review. Its fingerprint binds specification, exact concept bytes, provenance and style constraints. Only after that human decision is recorded does `gamefactory resume WORKFLOW_ID` open the separate paid-generation approval. That paid checkpoint binds provider, operation, asset/revision, concept hash, budget reservation and request fingerprint. Neither concept approval nor provider readiness authorizes spending.

**This sprint stops before any real paid request.** The actual production record is documented in [the human checkpoint](../reports/v0.4/human-review-checkpoint.md). Offline acceptance uses `--provider fake` and explicitly named test actors; these decisions never approve the production concept or Meshy generation.

Outside this sprint, an operator who deliberately grants the separate paid approval may then resume it. `approve` records a decision; `resume` is the dispatch step. Inspect the checkpoint first. Rejection is terminal for that revision and never regenerates automatically; a new revision requires new approvals.

## Cost and durable recovery

The CLI does not supply a reliable pre-generation estimate. Factory preserves **UNKNOWN** (`null` internally). `--budget-reservation` or the configured maximum operation cost supplies a positive reservation ceiling, not a fabricated estimate. The reservation must fit the project budget and be part of the approval fingerprint. Finite, non-negative values are mandatory. Reported consumed credits are recorded separately; missing actual cost remains UNKNOWN and retains conservative budget liability.

Before create, Factory atomically claims a durable intent unique to the task, fingerprint and asset/revision/provider/operation. It persists the external task ID as soon as returned. A known ID is queried and downloaded without another create. A potentially accepted submission without an ID remains UNCERTAIN and requires reconciliation; blind retry is forbidden. The CLI operation ID is a local journal identifier, **not a server idempotency guarantee**.

A Blender or Godot failure does not authorize regeneration. Use the explicit task retry path against retained, hash-verified raw/processed artifacts. A known-ID reconciliation does not reserve the same paid liability twice. Existing approval inputs are reconstructed from the frozen artifact scope, so new output records do not accidentally authorize a different paid operation.

## Download and untrusted input boundary

Factory validates the pinned CLI v1 task/download envelopes, successful state, exactly one written GLB, expected path, declared size and SHA-256. The output goes to a fresh managed attempt directory; existing, linked, escaped or mismatched files fail closed. Signed download URLs and raw credential-bearing responses are not exported into evidence.

The pinned CLI has its own **2 GiB transfer cap**. Factory's **50 MiB GLB limit is enforced after download**; it is not advertised as a 50 MiB network-transfer bound. Before Blender, the independent parser rejects malformed geometry/accessors, external URIs, unsafe extensions and excessive decoded resources. No provider archive extraction is implemented or required for this direct-GLB path.

See [the verified CLI contract](../reports/v0.4/meshy-cli-contract.md), [asset workflow](../pipelines/asset-production.md), and [Blender processing](blender-asset-processing.md). Unit tests use a fake CLI runner; real paid compatibility remains intentionally unverified until separately authorized acceptance.
