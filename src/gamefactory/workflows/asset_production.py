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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import preflight_glb
from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.engines.godot_execution import _ENGINE_ERROR_PATTERNS
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    AuditLogRepository,
    ConceptVersionRecord,
    ConceptVersionRepository,
    CostLedgerRepository,
    EvidenceRepository,
    ExecutionRepository,
    PaidRequestSnapshotRecord,
    PaidRequestSnapshotRepository,
    ProductionReadinessRecord,
    ProductionReadinessRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.errors import (
    ApprovalRequired,
    ArtifactError,
    AssetValidationFailedError,
    DccFailedError,
    EngineImportFailedError,
    PaidRequestIncompatibleError,
    PaidRequestInvalidError,
    ProductionReadinessFailedError,
    ProviderUncertainError,
    RawArtifactInvalidError,
    RuntimeValidationFailedError,
    ToolUnavailableError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    Artifact,
    AuditEvent,
    CostClass,
    Execution,
    ExecutionStatus,
    Task,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.domain.paid_request import (
    PAID_REQUEST_SCHEMA,
    PaidRequestSnapshot,
)
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.workflows.accounting import CostAccounting
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)
from gamefactory.workflows.ports import AssetGenerationProvider, GenerationRequest
from gamefactory.workflows.production_readiness import (
    READINESS_SCHEMA,
    ReadinessContext,
    ReadinessProbes,
    build_readiness_report,
)


