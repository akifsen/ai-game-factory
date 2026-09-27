"""V0.4 static-prop orchestration built on the existing WorkflowEngine.

This module creates one engine DAG per immutable asset revision. It does not
implement a second lifecycle machine: approval, resume, task attempts, artifacts,
and final workflow completion remain owned by WorkflowEngine.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import preflight_glb
from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.engines.godot_execution import _ENGINE_ERROR_PATTERNS
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.errors import (
    ArtifactError,
    AssetValidationFailedError,
    DccFailedError,
    EngineImportFailedError,
    ProviderUncertainError,
    RawArtifactInvalidError,
    RuntimeValidationFailedError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    CostClass,
    Execution,
    Task,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)
from gamefactory.workflows.ports import AssetGenerationProvider, GenerationRequest


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_asset_production_workflow(
    project_id: str,
    project_root: Path | str,
    specification: AssetSpecification,
    concept_image: Path | str,
    concept_provenance: Path | str,
    *,
    provider_name: str,
    provider_estimate: float | None = None,
    budget_reservation: float | None = None,
    provider_cost_unit: str = "credits",
    concept_source_type: str = "imported",
    workflow_id: str | None = None,
    revision_repository: AssetRevisionRepository | None = None,
) -> tuple[Workflow, list[Task]]:
    """Create an asset DAG after validating inputs; no provider is contacted here."""
    project_path = Path(project_root).resolve(strict=True)
    concept = Path(concept_image).resolve(strict=True)
    provenance = Path(concept_provenance).resolve(strict=True)
    if concept.suffix.lower() != ".png" or not concept.is_file() or not provenance.is_file():
        raise ValidationError("Concept must be a PNG and provenance must be a regular file")
    if provider_name not in {"fake", "meshy"}:
        raise ValidationError("Asset provider must be 'fake' or 'meshy'")
    try:
        concept_relative = concept.relative_to(project_path).as_posix()
        provenance_relative = provenance.relative_to(project_path).as_posix()
    except ValueError as exc:
        raise ValidationError(
            "Concept and provenance inputs must be within the project root"
        ) from exc
    reservation = provider_estimate if provider_estimate is not None else budget_reservation
    if reservation is None:
        reservation = 0.0  # No reservation; paid handler blocks before provider dispatch.
    if isinstance(reservation, bool) or not math.isfinite(float(reservation)) or reservation < 0:
        raise ValidationError("Provider estimate/reservation must be non-negative")
    if provider_estimate is not None and (
        isinstance(provider_estimate, bool)
        or not math.isfinite(float(provider_estimate))
        or provider_estimate < 0
    ):
        raise ValidationError("Provider estimate must be finite and non-negative")
    if budget_reservation is not None and (
        isinstance(budget_reservation, bool)
        or not math.isfinite(float(budget_reservation))
        or budget_reservation < 0
    ):
        raise ValidationError("Budget reservation must be non-negative")
    wf_id = workflow_id or generate_id("WF-ASSET")
    spec_hash = spec_fingerprint(specification)
    concept_hash = sha256_file(concept)
    if revision_repository is None:
        raise ValidationError("Asset workflow requires the durable AssetRevisionRepository")
    # The revision table references workflows. Persist the workflow identity before
    # atomically allocating its revision; register_workflow later adds the task DAG.
    base_workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Asset production: {specification.asset_id}",
        status=WorkflowStatus.PENDING,
    )
    WorkflowRepository(revision_repository.db).save(base_workflow)
    revision = revision_repository.allocate_revision(
        specification.asset_id, wf_id, spec_hash, concept_hash=concept_hash
    )
    revision_number = revision.revision_number
    asset_dir = f".gamefactory/assets/{specification.asset_id}/r{revision_number:03d}"
    workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Asset production: {specification.asset_id} r{revision_number:03d}",
        status=WorkflowStatus.PENDING,
    )
    common = {
        "asset_id": specification.asset_id,
        "revision_number": revision_number,
        "specification": specification.model_dump(mode="json"),
        "specification_hash": spec_hash,
        "concept_source": concept_relative,
        "concept_provenance_source": provenance_relative,
        "concept_source_hash": concept_hash,
        "concept_provenance_hash": sha256_file(provenance),
        "concept_source_type": concept_source_type,
        "provider": provider_name,
        "provider_estimate": provider_estimate,
        "budget_reservation": budget_reservation,
        "cost_unit": provider_cost_unit,
        "asset_dir": asset_dir,
    }
    prep, concept_review, generate, process, validate, runtime, final, finish = (
        f"{wf_id}-{suffix}"
        for suffix in (
            "PREPARE",
            "CONCEPT-REVIEW",
            "PAID-GENERATION",
            "PROCESS",
            "VALIDATE",
            "GODOT",
            "FINAL-REVIEW",
            "EVIDENCE",
        )
    )
    tasks = [
        Task(
            id=prep,
            workflow_id=wf_id,
            name="Validate and retain specification and concept",
            task_type="asset_prepare",
            parameters=common,
        ),
        Task(
            id=concept_review,
            workflow_id=wf_id,
            name="Human concept approval",
            task_type="asset_concept_review",
            depends_on=[prep],
            parameters=common,
        ),
        Task(
            id=generate,
            workflow_id=wf_id,
            name="Approved Meshy image-to-3D generation",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            depends_on=[concept_review],
            parameters={
                **common,
                "cost": float(reservation),
                "cost_unit": provider_cost_unit,
                "estimate_label": "UNKNOWN" if provider_estimate is None else "KNOWN",
                "mandatory_approval_type": "paid_generation",
            },
        ),
        Task(
            id=process,
            workflow_id=wf_id,
            name="Process raw GLB in Blender",
            task_type="asset_process",
            depends_on=[generate],
            parameters=common,
        ),
        Task(
            id=validate,
            workflow_id=wf_id,
            name="Independently validate processed GLB",
            task_type="asset_validate",
            depends_on=[process],
            parameters=common,
        ),
        Task(
            id=runtime,
            workflow_id=wf_id,
            name="Import, run, and render in staged Godot",
            task_type="asset_godot",
            depends_on=[validate],
            parameters=common,
            timeout_seconds=180.0,
        ),
        Task(
            id=final,
            workflow_id=wf_id,
            name="Human final runtime visual review",
            task_type="asset_final_review",
            depends_on=[runtime],
            parameters=common,
        ),
        Task(
            id=finish,
            workflow_id=wf_id,
            name="Record verified asset evidence",
            task_type="record_evidence",
            depends_on=[final],
        ),
    ]
    return workflow, tasks


@dataclass
class AssetProductionHandlers:
    root: Path
    artifacts: ArtifactRepository
    revisions: AssetRevisionRepository
    approvals: ApprovalRepository
    intents: ProviderOperationIntentRepository
    evidence: EvidenceRepository
    gates: QualityGateRepository
    executions: ExecutionRepository
    artifact_manager: ArtifactManager
    provider: AssetGenerationProvider
    blender_path: str | None = None
    godot_path: str | None = None
    runner: ProcessRunner | None = None

    def __post_init__(self) -> None:
        self.root = self.root.resolve(strict=True)
        self.runner = self.runner or ProcessRunner(sanitize_output=True)

    def _path(self, task: Task, name: str) -> Path:
        relative = f"{task.parameters['asset_dir']}/{name}"
        return PathGuard(self.root).ensure_safe_parent(relative)

    def _register(
        self, workflow: Workflow, task: Task, execution: Execution, kind: str, path: Path
    ) -> str:
        relative = path.resolve(strict=True).relative_to(self.root).as_posix()
        artifact = self.artifact_manager.register_file_artifact(
            workflow.id, task.id, kind, task.task_type, relative
        )
        self.artifacts.save(artifact)
        return artifact.id

    def prepare(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        spec = parse_asset_specification(task.parameters["specification"])
        if spec_fingerprint(spec) != task.parameters["specification_hash"]:
            raise ValidationError("Asset specification fingerprint changed")
        path_guard = PathGuard(self.root)
        image_source = path_guard.resolve_safe_path(task.parameters["concept_source"])
        provenance_source = path_guard.resolve_safe_path(
            task.parameters["concept_provenance_source"]
        )
        if sha256_file(image_source) != task.parameters["concept_source_hash"]:
            raise ValidationError("Concept image changed after workflow creation")
        if sha256_file(provenance_source) != task.parameters["concept_provenance_hash"]:
            raise ValidationError("Concept provenance changed after workflow creation")
        directory = self._path(task, "specification.yml").parent
        directory.mkdir(parents=True, exist_ok=True)
        spec_path = self._path(task, "specification.json")
        spec_path.write_text(
            json.dumps(spec.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        provenance_path = self._path(task, "concept-provenance.json")
        from gamefactory.adapters.images.concept_ingest import ingest_concept_image

        concept_path, provenance = ingest_concept_image(
            image_source,
            self._path(task, "concept.png"),
            spec_fingerprint(spec),
            sidecar_provenance_path=provenance_source,
            source_type=task.parameters["concept_source_type"],
        )
        provenance_path.write_text(
            json.dumps(provenance.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        ids = [
            self._register(workflow, task, execution, "asset-specification", spec_path),
            self._register(workflow, task, execution, "asset-concept", concept_path),
            self._register(workflow, task, execution, "asset-concept-provenance", provenance_path),
        ]
        return TaskHandlerResult(
            1, "Specification, concept, and provenance retained with immutable digests", ids
        )

    def concept_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        values = self._artifact_hashes(workflow.id)
        expected = {
            key: values[key]
            for key in ("asset-specification", "asset-concept", "asset-concept-provenance")
        }
        return {
            "asset_id": task.parameters["asset_id"],
            "revision": task.parameters["revision_number"],
            "specification_hash": task.parameters["specification_hash"],
            "artifacts": expected,
            "style_constraints": task.parameters["specification"]["style_constraints"],
        }

    def paid_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        concept = next(
            (
                a
                for a in self.artifacts.list_by_workflow(workflow.id)
                if a.artifact_type == "asset-concept"
            ),
            None,
        )
        if concept is None:
            raise ArtifactError("Paid review cannot proceed without the approved concept artifact")
        self.artifact_manager.verify_artifact_integrity(concept)
        return {
            "provider": task.parameters["provider"],
            "operation": "image-to-3d",
            "asset_id": task.parameters["asset_id"],
            "revision": task.parameters["revision_number"],
            "concept_sha256": concept.content_hash,
            "asset_specification_sha256": task.parameters["specification_hash"],
            "estimated_cost": task.parameters["provider_estimate"]
            if task.parameters["provider_estimate"] is not None
            else "UNKNOWN",
            "budget_reservation": task.parameters["budget_reservation"],
            "cost_unit": task.parameters["cost_unit"],
        }

    def _artifact_hashes(self, workflow_id: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in self.artifacts.list_by_workflow(workflow_id):
            self.artifact_manager.verify_artifact_integrity(item)
            values[item.artifact_type] = item.content_hash
        return values

    def review_concept(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id
                and a.approval_type == "concept_review"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ArtifactError("Concept evidence is missing")
        receipt = self._path(task, "concept-approval.json")
        receipt.write_text(
            json.dumps(
                {
                    "approval_id": approval.id,
                    "approval_type": approval.approval_type,
                    "status": approval.status.value,
                    "fingerprint": approval.operation_hash,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact_id = self._register(
            workflow, task, execution, "asset-concept-approval-record", receipt
        )
        return TaskHandlerResult(
            1,
            "Concept human approval recorded; paid generation remains a separate gate",
            [artifact_id],
        )

    def paid_generate(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        params = task.parameters
        concept = next(
            (
                a
                for a in self.artifacts.list_by_workflow(workflow.id)
                if a.artifact_type == "asset-concept"
            ),
            None,
        )
        if concept is None:
            raise ArtifactError("Approved concept artifact is missing")
        self.artifact_manager.verify_artifact_integrity(concept)
        approval_rows = self.approvals.list_by_workflow(workflow.id)
        approval = next(
            (
                a
                for a in approval_rows
                if a.task_id == task.id
                and a.approval_type == "paid_generation"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None or approval.status.value != "APPROVED":
            raise ValidationError("Current paid-generation approval is missing or stale")
        if (
            params.get("provider_estimate") is None
            and float(params.get("budget_reservation", 0.0)) <= 0
        ):
            raise ValidationError(
                "Provider cost is UNKNOWN and no explicit budget reservation was authorized; no paid request was sent"
            )
        raw_path = self._path(task, f"raw-attempt-{execution.attempt_number}.glb")
        if raw_path.exists():
            raise RawArtifactInvalidError("Refusing to overwrite a raw GLB from an earlier attempt")
        concept_path = self.root / concept.relative_path
        request = GenerationRequest(
            prompt=f"Image-to-3D static prop: {params['asset_id']}",
            target_format="glb",
            parameters={
                **params,
                "workflow_id": workflow.id,
                "task_id": task.id,
                "approval_id": approval.id,
                "concept_hash": concept.content_hash,
                "concept_image_path": str(concept_path),
                "output_path": str(raw_path),
                "cost": params.get("provider_estimate"),
                "budget_reservation": params.get("budget_reservation"),
                "max_triangles_lod0": params["specification"]["geometry_budget"][
                    "max_triangles_lod0"
                ],
            },
            operation_hash=approval.operation_hash,
        )

        def account_known_cost(response_cost: object = None) -> None:
            """Persist known billing before any later download or validation can fail."""
            intent = self.intents.get_by_task(task.id)
            values = [
                value
                for value in (response_cost, getattr(intent, "actual_cost", None))
                if value is not None
            ]
            if not values:
                return
            candidates: list[float] = []
            for value in values:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0
                ):
                    raise ValidationError(
                        "Provider actual cost must be a finite non-negative number"
                    )
                candidates.append(float(value))
            known_actual = max(candidates)
            previous_liability = sum(
                max(0.0, attempt.cost)
                for attempt in self.executions.list_by_task(task.id)
                if attempt.id != execution.id
            )
            authorized_reservation = max(0.0, float(params.get("cost", 0.0)))
            cumulative_liability = max(authorized_reservation, known_actual)
            execution.cost = max(0.0, cumulative_liability - previous_liability)
            execution.cost_unit = getattr(intent, "cost_unit", "credits")

        try:
            response = self.provider.generate(request)
            account_known_cost(response.details.get("actual_cost"))
        except ProviderUncertainError as exc:
            account_known_cost()
            raise ProviderUncertainError(
                str(exc),
                task_id=task.id,
                execution_id=execution.id,
                provider=self.provider.name,
                details={**(exc.details or {}), "process_state_uncertain": True},
            ) from exc
        except Exception:
            account_known_cost()
            raise
        if response.status == "SUBMITTED":
            deadline = time.monotonic() + min(task.timeout_seconds, 300.0)
            while response.status == "SUBMITTED" and time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    response = self.provider.generate(request)
                    account_known_cost(response.details.get("actual_cost"))
                except ProviderUncertainError as exc:
                    account_known_cost()
                    raise ProviderUncertainError(
                        str(exc),
                        task_id=task.id,
                        execution_id=execution.id,
                        provider=self.provider.name,
                        details={**(exc.details or {}), "process_state_uncertain": True},
                    ) from exc
                except Exception:
                    account_known_cost()
                    raise
        if response.status != "SUCCESS":
            raise ProviderUncertainError(
                "Provider operation has no confirmed downloadable terminal result; retry is forbidden",
                task_id=task.id,
                execution_id=execution.id,
                provider=self.provider.name,
                details={
                    "process_state_uncertain": True,
                    "external_op_id": response.external_op_id,
                },
            )
        output = Path(response.output_path).resolve(strict=True) if response.output_path else None
        if output is None and hasattr(self.provider, "cli_runner"):
            download_dir = self._path(
                task, f"download-attempt-{execution.attempt_number}/.keep"
            ).parent
            download_dir.mkdir(parents=True, exist_ok=False)
            output, _ = self.provider.cli_runner.download_glb(response.external_op_id, download_dir)
        if output is None or not output.is_file() or output.stat().st_size <= 0:
            raise RawArtifactInvalidError(
                "Provider terminal success did not produce a verified raw GLB"
            )
        try:
            preflight_glb(output)
        except (ValueError, OSError) as exc:
            raise RawArtifactInvalidError(
                f"Provider raw GLB failed structural validation: {exc}"
            ) from exc
        if output != raw_path.resolve():
            shutil.copyfile(output, raw_path)
        raw = self._register(workflow, task, execution, "asset-raw-glb", raw_path)
        revision = self.revisions.get(params["asset_id"], params["revision_number"])
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        revision.raw_glb_hash = sha256_file(raw_path)
        self.revisions.save(revision)
        execution.external_op_id = response.external_op_id
        execution.provider = self.provider.name
        # Keep actual provider billing in the durable intent as well. On query-only
        # recovery, only the incremental delta above the original reservation is
        # charged, preventing duplicate liability while covering overages.
        execution.cost_unit = response.cost_unit
        return TaskHandlerResult(
            1, "Provider task completed and a raw GLB was downloaded and retained", [raw]
        )

    def recovery_check(self, workflow: Workflow, task: Task, execution: Execution) -> bool:
        """Return true only when the durable intent has a known remote ID safe to query."""
        del workflow, execution
        intent = self.intents.get_by_task(task.id)
        if intent is None or not intent.external_task_id:
            return False
        cli = getattr(self.provider, "cli_runner", None)
        if cli is None:
            return True  # Deterministic fake adapter resolves the persisted intent locally.
        try:
            remote = cli.get_task(intent.external_task_id)
        except Exception:
            return False
        return isinstance(remote, dict) and bool(remote.get("status"))

    def process(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        spec = parse_asset_specification(task.parameters["specification"])
        raw_artifact = next(
            (
                a
                for a in self.artifacts.list_by_workflow(workflow.id)
                if a.artifact_type == "asset-raw-glb"
            ),
            None,
        )
        if raw_artifact is None:
            raise ArtifactError("Raw generation GLB is missing")
        raw = self.root / raw_artifact.relative_path
        self.artifact_manager.verify_artifact_integrity(raw_artifact)
        processed = self._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        report = self._path(task, f"processing-attempt-{execution.attempt_number}.json")
        try:
            result = BlenderAssetProcessor(self.blender_path, self.runner).process_asset(
                raw, processed, spec, report
            )
        except ValueError as exc:
            raise RawArtifactInvalidError(
                "Raw provider GLB failed safe preflight before Blender processing",
                details={"reason": str(exc)},
            ) from exc
        if result.status != "SUCCESS":
            raise DccFailedError(
                "Blender processor did not produce SUCCESS",
                exit_code=result.exit_code,
                details={"asset_id": spec.asset_id, "status": result.status},
            )
        ids = [
            self._register(workflow, task, execution, "asset-processed-glb", processed),
            self._register(workflow, task, execution, "asset-processing-report", report),
        ]
        revision = self.revisions.get(spec.asset_id, int(task.parameters["revision_number"]))
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        return TaskHandlerResult(
            1, "Blender produced a separate processed GLB from the retained raw artifact", ids
        )

    def validate(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        from gamefactory.adapters.assets.glb_validator import validate_glb

        spec = parse_asset_specification(task.parameters["specification"])
        artifact = next(
            (
                a
                for a in self.artifacts.list_by_workflow(workflow.id)
                if a.artifact_type == "asset-processed-glb"
            ),
            None,
        )
        if artifact is None:
            raise ArtifactError("Processed GLB is missing")
        path = self.root / artifact.relative_path
        self.artifact_manager.verify_artifact_integrity(artifact)
        result = validate_glb(path, spec)
        report = self._path(task, f"validation-attempt-{execution.attempt_number}.json")
        report.write_text(
            json.dumps(result.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        report_id = self._register(workflow, task, execution, "asset-validation-report", report)
        revision = self.revisions.get(spec.asset_id, int(task.parameters["revision_number"]))
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        if not result.passed:
            raise AssetValidationFailedError("Processed GLB failed deterministic asset validation")
        revision.processed_glb_hash = artifact.content_hash
        revision.validation_report_hash = sha256_file(report)
        self.revisions.save(revision)
        return TaskHandlerResult(1, "Independent GLB validator passed", [report_id])

    def godot(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        result = run_asset_in_godot(
            self.root,
            self.artifacts,
            self.artifact_manager,
            workflow,
            task,
            execution,
            self.godot_path,
            self.runner,
        )
        revision = self.revisions.get(
            str(task.parameters["asset_id"]), int(task.parameters["revision_number"])
        )
        if revision is None:
            raise ValidationError("Asset revision record disappeared")
        runtime_artifacts = [
            a for a in self.artifacts.list_by_workflow(workflow.id) if a.id in result
        ]
        revision.runtime_evidence_hashes = list(
            dict.fromkeys(
                [*revision.runtime_evidence_hashes, *(a.content_hash for a in runtime_artifacts)]
            )
        )
        self.revisions.save(revision)
        return TaskHandlerResult(
            1, "Staged Godot imported, observed, and rendered the validated asset", result
        )

    def final_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        hashes = self._artifact_hashes(workflow.id)
        artifact_list = self.artifacts.list_by_workflow(workflow.id)
        roles = {artifact.artifact_type: artifact.content_hash for artifact in artifact_list}
        required = (
            "asset-concept",
            "asset-processed-glb",
            "asset-validation-report",
            "asset-runtime-observation",
            "asset-runtime-capture",
        )
        if any(role not in roles for role in required):
            raise ArtifactError(
                "Final review cannot open: required concept, processing, validation, or runtime evidence is missing"
            )
        observation_artifact = next(
            a for a in reversed(artifact_list) if a.artifact_type == "asset-runtime-observation"
        )
        observation = json.loads(
            (self.root / observation_artifact.relative_path).read_text(encoding="utf-8")
        )
        runtime_capture_hashes = sorted(
            artifact.content_hash
            for artifact in artifact_list
            if artifact.artifact_type == "asset-runtime-capture"
            and observation.get("execution_id") in artifact.relative_path
        )
        if len(runtime_capture_hashes) != 3:
            raise ArtifactError(
                "Final review requires three captures from the bound runtime attempt"
            )
        return {
            "workflow_id": workflow.id,
            "revision": task.parameters["revision_number"],
            "specification_hash": task.parameters["specification_hash"],
            "artifacts": {key: roles[key] for key in required},
            "runtime_capture_hashes": runtime_capture_hashes,
            "all_artifacts_verified": bool(hashes),
        }

    def final_review(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id
                and a.approval_type == "final_visual_review"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ArtifactError("Final visual approval is missing")
        receipt = self._path(task, "final-approval.json")
        receipt.write_text(
            json.dumps(
                {
                    "approval_id": approval.id,
                    "approval_type": approval.approval_type,
                    "status": approval.status.value,
                    "fingerprint": approval.operation_hash,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact_id = self._register(
            workflow, task, execution, "asset-final-approval-record", receipt
        )
        return TaskHandlerResult(
            1,
            "Final asset visual approval recorded for the current evidence fingerprint",
            [artifact_id],
        )


def register_asset_production_handlers(
    registry: TaskHandlerRegistry,
    handlers: AssetProductionHandlers,
) -> None:
    """Register the domain stages; engine metadata owns approval/recovery semantics."""
    registry.register("asset_prepare", handlers.prepare)
    registry.register(
        "asset_concept_review",
        handlers.review_concept,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            mandatory_approval_type="concept_review",
            approval_context=handlers.concept_review_context,
        ),
    )
    registry.register(
        "asset_paid_generation",
        handlers.paid_generate,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            safe_paid_recovery=True,
            recovery_check=handlers.recovery_check,
            mandatory_approval_type="paid_generation",
        ),
    )
    registry.register(
        "asset_process",
        handlers.process,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register("asset_validate", handlers.validate)
    registry.register(
        "asset_godot",
        handlers.godot,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register(
        "asset_final_review",
        handlers.final_review,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            mandatory_approval_type="final_visual_review",
            approval_context=handlers.final_review_context,
        ),
    )


def run_asset_in_godot(
    root: Path,
    artifacts: ArtifactRepository,
    artifact_manager: ArtifactManager,
    workflow: Workflow,
    task: Task,
    execution: Execution,
    godot_path: str | None,
    runner: ProcessRunner | None,
) -> list[str]:
    """Stage a validated GLB, invoke the real Godot renderer, then independently inspect outputs."""
    if not godot_path:
        raise EngineImportFailedError("Godot executable is required for asset runtime verification")
    processed = next(
        (
            a
            for a in artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-processed-glb"
        ),
        None,
    )
    validation = next(
        (
            a
            for a in artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-validation-report"
        ),
        None,
    )
    if processed is None or validation is None:
        raise ArtifactError("Godot stage requires processed asset and validation report artifacts")
    artifact_manager.verify_artifact_integrity(processed)
    report = json.loads((root / validation.relative_path).read_text(encoding="utf-8"))
    if report.get("status") != "PASS":
        raise ValidationError(
            "Godot stage is blocked because the independent asset validator did not pass"
        )
    spec = parse_asset_specification(task.parameters["specification"])
    scratch = assert_managed_directory(root, ".gamefactory/scratch")
    relative_stage = f".gamefactory/scratch/asset-{workflow.id}-{execution.id}"
    stage = PathGuard(root).resolve_safe_path(relative_stage)
    if not stage.is_relative_to(scratch.resolve()):
        raise ValidationError("Godot stage escapes the managed scratch directory")
    if stage.exists():
        raise ValidationError("Refusing to overwrite an existing Godot attempt stage")
    stage.mkdir(parents=True)
    (stage / "assets").mkdir()
    shutil.copyfile(root / processed.relative_path, stage / "assets" / "asset.glb")
    resources = Path(__file__).parents[1] / "resources" / "godot" / "asset_runtime_harness.gd"
    shutil.copyfile(resources, stage / "asset_runtime_harness.gd")
    (stage / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="GameFactory Asset Stage"\n[display]\nwindow/size/viewport_width=1280\nwindow/size/viewport_height=720\n[rendering]\nrenderer/rendering_method="gl_compatibility"\nrenderer/rendering_method.mobile="gl_compatibility"\n',
        encoding="utf-8",
    )
    observation_path = stage / "observation.json"
    capture_dir = stage / "captures"
    capture_dir.mkdir()
    request = {
        "workflow_id": workflow.id,
        "revision": task.parameters["revision_number"],
        "asset_id": spec.asset_id,
        "execution_id": execution.id,
        "attempt_number": execution.attempt_number,
        "glb": "res://assets/asset.glb",
        "processed_glb_sha256": processed.content_hash,
        "output_dir": str(capture_dir),
        "observation_path": str(observation_path),
    }
    request_path = stage / "request.json"
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    proc = runner or ProcessRunner(sanitize_output=True)
    render_env: dict[str, str] = {}
    if sys.platform.startswith("linux"):
        display = os.environ.get("DISPLAY")
        if not display:
            raise ValidationError(
                "Linux asset capture requires DISPLAY; headless fallback is not a capture"
            )
        render_env["DISPLAY"] = display
        for name in ("XAUTHORITY", "LIBGL_ALWAYS_SOFTWARE"):
            if value := os.environ.get(name):
                render_env[name] = value
    importer = proc.run(
        CommandRequest(
            [godot_path, "--headless", "--path", str(stage), "--editor", "--import", "--quit"],
            stage,
            env_overrides=render_env,
            timeout_seconds=90,
        )
    )
    import_diagnostics = importer.stdout + "\n" + importer.stderr
    import_errors = [p.pattern for p in _ENGINE_ERROR_PATTERNS if p.search(import_diagnostics)]
    import_log = stage / "godot-import.log"
    import_log.write_text(import_diagnostics, encoding="utf-8")
    import_artifact = artifact_manager.register_file_artifact(
        workflow.id,
        task.id,
        "asset-godot-import-log",
        task.task_type,
        import_log.relative_to(root).as_posix(),
    )
    artifacts.save(import_artifact)
    if importer.exit_code != 0 or importer.timed_out or import_errors:
        raise EngineImportFailedError(
            f"Godot GLB import failed (exit={importer.exit_code}, diagnostics={import_errors})"
        )
    render = proc.run(
        CommandRequest(
            [
                godot_path,
                "--path",
                str(stage),
                "--script",
                "res://asset_runtime_harness.gd",
                "--",
                "--request",
                str(request_path),
            ],
            stage,
            env_overrides=render_env,
            timeout_seconds=90,
        )
    )
    render_diagnostics = render.stdout + "\n" + render.stderr
    render_errors = [p.pattern for p in _ENGINE_ERROR_PATTERNS if p.search(render_diagnostics)]
    render_log = stage / "godot-render.log"
    render_log.write_text(render_diagnostics, encoding="utf-8")
    render_artifact = artifact_manager.register_file_artifact(
        workflow.id,
        task.id,
        "asset-godot-render-log",
        task.task_type,
        render_log.relative_to(root).as_posix(),
    )
    artifacts.save(render_artifact)
    if render.exit_code != 0 or render.timed_out or render_errors or not observation_path.is_file():
        raise RuntimeValidationFailedError(
            f"Godot runtime/render verification failed (exit={render.exit_code}, diagnostics={render_errors})"
        )
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    if (
        observation.get("workflow_id") != workflow.id
        or observation.get("revision") != task.parameters["revision_number"]
        or observation.get("asset_id") != spec.asset_id
        or observation.get("execution_id") != execution.id
        or observation.get("attempt_number") != execution.attempt_number
        or observation.get("processed_glb_sha256") != processed.content_hash
    ):
        raise RuntimeValidationFailedError(
            "Godot runtime observation is bound to a different workflow, revision, or GLB"
        )
    if (
        observation.get("status") != "PASS"
        or not observation.get("mesh_visible")
        or not observation.get("physics_body_present")
        or not observation.get("collision_shape_present")
        or not observation.get("physics_ray_hit")
        or observation.get("errors") != []
    ):
        raise RuntimeValidationFailedError(
            "Independent Godot observation failed mesh/collider/error checks"
        )
    bounds = observation.get("mesh_bounds", {}).get("size", [])
    expected = spec.dimensions.model_dump()
    if len(bounds) != 3 or any(
        abs(float(actual) - float(target)) > max(0.15, float(target) * 0.2)
        for actual, target in zip(
            bounds,
            (expected["width_m"], expected["height_m"], expected["depth_m"]),
            strict=True,
        )
    ):
        raise RuntimeValidationFailedError(
            "Godot transformed mesh bounds differ materially from the specification"
        )
    output_ids: list[str] = []
    observation_relative = observation_path.relative_to(root).as_posix()
    obs_art = artifact_manager.register_file_artifact(
        workflow.id, task.id, "asset-runtime-observation", task.task_type, observation_relative
    )
    artifacts.save(obs_art)
    output_ids.append(obs_art.id)
    for angle in ("front", "three_quarter", "side"):
        image = capture_dir / f"{angle}.png"
        from gamefactory.adapters.engines.godot_image import decode_png

        decode_png(image.read_bytes(), 1280, 720)
        relative = image.relative_to(root).as_posix()
        artifact = artifact_manager.register_file_artifact(
            workflow.id, task.id, "asset-runtime-capture", task.task_type, relative
        )
        artifacts.save(artifact)
        output_ids.append(artifact.id)
    return output_ids
