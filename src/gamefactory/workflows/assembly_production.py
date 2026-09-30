"""V0.7 production path for operator-authored assemblies (ADR 0013-0016).

``local_operator_assembly`` sources never touch a provider: there is no concept
review, no paid request, no readiness check and no paid approval. The graph is::

    PREPARE -> PROCESS -> VALIDATE -> GODOT -> FINAL-REVIEW -> EVIDENCE

PREPARE retains the specification, the source GLB and its registration with
immutable digests. PROCESS applies the declared ``source_front`` normalization
without a Blender round trip. VALIDATE, GODOT and FINAL-REVIEW are the shared
asset stages, which branch on the asset-spec-0.7.0 contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    AssetRevisionRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.assembly_source import parse_source_registration
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07, spec_fingerprint
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import Task, Workflow, WorkflowStatus, generate_id
from gamefactory.core.execution.path_guard import PathGuard

if TYPE_CHECKING:
    from gamefactory.core.domain.models import Execution
    from gamefactory.workflows.asset_production import AssetProductionHandlers
    from gamefactory.workflows.handlers import TaskHandlerResult

ASSEMBLY_GRAPH_VERSION = "0.7.0"
SOURCE_ARTIFACT = "asset-source-glb"
REGISTRATION_ARTIFACT = "asset-source-registration"


def is_assembly_graph(task: Task | Any) -> bool:
    parameters = getattr(task, "parameters", None)
    if parameters is None and isinstance(task, dict):
        parameters = task.get("parameters")
    return (
        isinstance(parameters, dict)
        and parameters.get("graph_version") == ASSEMBLY_GRAPH_VERSION
        and parameters.get("source_kind") == "local_operator_assembly"
    )


def _inside(project: Path, path: Path, label: str) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError as exc:
        raise ValidationError(f"{label} must be within the project root") from exc


def create_assembly_workflow(
    project_id: str,
    project_root: Path | str,
    specification: AssetSpecificationV07,
    source_glb: Path | str,
    registration: Path | str,
    *,
    workflow_id: str | None = None,
    revision_repository: AssetRevisionRepository | None = None,
) -> tuple[Workflow, list[Task]]:
    """Create the assembly DAG after validating inputs. Nothing is processed here."""
    if not isinstance(specification, AssetSpecificationV07):
        raise ValidationError("Assembly production requires an asset-spec-0.7.0 specification")
    profile = specification.bound_profile()
    if profile.geometry_mode != "assembly" or specification.source_kind != (
        "local_operator_assembly"
    ):
        raise ValidationError(
            f"{profile.qualified} is not an assembly profile; use 'asset create' for "
            "provider-generated single-mesh assets"
        )
    project = Path(project_root).resolve(strict=True)
    source = Path(source_glb).resolve(strict=True)
    registration_path = Path(registration).resolve(strict=True)
    if source.suffix.casefold() != ".glb" or not source.is_file():
        raise ValidationError("Assembly source must be a regular .glb file")
    if not registration_path.is_file():
        raise ValidationError("Source registration must be a regular file")
    source_relative = _inside(project, source, "Assembly source")
    registration_relative = _inside(project, registration_path, "Source registration")
    record = parse_source_registration(registration_path)
    record.check_against_spec(specification)
    record.check_artifact(source.read_bytes())
    if revision_repository is None:
        raise ValidationError("Assembly workflow requires the durable AssetRevisionRepository")
    wf_id = workflow_id or generate_id("WF-ASSET")
    spec_hash = spec_fingerprint(specification)
    WorkflowRepository(revision_repository.db).save(
        Workflow(
            id=wf_id,
            project_id=project_id,
            name=f"Asset production: {specification.asset_id}",
            status=WorkflowStatus.PENDING,
        )
    )
    revision = revision_repository.allocate_revision(
        specification.asset_id,
        wf_id,
        spec_hash,
        raw_glb_hash=record.artifact_sha256,
        profile_id=profile.profile_id,
        profile_version=profile.version,
    )
    number = revision.revision_number
    workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Asset production: {specification.asset_id} r{number:03d}",
        status=WorkflowStatus.PENDING,
    )
    common: dict[str, Any] = {
        "graph_version": ASSEMBLY_GRAPH_VERSION,
        "source_kind": "local_operator_assembly",
        "asset_id": specification.asset_id,
        "revision_number": number,
        "specification": specification.model_dump(mode="json"),
        "specification_hash": spec_hash,
        "source_glb": source_relative,
        "source_glb_hash": record.artifact_sha256,
        "source_registration": registration_relative,
        "source_registration_hash": sha256_file(registration_path),
        "source_front": record.source_front,
        "asset_dir": f".gamefactory/assets/{specification.asset_id}/r{number:03d}",
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
        "paid_provider_invocations": 0,
    }
    ids = {
        suffix: f"{wf_id}-{suffix}"
        for suffix in ("PREPARE", "PROCESS", "VALIDATE", "GODOT", "FINAL-REVIEW", "EVIDENCE")
    }
    tasks = [
        Task(
            id=ids["PREPARE"],
            workflow_id=wf_id,
            name="Validate and retain specification and operator assembly source",
            task_type="asset_assembly_prepare",
            parameters=common,
        ),
        Task(
            id=ids["PROCESS"],
            workflow_id=wf_id,
            name="Normalize the declared source front without inferring orientation",
            task_type="asset_assembly_process",
            depends_on=[ids["PREPARE"]],
            parameters=common,
        ),
        Task(
            id=ids["VALIDATE"],
            workflow_id=wf_id,
            name="Independently validate the part tree, pivots, sockets and collider",
            task_type="asset_validate",
            depends_on=[ids["PROCESS"]],
            parameters=common,
        ),
        Task(
            id=ids["GODOT"],
            workflow_id=wf_id,
            name="Import, articulate and render in staged Godot",
            task_type="asset_godot",
            depends_on=[ids["VALIDATE"]],
            parameters=common,
            timeout_seconds=240.0,
        ),
        Task(
            id=ids["FINAL-REVIEW"],
            workflow_id=wf_id,
            name="Human final runtime visual review",
            task_type="asset_final_review",
            depends_on=[ids["GODOT"]],
            parameters=common,
        ),
        Task(
            id=ids["EVIDENCE"],
            workflow_id=wf_id,
            name="Record verified asset evidence",
            task_type="record_evidence",
            depends_on=[ids["FINAL-REVIEW"]],
        ),
    ]
    return workflow, tasks


def assembly_prepare(
    handlers: AssetProductionHandlers, workflow: Workflow, task: Task, execution: Execution
) -> TaskHandlerResult:
    from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
    from gamefactory.workflows.handlers import TaskHandlerResult

    params = task.parameters
    spec = parse_asset_specification_v07(params["specification"])
    if spec_fingerprint(spec) != params["specification_hash"]:
        raise ValidationError("Asset specification fingerprint changed")
    guard = PathGuard(handlers.root)
    source = guard.resolve_safe_path(params["source_glb"])
    registration_path = guard.resolve_safe_path(params["source_registration"])
    if sha256_file(source) != params["source_glb_hash"]:
        raise ValidationError("Assembly source changed after workflow creation")
    if sha256_file(registration_path) != params["source_registration_hash"]:
        raise ValidationError("Source registration changed after workflow creation")
    record = parse_source_registration(registration_path)
    record.check_against_spec(spec)
    data = source.read_bytes()
    record.check_artifact(data)
    spec_path = handlers._path(task, "specification.json")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    retained = handlers._path(task, "source.glb")
    retained.write_bytes(data)
    registration_out = handlers._path(task, "source-registration.json")
    registration_out.write_text(
        json.dumps(record.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    ids = [
        handlers._register(workflow, task, execution, "asset-specification", spec_path),
        handlers._register(workflow, task, execution, SOURCE_ARTIFACT, retained),
        handlers._register(workflow, task, execution, REGISTRATION_ARTIFACT, registration_out),
    ]
    return TaskHandlerResult(
        1,
        "Specification, operator assembly source and its registration retained",
        ids,
    )


def _latest(handlers: AssetProductionHandlers, workflow_id: str, kind: str) -> Any:
    candidates = [
        a for a in handlers.artifacts.list_by_workflow(workflow_id) if a.artifact_type == kind
    ]
    if not candidates:
        raise ArtifactError(f"Required artifact is missing: {kind}")
    artifact = candidates[-1]
    handlers.artifact_manager.verify_artifact_integrity(artifact)
    return artifact


def assembly_process(
    handlers: AssetProductionHandlers, workflow: Workflow, task: Task, execution: Execution
) -> TaskHandlerResult:
    from gamefactory.adapters.assets.assembly_processor import normalize_assembly
    from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
    from gamefactory.workflows.handlers import TaskHandlerResult

    spec = parse_asset_specification_v07(task.parameters["specification"])
    source_artifact = _latest(handlers, workflow.id, SOURCE_ARTIFACT)
    registration_artifact = _latest(handlers, workflow.id, REGISTRATION_ARTIFACT)
    record = parse_source_registration(handlers.root / registration_artifact.relative_path)
    record.check_against_spec(spec)
    attempt = execution.attempt_number
    processed = handlers._path(task, f"processed-attempt-{attempt}.glb")
    report_path = handlers._path(task, f"processing-attempt-{attempt}.json")
    if processed.exists() or report_path.exists():
        raise ArtifactError("Refusing to overwrite an existing processing attempt")
    result = normalize_assembly(
        handlers.root / source_artifact.relative_path,
        record,
        processed,
        asset_id=spec.asset_id,
        processing_contract=spec.bound_profile().processing_contract(spec),
    )
    report = {
        **result.report,
        "status": "SUCCESS",
        "profile_id": spec.bound_profile().profile_id,
        "profile_version": spec.bound_profile().version,
        "processing_contract": spec.bound_profile().processing_contract(spec),
    }
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    ids = [
        handlers._register(workflow, task, execution, "asset-processed-glb", processed),
        handlers._register(workflow, task, execution, "asset-processing-report", report_path),
    ]
    return TaskHandlerResult(
        1,
        "Source normalized"
        + (" with one declared 180 degree +Y root rotation" if record.source_front == "+Z" else "")
        + "; geometry retained",
        ids,
    )


def normalization_for_validation(handlers: AssetProductionHandlers, workflow_id: str) -> Any:
    """Rebuild the normalization record from the stored report and retained source."""
    from gamefactory.adapters.assets.assembly_processor import normalization_record_from

    report_artifact = _latest(handlers, workflow_id, "asset-processing-report")
    source_artifact = _latest(handlers, workflow_id, SOURCE_ARTIFACT)
    report = json.loads((handlers.root / report_artifact.relative_path).read_text("utf-8"))
    if report.get("source_sha256") != source_artifact.content_hash:
        raise ValidationError("Processing report does not bind the retained assembly source")
    return normalization_record_from(
        report.get("normalization") or {}, handlers.root / source_artifact.relative_path
    )


class LocalAssemblyNoProvider:
    """Provider slot for assembly workflows. It refuses every provider operation.

    Assembly graphs contain no paid task; this object only exists so the shared
    asset handlers can be constructed, and any call is a programming error.
    """

    name = "local_operator_assembly"

    @property
    def cost_class(self) -> Any:
        from gamefactory.core.domain.models import CostClass

        return CostClass.LOCAL

    def is_configured(self) -> bool:
        return True

    def generate(self, request: Any) -> Any:
        from gamefactory.core.domain.errors import PaidRequestInvalidError

        raise PaidRequestInvalidError("Assembly workflows never contact a generation provider")

    def resolve_paid_request(
        self, binding: dict[str, Any], specification: dict[str, Any], cost: dict[str, Any]
    ) -> dict[str, Any]:
        from gamefactory.core.domain.errors import PaidRequestInvalidError

        raise PaidRequestInvalidError("Assembly workflows never build a provider request")

    def check_paid_request(self, snapshot_content: dict[str, Any]) -> None:
        from gamefactory.core.domain.errors import PaidRequestInvalidError

        raise PaidRequestInvalidError("Assembly workflows never build a provider request")


def build_source_registration(
    specification: AssetSpecificationV07,
    source: Path,
    *,
    source_front: str,
    authoring_tool: str,
    authoring_tool_version: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Draft a registration whose maps are taken from the specification."""
    import hashlib

    data = source.read_bytes()
    registration = {
        "schema_version": "asset-source-registration-0.7.0",
        "source_provenance_type": "local_operator_assembly",
        "paid": False,
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
        "artifact_bytes": len(data),
        "authoring_tool": {"name": authoring_tool, "version": authoring_tool_version},
        "source_front": source_front,
        "part_map": [
            {"part_id": p.part_id, "role": p.role, "parent": p.parent}
            for p in specification.parts or []
        ],
        "socket_map": [
            {"socket_id": s.socket_id, "parent_part": s.parent_part}
            for s in specification.sockets or []
        ],
        "actor": actor,
        "reason": reason,
    }
    parse_source_registration(registration).check_against_spec(specification)
    return registration