def is_v06_graph(task: Task | Any) -> bool:
    """Detect whether a task belongs to a V0.6 workflow DAG."""
    if hasattr(task, "parameters") and isinstance(task.parameters, dict):
        return task.parameters.get("graph_version") == "0.6.0"
    if isinstance(task, dict):
        params = task.get("parameters", {})
        if isinstance(params, dict):
            return params.get("graph_version") == "0.6.0"
    return False


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
    profile = specification.bound_profile()
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
        specification.asset_id,
        wf_id,
        spec_hash,
        concept_hash=concept_hash,
        profile_id=profile.profile_id,
        profile_version=profile.version,
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
        "graph_version": "0.6.0",
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
        # Informational only: the paid task alone carries "cost", the key the engine
        # reserves budget for. Shared parameters must never reserve money.
        "paid_reservation": float(reservation),
        "cost_unit": provider_cost_unit,
        "asset_dir": asset_dir,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
    }
    (
        prep,
        concept_review,
        paid_request,
        readiness,
        generate,
        process,
        validate,
        runtime,
        final,
        finish,
    ) = (
        f"{wf_id}-{suffix}"
        for suffix in (
            "PREPARE",
            "CONCEPT-REVIEW",
            "PAID-REQUEST",
            "READINESS",
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
            id=paid_request,
            workflow_id=wf_id,
            name="Resolve canonical paid request snapshot",
            task_type="asset_paid_request_snapshot",
            depends_on=[concept_review],
            max_retries=10,
            parameters=common,
        ),
        Task(
            id=readiness,
            workflow_id=wf_id,
            name="Evaluate pre-spend production readiness",
            task_type="asset_production_readiness",
            depends_on=[paid_request],
            max_retries=10,
            parameters=common,
        ),
        Task(
            id=generate,
            workflow_id=wf_id,
            name="Approved Meshy image-to-3D generation",
            task_type="asset_paid_generation",
            cost_class=CostClass.PAID,
            depends_on=[readiness],
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
    ledger: CostLedgerRepository | None = None
    accounting: CostAccounting | None = None
    paid_snapshots: PaidRequestSnapshotRepository | None = None
    readiness_reports: ProductionReadinessRepository | None = None
    readiness_probes: ReadinessProbes | None = None
    audit: AuditLogRepository | None = None
    tasks: TaskRepository | None = None
    concept_versions: ConceptVersionRepository | None = None
    _snapshot_repo: PaidRequestSnapshotRepository = field(init=False, repr=False)
    _readiness_repo: ProductionReadinessRepository = field(init=False, repr=False)
    _audit_repo: AuditLogRepository = field(init=False, repr=False)
    _task_repo: TaskRepository = field(init=False, repr=False)
    _concept_version_repo: ConceptVersionRepository = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve(strict=True)
        self.runner = self.runner or ProcessRunner(sanitize_output=True)
        db = self.artifacts.db
        if self.accounting is None:
            ledger_repo = self.ledger or CostLedgerRepository(db)
            self.accounting = CostAccounting(ledger_repo, AuditLogRepository(db))
        self._snapshot_repo = self.paid_snapshots or PaidRequestSnapshotRepository(db)
        self._readiness_repo = self.readiness_reports or ProductionReadinessRepository(db)
        self._audit_repo = self.audit or AuditLogRepository(db)
        self._task_repo = self.tasks or TaskRepository(db)
        self._concept_version_repo = self.concept_versions or ConceptVersionRepository(db)

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
        bound = spec.bound_profile()
        if task.parameters.get("profile_id") not in (None, bound.profile_id) or (
            task.parameters.get("profile_version") not in (None, bound.version)
        ):
            raise ValidationError("Asset revision profile binding does not match the specification")
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
        if is_v06_graph(task):
            concept_hash = sha256_file(concept_path)
            provenance_hash = sha256_file(provenance_path)
            concept_record = ConceptVersionRecord(
                id=generate_id("VER"),
                workflow_id=workflow.id,
                asset_id=task.parameters["asset_id"],
                revision_number=int(task.parameters["revision_number"]),
                version=1,
                artifact_id=ids[1],
                content_hash=concept_hash,
                provenance_artifact_id=ids[2],
                provenance_hash=provenance_hash,
                provenance_type=provenance.provenance_type,
                source_type=provenance.source_type,
                status="ACTIVE",
                actor="system",
                reason="initial concept",
            )
            self._concept_version_repo.add(concept_record)
        return TaskHandlerResult(
            1, "Specification, concept, and provenance retained with immutable digests", ids
        )

    def concept_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        if not is_v06_graph(task):
            # Historical approvals were fingerprinted with this exact V0.5 shape;
            # changing it would invalidate every pre-V0.6 concept approval.
            values = self._artifact_hashes(workflow.id)
            legacy_expected = {
                key: values[key]
                for key in ("asset-specification", "asset-concept", "asset-concept-provenance")
            }
            return {
                "asset_id": task.parameters["asset_id"],
                "revision": task.parameters["revision_number"],
                "specification_hash": task.parameters["specification_hash"],
                "artifacts": legacy_expected,
                "style_constraints": task.parameters["specification"]["style_constraints"],
                "profile_id": bound_profile_id(task),
                "profile_version": bound_profile_version(task),
                "profile_qualified": bound_profile_qualified(task),
            }
        concept = self._active_concept_artifact(workflow.id)
        provenance = self._active_concept_provenance_artifact(workflow.id)
        concept_version = self._active_concept_version(workflow.id)
        spec_artifacts = [
            a
            for a in self.artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-specification"
        ]
        if not spec_artifacts:
            raise ArtifactError("Asset specification artifact is missing")
        spec_art = spec_artifacts[0]
        self.artifact_manager.verify_artifact_integrity(spec_art)
        expected = {
            "asset-specification": spec_art.content_hash,
            "asset-concept": concept.content_hash,
            "asset-concept-provenance": provenance.content_hash,
        }
        return {
            "concept_version": concept_version,
            "concept_sha256": concept.content_hash,
            "concept_provenance_sha256": provenance.content_hash,
            "asset_id": task.parameters["asset_id"],
            "revision": task.parameters["revision_number"],
            "specification_hash": task.parameters["specification_hash"],
            "artifacts": expected,
            "style_constraints": task.parameters["specification"]["style_constraints"],
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
            "profile_qualified": bound_profile_qualified(task),
        }

    def _active_concept_version(self, workflow_id: str) -> int:
        row = self._concept_version_repo.active_for_workflow(workflow_id)
        if row is not None:
            return int(row.version)
        return 1

    def _active_concept_artifact(self, workflow_id: str) -> Artifact:
        row = self._concept_version_repo.active_for_workflow(workflow_id)
        if row is not None:
            concept = self.artifacts.get(row.artifact_id)
            if concept is None:
                raise ArtifactError(f"Active concept artifact '{row.artifact_id}' is missing")
            self.artifact_manager.verify_artifact_integrity(concept)
            if concept.content_hash != row.content_hash:
                raise ArtifactError(
                    f"Active concept artifact content_hash '{concept.content_hash}' does not match concept_version record '{row.content_hash}'"
                )
            return concept
        concept_arts = [
            a
            for a in self.artifacts.list_by_workflow(workflow_id)
            if a.artifact_type == "asset-concept"
        ]
        if not concept_arts:
            raise ArtifactError("Active concept artifact is missing")
        if len(concept_arts) > 1:
            raise ArtifactError(f"Ambiguous concept artifacts found for workflow {workflow_id}")
        concept = concept_arts[0]
        self.artifact_manager.verify_artifact_integrity(concept)
        return concept

    def _active_concept_provenance_artifact(self, workflow_id: str) -> Artifact:
        row = self._concept_version_repo.active_for_workflow(workflow_id)
        if row is not None and row.provenance_artifact_id:
            prov = self.artifacts.get(row.provenance_artifact_id)
            if prov is None:
                raise ArtifactError(
                    f"Active concept provenance artifact '{row.provenance_artifact_id}' is missing"
                )
            self.artifact_manager.verify_artifact_integrity(prov)
            if row.provenance_hash and prov.content_hash != row.provenance_hash:
                raise ArtifactError(
                    f"Active concept provenance content_hash '{prov.content_hash}' does not match concept_version record '{row.provenance_hash}'"
                )
            return prov
        prov_arts = [
            a
            for a in self.artifacts.list_by_workflow(workflow_id)
            if a.artifact_type == "asset-concept-provenance"
        ]
        if not prov_arts:
            raise ArtifactError("Active concept provenance artifact is missing")
        if len(prov_arts) > 1:
            raise ArtifactError(
                f"Ambiguous concept provenance artifacts found for workflow {workflow_id}"
            )
        prov = prov_arts[0]
        self.artifact_manager.verify_artifact_integrity(prov)
        return prov

    def paid_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        concept = self._active_concept_artifact(workflow.id)
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
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
            "profile_qualified": bound_profile_qualified(task),
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
        concept_version = self._active_concept_version(workflow.id)
        attempt = execution.attempt_number
        receipt = self._path(task, f"concept-approval-v{concept_version}-a{attempt}.json")
        if receipt.exists():
            relative = receipt.resolve(strict=True).relative_to(self.root).as_posix()
            existing_artifacts = self.artifacts.list_by_workflow(workflow.id)
            if any(a.relative_path == relative for a in existing_artifacts):
                raise ArtifactError(f"Approval receipt file is already registered: {receipt.name}")
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

    def paid_request_snapshot(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        concept_task = next(
            (
                t
                for t in self._task_repo.list_by_workflow(workflow.id)
                if t.task_type == "asset_concept_review"
            ),
            None,
        )
        concept_task_id = concept_task.id if concept_task else f"{workflow.id}-CONCEPT-REVIEW"
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == concept_task_id
                and a.approval_type == "concept_review"
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ApprovalRequired(
                f"Approved concept review approval for '{concept_task_id}' is required before paid request snapshot can be generated"
            )

        params = task.parameters
        concept_art = self._active_concept_artifact(workflow.id)
        concept_ver = self._active_concept_version(workflow.id)

        binding = {
            "asset_id": params["asset_id"],
            "revision_number": int(params["revision_number"]),
            "concept_version": concept_ver,
            "concept_sha256": concept_art.content_hash,
            "specification_sha256": params["specification_hash"],
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
        }
        cost = {
            "estimate": params.get("provider_estimate"),
            "reservation": float(params["paid_reservation"]),
            "unit": params.get("cost_unit", "credits"),
        }

        if not hasattr(self.provider, "resolve_paid_request") or not callable(
            getattr(self.provider, "resolve_paid_request", None)
        ):
            raise PaidRequestIncompatibleError(
                f"Provider {getattr(self.provider, 'name', 'unknown')} does not implement resolve_paid_request",
                provider=getattr(self.provider, "name", None),
            )

        content = self.provider.resolve_paid_request(binding, params["specification"], cost)
        snapshot = PaidRequestSnapshot.from_content(content)

        if not hasattr(self.provider, "check_paid_request") or not callable(
            getattr(self.provider, "check_paid_request", None)
        ):
            raise PaidRequestIncompatibleError(
                f"Provider {getattr(self.provider, 'name', 'unknown')} does not implement check_paid_request",
                provider=getattr(self.provider, "name", None),
            )
        self.provider.check_paid_request(snapshot.content)

        attempt = execution.attempt_number
        filename = f"paid-request-snapshot-v{concept_ver}-a{attempt}.json"
        snapshot_bytes = snapshot.canonical_json.encode("utf-8")
        path = self._path(task, filename)
        path.write_bytes(snapshot_bytes)

        artifact_id = self._register(workflow, task, execution, "asset-paid-request-snapshot", path)
        registered_art = self.artifacts.get(artifact_id)
        assert registered_art is not None, "Failed to retrieve registered snapshot artifact"
        assert registered_art.content_hash == snapshot.sha256, (
            f"Artifact content hash {registered_art.content_hash} does not match snapshot sha256 {snapshot.sha256}"
        )

        active_rows = [
            r for r in self._snapshot_repo.list_by_workflow(workflow.id) if r.status == "ACTIVE"
        ]
        if active_rows:
            self._snapshot_repo.mark_superseded([r.id for r in active_rows])

        snap_record = PaidRequestSnapshotRecord(
            id=generate_id("SNAP"),
            workflow_id=workflow.id,
            task_id=task.id,
            asset_id=params["asset_id"],
            revision_number=int(params["revision_number"]),
            concept_version=concept_ver,
            schema_version=PAID_REQUEST_SCHEMA,
            snapshot_sha256=snapshot.sha256,
            canonical_json=snapshot.canonical_json,
            status="ACTIVE",
            artifact_id=artifact_id,
            created_at=utc_now_iso(),
        )
        self._snapshot_repo.save(snap_record)

        self._audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="PAID_REQUEST_SNAPSHOT_CREATED",
                actor="AssetProductionHandlers",
                details={
                    "snapshot_sha256": snapshot.sha256,
                    "concept_version": concept_ver,
                    "provider": getattr(self.provider, "name", params.get("provider")),
                    "operation": "image-to-3d",
                },
            )
        )

        return TaskHandlerResult(
            1,
            "Paid request snapshot created and verified",
            [artifact_id],
        )

    def production_readiness(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        if self.readiness_probes is None:
            raise ProductionReadinessFailedError("no readiness probes configured")

        active_snap_record = self._snapshot_repo.get_active_for_workflow(workflow.id)
        if active_snap_record is None or not active_snap_record.artifact_id:
            raise ArtifactError(
                f"No active paid request snapshot record for workflow {workflow.id}"
            )

        art = self.artifacts.get(active_snap_record.artifact_id)
        if art is None:
            raise ArtifactError(
                f"Active paid request snapshot artifact {active_snap_record.artifact_id} not found"
            )
        self.artifact_manager.verify_artifact_integrity(art)

        snap_path = self.root / art.relative_path
        snap_text = snap_path.read_text(encoding="utf-8")
        snapshot = PaidRequestSnapshot.load_verified(snap_text, active_snap_record.snapshot_sha256)

        params = task.parameters
        spec = params.get("specification", {})
        prof = bound_profile_id(task)
        asset_dir_path = self._path(task, "").parent

        ctx = ReadinessContext(
            root=self.root,
            asset_dir=asset_dir_path,
            snapshot_content=snapshot.content,
            snapshot_sha256=snapshot.sha256,
            provider=self.provider,
            blender_path=self.blender_path,
            godot_path=self.godot_path,
            specification=spec,
            profile=prof,
            snapshot=snapshot,
        )

        checks = self.readiness_probes.evaluate(ctx)
        now = utc_now_iso()
        report = build_readiness_report(ctx, checks, generated_at=now)
        result = report["result"]

        attempt = execution.attempt_number
        report_json = json.dumps(report, sort_keys=True, indent=2) + "\n"
        rep_file = self._path(task, f"production-readiness-a{attempt}.json")
        rep_file.write_text(report_json, encoding="utf-8")

        art_id = self._register(
            workflow, task, execution, "asset-production-readiness-report", rep_file
        )
        saved_art = self.artifacts.get(art_id)
        report_sha = (
            saved_art.content_hash
            if saved_art
            else hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        )

        active_prior = [
            r for r in self._readiness_repo.list_by_workflow(workflow.id) if r.status == "ACTIVE"
        ]
        if active_prior:
            self._readiness_repo.mark_superseded([r.id for r in active_prior])

        rec = ProductionReadinessRecord(
            id=generate_id("READINESS"),
            workflow_id=workflow.id,
            task_id=task.id,
            snapshot_sha256=snapshot.sha256,
            result=result,
            schema_version=READINESS_SCHEMA,
            report_json=report_json,
            report_sha256=report_sha,
            status="ACTIVE",
            artifact_id=art_id,
            created_at=now,
        )
        self._readiness_repo.save(rec)

        self._audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="PRODUCTION_READINESS_EVALUATED",
                actor="AssetProductionHandlers",
                details={
                    "result": result,
                    "snapshot_sha256": snapshot.sha256,
                    "report_sha256": report_sha,
                    "checks_count": len(checks),
                },
            )
        )

        if result == "FAIL":
            failing = [c.name for c in checks if c.status == "FAIL"]
            raise ProductionReadinessFailedError(
                f"Production readiness evaluation failed: {', '.join(failing)}",
                details={"failing_checks": failing, "report_sha256": report_sha},
            )

        return TaskHandlerResult(1, "Production readiness evaluated: PASS", [art_id])

    def bind_paid_request_parameters(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        params = dict(task.parameters)
        if not is_v06_graph(task):
            return params

        active_snap = self._snapshot_repo.get_active_for_workflow(workflow.id)
        active_readiness = self._readiness_repo.get_active_for_workflow(workflow.id)

        if (
            active_snap is not None
            and active_readiness is not None
            and active_readiness.result == "PASS"
        ):
            snap_content = json.loads(active_snap.canonical_json)
            params["paid_request_snapshot_sha256"] = active_snap.snapshot_sha256
            params["paid_request_snapshot"] = snap_content
            params["production_readiness_report_sha256"] = active_readiness.report_sha256
        else:
            params.pop("paid_request_snapshot_sha256", None)
            params.pop("paid_request_snapshot", None)
            params.pop("production_readiness_report_sha256", None)

        return params

    def _recheck_before_submission(
        self, workflow: Workflow, task: Task, snapshot: PaidRequestSnapshot
    ) -> None:
        failing: list[str]
        if self.readiness_probes is None:
            failing = ["readiness_probes_configured"]
        else:
            context = ReadinessContext(
                root=self.root,
                asset_dir=self._path(task, "readiness.probe").parent,
                snapshot_content=snapshot.content,
                snapshot_sha256=snapshot.sha256,
                provider=self.provider,
                blender_path=self.blender_path,
                godot_path=self.godot_path,
                specification=task.parameters.get("specification", {}),
                profile=bound_profile_id(task),
                snapshot=snapshot,
            )
            failing = [
                check.name
                for check in self.readiness_probes.recheck_critical(context)
                if check.status != "PASS"
            ]
        if not failing:
            return
        self._audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="PRODUCTION_READINESS_RECHECK_FAILED",
                actor="AssetProductionHandlers",
                details={
                    "workflow_id": workflow.id,
                    "failing_checks": failing,
                    "paid_request_snapshot_sha256": snapshot.sha256,
                    "provider_submission": False,
                },
            )
        )
        raise ProductionReadinessFailedError(
            "Critical readiness recheck failed before paid submission; no provider "
            f"request was sent: {', '.join(failing)}",
            details={"failing_checks": failing},
        )

    def paid_generate(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        params = task.parameters
        concept = self._active_concept_artifact(workflow.id)
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

        snapshot: PaidRequestSnapshot | None = None
        if is_v06_graph(task):
            snap_sha = params.get("paid_request_snapshot_sha256")
            readiness_sha = params.get("production_readiness_report_sha256")
            if not snap_sha or not readiness_sha:
                raise PaidRequestInvalidError(
                    "V0.6 paid generation requires paid_request_snapshot_sha256 and production_readiness_report_sha256 in task parameters"
                )
            active_snap_record = self._snapshot_repo.get_active_for_workflow(workflow.id)
            if active_snap_record is None or active_snap_record.snapshot_sha256 != snap_sha:
                raise PaidRequestInvalidError(
                    f"Task parameters snapshot sha '{snap_sha}' does not match active snapshot row '{active_snap_record.snapshot_sha256 if active_snap_record else None}'"
                )
            if approval.paid_request_snapshot_hash != snap_sha:
                raise PaidRequestInvalidError(
                    f"Approval paid_request_snapshot_hash '{approval.paid_request_snapshot_hash}' does not match snapshot sha '{snap_sha}'"
                )
            if not active_snap_record.artifact_id:
                raise PaidRequestInvalidError("Active snapshot row is missing artifact_id")
            snap_artifact = self.artifacts.get(active_snap_record.artifact_id)
            if snap_artifact is None:
                raise PaidRequestInvalidError(
                    f"Active snapshot artifact '{active_snap_record.artifact_id}' not found"
                )
            self.artifact_manager.verify_artifact_integrity(snap_artifact)
            if snap_artifact.content_hash != snap_sha:
                raise PaidRequestInvalidError(
                    f"Snapshot artifact content_hash '{snap_artifact.content_hash}' does not match expected '{snap_sha}'"
                )
            snap_file = self.root / snap_artifact.relative_path
            snap_text = snap_file.read_text(encoding="utf-8")
            snapshot = PaidRequestSnapshot.load_verified(snap_text, snap_sha)
            # Provider-agnostic: the approved request must describe the ACTIVE concept
            # of this revision (an interrupted concept replacement can never be paid for).
            binding = snapshot.content["binding"]
            if (
                binding.get("concept_sha256") != concept.content_hash
                or binding.get("concept_version") != self._active_concept_version(workflow.id)
                or binding.get("asset_id") != params["asset_id"]
                or binding.get("revision_number") != int(params["revision_number"])
                or binding.get("specification_sha256") != params["specification_hash"]
            ):
                raise PaidRequestInvalidError(
                    "Approved paid request snapshot is not bound to the active concept "
                    "version of this revision; no provider request was sent"
                )
            if self.intents.get_by_task(task.id) is None:
                # A NEW paid submission is about to happen: recheck the critical
                # prerequisites cheaply. Query-only recovery is never blocked here.
                self._recheck_before_submission(workflow, task, snapshot)

        concept_path = self.root / concept.relative_path
        request_params = {
            **params,
            "workflow_id": workflow.id,
            "task_id": task.id,
            "approval_id": approval.id,
            "concept_hash": concept.content_hash,
            "concept_image_path": str(concept_path),
            "output_path": str(raw_path),
            "cost": params.get("provider_estimate"),
            "budget_reservation": params.get("budget_reservation"),
            "max_triangles_lod0": params["specification"]["geometry_budget"]["max_triangles_lod0"],
        }
        if is_v06_graph(task) and params.get("paid_request_snapshot_sha256"):
            request_params["paid_request_snapshot_sha256"] = params["paid_request_snapshot_sha256"]

        request = GenerationRequest(
            prompt=f"Image-to-3D {bound_profile_qualified(task)}: {params['asset_id']}",
            target_format="glb",
            parameters=request_params,
            operation_hash=approval.operation_hash,
            paid_request=snapshot,
        )

        def account_known_cost(response_cost: object = None, *, settled: bool = False) -> None:
            """Persist known billing before any later download or validation can fail.

            While the provider task is in flight or its outcome is uncertain, the
            authorized reservation stays held even if a lower cost was reported.
            Once the provider reports terminal success, the reported actual is the
            settled charge and replaces the reservation.
            """
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
            cumulative_liability = (
                known_actual if settled else max(authorized_reservation, known_actual)
            )
            execution.cost = max(0.0, cumulative_liability - previous_liability)
            execution.cost_unit = getattr(intent, "cost_unit", "credits")

        def record_ledger_cost(response_cost: object = None, *, settled: bool = False) -> None:
            if self.accounting is None:
                return
            intent = self.intents.get_by_task(task.id)
            values = [
                value
                for value in (response_cost, getattr(intent, "actual_cost", None))
                if value is not None
                and not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and float(value) >= 0
            ]
            actual = max(values) if values else None
            if settled:
                self.accounting.settle_terminal_success(
                    task.id,
                    actual,
                    execution_id=execution.id,
                    intent=intent,
                    cost_unit=getattr(intent, "cost_unit", "credits"),
                )
            elif actual is not None:
                self.accounting.handle_in_flight(
                    task.id,
                    actual,
                    execution_id=execution.id,
                    intent=intent,
                    cost_unit=getattr(intent, "cost_unit", "credits"),
                )

        try:
            response = self.provider.generate(request)
            account_known_cost(
                response.details.get("actual_cost"),
                settled=response.status == "SUCCESS",
            )
            record_ledger_cost(
                response.details.get("actual_cost"),
                settled=response.status == "SUCCESS",
            )
        except ProviderUncertainError as exc:
            account_known_cost()
            if self.accounting is not None:
                self.accounting.handle_failure(
                    task.id,
                    execution_id=execution.id,
                    intent=self.intents.get_by_task(task.id),
                    submission_uncertain=True,
                )
            raise ProviderUncertainError(
                str(exc),
                task_id=task.id,
                execution_id=execution.id,
                provider=self.provider.name,
                details={**(exc.details or {}), "process_state_uncertain": True},
            ) from exc
        except Exception:
            account_known_cost()
            if self.accounting is not None:
                self.accounting.handle_failure(
                    task.id,
                    execution_id=execution.id,
                    intent=self.intents.get_by_task(task.id),
                )
            raise
        if response.status == "SUBMITTED":
            deadline = time.monotonic() + min(task.timeout_seconds, 300.0)
            while response.status == "SUBMITTED" and time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    response = self.provider.generate(request)
                    account_known_cost(
                        response.details.get("actual_cost"),
                        settled=response.status == "SUCCESS",
                    )
                    record_ledger_cost(
                        response.details.get("actual_cost"),
                        settled=response.status == "SUCCESS",
                    )
                except ProviderUncertainError as exc:
                    account_known_cost()
                    if self.accounting is not None:
                        self.accounting.handle_failure(
                            task.id,
                            execution_id=execution.id,
                            intent=self.intents.get_by_task(task.id),
                            submission_uncertain=True,
                        )
                    raise ProviderUncertainError(
                        str(exc),
                        task_id=task.id,
                        execution_id=execution.id,
                        provider=self.provider.name,
                        details={**(exc.details or {}), "process_state_uncertain": True},
                    ) from exc
                except Exception:
                    account_known_cost()
                    if self.accounting is not None:
                        self.accounting.handle_failure(
                            task.id,
                            execution_id=execution.id,
                            intent=self.intents.get_by_task(task.id),
                        )
                    raise
        if response.status != "SUCCESS":
            if self.accounting is not None:
                self.accounting.handle_failure(
                    task.id,
                    execution_id=execution.id,
                    intent=self.intents.get_by_task(task.id),
                    submission_uncertain=True,
                )
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
            download_dir.mkdir(parents=True, exist_ok=True)
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
        active_concept = self._active_concept_artifact(workflow.id)
        roles["asset-concept"] = active_concept.content_hash
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
        selected_captures = select_review_captures(
            [
                artifact
                for artifact in artifact_list
                if artifact.artifact_type == "asset-runtime-capture"
            ],
            str(observation.get("execution_id", "")),
            parse_asset_specification(task.parameters["specification"])
            .bound_profile()
            .review_views,
        )
        runtime_capture_hashes = sorted(artifact.content_hash for artifact in selected_captures)
        return {
            "workflow_id": workflow.id,
            "revision": task.parameters["revision_number"],
            "specification_hash": task.parameters["specification_hash"],
            "artifacts": {key: roles[key] for key in required},
            "runtime_capture_hashes": runtime_capture_hashes,
            "all_artifacts_verified": bool(hashes),
            "profile_id": bound_profile_id(task),
            "profile_version": bound_profile_version(task),
            "profile_qualified": bound_profile_qualified(task),
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
        attempt = execution.attempt_number
        receipt = self._path(task, f"final-approval-a{attempt}.json")
        if receipt.exists():
            relative = receipt.resolve(strict=True).relative_to(self.root).as_posix()
            existing_artifacts = self.artifacts.list_by_workflow(workflow.id)
            if any(a.relative_path == relative for a in existing_artifacts):
                raise ArtifactError(
                    f"Final approval receipt file is already registered: {receipt.name}"
                )
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
            changes_requested_blocks=is_v06_graph,
        ),
    )
    registry.register("asset_paid_request_snapshot", handlers.paid_request_snapshot)
    registry.register("asset_production_readiness", handlers.production_readiness)
    registry.register(
        "asset_paid_generation",
        handlers.paid_generate,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            safe_paid_recovery=True,
            recovery_check=handlers.recovery_check,
            mandatory_approval_type="paid_generation",
            refresh_parameters=handlers.bind_paid_request_parameters,
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


def bound_profile_id(task: Task) -> str:
    spec = parse_asset_specification(task.parameters["specification"])
    return str(spec.bound_profile().profile_id)


def bound_profile_version(task: Task) -> int:
    spec = parse_asset_specification(task.parameters["specification"])
    return int(spec.bound_profile().version)


def bound_profile_qualified(task: Task) -> str:
    spec = parse_asset_specification(task.parameters["specification"])
    return str(spec.bound_profile().qualified)


REVIEW_CAPTURE_ANGLES = ("front", "three_quarter", "side")
SIDE_HEIGHT_MIN = 0.55
SIDE_HEIGHT_MAX = 0.75


def capture_belongs_to_execution(relative_path: str, execution_id: str) -> bool:
    """True when a capture path is bound to this execution attempt."""
    if not execution_id:
        return False
    normalized = relative_path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1]
    if filename.startswith(f"{execution_id}-") or filename.startswith(f"{execution_id}."):
        return True
    return f"-{execution_id}/" in normalized or f"/{execution_id}/" in normalized


