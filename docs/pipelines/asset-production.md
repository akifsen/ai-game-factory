# V0.4 asset production pipeline

The asset workflow is a `WorkflowEngine` DAG. The engine remains the sole lifecycle authority; asset revision records bind immutable inputs and outputs to that workflow without maintaining a second status machine.

## Gates and stages

1. Validate the strict `asset-spec-0.4.0` specification and create a monotonic revision.
2. Ingest one concept image with provenance. The concept gate fingerprints the specification, concept bytes and metadata, and style constraints. A changed input requires a new decision.
3. Create a distinct paid Meshy image-to-3D approval. Display the provider, operation, revision, concept hash, estimate (`UNKNOWN` when the provider cannot state it), approval ID, and exact resume command. No provider request occurs before explicit approval and a persisted operation intent.
4. Retain the downloaded raw GLB. Process a copy in Blender background mode, preserving the raw bytes and recording tool/script/input/output hashes.
5. Run the deterministic GLB validator. Blender exit success alone does not pass this gate.
6. Stage the validated GLB in an isolated Godot project, import it, instantiate the LOD0 mesh and build a live `StaticBody3D` / `CollisionShape3D` from the collider proxy geometry. The runtime observer checks mesh, collider, bounds, revision, attempt, and engine errors. It saves front, three-quarter, and side images from Godot's renderer.
7. The final human review binds the approved concept, specification revision, processed GLB, validation, runtime observation and rendered evidence. Rejection is terminal for that revision; it never submits another paid request.

Generic optional policy settings cannot waive the concept, paid-operation, or final human gates. Test actors can be used by isolated tests only and never count as production human approval.

## Create, approve, and resume

Validate inputs without creating a workflow or submitting a provider request:

```powershell
gamefactory --project . asset-create --spec src/gamefactory/resources/specs/prop_energy_crate_01.yml --concept docs/reports/v0.4/concept/prop_energy_crate_01.png --provenance docs/reports/v0.4/concept/local-sdxl-provenance.json --provider meshy --dry-run
```

The dry run reports the full specification, concept hash/state, local readiness, required gates, expected stages, and cost reservation. `UNKNOWN` is the estimate when Meshy does not supply a reliable price. A budget reservation is a separately authorized cap, not an estimate. For a real workflow, use `--budget-reservation <credits>` or the configured policy maximum; zero or missing budget blocks before provider dispatch.

```powershell
gamefactory --project . asset-create --spec src/gamefactory/resources/specs/prop_energy_crate_01.yml --concept docs/reports/v0.4/concept/prop_energy_crate_01.png --provenance docs/reports/v0.4/concept/local-sdxl-provenance.json --provider meshy --budget-reservation 25
gamefactory --project . approve CONCEPT_APPROVAL_ID --actor "Human reviewer"
gamefactory --project . resume WORKFLOW_ID
gamefactory --project . approve PAID_APPROVAL_ID --actor "Human reviewer"
gamefactory --project . resume WORKFLOW_ID
gamefactory --project . report --workflow WORKFLOW_ID
```

The last `report` command exports the static comparison while final review is pending, so the reviewer can inspect the evidence before deciding. It creates an immutable numbered snapshot under `.gamefactory/reports/<workflow-id>/`. After that review, approve or reject the final approval and resume the workflow. Use `gamefactory retry WORKFLOW_ID TASK_ID` only for a retryable failed local stage. The retained raw GLB is reused; provider generation is never repeated by a local retry. A known remote task ID is queried and downloaded by ID; an accepted operation without a known ID stays uncertain and cannot be resubmitted automatically.

The real sample concept has an SDXL provenance sidecar and remains pending. A `--dry-run` result is not concept approval. CI and integration fixtures use `--provider fake` and explicitly labeled test actors; their fake-provider evidence is not production artwork approval.

## Domain records and lifecycle ownership

The V0.4 concepts map onto the existing application records rather than shadowing `WorkflowEngine`:

| V0.4 concept | Current record or API | Owner |
| --- | --- | --- |
| `AssetRequest` | `GenerationRequest` plus the immutable asset task parameter snapshot and `workflow_id` / `task_id` | workflow adapter contract |
| `AssetSpecification` | strict `AssetSpecification` model, schema, and semantic fingerprint | domain contract |
| `AssetRevision` | `AssetRevision` row keyed by asset and monotonic revision, linked to one workflow ID | asset revision repository |
| `AssetArtifact` | existing `Artifact` row and verified project-relative file; revision hashes point to accepted outputs | artifact manager/repository |
| `AssetValidation` | `AssetValidationResult` with structured `ValidationFinding` values in the validator report artifact | GLB validator |
| `GenerationOperation` | `ProviderOperationIntent` durable claim plus the paid task's `Execution` attempts and provider operation ID | provider adapter and WorkflowEngine |
| `ProcessingOperation` | local processing task `Execution`, processing report artifact, and separate processed GLB artifact | Blender adapter and WorkflowEngine |

Workflow and task state, approvals, retries, and execution history remain authoritative in WorkflowEngine and its existing repositories. Revision rows do not duplicate lifecycle status. Each approval fingerprint binds the current parameters and the artifact IDs recorded when that decision was requested. The paid operation intent is durable before submission; the adapter may query a known ID but must never make a second create request to recover an uncertain operation.

## Evidence bundle

An offline review package has a `manifest.json` with schema `asset-evidence-0.4.0`. Each referenced file entry declares a unique relative path, role, byte size, and SHA-256. The package carries the specification, concept and provenance, both approval receipts, provider operation and cost record, raw and processed GLBs, processing report, validation findings, runtime observation, and front/three-quarter/side Godot captures. The static HTML compares the concept with runtime views and summarizes metrics, findings, costs, actors, decisions, and hashes. Test/fake provider evidence is prominently labeled. A pending final decision remains `PENDING`; the report separates technical integrity from human approval.

Cold verification uses only the Python standard library and isolated mode. The bundle also includes the verifier, so this remains usable from an installed wheel or copied review directory:

```text
python -I scripts/verify_asset_bundle.py <bundle-directory>
```

The verifier rejects absolute/traversal paths, linked files/directories, unlisted or altered references, stale workflow/revision/attempt observations, wrong processed-GLB hashes, undecodable captures, missing collider or physics-ray evidence, engine errors, missing/invalid approval fingerprints, and final decisions that do not match the receipt. Its integrity result does not make an aesthetic judgment or turn a pending decision into approval.

## Recovery and paid safety

An existing provider task ID is queried/resumed through the provider adapter. A possibly submitted request without a known ID remains `UNCERTAIN`; the workflow must not blindly submit again. A failed DCC stage can be retried against the retained raw GLB. Unknown cost remains explicit and is blocked when the applicable budget cannot accommodate the operation. CI uses only the fake provider and never receives a Meshy credential.

See [Meshy integration](../integrations/meshy.md), [Blender asset processing](../integrations/blender-asset-processing.md), and the [V0.4 work plan](../work-plan-v0.4.md).
