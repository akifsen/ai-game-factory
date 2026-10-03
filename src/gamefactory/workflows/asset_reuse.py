"""Static-prop reuse: retain an external GLB, then run the shared Blender/Godot path.

Graph::

    REUSE-PREPARE -> PROCESS -> VALIDATE -> GODOT -> FINAL-REVIEW -> EVIDENCE

No concept review, paid request, readiness, provider invocation, or ledger spend.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gamefactory.adapters.assets.glb_validator import preflight_glb
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    parse_any_asset_specification,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.existing_external_provenance import (
    load_existing_external_provenance,
    provenance_from_bounded_bytes,
    read_bounded_provenance_bytes,
)
from gamefactory.core.domain.models import Task, Workflow, WorkflowStatus, generate_id
from gamefactory.core.execution.path_guard import PathGuard

if TYPE_CHECKING:
    from gamefactory.core.domain.models import Execution
    from gamefactory.workflows.asset_production import AssetProductionHandlers
    from gamefactory.workflows.handlers import TaskHandlerResult

REUSE_GRAPH_VERSION = "static-prop-reuse-1.0"
SOURCE_PROVENANCE_ARTIFACT = "asset-existing-source-provenance"
_REUSE_GLB_MAX_BYTES = 50 * 1024 * 1024


_REUSE_MANAGED_ARTIFACT_TYPES: tuple[str, ...] = (
    "asset-existing-source-provenance",
    "asset-raw-glb",
    "asset-processed-glb",
    "asset-processing-report",
    "asset-validation-report",
    "asset-runtime-observation",
)


def workflow_uses_existing_external_reuse(tasks: list[Task]) -> bool:
    return any(task.task_type == "asset_reuse_prepare" for task in tasks)


def is_reuse_graph(task: Task | Any) -> bool:
    parameters = getattr(task, "parameters", None)
    if parameters is None and isinstance(task, dict):
        parameters = task.get("parameters")
    return (
        isinstance(parameters, dict)
        and parameters.get("graph_version") == REUSE_GRAPH_VERSION
        and parameters.get("source_mode") == "existing_external"
    )


def _reuse_lexical_unsafe(path: Path) -> bool:
    lexical = path.absolute()
    ancestors: list[Path] = []
    for current in [lexical, *lexical.parents]:
        ancestors.append(current)
        if current.anchor == current:
            break
    for ancestor in ancestors:
        try:
            st = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
        reparse_tag = int(getattr(st, "st_reparse_tag", 0) or 0)
        if reparse_tag != 0:
            return True
        if os.name == "nt":
            attrs = getattr(st, "st_file_attributes", None)
            if attrs is None:
                return True
            if int(attrs) & 0x400 and reparse_tag == 0:
                return True
    return False


def resolve_reuse_input_path(project_root: Path, value: Path | str) -> Path:
    """Resolve reuse GLB/provenance paths; relative paths stay under project_root."""
    path = Path(value).expanduser()
    if path.is_absolute():
        resolved = path.resolve(strict=True)
    else:
        resolved = PathGuard(project_root).resolve_safe_path(path.as_posix())
    if _reuse_lexical_unsafe(resolved):
        raise ValidationError("Reuse input path is not a bounded safe location")
    return resolved


def read_bounded_reuse_glb(path: Path) -> tuple[bytes, str]:
    """Bounded read of an external GLB; digest is from the exact bytes validated."""
    if path.suffix.casefold() != ".glb":
        raise ValidationError("Reuse source must be a regular .glb file")
    if _reuse_lexical_unsafe(path):
        raise ValidationError("Reuse source path is not a bounded safe location")
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ValidationError("Reuse source must be a regular file") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValidationError("Reuse source must be a regular file")
    size = st.st_size
    if size <= 0 or size > _REUSE_GLB_MAX_BYTES:
        raise ValidationError("Reuse source GLB size is outside bounded limits")
    hasher = hashlib.sha256()
    read_total = 0
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            read_total += len(chunk)
            if read_total > _REUSE_GLB_MAX_BYTES:
                raise ValidationError("Reuse source GLB exceeded bounded stream read limit")
            hasher.update(chunk)
            chunks.append(chunk)
    if read_total != size:
        raise ValidationError("Reuse source GLB size changed during bounded read")
    data = b"".join(chunks)
    try:
        preflight_glb(path)
    except (ValueError, OSError) as exc:
        raise ValidationError(f"Reuse source failed GLB preflight: {exc}") from exc
    return data, hasher.hexdigest()


def validate_static_prop_reuse_inputs(
    project_root: Path | str,
    specification: AssetSpecification,
    source_glb: Path | str,
    provenance: Path | str,
) -> dict[str, Any]:
    """Read-only validation shared by workflow creation and CLI dry-run."""
    if not isinstance(specification, AssetSpecification):
        raise ValidationError("Reuse requires an asset-spec-0.4.0 static_prop specification")
    profile = specification.bound_profile()
    if profile.qualified != "static_prop@1":
        raise ValidationError(
            f"{profile.qualified} is not supported for asset reuse; only static_prop@1 is allowed"
        )
    project = Path(project_root).resolve(strict=True)
    source_path = resolve_reuse_input_path(project, source_glb)
    provenance_path = resolve_reuse_input_path(project, provenance)
    glb_bytes, source_hash = read_bounded_reuse_glb(source_path)
    record, _prov_raw, provenance_hash = load_existing_external_provenance(provenance_path)
    if record.source_mode != "existing_external":
        raise ValidationError("Provenance source_mode must be existing_external")
    record.check_artifact(glb_bytes)
    return {
        "asset_id": specification.asset_id,
        "profile": profile.qualified,
        "source_mode": record.source_mode,
        "original_provider": record.original_provider,
        "specification_hash": spec_fingerprint(specification),
        "source_sha256": source_hash,
        "source_glb_hash": source_hash,
        "source_provenance_hash": provenance_hash,
        "source_glb": _store_path(source_path),
        "source_provenance": _store_path(provenance_path),
    }


def _resolve_read_path(root: Path, stored: str) -> Path:
    return resolve_reuse_input_path(root, stored)


def _store_path(path: Path) -> str:
    return path.resolve(strict=True).as_posix()


def create_static_prop_reuse_workflow(
    project_id: str,
    project_root: Path | str,
    specification: AssetSpecification,
    source_glb: Path | str,
    provenance: Path | str,
    *,
    workflow_id: str | None = None,
    revision_repository: AssetRevisionRepository | None = None,
) -> tuple[Workflow, list[Task]]:
    """Validate inputs and allocate a reuse DAG. No files are retained here."""
    validated = validate_static_prop_reuse_inputs(
        project_root, specification, source_glb, provenance
    )
    source_hash = str(validated["source_glb_hash"])
    provenance_hash = str(validated["source_provenance_hash"])
    if revision_repository is None:
        raise ValidationError("Reuse workflow requires the durable AssetRevisionRepository")
    profile = specification.bound_profile()
    wf_id = workflow_id or generate_id("WF-REUSE")
    spec_hash = spec_fingerprint(specification)
    WorkflowRepository(revision_repository.db).save(
        Workflow(
            id=wf_id,
            project_id=project_id,
            name=f"Asset reuse: {specification.asset_id}",
            status=WorkflowStatus.PENDING,
        )
    )
    revision = revision_repository.allocate_revision(
        specification.asset_id,
        wf_id,
        spec_hash,
        raw_glb_hash=source_hash,
        profile_id=profile.profile_id,
        profile_version=profile.version,
    )
    number = revision.revision_number
    workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Asset reuse: {specification.asset_id} r{number:03d}",
        status=WorkflowStatus.PENDING,
    )
    common: dict[str, Any] = {
        "graph_version": REUSE_GRAPH_VERSION,
        "workflow_kind": "static_prop_reuse",
        "source_mode": "existing_external",
        "asset_id": specification.asset_id,
        "revision_number": number,
        "specification": specification.model_dump(mode="json"),
        "specification_hash": spec_hash,
        "source_glb": validated["source_glb"],
        "source_glb_hash": source_hash,
        "source_provenance": validated["source_provenance"],
        "source_provenance_hash": provenance_hash,
        "asset_dir": f".gamefactory/assets/{specification.asset_id}/r{number:03d}",
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
        "paid_provider_invocations": 0,
        "original_provider": validated["original_provider"],
    }
    ids = {
        suffix: f"{wf_id}-{suffix}"
        for suffix in ("REUSE-PREPARE", "PROCESS", "VALIDATE", "GODOT", "FINAL-REVIEW", "EVIDENCE")
    }
    tasks = [
        Task(
            id=ids["REUSE-PREPARE"],
            workflow_id=wf_id,
            name="Validate and retain specification, external source GLB and provenance",
            task_type="asset_reuse_prepare",
            parameters=common,
        ),
        Task(
            id=ids["PROCESS"],
            workflow_id=wf_id,
            name="Process retained raw GLB in Blender",
            task_type="asset_process",
            depends_on=[ids["REUSE-PREPARE"]],
            parameters=common,
        ),
        Task(
            id=ids["VALIDATE"],
            workflow_id=wf_id,
            name="Independently validate processed GLB",
            task_type="asset_validate",
            depends_on=[ids["PROCESS"]],
            parameters=common,
        ),
        Task(
            id=ids["GODOT"],
            workflow_id=wf_id,
            name="Import, run, and render in staged Godot",
            task_type="asset_godot",
            depends_on=[ids["VALIDATE"]],
            parameters=common,
            timeout_seconds=180.0,
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


def reuse_prepare(
    handlers: AssetProductionHandlers, workflow: Workflow, task: Task, execution: Execution
) -> TaskHandlerResult:
    from gamefactory.workflows.handlers import TaskHandlerResult

    params = task.parameters
    spec = parse_asset_specification(params["specification"])
    if spec_fingerprint(spec) != params["specification_hash"]:
        raise ValidationError("Asset specification fingerprint changed")
    bound = spec.bound_profile()
    if bound.qualified != "static_prop@1":
        raise ValidationError("Reuse prepare only supports static_prop@1")
    source = _resolve_read_path(handlers.root, params["source_glb"])
    provenance_path = _resolve_read_path(handlers.root, params["source_provenance"])
    glb_bytes, source_digest = read_bounded_reuse_glb(source)
    if source_digest != params["source_glb_hash"]:
        raise ValidationError("External source GLB changed after workflow creation")
    prov_raw = read_bounded_provenance_bytes(provenance_path)
    provenance_digest = hashlib.sha256(prov_raw).hexdigest()
    if provenance_digest != params["source_provenance_hash"]:
        raise ValidationError("Source provenance changed after workflow creation")
    record = provenance_from_bounded_bytes(prov_raw)
    record.check_artifact(glb_bytes)
    spec_path = handlers._path(task, "specification.json")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    raw_path = handlers._path(task, "raw.glb")
    if raw_path.exists():
        raise ValidationError("Refusing to overwrite an existing retained raw GLB")
    raw_path.write_bytes(glb_bytes)
    if hashlib.sha256(glb_bytes).hexdigest() != params["source_glb_hash"]:
        raise ValidationError("Retained raw GLB digest does not match workflow binding")
    provenance_out = handlers._path(task, "source-provenance.json")
    if provenance_out.exists():
        raise ValidationError("Refusing to overwrite an existing retained provenance file")
    provenance_out.write_bytes(prov_raw)
    if hashlib.sha256(prov_raw).hexdigest() != params["source_provenance_hash"]:
        raise ValidationError("Retained provenance digest does not match workflow binding")
    ids = [
        handlers._register(workflow, task, execution, "asset-specification", spec_path),
        handlers._register(workflow, task, execution, "asset-raw-glb", raw_path),
        handlers._register(workflow, task, execution, SOURCE_PROVENANCE_ARTIFACT, provenance_out),
    ]
    revision = handlers.revisions.get(params["asset_id"], int(params["revision_number"]))
    if revision is None:
        raise ValidationError("Asset revision record disappeared")
    if revision.raw_glb_hash != params["source_glb_hash"]:
        raise ValidationError("Asset revision raw_glb_hash does not match retained source")
    revision.raw_glb_hash = hashlib.sha256(glb_bytes).hexdigest()
    handlers.revisions.save(revision)
    return TaskHandlerResult(
        1,
        "Specification, external source GLB and provenance retained with immutable digests",
        ids,
    )


def verify_retained_raw_artifact(
    handlers: AssetProductionHandlers, workflow_id: str, expected_hash: str
) -> None:
    """Fail closed when the managed raw GLB drifts from the revision binding."""
    raw_artifacts = [
        a
        for a in handlers.artifacts.list_by_workflow(workflow_id)
        if a.artifact_type == "asset-raw-glb"
    ]
    if not raw_artifacts:
        raise ValidationError("Retained raw GLB artifact is missing")
    raw = raw_artifacts[-1]
    handlers.artifact_manager.verify_artifact_integrity(raw)
    if raw.content_hash != expected_hash:
        raise ValidationError("Retained raw GLB artifact drifted from workflow binding")


def _artifact_entry(root: Path, artifact: Any) -> dict[str, str]:
    path = (root / artifact.relative_path).resolve()
    return {
        "relative_path": artifact.relative_path,
        "path": str(path),
        "sha256": artifact.content_hash,
    }


def build_static_prop_reuse_report(
    project_root: Path | str, db: Any, workflow_id: str
) -> tuple[dict[str, Any], str]:
    """Read-only reuse review summary: verified managed artifacts and final approval status."""
    root = Path(project_root).resolve(strict=True)
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None:
        raise ValidationError(f"Workflow not found: {workflow_id}")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    if not workflow_uses_existing_external_reuse(tasks):
        raise ValidationError("Workflow is not an existing_external static_prop reuse graph")
    prepare = next(t for t in tasks if t.task_type == "asset_reuse_prepare")
    params = prepare.parameters
    artifacts = ArtifactRepository(db).list_by_workflow(workflow_id)
    manager = ArtifactManager(root)
    for artifact in artifacts:
        manager.verify_artifact_integrity(artifact)
    from gamefactory.workflows.assembly_production import LocalAssemblyNoProvider
    from gamefactory.workflows.asset_production import (
        AssetProductionHandlers,
        select_review_captures,
    )

    handlers = AssetProductionHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        EvidenceRepository(db),
        QualityGateRepository(db),
        ExecutionRepository(db),
        manager,
        LocalAssemblyNoProvider(),
    )
    verify_retained_raw_artifact(handlers, workflow_id, str(params["source_glb_hash"]))
    verify_retained_provenance_artifact(
        handlers, workflow_id, str(params["source_provenance_hash"])
    )
    by_type: dict[str, list[Any]] = {}
    for item in artifacts:
        by_type.setdefault(item.artifact_type, []).append(item)
    managed: dict[str, dict[str, str]] = {}
    for kind in _REUSE_MANAGED_ARTIFACT_TYPES:
        candidates = by_type.get(kind, [])
        if not candidates:
            raise ArtifactError(f"Reuse report is missing required managed artifact: {kind}")
        managed[kind] = _artifact_entry(root, candidates[-1])
    observation_artifact = managed["asset-runtime-observation"]
    observation = json.loads(Path(observation_artifact["path"]).read_text(encoding="utf-8"))
    execution_id = str(observation.get("execution_id", ""))
    if not execution_id:
        raise ArtifactError("Runtime observation does not name a bound execution id")
    spec = parse_any_asset_specification(params["specification"])
    selected_captures = select_review_captures(
        by_type.get("asset-runtime-capture", []),
        execution_id,
        spec.bound_profile().review_views,
    )
    runtime_captures = [_artifact_entry(root, artifact) for artifact in selected_captures]
    final_task = next(t for t in tasks if t.task_type == "asset_final_review")
    final_candidates = [
        approval
        for approval in ApprovalRepository(db).list_by_workflow(workflow_id)
        if approval.task_id == final_task.id and approval.approval_type == "final_visual_review"
    ]
    if not final_candidates:
        raise ArtifactError("Reuse report requires a final_visual_review approval record")
    final_approval = max(final_candidates, key=lambda item: item.requested_at)
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "workflow_kind": "static_prop_reuse",
        "source_mode": params.get("source_mode", "existing_external"),
        "revision_number": int(params["revision_number"]),
        "external_binding": {
            "source_glb": {
                "path": params["source_glb"],
                "sha256": params["source_glb_hash"],
            },
            "source_provenance": {
                "path": params["source_provenance"],
                "sha256": params["source_provenance_hash"],
            },
        },
        "managed_artifacts": managed,
        "runtime_captures": runtime_captures,
        "final_visual_review": {
            "approval_id": final_approval.id,
            "status": final_approval.status.value,
        },
        "note": "This report does not export a cold evidence bundle and does not grant approval.",
    }
    human_lines = [
        f"Reuse review report for {workflow_id} (revision r{payload['revision_number']:03d})",
        (f"final_visual_review: {final_approval.status.value} ({final_approval.id})"),
        "",
        "External binding:",
        f"  source_glb: {payload['external_binding']['source_glb']['path']} "
        f"sha256={payload['external_binding']['source_glb']['sha256']}",
        f"  source_provenance: {payload['external_binding']['source_provenance']['path']} "
        f"sha256={payload['external_binding']['source_provenance']['sha256']}",
        "",
        "Managed artifacts:",
    ]
    for kind in _REUSE_MANAGED_ARTIFACT_TYPES:
        entry = managed[kind]
        human_lines.append(f"  {kind}: {entry['relative_path']} sha256={entry['sha256']}")
    human_lines.append("")
    human_lines.append("Runtime captures:")
    for entry in runtime_captures:
        human_lines.append(f"  {entry['relative_path']} sha256={entry['sha256']}")
    human_lines.append("")
    human_lines.append(payload["note"])
    return payload, "\n".join(human_lines)


def verify_retained_provenance_artifact(
    handlers: AssetProductionHandlers, workflow_id: str, expected_hash: str
) -> None:
    """Fail closed when retained provenance drifts from the workflow binding."""
    provenance_artifacts = [
        a
        for a in handlers.artifacts.list_by_workflow(workflow_id)
        if a.artifact_type == SOURCE_PROVENANCE_ARTIFACT
    ]
    if not provenance_artifacts:
        raise ValidationError("Retained source provenance artifact is missing")
    provenance = provenance_artifacts[-1]
    handlers.artifact_manager.verify_artifact_integrity(provenance)
    if provenance.content_hash != expected_hash:
        raise ValidationError("Retained source provenance drifted from workflow binding")