def select_review_captures(
    captures: list[Any],
    execution_id: str,
    angles: tuple[str, ...] | None = None,
) -> list[Any]:
    """Select one capture per required view from the bound runtime attempt.

    A later side-only recapture replaces the review side view when side is required.
    The earlier side file remains registered and is not selected.
    """
    required = angles or REVIEW_CAPTURE_ANGLES
    by_angle: dict[str, list[Any]] = {angle: [] for angle in required}
    for artifact in captures:
        angle = Path(artifact.relative_path).stem
        if angle.startswith(f"{execution_id}-"):
            angle = angle[len(execution_id) + 1 :]
        if angle in by_angle:
            by_angle[angle].append(artifact)
    selected: list[Any] = []
    for angle in required:
        owned = [
            artifact
            for artifact in by_angle[angle]
            if capture_belongs_to_execution(artifact.relative_path, execution_id)
        ]
        if angle == "side":
            corrections = [
                artifact
                for artifact in by_angle[angle]
                if not capture_belongs_to_execution(artifact.relative_path, execution_id)
            ]
            if corrections:
                selected.append(
                    max(corrections, key=lambda artifact: (artifact.created_at, artifact.id))
                )
                continue
        if len(owned) != 1:
            raise ArtifactError(
                f"Final review requires one {angle} capture from the bound runtime attempt"
            )
        selected.append(owned[0])
    return selected


def validate_view_framing(
    framing: Any,
    *,
    view: str,
    minimum: float,
    maximum: float,
) -> None:
    """Reject a capture whose projected bounds miss the profile framing contract."""
    if not isinstance(framing, dict):
        raise RuntimeValidationFailedError(f"{view} capture has no framing measurement")
    height = framing.get("height_ratio")
    if (
        isinstance(height, bool)
        or not isinstance(height, (int, float))
        or not math.isfinite(float(height))
        or float(height) < minimum
        or float(height) > maximum
    ):
        raise RuntimeValidationFailedError(
            f"{view} capture height fraction is outside {minimum}-{maximum}"
        )
    if framing.get("inside_viewport") is not True or framing.get("margin_ok") is not True:
        raise RuntimeValidationFailedError(
            f"{view} capture bounds are outside the viewport safety margin"
        )
    if framing.get("horizontally_centered") is not True:
        raise RuntimeValidationFailedError(f"{view} capture is not horizontally centered")
    if framing.get("reference_between_camera_and_asset") is not False:
        raise RuntimeValidationFailedError(
            f"{view} capture reference object occludes the production asset"
        )
    if view == "side" and framing.get("view_axis") != "+X":
        raise RuntimeValidationFailedError("side capture is not a principal-side view")


def validate_side_framing(framing: Any) -> None:
    """Reject a side capture outside the static_prop viewport contract."""
    validate_view_framing(framing, view="side", minimum=SIDE_HEIGHT_MIN, maximum=SIDE_HEIGHT_MAX)


def run_asset_in_godot(
    root: Path,
    artifacts: ArtifactRepository,
    artifact_manager: ArtifactManager,
    workflow: Workflow,
    task: Task,
    execution: Execution,
    godot_path: str | None,
    runner: ProcessRunner | None,
    *,
    angles: tuple[str, ...] | None = None,
    observation_artifact_type: str = "asset-runtime-observation",
) -> list[str]:
    """Stage a validated GLB, invoke the real Godot renderer, then independently inspect outputs."""
    from gamefactory.core.domain.asset_profiles import render_scene_contract
    from gamefactory.core.domain.camera_framing import PLACED_VIEWS

    if observation_artifact_type not in {"asset-runtime-observation", "asset-side-correction"}:
        raise ValidationError("Unsupported runtime observation artifact type")
    if not godot_path:
        raise ToolUnavailableError(
            "Godot executable is required for asset runtime verification",
            tool="godot",
            reason="executable_missing",
            configured_path=None,
            task_id=task.id,
        )
    candidate_path = Path(godot_path)
    if not candidate_path.exists():
        raise ToolUnavailableError(
            f"Configured Godot executable does not exist: {godot_path}",
            tool="godot",
            reason="executable_not_found",
            configured_path=godot_path,
            task_id=task.id,
        )
    if not candidate_path.is_file():
        raise ToolUnavailableError(
            f"Configured Godot path is not a regular file: {godot_path}",
            tool="godot",
            reason="executable_not_file",
            configured_path=godot_path,
            task_id=task.id,
        )
    if not os.access(candidate_path, os.X_OK):
        raise ToolUnavailableError(
            f"Configured Godot executable is not executable: {godot_path}",
            tool="godot",
            reason="executable_not_executable",
            configured_path=godot_path,
            task_id=task.id,
        )
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
    profile = spec.bound_profile()
    capture_angles = profile.review_views if angles is None else angles
    if (
        not capture_angles
        or any(not isinstance(angle, str) for angle in capture_angles)
        or len(capture_angles) != len(set(capture_angles))
        or any(angle not in PLACED_VIEWS for angle in capture_angles)
    ):
        raise ValidationError("Asset capture angles are not an implemented review view set")
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
        "angles": list(capture_angles),
        "profile": profile.capture_request_profile(),
    }
    (stage / "asset_wrapper.tscn").write_text(
        render_scene_contract(profile.scene_contract()),
        encoding="utf-8",
    )
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
    requirements = profile.runtime_requirements()
    if (
        observation.get("status") != "PASS"
        or not observation.get("mesh_visible")
        or not observation.get("collision_shape_present")
        or observation.get("errors") != []
        or (
            requirements["require_physics_body"]
            and observation.get("physics_body_present") is not True
        )
        or (requirements["require_area"] and observation.get("area_present") is not True)
        or (requirements["require_ray_hit"] and observation.get("physics_ray_hit") is not True)
    ):
        raise RuntimeValidationFailedError(
            "Independent Godot observation failed mesh, collision, or error checks"
        )
    bounds = observation.get("mesh_bounds", {}).get("size", [])
    expected = spec.dimensions.model_dump()
    if len(bounds) != 3 or any(
        abs(float(actual) - float(target))
        > max(
            float(requirements["bounds_tolerance_floor_m"]),
            float(target) * float(requirements["bounds_tolerance_ratio"]),
        )
        for actual, target in zip(
            bounds,
            (expected["width_m"], expected["height_m"], expected["depth_m"]),
            strict=True,
        )
    ):
        raise RuntimeValidationFailedError(
            "Godot transformed mesh bounds differ materially from the specification"
        )
    view_framing = observation.get("view_framing")
    if not isinstance(view_framing, dict):
        view_framing = {}
    framing_policy = profile.framing
    for angle in capture_angles:
        measured = view_framing.get(angle)
        if angle == "side" and measured is None:
            measured = observation.get("side_framing")
        validate_view_framing(
            measured,
            view=angle,
            minimum=framing_policy.min_screen_fraction,
            maximum=framing_policy.max_screen_fraction,
        )
    output_ids: list[str] = []
    observation_relative = observation_path.relative_to(root).as_posix()
    obs_art = artifact_manager.register_file_artifact(
        workflow.id, task.id, observation_artifact_type, task.task_type, observation_relative
    )
    artifacts.save(obs_art)
    output_ids.append(obs_art.id)
    for angle in capture_angles:
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


def recapture_side_evidence(
    root: Path,
    db: Database,
    workflow_id: str,
    *,
    godot_path: str,
    expected_asset_id: str,
    expected_revision_number: int,
    expected_processed_sha256: str,
    expected_external_task_id: str,
    expected_paid_invocations: int,
    expected_approval_id: str,
) -> dict[str, Any]:
    """Render a new side view of the current processed GLB without a new provider call."""
    project = Path(root).resolve(strict=True)
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None:
        raise ValidationError(f"Workflow not found: {workflow_id}")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    godot_task = next((task for task in tasks if task.task_type == "asset_godot"), None)
    review_task = next((task for task in tasks if task.task_type == "asset_final_review"), None)
    if godot_task is None or review_task is None:
        raise ValidationError("Asset workflow is missing Godot or final-review tasks")
    if str(godot_task.parameters.get("asset_id")) != expected_asset_id:
        raise ValidationError("Refusing side recapture: asset id does not match")
    if int(godot_task.parameters.get("revision_number", -1)) != expected_revision_number:
        raise ValidationError("Refusing side recapture: revision does not match")
    artifacts = ArtifactRepository(db)
    artifact_manager = ArtifactManager(project)
    processed = next(
        (
            artifact
            for artifact in artifacts.list_by_workflow(workflow_id)
            if artifact.artifact_type == "asset-processed-glb"
        ),
        None,
    )
    if processed is None or processed.content_hash != expected_processed_sha256:
        raise ValidationError("Refusing side recapture: processed GLB hash does not match")
    artifact_manager.verify_artifact_integrity(processed)
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    external_ids = [intent.external_task_id for intent in intents if intent.external_task_id]
    paid_count = ProviderInvocationRepository(db).count(workflow_id) + len(external_ids)
    if paid_count != expected_paid_invocations or external_ids != [expected_external_task_id]:
        raise ValidationError("Refusing side recapture: paid provider identity does not match")
    approval = ApprovalRepository(db).get(expected_approval_id)
    if (
        approval is None
        or approval.workflow_id != workflow_id
        or approval.task_id != review_task.id
        or approval.approval_type != "final_visual_review"
        or approval.status.value != "PENDING"
    ):
        raise ValidationError(
            "Refusing side recapture: final review is not the expected pending approval"
        )
    preserved = [
        artifact
        for artifact in artifacts.list_by_workflow(workflow_id)
        if artifact.artifact_type == "asset-runtime-capture"
    ]
    preserved_hashes = {artifact.id: artifact.content_hash for artifact in preserved}
    executions = ExecutionRepository(db)
    attempt_number = (
        max((item.attempt_number for item in executions.list_by_task(godot_task.id)), default=0) + 1
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=godot_task.id,
        attempt_number=attempt_number,
        status=ExecutionStatus.RUNNING,
        cost=0.0,
        estimated_cost=0.0,
        provider=None,
    )
    executions.save(execution)
    try:
        output_ids = run_asset_in_godot(
            project,
            artifacts,
            artifact_manager,
            workflow,
            godot_task,
            execution,
            godot_path,
            None,
            angles=("side",),
            observation_artifact_type="asset-side-correction",
        )
    except Exception:
        execution.status = ExecutionStatus.FAILED
        execution.completed_at = utc_now_iso()
        execution.retryable = False
        executions.save(execution)
        raise
    for artifact_id, content_hash in preserved_hashes.items():
        current = artifacts.get(artifact_id)
        if current is None or current.content_hash != content_hash:
            execution.status = ExecutionStatus.FAILED
            execution.completed_at = utc_now_iso()
            execution.retryable = False
            executions.save(execution)
            raise ValidationError("Side recapture changed a previously registered capture")
        artifact_manager.verify_artifact_integrity(current)
    execution.status = ExecutionStatus.COMPLETED
    execution.completed_at = utc_now_iso()
    execution.exit_code = 0
    execution.cost = 0.0
    execution.stdout = (
        "Corrected side capture rendered from the existing processed GLB\n"
        "provider generation submitted = false\n"
    )
    executions.save(execution)
    registered = [artifacts.get(artifact_id) for artifact_id in output_ids]
    side = next(
        artifact
        for artifact in registered
        if artifact is not None and artifact.artifact_type == "asset-runtime-capture"
    )
    correction = next(
        artifact
        for artifact in registered
        if artifact is not None and artifact.artifact_type == "asset-side-correction"
    )
    paid_after = ProviderInvocationRepository(db).count(workflow_id) + len(
        [
            intent.external_task_id
            for intent in ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
            if intent.external_task_id
        ]
    )
    if paid_after != expected_paid_invocations:
        raise ValidationError("Side recapture changed the paid invocation count")
    return {
        "execution_id": execution.id,
        "attempt_number": execution.attempt_number,
        "task_id": godot_task.id,
        "side_artifact_id": side.id,
        "side_relative_path": side.relative_path,
        "side_sha256": side.content_hash,
        "correction_artifact_id": correction.id,
        "correction_relative_path": correction.relative_path,
        "processed_glb_sha256": processed.content_hash,
        "paid_invocations": paid_after,
    }
